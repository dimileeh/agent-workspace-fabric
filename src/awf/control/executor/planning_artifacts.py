"""WorkspaceExecutor planning-artifact handling.

Mechanically extracted from ``execution_flow.execute``; behavior is unchanged.
Keeping the plan/conformance-report deposit and the agent/planning result
interpretation here keeps ``execution_flow`` focused on the agent-run →
commit → validate → push pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from awf.common.logging import get_logger
from awf.control.executor.helpers import _failure_salvage_payload
from awf.control.executor.types import _PlanningRunFailure, _PlanningValidationHandoff
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.runtime.planning import AGENT_PLAN_PHASE_SCOPE_VIOLATION, render_workspace_path
from awf.service.artifacts import deposit_workspace_planning_artifacts

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
    deposit_workspace_planning_artifacts(
        work_dir=self._config.compose_projects_root.parent,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        plan_path=plan_path,
        report_path=report_path,
    )


async def handle_agent_planning_result(
    self: Any,
    *,
    workspace_id: str,
    ws: Any,
    worktree_path: Path,
    profile: Any,
    planning_failure: Any,
) -> tuple[_PlanningValidationHandoff | None, bool]:
    """Interpret the agent/planning run result.

    Returns ``(handoff, should_return)`` where ``handoff`` is the planning
    validation handoff to thread into validation (``None`` when none applies)
    and ``should_return`` signals that ``execute`` must stop because the
    workspace was already marked FAILED.
    """
    if isinstance(planning_failure, _PlanningValidationHandoff):
        await self._record_planning_validation_handoff_event(
            workspace_id=workspace_id,
            handoff=planning_failure,
        )
        return planning_failure, False
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
