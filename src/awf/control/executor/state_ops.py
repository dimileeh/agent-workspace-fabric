"""WorkspaceExecutor state operations.

Mechanically extracted from the original orchestrator; behavior is unchanged.
"""

from __future__ import annotations

import enum
import json
from collections.abc import Mapping, Sequence
from datetime import (
    UTC,
    datetime,
)
from typing import Any

from sqlalchemy import (
    String,
    cast,
    or_,
    select,
    update,
)

from awf.common.audit import redact_audit_text
from awf.common.compose_exec import EXEC_PROCESS_CLEANUP_FAILED
from awf.control.blocked_transition import (
    enter_blocked_for_protected_violation_in_session,
)
from awf.control.executor.helpers import (
    _realign_profile_from_resolved_profile_snapshot,
    _reuse_persisted_block_baseline,
)
from awf.control.executor.metadata import (
    _metadata_int,
    _metadata_number,
    _metadata_str,
)
from awf.control.executor.quality_gates import (
    _log,
)
from awf.control.executor.recovery_payloads import (
    _get_active_recovery_payload,
    _planning_validation_handoff_metadata,
)
from awf.control.executor.status_helpers import _is_callback_terminal_status
from awf.control.executor.types import PauseResumeReason, _PlanningValidationHandoff
from awf.control.operator_grants import (
    active_operator_grant_specs_in_session,
    consume_active_operator_grants_in_session,
)
from awf.control.quality_gates import (
    QUALITY_GATE_POLICY_CHANGED_REASON_CODE,
    GrantSpec,
    QualityGateViolation,
)
from awf.db.enums import (
    AgentRuntime,
    FailureReason,
    OperationStatus,
    WorkspaceStatus,
)
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.profiles.models import WorkspaceProfile
from awf.runtime.operator_hints import pre_pr_operator_hint_from_payload
from awf.runtime.validation import ValidationCommandResult, ValidationCoverageResult
from awf.service.provider_recovery import (
    _policy_model,
    in_place_recovery_task_policy,
    should_recover_in_place,
)


