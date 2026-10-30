"""WorkspaceExecutor planning-artifact handling.

Mechanically extracted from ``execution_flow.execute``; behavior is unchanged.
Keeping the plan/conformance-report deposit and the agent/planning result
interpretation here keeps ``execution_flow`` focused on the agent-run →
commit → validate → push pipeline.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from awf.common.audit import redact_audit_value
from awf.common.logging import get_logger
from awf.control.executor.helpers import _failure_salvage_payload
from awf.control.executor.types import (
    _ExpectedBaseShaLaunchSettled,
    _PlanningNeedsHuman,
    _PlanningRunFailure,
    _PlanningValidationHandoff,
)
from awf.db.enums import PLAN_CONFORMANCE_DECISION_BLOCK_TYPE, FailureReason, WorkspaceStatus
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    MAX_CONFORMANCE_GAPS,
    MAX_CONFORMANCE_TEXT_CHARS,
    PLAN_CONFORMANCE_NEEDS_HUMAN,
    render_workspace_path,
)
from awf.service.artifacts import (
    DEPOSITED_CONFORMANCE_NAME,
    DEPOSITED_PLAN_NAME,
    _deposit_one_planning_artifact,
    deposit_workspace_planning_artifacts,
    workspace_artifact_dir,
)

_log = get_logger(__name__)


def _deposit_planning_artifacts_best_effort(
    self: Any,
    *,
    profile: Any,
    workspace_id: str,
    worktree_path: Path,
) -> None:
    """Deposit the worktree plan + conformance report into the served artifact
    dir (a sibling of the worktree) before any teardown removes the worktree,
    so the console can surface them. Gated on the resolved planning profile —
    the only reliable "planning was required" signal (the handoff is set only
    on the AWF-validation path, not when conformance is satisfied inline).
    Runs on the success path, the validation/conformance stop paths, and the
    early planning-failure return — all preserve a FAILED workspace whose
    worktree still holds the plan/report. A copy failure is best-effort and
    non-fatal.
    """
    if profile is None or not profile.planning.required:
        return
    # An invalid plan_path/conformance_report_path template is itself a
    # planning failure: the planning loop already renders these paths and, on
    # ``ValueError``, returns the ``"planning profile is invalid"`` string that
    # routes the workspace to ``_mark_failed``. Re-rendering the same invalid
    # template here — *before* that ``_mark_failed`` runs — would let the
    # ValueError escape and strand the workspace in ``running`` instead of
    # FAILED. The deposit is best-effort, so treat an unrenderable template the
    # same as a copy failure: log and skip, never raise.
    try:
        plan_path = render_workspace_path(profile.planning.plan_path, workspace_id=workspace_id)
        report_path = render_workspace_path(
            profile.planning.conformance_report_path, workspace_id=workspace_id
        )
    except ValueError as exc:
        _log.warning(
            "executor.planning_artifact_deposit_skipped_invalid_profile",
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
        )
        return
    config = getattr(self, "_config", None)
    compose_projects_root = getattr(config, "compose_projects_root", None)
    if compose_projects_root is None:
        _log.warning(
            "executor.planning_artifact_deposit_skipped_missing_artifact_root",
            workspace_id=workspace_id,
        )
        return
    deposit_workspace_planning_artifacts(
        work_dir=compose_projects_root.parent,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        plan_path=plan_path,
        report_path=report_path,
    )


def _deposit_validation_planning_artifacts(
    self: Any,
    *,
    profile: Any,
    workspace_id: str,
    worktree_path: Path,
) -> None:
    _deposit_planning_artifacts_best_effort(
        self,
        profile=profile,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
    )


async def _mark_failed_preserving_validation_planning_artifacts(
    self: Any,
    *,
    artifact_profile: Any,
    artifact_workspace_id: str,
    artifact_worktree_path: Path,
    **mark_kwargs: Any,
) -> None:
    _deposit_validation_planning_artifacts(
        self,
        profile=artifact_profile,
        workspace_id=artifact_workspace_id,
        worktree_path=artifact_worktree_path,
    )
    await self._mark_failed(**mark_kwargs)


async def _enter_blocked_preserving_validation_planning_artifacts(
    self: Any,
    *,
    artifact_profile: Any,
    artifact_workspace_id: str,
    artifact_worktree_path: Path,
    **block_kwargs: Any,
) -> None:
    _deposit_validation_planning_artifacts(
        self,
        profile=artifact_profile,
        workspace_id=artifact_workspace_id,
        worktree_path=artifact_worktree_path,
    )
    await self.enter_blocked_for_protected_violation(**block_kwargs)


async def handle_agent_planning_result(
    self: Any,
    *,
    workspace_id: str,
    ws: Any,
    worktree_path: Path,
    profile: Any,
    planning_failure: Any,
    execution_owner_id: str | None = None,
) -> tuple[_PlanningValidationHandoff | None, bool]:
    """Interpret the agent/planning run result.

    Returns ``(handoff, should_return)`` where ``handoff`` is the planning
    validation handoff to thread into validation (``None`` when none applies)
    and ``should_return`` signals that ``execute`` must stop because the
    workspace was failed, paused for a decision, or otherwise settled.
    """
    if isinstance(planning_failure, _PlanningNeedsHuman):
        await pause_for_conformance_decision(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            decision=planning_failure,
            from_status=WorkspaceStatus.running,
            execution_owner_id=execution_owner_id,
        )
        return None, True
    if isinstance(planning_failure, _PlanningValidationHandoff):
        await self._record_planning_validation_handoff_event(
            workspace_id=workspace_id,
            handoff=planning_failure,
        )
        return planning_failure, False
    if isinstance(planning_failure, _ExpectedBaseShaLaunchSettled):
        return None, True
    if planning_failure is not None:
        failure_message = (
            planning_failure if isinstance(planning_failure, str) else planning_failure.message
        )
        reason_code = None if isinstance(planning_failure, str) else planning_failure.reason_code
        details = None if isinstance(planning_failure, str) else planning_failure.details
        # The planning loop preserves the FAILED workspace's worktree,
        # which can already hold the plan + (unsatisfied) conformance
        # report. Deposit them BEFORE ``_mark_failed`` publishes the
        # terminal status, mirroring every agent-phase failure handler in
        # ``execution_flow``. The console keys its artifact refetch on the
        # workspace ``updated_at`` (TaskArtifactsSection ``refreshKey``);
        # marking FAILED first would bump ``updated_at`` and let the poll
        # observe it in the window before the deposit, record an empty
        # artifact list, then never refetch (the filesystem copy does not
        # touch the row) — leaving the Plan/Validation buttons hidden.
        # Depositing first orders artifact availability ahead of the
        # polling signal. Best-effort and non-fatal.
        _deposit_planning_artifacts_best_effort(
            self,
            profile=profile,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
        )
        await self._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.running,
            failure_reason=FailureReason.agent_failure,
            message=failure_message[:2000],
            reason_code=reason_code,
            details=details,
            salvage=_failure_salvage_payload(ws, worktree_path=worktree_path),
        )
        if (
            isinstance(planning_failure, _PlanningRunFailure)
            and planning_failure.reason_code == AGENT_PLAN_PHASE_SCOPE_VIOLATION
        ):
            await self._auto_retry_planning_scope_failure(
                workspace_id=workspace_id,
                failure=planning_failure,
            )
        return None, True
    return None, False


async def pause_for_conformance_decision(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    decision: _PlanningNeedsHuman,
    from_status: WorkspaceStatus,
    execution_owner_id: str | None,
) -> None:
    """Publish decision evidence and a fenced pause, never an implementation success.

    valid report -> owner-fenced transition -> artifacts -> commit blocked
    The artifact callback runs only after winning the row lock/CAS and before
    publishing the transaction, so stale workers cannot overwrite newer evidence.
    """
    report = decision.report
    evidence = redact_audit_value(
        {
            "status": report.status.value,
            "summary": report.summary[:MAX_CONFORMANCE_TEXT_CHARS],
            "decision_request": report.decision_request,
            "reason_code": PLAN_CONFORMANCE_NEEDS_HUMAN,
            "gaps": [
                {"kind": gap.kind.value, "detail": gap.detail[:MAX_CONFORMANCE_TEXT_CHARS]}
                for gap in report.gaps[:MAX_CONFORMANCE_GAPS]
            ],
            "plan_path": decision.plan_path.as_posix(),
            "report_path": decision.report_path.as_posix(),
            "phase": decision.phase,
            "validation_run_id": decision.validation_run_id,
        }
    )

    def deposit() -> None:
        artifact_dir = workspace_artifact_dir(
            self._config.compose_projects_root.parent, workspace_id
        )
        _deposit_one_planning_artifact(
            artifact_dir=artifact_dir,
            workspace_id=workspace_id,
            source=worktree_path / decision.plan_path,
            dest_name=DEPOSITED_PLAN_NAME,
            worktree_root=worktree_path.resolve(),
        )
        # Serialize the accepted report (possibly from stdout), not an old file.
        # The managed artifact root is not agent-writable. Atomic replacement
        # prevents partial reads and does not follow an existing destination link.
        temporary: Path | None = None
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=artifact_dir, prefix=".decision-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(evidence, stream, indent=2)
                stream.write("\n")
            temporary.replace(artifact_dir / DEPOSITED_CONFORMANCE_NAME)
        except OSError as exc:
            _log.warning(
                "executor.conformance_decision_artifact_failed",
                workspace_id=workspace_id,
                error_type=type(exc).__name__,
            )
            # Do not present an older report as the current question. The exact
            # bounded decision survives in the transition event even on disk failure.
            try:
                (artifact_dir / DEPOSITED_CONFORMANCE_NAME).unlink(missing_ok=True)
            except OSError:
                _log.warning(
                    "executor.conformance_decision_stale_artifact_retained",
                    workspace_id=workspace_id,
                )
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    _log.warning(
                        "executor.conformance_decision_artifact_temp_retained",
                        workspace_id=workspace_id,
                    )

    await self.enter_blocked_for_protected_violation(
        workspace_id=workspace_id,
        from_status=from_status,
        violations=[],
        resume_phase=decision.phase,
        execution_owner_id=execution_owner_id,
        block_type=PLAN_CONFORMANCE_DECISION_BLOCK_TYPE,
        block_reason_code=PLAN_CONFORMANCE_NEEDS_HUMAN,
        transition_reason_code=PLAN_CONFORMANCE_NEEDS_HUMAN,
        recovery_finalize_reason_code=PLAN_CONFORMANCE_NEEDS_HUMAN,
        recovery_finalize_message="Plan conformance requires an operator decision.",
        extra_payload={"conformance": evidence},
        before_publish=deposit,
    )
