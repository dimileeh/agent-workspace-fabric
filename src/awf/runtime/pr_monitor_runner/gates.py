"""Submodule for handling merge gates, settle Decisions, and merge queue blockers."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import inspect

from awf.common.logging import get_logger
from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceEventCreate
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.recovery_payloads import (
    _is_active_pr_monitor_recovery_operation,
    _monitor_recovery_conformance_payload,
)

if TYPE_CHECKING:
    from awf.common.github_client import RepoRef
    from awf.runtime.logs import WorkspaceLogSink
    from awf.runtime.pr_monitor import PRStatus
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner
    from awf.service.merge_queue import MergeQueueBlocker

_log = get_logger(__name__)

# Constants
_ACTIVE_RECOVERY_OPERATION_STATUSES = frozenset(
    {
        OperationStatus.pending.value,
        OperationStatus.running.value,
    }
)
_RECOVERY_SNAPSHOT_ALREADY_HANDLED_REASON = "RECOVERY_SNAPSHOT_ALREADY_HANDLED"


@dataclass(frozen=True)
class _MergeGateResult:
    workspace: Workspace
    stale_reason: str | None = None
    req_action: str | None = None
    notify_message: str | None = None


@dataclass(frozen=True)
class _NonCheckReviewerSettleDecision:
    """Decision result for non-check reviewer settle scheduling."""

    action: str
    wait_seconds: float = 0.0
    configured_reviewers: tuple[str, ...] = ()
    missing_reviewers: tuple[str, ...] = ()
    visible_reviewers: tuple[str, ...] = ()
    started_at: float | None = None
    elapsed_seconds: float | None = None
    remaining_seconds: float | None = None
    activity_anchor_at: datetime | None = None
    activity_anchor_source: str | None = None
    quiet_until: datetime | None = None
    latest_external_review_activity_at: datetime | None = None
    latest_external_review_activity_source: str | None = None
    activity_signature: str | None = None
    state_changed: bool = False


@dataclass(frozen=True)
class _NonCheckReviewerSettleWaitOperationContext:
    """Monitor-state payload and identity fields for reviewer-settle waits."""

    extra_payload: dict[str, object]
    extra_identity: tuple[object, ...]


def _has_successful_validation_for_pr_head(
    workspace: Workspace,
    *,
    attempt_id: str,
    current_head_sha: str | None,
) -> bool:
    """Return ``True`` when the attempt has a succeeded run for ``current_head_sha``."""
    if current_head_sha is None:
        return False
    state = inspect(workspace)
    validation_runs = workspace.validation_runs if "validation_runs" not in state.unloaded else ()
    for run in validation_runs:
        if run.attempt_id != attempt_id:
            continue
        if run.status != "succeeded":
            continue
        if run.workspace_head_sha == current_head_sha or run.target_head_sha == current_head_sha:
            return True
    return False


async def _merge_gate_for_workspace(
    runner: PullRequestMonitorRunner,
    workspace_id: str,
    *,
    check_policy: bool = False,
    current_head_sha: str | None = None,
) -> _MergeGateResult:
    from awf.db.repositories import (
        MergeCandidateRepository,
        PolicyFindingRepository,
        StaleReasonRepository,
        WorkspaceRepository,
        sync_candidate_readiness,
    )
    from awf.runtime.merge_eligibility import (
        DOCS_TASK_SCOPE_VIOLATION_STALE_REASON,
        VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
        VALIDATION_MISSING_FOR_CURRENT_HEAD_STALE_REASON,
        compute_stale_reason_for_attempt,
        stale_reason_blocks_merge,
        stale_reason_required_action,
    )

    async with runner._deps.session_factory() as s:
        candidate = await MergeCandidateRepository(s).get_open_for_workspace_with_merge_inputs(
            workspace_id
        )
        if candidate is None:
            ws = await WorkspaceRepository(s).get_with_validation_runs(workspace_id)
            if ws is None:  # pragma: no cover - defensive invariant
                raise RuntimeError(f"workspace {workspace_id} disappeared mid-monitor")
            return _MergeGateResult(
                workspace=ws,
                notify_message=(
                    "AWF could not find an open merge candidate for this "
                    "workspace. Auto-merge is blocked until candidate "
                    "provenance is repaired."
                ),
            )

        ws = candidate.workspace
        active_stale_reasons = await StaleReasonRepository(s).list_active_for_candidate(
            candidate.id
        )
        active_policy_findings = await PolicyFindingRepository(s).list_active_for_candidate(
            candidate.id
        )
        validation_reason, validation_action = compute_stale_reason_for_attempt(
            ws,
            attempt_id=candidate.attempt_id,
        )
        if (
            validation_reason is None
            and current_head_sha is not None
            and not _has_successful_validation_for_pr_head(
                ws,
                attempt_id=candidate.attempt_id,
                current_head_sha=current_head_sha,
            )
        ):
            validation_reason = VALIDATION_MISSING_FOR_CURRENT_HEAD_STALE_REASON
            validation_action = "validate"

        persisted_stale_reason = candidate.stale_reason or "stale" if candidate.stale else None
        if not stale_reason_blocks_merge(persisted_stale_reason):
            persisted_stale_reason = None
        if validation_reason is None and persisted_stale_reason in (
            VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
            VALIDATION_MISSING_FOR_CURRENT_HEAD_STALE_REASON,
        ):
            persisted_stale_reason = None
        docs_scope_validated_current_head = (
            validation_reason is None
            and persisted_stale_reason == DOCS_TASK_SCOPE_VIOLATION_STALE_REASON
            and _has_successful_validation_for_pr_head(
                ws,
                attempt_id=candidate.attempt_id,
                current_head_sha=current_head_sha,
            )
        )
        if docs_scope_validated_current_head:
            persisted_stale_reason = None
            if current_head_sha is not None:
                candidate.head_sha = current_head_sha
            for reason in active_stale_reasons:
                if reason.reason_code == DOCS_TASK_SCOPE_VIOLATION_STALE_REASON:
                    reason.status = "resolved"
                    reason.resolved_at = datetime.now(UTC)
        blocking_stale_reasons = [
            reason
            for reason in active_stale_reasons
            if reason.blocks_merge
            and not (
                docs_scope_validated_current_head
                and reason.reason_code == DOCS_TASK_SCOPE_VIOLATION_STALE_REASON
            )
        ]
        active_stale_reason = (
            blocking_stale_reasons[0].reason_code if blocking_stale_reasons else None
        )
        stale_reason = validation_reason or active_stale_reason or persisted_stale_reason
        req_action = (
            validation_action
            if validation_reason is not None
            else stale_reason_required_action(stale_reason)
        )
        notify_message: str | None = None

        if not candidate.attempt.is_canonical_for_merge:
            notify_message = (
                "AWF blocked auto-merge because this PR is no longer the "
                "canonical attempt for its task."
            )
        elif not ws.auto_merge:
            notify_message = "AWF blocked auto-merge because auto_merge is disabled."
        elif check_policy and (
            candidate.policy_blocked
            or any(finding.severity == "blocking" for finding in active_policy_findings)
        ):
            notify_message = (
                "OUT_OF_SCOPE_CHANGE: changed files outside declared "
                "owned_paths require an operator scope decision."
            )

        if validation_reason is not None:
            candidate.stale = True
            candidate.stale_reason = validation_reason
        elif active_stale_reason is not None:
            candidate.stale = True
            candidate.stale_reason = active_stale_reason
        elif (
            candidate.stale != (stale_reason is not None) or candidate.stale_reason != stale_reason
        ):
            should_be_stale = stale_reason is not None
            candidate.stale = should_be_stale
            candidate.stale_reason = stale_reason

        sync_candidate_readiness(
            candidate,
            workspace=ws,
            attempt=candidate.attempt,
            sync_validation_staleness=False,
        )
        await s.commit()

        return _MergeGateResult(
            workspace=ws,
            stale_reason=stale_reason,
            req_action=req_action,
            notify_message=notify_message,
        )


async def _merge_gate_with_legacy_head_support(
    runner: PullRequestMonitorRunner,
    workspace_id: str,
    *,
    check_policy: bool = False,
    current_head_sha: str | None = None,
) -> _MergeGateResult:
    if current_head_sha is None:
        return await runner._merge_gate_for_workspace(
            workspace_id,
            check_policy=check_policy,
        )
    try:
        return await runner._merge_gate_for_workspace(
            workspace_id,
            check_policy=check_policy,
            current_head_sha=current_head_sha,
        )
    except TypeError as exc:
        if "current_head_sha" not in str(exc):
            raise
        return await runner._merge_gate_for_workspace(
            workspace_id,
            check_policy=check_policy,
        )


async def _handle_merge_gate_blocker(
    runner: PullRequestMonitorRunner,
    *,
    gate: _MergeGateResult,
    workspace_id: str,
    repo_url: str,
    repo: RepoRef,
    pr_number: int,
    status: PRStatus,
    state: MonitorState,
    base_branch: str,
    remote_branch: str,
    compose_project: str,
    compose_file: Path,
    monitor_log: WorkspaceLogSink | None,
    skip_initial_review_grace: bool = False,
) -> bool | None:
    from awf.runtime.pr_monitor import Abort, AbortReason, NotifyHuman
    from awf.runtime.pr_monitor_operations import (
        RETRYABLE_MONITOR_OPERATION_STATUSES as _RETRYABLE_RECOVERY_TERMINAL_OPERATION_STATUSES,
    )
    from awf.runtime.pr_monitor_operations import (
        build_monitor_operation_payload,
        retryable_monitor_operation_idempotency_key,
    )
    from awf.runtime.pr_monitor_runner.helpers import (
        _gate_requires_validation_recovery,
        _initial_review_grace_wait_seconds,
        _is_callback_terminal_workspace_status,
        _latest_successful_remonitor_at,
        _operation_observed_at,
        _pr_monitor_recovery_reason,
        _pr_monitor_recovery_reason_code,
    )

    stale_reason = gate.stale_reason
    req_action = gate.req_action
    ws = gate.workspace

    if (
        stale_reason is not None
        and not ws.auto_merge
        and not _gate_requires_validation_recovery(gate)
    ):
        return await runner._execute(
            action=Abort(AbortReason.stale),
            workspace_id=workspace_id,
            repo_url=repo_url,
            repo=repo,
            pr_number=pr_number,
            status=status,
            state=state,
            base_branch=base_branch,
            remote_branch=remote_branch,
            compose_project=compose_project,
            compose_file=compose_file,
            monitor_log=monitor_log,
        )

    if stale_reason is not None and req_action == "rebase":
        latest_remonitor_at = _latest_successful_remonitor_at(ws.operations)
        has_failed_rebase = any(
            (
                op.type == "rebase"
                or (
                    isinstance(op.payload, dict)
                    and op.payload.get("source") == "pr_monitor"
                    and op.payload.get("recovery_mode") == "rebase_only"
                )
            )
            and op.status == "failed"
            and (latest_remonitor_at is None or _operation_observed_at(op) > latest_remonitor_at)
            for op in ws.operations
        )
        if has_failed_rebase:
            return await runner._execute(
                action=NotifyHuman(
                    message=(
                        f"Agent could not resolve {stale_reason}. "
                        "Rebase conflicted. Manual intervention required."
                    )
                ),
                workspace_id=workspace_id,
                repo_url=repo_url,
                repo=repo,
                pr_number=pr_number,
                status=status,
                state=state,
                base_branch=base_branch,
                remote_branch=remote_branch,
                compose_project=compose_project,
                compose_file=compose_file,
                monitor_log=monitor_log,
            )

    grace_wait_seconds = (
        0.0
        if skip_initial_review_grace
        else _initial_review_grace_wait_seconds(
            state,
            pr_number=pr_number,
            now=time.monotonic(),
            grace_seconds=runner._config.initial_review_grace_period_seconds,
            poll_interval_seconds=runner._config.poll_interval_seconds,
        )
    )
    if grace_wait_seconds > 0:
        if stale_reason is not None:
            grace_defer_payload: dict[str, object] = {
                "stale_reason": stale_reason,
                "req_action": req_action,
                "wait_seconds": grace_wait_seconds,
                "grace_seconds": runner._config.initial_review_grace_period_seconds,
                "pr_number": pr_number,
                "head_sha": status.head_sha,
            }
            _log.info(
                "monitor.grace_defers_recovery",
                workspace_id=workspace_id,
                **grace_defer_payload,
            )
            await runner._write_monitor_log(
                monitor_log,
                {
                    "event": "monitor.grace_defers_recovery",
                    "workspace_id": workspace_id,
                    **grace_defer_payload,
                },
            )
            await runner._append_workspace_events(
                workspace_id=workspace_id,
                events=[
                    WorkspaceEventCreate(
                        event_type="monitor.grace_defers_recovery",
                        reason_code="GRACE_DEFERS_RECOVERY",
                        payload=grace_defer_payload,
                    )
                ],
            )
        else:
            _log.info(
                "monitor.initial_review_grace_waiting",
                workspace_id=workspace_id,
                pr_number=pr_number,
                wait_seconds=grace_wait_seconds,
                grace_seconds=runner._config.initial_review_grace_period_seconds,
                head_sha=status.head_sha[:10],
            )
        await runner._sleep_with_monitor_state_operation(
            workspace_id=workspace_id,
            action="grace_wait",
            requested_action=req_action or "merge",
            reason="Initial review grace period is still active.",
            reason_code="INITIAL_REVIEW_GRACE",
            pr_number=pr_number,
            status=status,
            base_branch=base_branch,
            remote_branch=remote_branch,
            wait_seconds=grace_wait_seconds,
            monitor_log=monitor_log,
            stale_reason=stale_reason,
            extra_payload={
                "grace_seconds": runner._config.initial_review_grace_period_seconds,
                "req_action": req_action,
                "stale_reason": stale_reason,
            },
            extra_identity=(stale_reason, req_action),
        )
        return False

    if stale_reason is not None:
        recovery_mode = "rebase_only" if req_action == "rebase" else "validate_only"
        requested_action = req_action or "validate"
        recovery_reason = _pr_monitor_recovery_reason(stale_reason)
        recovery_reason_code = _pr_monitor_recovery_reason_code(stale_reason)
        async with runner._deps.session_factory() as s:
            from awf.db.repositories import OperationRepository, WorkspaceRepository

            workspace_repo = WorkspaceRepository(s)
            _ws = await workspace_repo.get(workspace_id)
            if _ws is None:  # pragma: no cover - defensive invariant
                return True
            if _is_callback_terminal_workspace_status(_ws.status):
                await workspace_repo.record_ignored_stale_callback(
                    _ws,
                    callback_source="pr_monitor",
                    callback_action="recovery_dispatch",
                    expected_status=WorkspaceStatus.monitoring_pr,
                    requested_status=WorkspaceStatus.ready,
                    reason_code="STALE_CALLBACK_IGNORED",
                )
                await s.commit()
                return True
            active_recovery = any(
                _is_active_pr_monitor_recovery_operation(op) for op in _ws.operations
            )
            if active_recovery:
                await s.commit()
                await runner._sleep_with_monitor_state_operation(
                    workspace_id=workspace_id,
                    action="recovery_wait",
                    requested_action=requested_action,
                    reason="Waiting for an active PR monitor recovery operation to finish.",
                    reason_code="RECOVERY_IN_PROGRESS",
                    pr_number=pr_number,
                    status=status,
                    base_branch=base_branch,
                    remote_branch=remote_branch,
                    wait_seconds=runner._config.poll_interval_seconds,
                    monitor_log=monitor_log,
                    stale_reason=stale_reason,
                    extra_payload={
                        "recovery_mode": recovery_mode,
                        "stale_reason": stale_reason,
                    },
                    extra_identity=(recovery_mode, stale_reason),
                )
                return False
            if _ws.status != WorkspaceStatus.monitoring_pr.value:
                await workspace_repo.record_ignored_stale_callback(
                    _ws,
                    callback_source="pr_monitor",
                    callback_action="recovery_dispatch",
                    expected_status=WorkspaceStatus.monitoring_pr,
                    requested_status=WorkspaceStatus.ready,
                    reason_code="STALE_CALLBACK_IGNORED",
                )
                await s.commit()
                return True
            operation_payload = build_monitor_operation_payload(
                workspace=_ws,
                action=recovery_mode,
                requested_action=requested_action,
                reason=recovery_reason,
                reason_code=recovery_reason_code,
                pr_number=pr_number,
                source_head_sha=status.head_sha,
                source_base_sha=_ws.base_commit,
                target_branch=base_branch,
                remote_branch=remote_branch,
                recovery_mode=recovery_mode,
                stale_reason=stale_reason,
                log_stream_refs=(
                    {"monitor": monitor_log.stream_id} if monitor_log is not None else None
                ),
                extra=_monitor_recovery_conformance_payload(_ws),
            )
            operation_repo = OperationRepository(s)
            idempotency_key = await retryable_monitor_operation_idempotency_key(
                operation_repo,
                workspace_id=workspace_id,
                action=recovery_mode,
                pr_number=pr_number,
                reason_code=recovery_reason_code,
                source_head_sha=status.head_sha,
                source_base_sha=_ws.base_commit,
            )
            while True:
                await s.refresh(_ws, attribute_names=["status"])
                if _ws.status != WorkspaceStatus.monitoring_pr.value:
                    await workspace_repo.record_ignored_stale_callback(
                        _ws,
                        callback_source="pr_monitor",
                        callback_action="recovery_dispatch",
                        expected_status=WorkspaceStatus.monitoring_pr,
                        requested_status=WorkspaceStatus.ready,
                        reason_code="STALE_CALLBACK_IGNORED",
                    )
                    await s.commit()
                    return True
                operation, created = await operation_repo.create_idempotent(
                    workspace_id=workspace_id,
                    operation_type="validate",
                    payload=operation_payload,
                    idempotency_key=idempotency_key,
                )
                if created:
                    break
                existing_operation_id = operation.id
                existing_operation_status = operation.status
                if existing_operation_status in _RETRYABLE_RECOVERY_TERMINAL_OPERATION_STATUSES:
                    idempotency_key = await retryable_monitor_operation_idempotency_key(
                        operation_repo,
                        workspace_id=workspace_id,
                        action=recovery_mode,
                        pr_number=pr_number,
                        reason_code=recovery_reason_code,
                        source_head_sha=status.head_sha,
                        source_base_sha=_ws.base_commit,
                    )
                    continue
                wait_payload = {
                    "recovery_mode": recovery_mode,
                    "stale_reason": stale_reason,
                    "operation_id": existing_operation_id,
                    "operation_status": existing_operation_status,
                }
                wait_identity = (
                    recovery_mode,
                    stale_reason,
                    existing_operation_id,
                    existing_operation_status,
                )
                await s.commit()
                if existing_operation_status in _ACTIVE_RECOVERY_OPERATION_STATUSES:
                    await runner._sleep_with_monitor_state_operation(
                        workspace_id=workspace_id,
                        action="recovery_wait",
                        requested_action=requested_action,
                        reason="Waiting for an active PR monitor recovery operation to finish.",
                        reason_code="RECOVERY_IN_PROGRESS",
                        pr_number=pr_number,
                        status=status,
                        base_branch=base_branch,
                        remote_branch=remote_branch,
                        wait_seconds=runner._config.poll_interval_seconds,
                        monitor_log=monitor_log,
                        stale_reason=stale_reason,
                        extra_payload=wait_payload,
                        extra_identity=wait_identity,
                    )
                else:
                    await runner._sleep_with_monitor_state_operation(
                        workspace_id=workspace_id,
                        action="recovery_already_handled",
                        requested_action=requested_action,
                        reason=(
                            "A prior PR monitor recovery operation already handled this "
                            "observed PR snapshot."
                        ),
                        reason_code=_RECOVERY_SNAPSHOT_ALREADY_HANDLED_REASON,
                        pr_number=pr_number,
                        status=status,
                        base_branch=base_branch,
                        remote_branch=remote_branch,
                        wait_seconds=runner._config.poll_interval_seconds,
                        monitor_log=monitor_log,
                        stale_reason=stale_reason,
                        extra_payload=wait_payload,
                        extra_identity=wait_identity,
                    )
                return False
            await workspace_repo.transition(
                _ws,
                to=WorkspaceStatus.ready,
                reason_code="RECOVERY_DISPATCH",
            )
            await s.commit()
        dispatch_payload: dict[str, object] = {
            "pr_number": pr_number,
            "head_sha": status.head_sha,
            "reason": stale_reason,
            "req_action": req_action,
            "recovery_mode": recovery_mode,
        }
        _log.info(
            "monitor.recovery_dispatched",
            workspace_id=workspace_id,
            **dispatch_payload,
        )
        await runner._write_monitor_log(
            monitor_log,
            {
                "event": "monitor.recovery_dispatched",
                "workspace_id": workspace_id,
                **dispatch_payload,
            },
        )
        await runner._append_workspace_events(
            workspace_id=workspace_id,
            events=[
                WorkspaceEventCreate(
                    event_type="monitor.recovery_dispatched",
                    reason_code="RECOVERY_DISPATCH",
                    payload=dispatch_payload,
                )
            ],
        )
        return True

    if gate.notify_message is not None:
        return await runner._execute(
            action=NotifyHuman(message=gate.notify_message),
            workspace_id=workspace_id,
            repo_url=repo_url,
            repo=repo,
            pr_number=pr_number,
            status=status,
            state=state,
            base_branch=base_branch,
            remote_branch=remote_branch,
            compose_project=compose_project,
            compose_file=compose_file,
            monitor_log=monitor_log,
        )

    return None


async def _merge_queue_blockers_for_workspace(
    runner: PullRequestMonitorRunner,
    workspace_id: str,
) -> list[MergeQueueBlocker]:
    from awf.service.merge_queue import list_merge_queue_blockers_for_workspace

    async with runner._deps.session_factory() as s:
        return await list_merge_queue_blockers_for_workspace(
            s,
            workspace_id=workspace_id,
        )


async def _wait_for_merge_queue(
    runner: PullRequestMonitorRunner,
    *,
    blockers: list[MergeQueueBlocker],
    workspace_id: str,
    repo_url: str,
    base_branch: str,
    pr_number: int,
    status: PRStatus,
    state: MonitorState,
    monitor_log: WorkspaceLogSink | None,
) -> None:
    from awf.db.repositories import WorkspaceEventCreate
    from awf.runtime.pr_monitor_runner.helpers import (
        _merge_queue_wait_key,
    )

    blocker = blockers[0]
    payload = blocker.event_payload(repo_url=repo_url, base_branch=base_branch)
    log_payload = {
        "workspace_id": workspace_id,
        "pr_number": pr_number,
        "head_sha": status.head_sha[:10],
        "blocker_count": len(blockers),
        **payload,
    }
    _log.info("monitor.merge_queue_waiting", **log_payload)
    await runner._write_monitor_log(
        monitor_log,
        {"event": "monitor.merge_queue_waiting", **log_payload},
    )

    key = _merge_queue_wait_key(
        head_sha=status.head_sha,
        blocker_candidate_id=blocker.candidate_id,
    )
    if state.threads_addressed_ids.get(key) != "waiting":
        await runner._append_workspace_events(
            workspace_id=workspace_id,
            events=[
                WorkspaceEventCreate(
                    event_type="workspace.merge_queue_waiting",
                    reason_code=blocker.reason_code,
                    payload=payload,
                )
            ],
        )
        state.mark_addressed(key, "waiting")
    await runner._sleep_with_monitor_state_operation(
        workspace_id=workspace_id,
        action="merge_queue_wait",
        requested_action="merge",
        reason="An older merge candidate must clear first.",
        reason_code="MERGE_QUEUE_WAIT",
        pr_number=pr_number,
        status=status,
        base_branch=base_branch,
        remote_branch=None,
        wait_seconds=runner._config.poll_interval_seconds,
        monitor_log=monitor_log,
        extra_payload={
            "blocker_count": len(blockers),
            "blocker_reason_code": blocker.reason_code,
            **{k: v for k, v in payload.items() if k != "reason_code"},
        },
        extra_identity=(blocker.candidate_id,),
    )


async def _refresh_scope_policy_for_merge(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    changed_paths: tuple[str, ...],
) -> bool:
    from awf.service.scope_policy import ScopePolicyRefreshService

    async with runner._deps.session_factory() as s:
        result = await ScopePolicyRefreshService(s).refresh_workspace_open_candidate(
            workspace_id,
            changed_paths=changed_paths,
        )
        await s.commit()
    return bool(result and result.policy_blocked)