def _resolved_profile_snapshot_from_db_value(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def _load_workspace(self: Any, workspace_id: str) -> Workspace | None:
    async with self._session_factory() as session:
        return await WorkspaceRepository(session).get(workspace_id)


async def _persist_resolved_profile_snapshot_if_missing(
    self: Any,
    *,
    workspace_id: str,
    profile: WorkspaceProfile,
) -> dict[str, Any] | None:
    """Freeze the runtime-resolved profile snapshot and return the stored snapshot."""
    snapshot = profile.model_dump(mode="json", by_alias=True)
    async with self._session_factory() as session:
        result = await session.execute(
            update(Workspace)
            .where(
                Workspace.id == workspace_id,
                or_(
                    Workspace.resolved_profile.is_(None),
                    cast(Workspace.resolved_profile, String) == "null",
                ),
            )
            .values(resolved_profile=snapshot)
            .returning(Workspace.resolved_profile)
            .execution_options(synchronize_session=False)
        )
        returned_snapshot = result.scalar_one_or_none()
        persisted_snapshot = _resolved_profile_snapshot_from_db_value(returned_snapshot)
        if returned_snapshot is not None:
            if persisted_snapshot is None:
                _log.warning(
                    "executor.resolved_profile_returning_unparseable",
                    workspace_id=workspace_id,
                    returned_type=type(returned_snapshot).__name__,
                )
            await session.commit()
            return persisted_snapshot if persisted_snapshot is not None else snapshot
        frozen_snapshot = await session.scalar(
            select(Workspace.resolved_profile).where(Workspace.id == workspace_id)
        )
        return _resolved_profile_snapshot_from_db_value(frozen_snapshot)


async def _sync_resolved_profile(
    self: Any,
    *,
    ws: Workspace,
    workspace_id: str,
    profile: WorkspaceProfile,
    planning_max_iterations_default: int = 3,
) -> WorkspaceProfile:
    """Freeze the resolved profile snapshot and align the active profile to the winner."""
    persisted_profile_snapshot = await _persist_resolved_profile_snapshot_if_missing(
        self,
        workspace_id=workspace_id,
        profile=profile,
    )
    persisted_profile = _realign_profile_from_resolved_profile_snapshot(
        ws,
        persisted_profile_snapshot,
        planning_max_iterations_default=planning_max_iterations_default,
    )
    return persisted_profile if persisted_profile is not None else profile


async def _claim_ready(
    self: Any,
    workspace_id: str,
    *,
    execution_owner_id: str | None = None,
    execution_lease_expires_at: datetime | None = None,
) -> Workspace | None:
    """Atomically transition a ready workspace to running before execution."""
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.transition_if_current(
            workspace_id,
            from_status=WorkspaceStatus.ready,
            to=WorkspaceStatus.running,
            reason_code="EXECUTOR_CLAIMED",
        )
        if ws is not None:
            ws.execution_claimed_by = execution_owner_id
            ws.execution_claim_expires_at = execution_lease_expires_at
            await session.commit()
            return ws

        current: Workspace | None = None
        if execution_owner_id is not None and execution_lease_expires_at is not None:
            current = await repo.claim_worker_restart_recovery_execution(
                workspace_id,
                owner_id=execution_owner_id,
                lease_expires_at=execution_lease_expires_at,
                claim_cutoff=datetime.now(UTC),
            )
            if current is not None:
                await session.commit()
                return current

        current = await repo.get_with_operations(workspace_id)
        if current is None:
            _log.warning("executor.skip_unknown", workspace_id=workspace_id)
            return None
        recovery = _get_active_recovery_payload(current)
        if (
            current.status == WorkspaceStatus.running.value
            and recovery is not None
            and recovery.get("source") == "worker_restart"
        ):
            _log.info(
                "executor.skip_active_execution_claim",
                workspace_id=workspace_id,
                execution_claimed_by=current.execution_claimed_by,
            )
            return None
        _log.info(
            "executor.skip_not_ready",
            workspace_id=workspace_id,
            status=current.status,
        )
        return None


async def _update_subphase(self: Any, workspace_id: str, subphase: str) -> None:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        await repo.update_activity(workspace_id, subphase=subphase)
        await session.commit()


async def _recheck_status(
    self: Any,
    workspace_id: str,
    *,
    expected: WorkspaceStatus,
    action: str,
    reason_code: str = "EXECUTOR_STALE_STATUS",
    owner_id: str | None = None,
    owner_mismatch_reason_code: str = "EXECUTOR_STALE_CLAIM",
) -> bool:
    """Confirm the row is still in ``expected`` (and, when ``owner_id`` is
    provided, still claimed by it) before resuming work.

    ``owner_id`` fences resume entry on claim ownership the same way the
    CAS-guarded transitions do: a stale executor whose execution claim was
    transferred to a newer claimant sees ``status == expected`` but a mismatched
    ``execution_claimed_by`` and is skipped, so it cannot drive a duplicate run
    behind the new claimant's back."""
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        if ws is None:  # pragma: no cover - destroyed mid-flight
            _log.warning(
                "executor.skip_unknown",
                workspace_id=workspace_id,
                action=action,
            )
            return False
        status_ok = ws.status == expected.value
        owner_ok = owner_id is None or ws.execution_claimed_by == owner_id
        if status_ok and owner_ok:
            return True
        await self._record_stale_action_skip(
            repo,
            ws,
            action=action,
            expected=expected,
            reason_code=reason_code if not status_ok else owner_mismatch_reason_code,
        )
        if _is_callback_terminal_status(ws.status):
            await self._finish_ignored_stale_callback_operations_in_session(
                session,
                workspace_id=workspace_id,
                callback_source="executor",
                callback_action=action,
                expected_status=expected,
                actual_status=ws.status,
            )
        await session.commit()
        return False


async def _transition_if_current(
    self: Any,
    workspace_id: str,
    *,
    from_status: WorkspaceStatus,
    to: WorkspaceStatus,
    reason: str,
    action: str,
) -> bool:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.transition_if_current(
            workspace_id,
            from_status=from_status,
            to=to,
            reason_code=reason,
        )
        if ws is not None:
            await session.commit()
            return True

        current = await repo.get(workspace_id)
        if current is None:
            # pragma: no cover - destroyed mid-flight
            return False

        await self._record_stale_action_skip(
            repo,
            current,
            action=action,
            expected=from_status,
            reason_code="EXECUTOR_STALE_STATUS",
        )
        if _is_callback_terminal_status(current.status):
            await self._finish_ignored_stale_callback_operations_in_session(
                session,
                workspace_id=workspace_id,
                callback_source="executor",
                callback_action=action,
                expected_status=from_status,
                actual_status=current.status,
            )
        await session.commit()
        return False


async def _record_stale_action_skip(
    _self: Any,
    repo: WorkspaceRepository,
    ws: Workspace,
    *,
    action: str,
    expected: WorkspaceStatus,
    reason_code: str,
) -> None:
    _log.info(
        "executor.skip_stale_status",
        workspace_id=ws.id,
        action=action,
        expected_status=expected.value,
        status=ws.status,
    )
    if _is_callback_terminal_status(ws.status):
        await repo.record_ignored_stale_callback(
            ws,
            callback_source="executor",
            callback_action=action,
            expected_status=expected,
            reason_code=reason_code,
        )
    await repo.add_event(
        ws,
        event_type="workspace.stale_action_skipped",
        reason_code=reason_code,
        payload={
            "action": action,
            "expected_status": expected.value,
            "actual_status": ws.status,
        },
    )


async def _record_health_check_failed_event(
    self: Any,
    *,
    workspace_id: str,
    failure: ValidationCommandResult,
) -> None:
    metadata = failure.metadata if isinstance(failure.metadata, dict) else {}
    stream_ids = metadata.get("stream_ids")
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        if ws is None or ws.status != WorkspaceStatus.validating.value:
            return
        await repo.add_event(
            ws,
            event_type="workspace.health_check_failed",
            reason_code=failure.reason_code,
            payload={
                "healthcheck_name": _metadata_str(metadata, "healthcheck_name"),
                "healthcheck_kind": _metadata_str(metadata, "healthcheck_kind"),
                "target": _metadata_str(metadata, "target") or failure.command,
                "attempts": _metadata_int(metadata, "attempts"),
                "timeout_seconds": _metadata_number(metadata, "timeout_seconds"),
                "stream_ids": dict(stream_ids) if isinstance(stream_ids, dict) else {},
            },
        )
        await session.commit()


async def _mark_failed(
    self: Any,
    *,
    workspace_id: str,
    from_status: WorkspaceStatus,
    failure_reason: FailureReason,
    message: str,
    reason_code: str | None = None,
    details: Mapping[str, Any] | None = None,
    salvage: Mapping[str, Any] | None = None,
) -> None:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        final_reason_code = reason_code or failure_reason.value.upper()
        safe_message = redact_audit_text(message, limit=2000)
        payload: dict[str, Any] | None = None
        if details is not None or salvage is not None:
            payload = {
                "failure_reason": failure_reason.value,
                "reason_code": final_reason_code,
                "message": safe_message,
            }
            if details is not None:
                payload["details"] = dict(details)
            if salvage is not None:
                payload["salvage"] = dict(salvage)

        ws = await repo.transition_if_current(
            workspace_id,
            from_status=from_status,
            to=WorkspaceStatus.failed,
            reason_code=final_reason_code,
            payload=payload,
        )
        if ws is None:
            ws = await repo.get(workspace_id)
            if ws is None:  # pragma: no cover
                return
            # Already moved (e.g. cancelled) — respect it.
            await self._record_stale_action_skip(
                repo,
                ws,
                action="mark_failed",
                expected=from_status,
                reason_code="EXECUTOR_MARK_FAILED_SKIPPED",
            )
            if _is_callback_terminal_status(ws.status):
                await self._finish_ignored_stale_callback_operations_in_session(
                    session,
                    workspace_id=workspace_id,
                    callback_source="executor",
                    callback_action="mark_failed",
                    expected_status=from_status,
                    actual_status=ws.status,
                )
            await session.commit()
            return

        ws.failure_reason = failure_reason.value
        ws.failure_message = safe_message
        if final_reason_code == EXEC_PROCESS_CLEANUP_FAILED:
            await repo.add_event(
                ws,
                event_type="workspace.exec_process_cleanup_failed",
                reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                payload={"message": safe_message[:1000]},
            )
        await session.commit()


async def enter_blocked_for_protected_violation(
    self: Any,
    *,
    workspace_id: str,
    from_status: WorkspaceStatus,
    violations: Sequence[QualityGateViolation],
    resume_phase: str,
    execution_owner_id: str | None = None,
) -> bool:
    """Pause a workspace into ``blocked`` for an operator decision instead of
    terminally failing on a protected quality-gate violation.

    This is the single entry point used at every pre-PR protected-gate fail site:
    it replaces ``_mark_failed(... QUALITY_GATE_POLICY_CHANGED ...)`` so the spent
    work is preserved (worktree + warm stack + execution claim are all kept — the
    block transition is not a provisioning release) while an operator resolves the
    violation through the ``guide`` channel.

    Epoch-guarded clean exit (mirrors the #421 terminal-transition CAS): the
    transition is an atomic compare-and-set gated on the row still being in
    ``from_status`` and — when the dispatching worker identity is known — still
    claimed by ``execution_owner_id``. 0 rows means a stale/raced executor lost
    the claim to a newer claimant; the caller returns ``False`` WITHOUT clobbering
    the row, so a late error path cannot drive ``blocked -> failed`` behind the
    new claimant's back. ``block_epoch`` is bumped on every entry so a re-block
    invalidates all prior operator grants (a later DIFFERENT change re-blocks).

    Returns ``True`` when the workspace was paused into ``blocked``.
    """
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await enter_blocked_for_protected_violation_in_session(
            session,
            repo,
            workspace_id=workspace_id,
            from_status=from_status,
            violations=violations,
            resume_phase=resume_phase,
            execution_owner_id=execution_owner_id,
        )
        if ws is None:
            current = await repo.get(workspace_id)
            if current is not None:
                await self._record_stale_action_skip(
                    repo,
                    current,
                    action="enter_blocked_for_protected_violation",
                    expected=from_status,
                    reason_code="EXECUTOR_ENTER_BLOCKED_SKIPPED",
                )
                # Mirror the _transition_if_current / _mark_failed stale paths:
                # a row raced into a callback-terminal status must also finalize
                # any ignored stale callback operations, or they hang forever.
                if _is_callback_terminal_status(current.status):
                    await self._finish_ignored_stale_callback_operations_in_session(
                        session,
                        workspace_id=workspace_id,
                        callback_source="executor",
                        callback_action="enter_blocked_for_protected_violation",
                        expected_status=from_status,
                        actual_status=current.status,
                    )
                await session.commit()
            return False
        # The shared core (``enter_blocked_for_protected_violation_in_session``)
        # already wrote the ``block_*`` columns, bumped ``block_epoch``, stamped
        # ``blocked_at``, and cleared the stale pre-PR directive. This executor
        # wrapper adds only the mid-recovery operation finalization below.
        #
        # A workspace that blocks mid-recovery (a validate/rebase recovery fix
        # pass produced the protected edit) must not leave the recovery
        # Operation active: ``execute`` reloads it via
        # ``_get_active_recovery_payload`` and, while it is non-None, takes the
        # validate-only recovery branch and skips the agent entirely — so a
        # later REVERT/REDO directive resume would silently never run and just
        # revalidate the unchanged protected edit (re-blocking). Finalizing the
        # active recovery operations here clears that branch so a directive
        # resume re-invokes the agent (PRRT_kwDOSJAM6s6KBFxU). The fix-cycle
        # ``_finish_pending_validate_operations`` call already finalizes the
        # ``validate`` recovery row before this transition; this additionally
        # covers the ``rebase_only`` recovery, recorded as an
        # ``OperationType.rebase`` row that path does not touch.
        await self._finish_active_recovery_operations_in_session(
            session,
            workspace_id=workspace_id,
            status=OperationStatus.failed,
            reason_code=QUALITY_GATE_POLICY_CHANGED_REASON_CODE,
            error_message=("Protected quality-gate violation paused the workspace mid-recovery."),
        )
        await session.commit()
        return True


class ProviderFailureDivert(enum.Enum):
    """Outcome of the agent-run provider-failure divert.

    ``paused`` — the row entered ``recovering`` for in-place retry; the caller
    returns WITHOUT the terminal teardown. ``fenced`` — the owner-gated CAS
    matched 0 rows because the row is still in ``from_status`` but a *newer*
    claimant holds the execution claim (this stale executor lost its claim);
    the caller must ALSO return without the terminal teardown, because
    ``_mark_failed`` is not owner-gated and would otherwise drive the newer
    claimant's ``running`` row to ``failed`` (mirrors the provisioner D7
    terminal-CAS fence, #421). ``terminal`` — a non-provider / budget-exhausted
    / fallback failure, or the row genuinely moved on; the caller falls through
    to today's terminal ``_mark_failed`` + ``_prepare_provider_recovery`` path.
    """

    paused = "paused"
    fenced = "fenced"
    terminal = "terminal"


async def enter_recovering_for_provider_failure(
    self: Any,
    *,
    workspace_id: str,
    from_status: WorkspaceStatus,
    message: str,
    reason_code: str | None,
    details: Mapping[str, Any] | None,
    execution_owner_id: str | None = None,
) -> ProviderFailureDivert:
    """Divert a retryable provider failure into the auto-healing ``recovering`` pause.

    Replaces ``_mark_failed(... agent_failure ...)`` at the agent-run failure forks
    when the failure is a retryable provider failure with retry budget remaining
    (#612): the classify + budget decision (``should_recover_in_place``, the SAME
    logic the fail-and-relaunch path runs downstream — T7) is hoisted here so the
    divert lands in ``recovering`` BEFORE the terminal transition / warm-stack
    teardown fires. Returns ``ProviderFailureDivert.paused`` when the workspace was
    paused into ``recovering`` (the caller then returns WITHOUT calling
    ``_mark_failed`` / ``_prepare_provider_recovery``); ``ProviderFailureDivert.terminal``
    for a non-provider / terminal / budget-exhausted / fallback failure (or a row
    that moved on), so the caller falls through to today's terminal path unchanged.

    Epoch-guarded clean exit (mirrors the #421 / blocked CAS): the
    ``running -> recovering`` transition is gated on the row still being in
    ``from_status`` and — when known — still claimed by ``execution_owner_id``.
    0 rows because the row is still in ``from_status`` but claimed by a *newer*
    claimant means this executor is stale/fenced, so we return
    ``ProviderFailureDivert.fenced`` WITHOUT clobbering the row — and the caller
    must NOT fall through, because the terminal ``_mark_failed`` is not owner-gated
    and would otherwise drive the newer claimant's ``running`` row to ``failed``
    behind its back (the D7 fence the provisioner already applies to its terminal
    CAS). The provider cooldown deadline is persisted in
    ``task_policy.provider_recovery_state.not_before`` (read back by
    ``provider_cooldown_not_before`` to gate the resume) and the held execution
    claim is left in place as the warm-stack lease.
    """
    decision_now = datetime.now(UTC)
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        if ws is None:  # pragma: no cover - destroyed mid-flight
            return ProviderFailureDivert.terminal
        decision = should_recover_in_place(
            reason_code=reason_code,
            message=message,
            details=details,
            task_policy=ws.task_policy,
            agent=ws.agent,
            current_model=_policy_model(ws.task_policy),
            now=decision_now,
            effective_default_model=_executor_default_model_for_agent(self, ws.agent),
        )
        if decision is None:
            return ProviderFailureDivert.terminal

        extra_conditions: tuple[Any, ...] = ()
        if execution_owner_id is not None:
            extra_conditions = (Workspace.execution_claimed_by == execution_owner_id,)
        recovery_payload = {
            "provider_recovery": {
                **decision.metadata,
                "action": "retry",
                "recovery_scope": "agent_run_in_place",
                "decision_reason_code": decision.reason_code,
                "retry_attempt_number": decision.retry_attempt_number,
                "not_before": decision.not_before.isoformat(),
            }
        }
        transitioned = await repo.transition_if_current(
            workspace_id,
            from_status=from_status,
            to=WorkspaceStatus.recovering,
            reason_code=decision.reason_code,
            payload=recovery_payload,
            extra_conditions=extra_conditions,
        )
        if transitioned is None:
            current = await repo.get(workspace_id)
            if current is not None:
                await self._record_stale_action_skip(
                    repo,
                    current,
                    action="enter_recovering_for_provider_failure",
                    expected=from_status,
                    reason_code="EXECUTOR_ENTER_RECOVERING_SKIPPED",
                )
                if _is_callback_terminal_status(current.status):
                    await self._finish_ignored_stale_callback_operations_in_session(
                        session,
                        workspace_id=workspace_id,
                        callback_source="executor",
                        callback_action="enter_recovering_for_provider_failure",
                        expected_status=from_status,
                        actual_status=current.status,
                    )
                await session.commit()
            # The CAS lost because a newer claimant holds the row while it is STILL
            # in ``from_status`` (owner mismatch): this executor is fenced. Signal
            # the caller to skip the non-owner-gated terminal ``_mark_failed`` so a
            # stale executor cannot fail a newer claimant's running row. A row that
            # genuinely moved on (status != ``from_status``) is reported terminal —
            # the terminal fallthrough is a safe no-op there (its own ``from_status``
            # CAS rejects it), preserving today's behavior.
            if (
                execution_owner_id is not None
                and current is not None
                and current.status == from_status.value
                and current.execution_claimed_by != execution_owner_id
            ):
                return ProviderFailureDivert.fenced
            return ProviderFailureDivert.terminal

        transitioned.task_policy = in_place_recovery_task_policy(
            transitioned.task_policy,
            decision=decision,
        )
        # A provider failure mid-recovery (a validate/rebase recovery fix pass) must
        # not leave the recovery Operation active — mirror the blocked enter so a
        # later resume re-invokes the agent cleanly rather than taking the
        # validate-only recovery branch and skipping it.
        await self._finish_active_recovery_operations_in_session(
            session,
            workspace_id=workspace_id,
            status=OperationStatus.failed,
            reason_code=decision.reason_code,
            error_message=("Retryable provider failure paused the workspace for in-place retry."),
        )
        await session.commit()
        return ProviderFailureDivert.paused


def _executor_default_model_for_agent(self: Any, agent: str) -> str | None:
    """Resolve the configured default model for ``agent`` (matches ``_prepare_provider_recovery``)."""
    try:
        defaults = self._defaults_for(AgentRuntime(agent))
    except ValueError:
        return None
    return defaults.model if defaults is not None else None


async def _persist_block_baseline_coverage(
    self: Any,
    workspace_id: str,
    *,
    baseline_coverage: ValidationCoverageResult | None,
) -> None:
    """Record the pre-agent baseline coverage so a blocked-resume can reuse it.

    The baseline is measured exactly once per execution, before the agent
    mutates the worktree. A later pause into ``blocked`` preserves the row, so
    persisting the measurement here — at the single point it is known for the
    whole run, ahead of every protected-gate block site — lets a resume ratchet
    final coverage against the ORIGINAL base instead of recomputing against the
    mutated worktree (directive resume) or dropping it (approve-and-keep resume).
    Stores ``None`` when no baseline was measured (coverage skipped / absent), so
    a resume faithfully reproduces the original run's missing baseline.
    """
    metadata = baseline_coverage.as_metadata() if baseline_coverage is not None else None
    async with self._session_factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        if ws is None:
            return
        ws.block_baseline_coverage = metadata
        await session.commit()


async def _persist_block_planning_conformance_handoff(
    self: Any,
    workspace_id: str,
    *,
    handoff: _PlanningValidationHandoff | None,
) -> None:
    """Record the pending plan-conformance handoff so a blocked-resume can reuse it.

    The handoff is produced exactly once per run, in the agent/planning block.
    A later pause into ``blocked`` preserves the row, so persisting it here lets
    an approve-and-keep resume — which skips that block entirely — reconstruct the
    handoff and still run the post-validation plan conformance check (gated on a
    non-None handoff). Stores ``None`` when no handoff applies (conformance
    satisfied inline), so a resume faithfully reproduces the original run rather
    than inventing a pending conformance requirement (mirrors
    ``_persist_block_baseline_coverage``).
    """
    metadata = _planning_validation_handoff_metadata(handoff) if handoff is not None else None
    async with self._session_factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        if ws is None:
            return
        ws.block_planning_conformance_handoff = metadata
        await session.commit()


async def _active_operator_grant_specs(self: Any, workspace_id: str) -> list[GrantSpec]:
    """Return the operator grants active for the workspace's CURRENT block epoch.

    A grant authorizes the protected gate only while it is unconsumed, unrevoked,
    and scoped to ``workspace.block_epoch`` (a re-block bumps the epoch and
    invalidates prior grants). Empty for a normal run — grant rows only exist
    after an operator resolves a ``blocked`` workspace — so threading this into
    every gate caller is a no-op outside the resume path. Delegates to the shared
    session-bound loader so the monitor's grant-aware push checks use the same
    query (no second grant model)."""
    async with self._session_factory() as session:
        return await active_operator_grant_specs_in_session(session, workspace_id)


async def _consume_active_operator_grants(self: Any, workspace_id: str) -> int:
    """Mark the workspace's active (current-epoch) operator grants as consumed.

    Called once a resumed workspace clears the protected gate so a grant is
    strictly single-use: a later DIFFERENT change to the same file must be
    granted again. Delegates to the shared session-bound consumer (same as the
    monitor post-PR resume). Returns the number of grants consumed."""
    async with self._session_factory() as session:
        consumed = await consume_active_operator_grants_in_session(session, workspace_id)
        if consumed:
            await session.commit()
        return consumed


async def _begin_execution(
    self: Any,
    workspace_id: str,
    *,
    resume_reason: PauseResumeReason | None = None,
    execution_owner_id: str | None,
    execution_lease_expires_at: datetime | None,
) -> tuple[Workspace, bool, bool, ValidationCoverageResult | None] | None:
    """Acquire the workspace for ``execute`` and decide the resume mode.

    Returns ``(ws, resume_skip_agent, resume_disable_fix_passes,
    baseline_coverage)`` once the workspace is owned and confirmed ``running``,
    or ``None`` to signal ``execute`` should return early (claim lost, status
    race, or missing row). ``baseline_coverage`` is the pre-agent base
    measurement persisted at block time and reused on a blocked-resume (so the
    coverage ratchet compares against the original base rather than the mutated
    worktree); it is ``None`` on a fresh claim.

    ``resume_reason == "blocked"`` re-enters a workspace the worker has already
    transitioned ``blocked -> running`` (re-acquiring the epoch-fenced execution
    claim) after an operator resolved a protected quality-gate violation. The
    claim was taken by the worker's resume CAS, so ``_claim_ready`` is skipped;
    instead the status recheck is fenced on ``execution_owner_id`` so a stale
    worker whose claim was transferred to a newer claimant cannot resume.
    A revert/redo directive re-invokes the agent with the directive appended; an
    approve-and-keep grant skips the agent entirely (``resume_skip_agent``) and
    re-runs setup + full validation.

    ``resume_reason == "recovering"`` re-enters a workspace the worker moved
    ``recovering -> running`` once the provider cooldown elapsed (#612). Unlike
    the blocked resume this is a fresh agent re-run on a worktree the worker has
    already stashed + reset to HEAD, so it deliberately BYPASSES every blocked-only
    resume semantic: no persisted ``block_baseline_coverage`` reuse (baseline is
    re-measured), no ``pending_operator_hint`` directive injection, no operator
    grants / ``resume_disable_fix_passes``, and never ``resume_skip_agent`` — the
    agent always runs. The status recheck is still epoch-fenced on
    ``execution_owner_id`` so a stale worker cannot drive a duplicate resume.

    ``resume_disable_fix_passes`` is the separate signal that the *secondary*
    agent passes (validation fix passes + pre-commit repair) must stay off. It is
    true whenever operator grants are active — including a combined
    ``--directive ... --grant ...`` guide where ``resume_skip_agent`` is ``False``
    (the directive must run) but the active grants would otherwise let an
    automatic fix pass rewrite a granted protected file and have the new
    violation silently suppressed by the same single-use grant
    (PRRT_kwDOSJAM6s6J7EUX / PRRT_kwDOSJAM6s6J5SDf).
    """
    if resume_reason == "recovering":
        # Auto-healing provider-failure resume: a fresh agent re-run on the
        # worker-reset worktree. Bypass all blocked-only resume semantics — no
        # baseline reuse, no operator hint/grant replay, always re-run the agent.
        ws = await self._load_workspace(workspace_id)
        if ws is None:
            return None
        if not await self._recheck_status(
            workspace_id,
            expected=WorkspaceStatus.running,
            action="resume_recovering_execution",
            owner_id=execution_owner_id,
        ):
            return None
        return ws, False, False, None

    if resume_reason == "blocked":
        ws = await self._load_workspace(workspace_id)
        if ws is None:
            return None
        if not await self._recheck_status(
            workspace_id,
            expected=WorkspaceStatus.running,
            action="resume_blocked_execution",
            owner_id=execution_owner_id,
        ):
            return None
        baseline_coverage = _reuse_persisted_block_baseline(ws.block_baseline_coverage)
        hint = pre_pr_operator_hint_from_payload(ws.pending_operator_hint)
        grant_specs = await self._active_operator_grant_specs(workspace_id)
        grants_active = bool(grant_specs)
        if hint is not None and hint.directive:
            # Revert/redo: re-invoke the agent with the operator directive.
            ws.task_prompt = (
                f"{ws.task_prompt}\n\n[Operator directive — resolve the paused "
                f"protected quality-gate violation]\n{hint.directive}"
            )
            # A combined ``--directive ... --grant ...`` guide arms BOTH: the
            # directive re-invokes the agent (``resume_skip_agent`` stays False)
            # while the grants stay active for the protected-file gates. The
            # directive run is intended, but ``resume_disable_fix_passes`` must
            # still be set when grants are active so the secondary fix passes /
            # pre-commit repair stay off — otherwise an automatic fix pass could
            # rewrite the granted protected file and have the new violation
            # suppressed by the same single-use grant.
            return ws, False, grants_active, baseline_coverage
        # Approve-and-keep: skip the agent (no tokens), re-run setup + validate.
        return ws, grants_active, grants_active, baseline_coverage

    ws = await self._claim_ready(
        workspace_id,
        execution_owner_id=execution_owner_id,
        execution_lease_expires_at=execution_lease_expires_at,
    )
    if ws is None:
        return None
    if not await self._recheck_status(
        workspace_id,
        expected=WorkspaceStatus.running,
        action="execute",
    ):
        return None
    return ws, False, False, None
