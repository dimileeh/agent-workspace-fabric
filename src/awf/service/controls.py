"""Shared workspace control operations for REST and MCP adapters."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.schemas import WorkspaceControlResponse, WorkspaceControlWarningResponse
from awf.control.state_machine import WorkspaceStateMachine
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import MergeCandidate, Operation, Workspace
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    ResourceReservationRepository,
    WorkspaceRepository,
)
from awf.db.repositories.base import (
    HOST_PORT_TERMINAL_RELEASE_STATUSES,
    TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
    TERMINAL_RUNTIME_RELEASE_REASON_CODE,
    has_terminal_runtime_released_event,
)
from awf.node.cleanup import (
    WorkspaceCleanupResult,
)
from awf.runtime.operator_hints import (
    arm_operator_hint_freeze,
    build_pending_operator_hint_payload,
    persist_operator_hint,
    remonitor_elapsed_settle_head_shas,
    remonitor_has_elapsed_settle,
    utcnow,
)
from awf.runtime.pr_monitor import OperatorHint
from awf.service.controls_errors import (
    ActiveWorkspaceDestroyError,
    IdempotencyConflictError,
    VersionConflictError,
    WorkspaceControlError,
    WorkspaceNotFoundError,
    WorkspaceRebaseActiveConflictError,
    WorkspaceRebaseMissingCandidateError,
    WorkspaceRebaseMissingPrUrlError,
    WorkspaceRebaseStateError,
    WorkspaceRefreshStateError,
    WorkspaceRemonitorMissingPrUrlError,
    WorkspaceRemonitorStateError,
    WorkspaceStackStopError,
    WorkspaceValidateMissingPrUrlError,
    WorkspaceValidateStateError,
)
from awf.service.controls_types import (
    CleanerFactory,
    ProjectStopper,
    _PreparedOperation,
    _PreparedOperationKind,
)
from awf.service.failure_causality import (
    PRIMARY_FAILURE_KEY,
    SECONDARY_FAILURE_KEY,
    SECONDARY_FAILURE_RECORDED_EVENT_TYPE,
    SECONDARY_FAILURES_KEY,
    build_preserved_failure_payload,
    load_failure_causality_snapshot,
    primary_failure_reason_code,
    restore_primary_failure_row_fields,
)
from awf.service.gc_companions import companion_worktree_remove_targets
from awf.service.secret_leases import (
    TERMINAL_CLEANUP_REVOKE_REASON,
    SecretLeaseService,
    secret_lease_revocation_summary,
)
from awf.service.workspace_runtime_health import (
    OPERATOR_REFRESH_EVENT_TYPE,
    OPERATOR_REFRESH_REASON_CODE,
)

_REMONITOR_ELIGIBLE_STATUSES = (
    WorkspaceStatus.monitoring_pr,
    WorkspaceStatus.failed,
)
_VALIDATE_ELIGIBLE_STATUSES = frozenset({WorkspaceStatus.monitoring_pr})
_VALIDATE_REPLAY_STATUSES = frozenset(
    {
        WorkspaceStatus.monitoring_pr,
        WorkspaceStatus.ready,
        WorkspaceStatus.running,
        WorkspaceStatus.validating,
    }
)
_REBASE_ELIGIBLE_STATUSES = frozenset({WorkspaceStatus.monitoring_pr})
_DESTROYING_OR_DESTROYED_STATUSES = frozenset(
    {WorkspaceStatus.destroying, WorkspaceStatus.destroyed}
)
_REBASE_DESTRUCTIVE_CONFLICT_TYPES = frozenset(
    {
        OperationType.cancel.value,
        OperationType.stop.value,
        OperationType.destroy.value,
    }
)
_OPERATOR_API_SOURCE = "operator_api"
_OPERATOR_CANCEL_REASON_CODE = "OPERATOR_CANCEL"
_OPERATOR_STOP_REASON_CODE = "OPERATOR_STOP"
_OPERATOR_REMONITOR_REASON_CODE = "OPERATOR_REMONITOR"
_OPERATOR_VALIDATE_REASON_CODE = "OPERATOR_VALIDATE"
_OPERATOR_REBASE_REASON_CODE = "OPERATOR_REBASE"
_OPERATOR_DESTROY_REASON_CODE = "OPERATOR_DESTROY"
_REMONITOR_PAST_SETTLE_WARNING_CODE = "REMONITOR_PAST_SETTLE"
_REMONITOR_PAST_SETTLE_HINT_MESSAGE = (
    "Workspace is past reviewer-settle window; auto-merge is frozen "
    "until the operator hint is processed."
)
_REMONITOR_PAST_SETTLE_NO_HINT_MESSAGE = (
    "Workspace is past reviewer-settle window; auto-merge is paused "
    "until reviewer settle is re-evaluated."
)
_AUDIT_CONTROL_OPERATION_EVENT = "workspace.audit.control_operation"
_OPERATION_ERROR_MESSAGE_MAX_LENGTH = 2048


def _require_operator_remonitor_requested_at(requested_at: datetime | None) -> datetime:
    """Return requested_at or raise if the operator remonitor timestamp was never set."""
    if requested_at is None:
        raise RuntimeError("operator remonitor requested_at was not initialized")
    return requested_at


def _remonitor_current_head_sha(
    workspace: Workspace,
    candidate: MergeCandidate | None,
    monitor_state: dict[str, str],
) -> str | None:
    """Determine the effective head SHA for a remonitor, preferring workspace if past settle."""
    candidate_head_sha = (
        candidate.head_sha if candidate is not None and candidate.head_sha else None
    )
    workspace_head_sha = workspace.monitor_last_commit_sha
    if (
        candidate is not None
        and candidate_head_sha is not None
        and workspace_head_sha
        and workspace_head_sha != candidate_head_sha
        and workspace.pr_number is not None
        and workspace.updated_at > candidate.updated_at
        and remonitor_has_elapsed_settle(
            monitor_state,
            pr_number=workspace.pr_number,
            head_sha=workspace_head_sha,
        )
    ):
        return workspace_head_sha
    return candidate_head_sha


class WorkspaceControlService:
    """Business logic for sensitive workspace lifecycle controls."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        project_stopper: ProjectStopper,
        cleaner_factory: CleanerFactory,
    ) -> None:
        """Initialize the workspace control service with session and cleanup dependencies."""
        self._session = session
        self._project_stopper = project_stopper
        self._cleaner_factory = cleaner_factory

    async def cancel_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        stop_stack: bool,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        """Cancel a running workspace, optionally stopping its Docker stack."""

        repo = WorkspaceRepository(self._session)
        operations = OperationRepository(self._session)
        event_payload = _event_payload(
            {"reason": reason, "stop_stack": stop_stack},
            expected_version=expected_version,
        )
        payload = _operator_operation_payload(
            reason=reason,
            reason_code=_OPERATOR_CANCEL_REASON_CODE,
            requested_action=OperationType.cancel.value,
            extra={"stop_stack": stop_stack},
        )
        operation_payload = _operation_payload(payload, expected_version=expected_version)
        prepared = await self._prepare_operation(
            repo,
            operations,
            workspace_id=workspace_id,
            operation_type=OperationType.cancel,
            payload=operation_payload,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
        )
        workspace = prepared.workspace
        replay = prepared.replay
        if replay is not None:
            return _control_response(
                workspace=workspace,
                operation=replay,
                message="workspace cancellation requested",
            )

        operation = await operations.create(
            workspace_id=workspace_id,
            operation_type=OperationType.cancel,
            status=OperationStatus.running,
            payload=operation_payload,
            idempotency_key=prepared.idempotency_key,
        )
        if stop_stack:
            try:
                await self._project_stopper(workspace.compose_project_name)
            except WorkspaceStackStopError as exc:
                await _finish_stack_stop_failed_operation(
                    self._session,
                    operations,
                    operation,
                    workspace=workspace,
                    exc=exc,
                )
                raise
        if (
            workspace.status != WorkspaceStatus.cancelled.value
            and WorkspaceStateMachine.can_transition(
                WorkspaceStatus(workspace.status),
                WorkspaceStatus.cancelled,
            )
        ):
            await repo.transition(
                workspace,
                to=WorkspaceStatus.cancelled,
                reason_code=_OPERATOR_CANCEL_REASON_CODE,
                payload=event_payload,
            )
        else:
            await repo.add_event(
                workspace,
                event_type="workspace.cancel_requested",
                reason_code=_OPERATOR_CANCEL_REASON_CODE,
                payload=event_payload,
            )
        _cancel_final_status = workspace.status
        if (
            stop_stack
            and workspace.compose_project_name is not None
            and _cancel_final_status in HOST_PORT_TERMINAL_RELEASE_STATUSES
            and not await has_terminal_runtime_released_event(self._session, workspace.id)
        ):
            await repo.add_event(
                workspace,
                event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
                payload={
                    "compose_project_name": workspace.compose_project_name,
                    "workspace_status": _cancel_final_status,
                    "cleanup": {
                        "stack_stopped": True,
                        "source": "cancel_workspace",
                    },
                },
            )
        await operations.finish(
            operation,
            status=OperationStatus.succeeded,
            result={"status": workspace.status},
        )
        await _add_control_audit_event(
            repo,
            workspace,
            operation=operation,
            action=OperationType.cancel.value,
            outcome="succeeded",
            reason_code=_OPERATOR_CANCEL_REASON_CODE,
            extra={
                "stop_stack": stop_stack,
                "expected_version": expected_version,
            },
        )
        return _control_response(
            workspace=workspace,
            operation=operation,
            message="workspace cancellation requested",
        )

    async def stop_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        """Stop a workspace's Docker stack and transition it to cancelled."""

        repo = WorkspaceRepository(self._session)
        operations = OperationRepository(self._session)
        event_payload = _event_payload(
            {"reason": reason},
            expected_version=expected_version,
        )
        payload = _operator_operation_payload(
            reason=reason,
            reason_code=_OPERATOR_STOP_REASON_CODE,
            requested_action=OperationType.stop.value,
        )
        operation_payload = _operation_payload(payload, expected_version=expected_version)
        prepared = await self._prepare_operation(
            repo,
            operations,
            workspace_id=workspace_id,
            operation_type=OperationType.stop,
            payload=operation_payload,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
        )
        workspace = prepared.workspace
        replay = prepared.replay
        if replay is not None:
            return _control_response(
                workspace=workspace,
                operation=replay,
                message="workspace stack stopped",
            )

        operation = await operations.create(
            workspace_id=workspace_id,
            operation_type=OperationType.stop,
            status=OperationStatus.running,
            payload=operation_payload,
            idempotency_key=prepared.idempotency_key,
        )
        try:
            await self._project_stopper(workspace.compose_project_name)
        except WorkspaceStackStopError as exc:
            await _finish_stack_stop_failed_operation(
                self._session,
                operations,
                operation,
                workspace=workspace,
                exc=exc,
            )
            raise
        if _is_active(WorkspaceStatus(workspace.status)) and WorkspaceStateMachine.can_transition(
            WorkspaceStatus(workspace.status),
            WorkspaceStatus.cancelled,
        ):
            await repo.transition(
                workspace,
                to=WorkspaceStatus.cancelled,
                reason_code=_OPERATOR_STOP_REASON_CODE,
                payload=event_payload,
            )
        else:
            await repo.add_event(
                workspace,
                event_type="workspace.stack_stopped",
                reason_code=_OPERATOR_STOP_REASON_CODE,
                payload=event_payload,
            )
        if (
            workspace.compose_project_name is not None
            and workspace.status in HOST_PORT_TERMINAL_RELEASE_STATUSES
            and not await has_terminal_runtime_released_event(self._session, workspace.id)
        ):
            await repo.add_event(
                workspace,
                event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
                payload={
                    "compose_project_name": workspace.compose_project_name,
                    "workspace_status": workspace.status,
                    "cleanup": {"stack_stopped": True, "source": "stop_workspace"},
                },
            )
        await operations.finish(
            operation,
            status=OperationStatus.succeeded,
            result={"status": workspace.status},
        )
        await _add_control_audit_event(
            repo,
            workspace,
            operation=operation,
            action=OperationType.stop.value,
            outcome="succeeded",
            reason_code=_OPERATOR_STOP_REASON_CODE,
            extra={"expected_version": expected_version},
        )
        return _control_response(
            workspace=workspace,
            operation=operation,
            message="workspace stack stopped",
        )

    async def remonitor_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        """Restart PR monitoring for a workspace that lost its monitor claim."""

        repo = WorkspaceRepository(self._session)
        operations = OperationRepository(self._session)
        workspace_for_payload = await self._require_workspace(repo, workspace_id)
        payload = _operator_operation_payload(
            reason=reason,
            reason_code=_OPERATOR_REMONITOR_REASON_CODE,
            requested_action=OperationType.remonitor.value,
            extra=_workspace_pr_operation_context(workspace_for_payload),
        )
        operation_payload = _operation_payload(payload, expected_version=expected_version)
        prepared = await self._prepare_operation(
            repo,
            operations,
            workspace_id=workspace_id,
            operation_type=OperationType.remonitor,
            payload=operation_payload,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
        )
        workspace = prepared.workspace
        replay = prepared.replay
        if replay is not None:
            return _control_response(
                workspace=workspace,
                operation=replay,
                message="workspace PR monitor recovery requested",
            )

        current = WorkspaceStatus(workspace.status)
        if current not in _REMONITOR_ELIGIBLE_STATUSES:
            raise WorkspaceRemonitorStateError(workspace)

        if not workspace.pr_url:
            raise WorkspaceRemonitorMissingPrUrlError(workspace)

        operation = await operations.create(
            workspace_id=workspace_id,
            operation_type=OperationType.remonitor,
            status=OperationStatus.running,
            payload=operation_payload,
            idempotency_key=prepared.idempotency_key,
        )
        warnings: list[WorkspaceControlWarningResponse] = []
        pending_operator_hint: dict[str, object] | None = None
        monitor_state_changed = False
        reason_text = (reason or "").strip()
        monitor_state = dict(workspace.monitor_threads_addressed or {})
        candidate_repo = MergeCandidateRepository(self._session)
        candidate = await candidate_repo.get_open_for_workspace_with_merge_inputs(workspace_id)
        current_head_sha = _remonitor_current_head_sha(workspace, candidate, monitor_state)
        if current_head_sha is None:
            latest_candidate = await candidate_repo.get_latest_for_workspace_with_merge_inputs(
                workspace_id
            )
            current_head_sha = (
                latest_candidate.head_sha
                if latest_candidate is not None and latest_candidate.head_sha
                else None
            )
        settle_head_shas = remonitor_elapsed_settle_head_shas(
            monitor_state,
            pr_number=workspace.pr_number,
            preferred_head_sha=workspace.monitor_last_commit_sha,
            current_head_sha=current_head_sha,
        )
        past_settle = bool(settle_head_shas)
        requested_at = utcnow() if reason_text or past_settle else None
        if reason_text:
            requested_at = _require_operator_remonitor_requested_at(requested_at)
            hint = OperatorHint(
                reason=reason_text,
                operation_id=operation.id,
                requested_at=requested_at.isoformat(),
                reason_code=_OPERATOR_REMONITOR_REASON_CODE,
            )
            persist_operator_hint(monitor_state, hint)
            monitor_state_changed = True
            pending_operator_hint = build_pending_operator_hint_payload(hint)
        if past_settle and workspace.pr_number is not None:
            requested_at = _require_operator_remonitor_requested_at(requested_at)
            for settle_head_sha in settle_head_shas:
                arm_operator_hint_freeze(
                    monitor_state,
                    pr_number=workspace.pr_number,
                    head_sha=settle_head_sha,
                    now=requested_at,
                )
            warnings.append(
                WorkspaceControlWarningResponse(
                    warning_code=_REMONITOR_PAST_SETTLE_WARNING_CODE,
                    message=(
                        _REMONITOR_PAST_SETTLE_HINT_MESSAGE
                        if reason_text
                        else _REMONITOR_PAST_SETTLE_NO_HINT_MESSAGE
                    ),
                )
            )
            monitor_state_changed = True
        if monitor_state_changed:
            workspace.monitor_threads_addressed = monitor_state
        claims_reset = _claim_reset_snapshot(workspace)
        claims_will_reset = any(value is not None for value in claims_reset.values())
        state_reset = await _reset_failed_workspace_for_remonitor(
            self._session,
            workspace,
            candidate_head_sha=current_head_sha,
        )
        cancelled_recovery_operations = await _cancel_stale_pr_monitor_recovery_operations(
            operations,
            workspace_id=workspace.id,
        )
        workspace.monitor_claimed_by = None
        workspace.monitor_claim_expires_at = None
        workspace.execution_claimed_by = None
        workspace.execution_claim_expires_at = None
        if state_reset is None and (claims_will_reset or monitor_state_changed):
            await repo.advance_workspace_version(workspace)
        event_payload: dict[str, object | None] = {
            "reason": reason,
            "operation_id": operation.id,
            "claims_reset": claims_reset,
        }
        event_payload = _event_payload(event_payload, expected_version=expected_version)
        if state_reset is not None:
            event_payload["state_reset"] = state_reset
        if cancelled_recovery_operations:
            event_payload["cancelled_recovery_operations"] = cancelled_recovery_operations
            event_payload["cancelled_recovery_reason_code"] = _OPERATOR_REMONITOR_REASON_CODE
            event_payload["cancelled_recovery_requested_action"] = OperationType.remonitor.value
        if pending_operator_hint is not None:
            event_payload["pending_operator_hint"] = pending_operator_hint
        if warnings:
            event_payload["warnings"] = _control_warning_payloads(warnings)
        if state_reset is not None:
            await repo.add_event_with_states(
                workspace,
                event_type="workspace.remonitor_requested",
                old_state=cast(str, state_reset["from"]),
                new_state=cast(str, state_reset["to"]),
                reason_code=_OPERATOR_REMONITOR_REASON_CODE,
                payload=event_payload,
            )
        else:
            await repo.add_event(
                workspace,
                event_type="workspace.remonitor_requested",
                reason_code=_OPERATOR_REMONITOR_REASON_CODE,
                payload=event_payload,
            )
        result: dict[str, object | None] = {
            "status": workspace.status,
            "claims_reset": claims_reset,
            **_workspace_pr_operation_context(workspace),
        }
        if state_reset is not None:
            result["state_reset"] = state_reset
        if cancelled_recovery_operations:
            result["cancelled_recovery_operations"] = cancelled_recovery_operations
        if warnings:
            result["warnings"] = _control_warning_payloads(warnings)
        await operations.finish(
            operation,
            status=OperationStatus.succeeded,
            result=result,
        )
        return _control_response(
            workspace=workspace,
            operation=operation,
            message="workspace PR monitor recovery requested",
            warnings=warnings,
        )

    async def request_refresh_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> Operation:
        """Create or replay an operator refresh operation.

        Exact idempotency-key replays return the stored operation even if the
        workspace later enters destruction. Fresh-key active coalescing still
        observes current state eligibility, so it is rejected once destruction
        has started.
        """
        repo = WorkspaceRepository(self._session)
        operations = OperationRepository(self._session)
        payload = _operator_operation_payload(
            reason=reason,
            reason_code=OPERATOR_REFRESH_REASON_CODE,
            requested_action=OperationType.refresh.value,
        )
        operation_payload = _operation_payload(payload, expected_version=expected_version)
        prepared = await self._prepare_operation(
            repo,
            operations,
            workspace_id=workspace_id,
            operation_type=OperationType.refresh,
            payload=operation_payload,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            active_payload_identity=payload,
        )
        workspace = prepared.workspace
        replay = prepared.replay
        if replay is not None:
            if (
                prepared.kind != _PreparedOperationKind.exact_replay
                and WorkspaceStatus(workspace.status) in _DESTROYING_OR_DESTROYED_STATUSES
            ):
                raise WorkspaceRefreshStateError(workspace)
            return replay
        if WorkspaceStatus(workspace.status) in _DESTROYING_OR_DESTROYED_STATUSES:
            raise WorkspaceRefreshStateError(workspace)

        operation = await operations.create(
            workspace_id=workspace_id,
            operation_type=OperationType.refresh,
            status=OperationStatus.pending,
            payload=operation_payload,
            idempotency_key=prepared.idempotency_key,
        )
        await repo.add_event(
            workspace,
            event_type=OPERATOR_REFRESH_EVENT_TYPE,
            reason_code=OPERATOR_REFRESH_REASON_CODE,
            payload=_event_payload(
                {
                    "source": _OPERATOR_API_SOURCE,
                    "reason": reason,
                    "operation_id": operation.id,
                },
                expected_version=expected_version,
            ),
        )
        return operation

    async def request_validate_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        requested_tier: int | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> Operation:
        """Enqueue a validation recovery operation for the workspace."""

        repo = WorkspaceRepository(self._session)
        operations = OperationRepository(self._session)
        payload = _operator_operation_payload(
            reason=reason,
            reason_code=_OPERATOR_VALIDATE_REASON_CODE,
            requested_action=OperationType.validate.value,
            extra={"recovery_mode": "validate_only"},
        )
        if requested_tier is not None:
            payload["requested_tier"] = requested_tier
        operation_payload = _operation_payload(payload, expected_version=expected_version)
        prepared = await self._prepare_operation(
            repo,
            operations,
            workspace_id=workspace_id,
            operation_type=OperationType.validate,
            payload=operation_payload,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            active_payload_identity=payload,
        )
        workspace = prepared.workspace
        replay = prepared.replay
        current = WorkspaceStatus(workspace.status)
        if replay is not None:
            if (
                prepared.kind != _PreparedOperationKind.exact_replay
                and current not in _VALIDATE_REPLAY_STATUSES
            ):
                raise WorkspaceValidateStateError(workspace)
            return replay
        if current not in _VALIDATE_ELIGIBLE_STATUSES:
            raise WorkspaceValidateStateError(workspace)
        if not workspace.pr_url:
            raise WorkspaceValidateMissingPrUrlError(workspace)

        operation = await operations.create(
            workspace_id=workspace_id,
            operation_type=OperationType.validate,
            status=OperationStatus.pending,
            payload=operation_payload,
            idempotency_key=prepared.idempotency_key,
        )
        validate_event_payload: dict[str, object | None] = {
            "source": _OPERATOR_API_SOURCE,
            "reason": reason,
            "operation_id": operation.id,
            "recovery_mode": "validate_only",
        }
        if requested_tier is not None:
            validate_event_payload["requested_tier"] = requested_tier
        validate_event_payload = _event_payload(
            validate_event_payload,
            expected_version=expected_version,
        )
        await repo.add_event(
            workspace,
            event_type="workspace.validate_requested",
            reason_code=_OPERATOR_VALIDATE_REASON_CODE,
            payload=validate_event_payload,
        )
        await repo.transition(
            workspace,
            to=WorkspaceStatus.ready,
            reason_code=_OPERATOR_VALIDATE_REASON_CODE,
            payload=validate_event_payload,
        )
        return operation

    async def request_rebase_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> Operation:
        """Enqueue a rebase recovery operation for the workspace."""

        repo = WorkspaceRepository(self._session)
        operations = OperationRepository(self._session)
        base_payload = _operator_operation_payload(
            reason=reason,
            reason_code=_OPERATOR_REBASE_REASON_CODE,
            requested_action=OperationType.rebase.value,
            extra={"recovery_mode": "rebase_only"},
        )
        idempotency_payload = _operation_payload(
            base_payload,
            expected_version=expected_version,
        )
        prepared = await self._prepare_operation(
            repo,
            operations,
            workspace_id=workspace_id,
            operation_type=OperationType.rebase,
            payload=idempotency_payload,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            active_payload_identity=base_payload,
            idempotency_payload_identity=idempotency_payload,
            idempotency_identity_keys=frozenset({*base_payload.keys(), "expected_version"}),
        )
        workspace = prepared.workspace
        replay = prepared.replay
        current = WorkspaceStatus(workspace.status)
        if replay is not None:
            if (
                prepared.kind != _PreparedOperationKind.exact_replay
                and current not in _REBASE_ELIGIBLE_STATUSES
            ):
                raise WorkspaceRebaseStateError(workspace)
            return replay

        destructive_conflict = await _find_active_operation(
            operations,
            workspace_id=workspace_id,
            operation_types=_REBASE_DESTRUCTIVE_CONFLICT_TYPES,
        )
        if destructive_conflict is not None:
            raise WorkspaceRebaseActiveConflictError(
                destructive_conflict,
                error_code="WORKSPACE_OPERATION_CONFLICT",
                message=("Workspace rebase conflicts with an active destructive operation."),
            )

        active_rebase = await _find_active_operation(
            operations,
            workspace_id=workspace_id,
            operation_types={OperationType.rebase.value},
        )
        if active_rebase is not None:
            raise WorkspaceRebaseActiveConflictError(active_rebase)

        if current not in _REBASE_ELIGIBLE_STATUSES:
            raise WorkspaceRebaseStateError(workspace)
        if not workspace.pr_url:
            raise WorkspaceRebaseMissingPrUrlError(workspace)

        candidate = await MergeCandidateRepository(
            self._session
        ).get_open_for_workspace_with_merge_inputs(workspace_id)
        if candidate is None:
            raise WorkspaceRebaseMissingCandidateError(workspace)

        rebase_payload = _operation_payload(
            {
                **base_payload,
                **_workspace_rebase_operation_context(workspace, candidate),
            },
            expected_version=expected_version,
        )
        operation = await operations.create(
            workspace_id=workspace_id,
            operation_type=OperationType.rebase,
            status=OperationStatus.pending,
            payload=rebase_payload,
            idempotency_key=prepared.idempotency_key,
        )
        rebase_event_payload = _event_payload(
            {
                "source": _OPERATOR_API_SOURCE,
                "reason": reason,
                "operation_id": operation.id,
                "recovery_mode": "rebase_only",
                "candidate_id": candidate.id,
            },
            expected_version=expected_version,
        )
        await repo.add_event(
            workspace,
            event_type="workspace.rebase_requested",
            reason_code=_OPERATOR_REBASE_REASON_CODE,
            payload=rebase_event_payload,
        )
        await repo.transition(
            workspace,
            to=WorkspaceStatus.ready,
            reason_code=_OPERATOR_REBASE_REASON_CODE,
            payload=rebase_event_payload,
        )
        return operation

    async def destroy_workspace(
        self,
        workspace_id: str,
        *,
        force: bool,
        remove_volumes: bool,
        remove_worktree: bool,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WorkspaceControlResponse:
        """Destroy a workspace, releasing all resources and cleaning up artifacts."""

        repo = WorkspaceRepository(self._session)
        operations = OperationRepository(self._session)
        payload = _operator_operation_payload(
            reason=None,
            reason_code=_OPERATOR_DESTROY_REASON_CODE,
            requested_action=OperationType.destroy.value,
            extra={
                "force": force,
                "remove_volumes": remove_volumes,
                "remove_worktree": remove_worktree,
            },
        )
        operation_payload = _operation_payload(payload, expected_version=expected_version)
        event_payload = _event_payload(
            {
                "force": force,
                "remove_volumes": remove_volumes,
                "remove_worktree": remove_worktree,
            },
            expected_version=expected_version,
        )
        prepared = await self._prepare_operation(
            repo,
            operations,
            workspace_id=workspace_id,
            operation_type=OperationType.destroy,
            payload=operation_payload,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
        )
        workspace = prepared.workspace
        replay = prepared.replay
        if replay is not None:
            message = (
                "workspace already destroyed"
                if workspace.status == WorkspaceStatus.destroyed.value
                else "workspace destroy requested"
            )
            return _control_response(workspace=workspace, operation=replay, message=message)

        current = WorkspaceStatus(workspace.status)
        if _is_active(current) and not force:
            raise ActiveWorkspaceDestroyError()

        operation = await operations.create(
            workspace_id=workspace_id,
            operation_type=OperationType.destroy,
            status=OperationStatus.running,
            payload=operation_payload,
            idempotency_key=prepared.idempotency_key,
        )
        secret_lease_summary = await self._revoke_destroy_secret_leases(workspace)
        if current == WorkspaceStatus.destroyed:
            await ResourceReservationRepository(self._session).release_active_for_workspace(
                workspace_id
            )
            cleanup_result = WorkspaceCleanupResult.skipped(
                reason_code="WORKSPACE_ALREADY_DESTROYED"
            )
            cleanup_payload = cleanup_result.to_dict()
            operation_result = _with_secret_lease_result(
                {
                    "status": WorkspaceStatus.destroyed.value,
                    "cleanup": cleanup_payload,
                },
                secret_lease_summary,
            )
            await operations.finish(
                operation,
                status=OperationStatus.succeeded,
                result=operation_result,
            )
            audit_evidence = _with_secret_lease_evidence(
                {"cleanup": cleanup_payload},
                secret_lease_summary,
            )
            await _add_control_audit_event(
                repo,
                workspace,
                operation=operation,
                action=OperationType.destroy.value,
                outcome="skipped",
                reason_code=_OPERATOR_DESTROY_REASON_CODE,
                extra={
                    "force": force,
                    "remove_volumes": remove_volumes,
                    "remove_worktree": remove_worktree,
                    "expected_version": expected_version,
                },
                evidence=audit_evidence,
            )
            return _control_response(
                workspace=workspace,
                operation=operation,
                message="workspace already destroyed",
            )

        if _is_active(current) and WorkspaceStateMachine.can_transition(
            current, WorkspaceStatus.cancelled
        ):
            await repo.transition(
                workspace,
                to=WorkspaceStatus.cancelled,
                reason_code=_OPERATOR_DESTROY_REASON_CODE,
                payload=event_payload,
            )
            current = WorkspaceStatus.cancelled
        if WorkspaceStateMachine.can_transition(current, WorkspaceStatus.destroying):
            await repo.transition(
                workspace,
                to=WorkspaceStatus.destroying,
                reason_code=_OPERATOR_DESTROY_REASON_CODE,
                payload=event_payload,
            )

        await self._session.flush()
        cleaner = self._cleaner_factory()
        companion_worktrees = companion_worktree_remove_targets(workspace)
        cleanup_result = _normalize_cleanup_result(
            await cleaner.cleanup(
                workspace_id=workspace_id,
                repo_url=workspace.repo_url,
                companion_worktrees=companion_worktrees,
                compose_project_name=workspace.compose_project_name,
                compose_file_path=(
                    Path(workspace.compose_file_path) if workspace.compose_file_path else None
                ),
                worktree_host_path=None,
                remove_volumes=remove_volumes,
                remove_worktree=remove_worktree,
            )
        )
        cleanup_payload = cleanup_result.to_dict()
        # The cleanup callback may append an already-failed secondary event.
        # Refresh with a row lock so the terminal/status decision stays
        # serialized with other workspace mutations.
        await self._session.refresh(workspace, with_for_update=True)
        requested_status = (
            WorkspaceStatus.failed if not cleanup_result.ok else WorkspaceStatus.destroyed
        )
        already_failed_cleanup_failure = (
            not cleanup_result.ok and workspace.status == WorkspaceStatus.failed.value
        )
        if (
            workspace.status != WorkspaceStatus.destroying.value
            and WorkspaceStateMachine.is_callback_terminal(WorkspaceStatus(workspace.status))
            and not already_failed_cleanup_failure
        ):
            ignored_event = await repo.record_ignored_stale_callback(
                workspace,
                callback_source="service.controls",
                callback_action="destroy_cleanup",
                expected_status=WorkspaceStatus.destroying,
                requested_status=requested_status,
                operation_id=operation.id,
                reason_code="STALE_CALLBACK_IGNORED",
            )
            ignored_payload = dict(ignored_event.payload or {})
            operation_result = _with_secret_lease_result(
                {
                    "status": workspace.status,
                    "cleanup": cleanup_payload,
                    "ignored_callback": ignored_payload,
                },
                secret_lease_summary,
            )
            await operations.finish(
                operation,
                status=OperationStatus.cancelled,
                result=operation_result,
            )
            audit_evidence = _with_secret_lease_evidence(
                {
                    "cleanup": cleanup_payload,
                    "ignored_callback": ignored_payload,
                },
                secret_lease_summary,
            )
            await _add_control_audit_event(
                repo,
                workspace,
                operation=operation,
                action=OperationType.destroy.value,
                outcome="skipped",
                reason_code="STALE_CALLBACK_IGNORED",
                extra={
                    "force": force,
                    "remove_volumes": remove_volumes,
                    "remove_worktree": remove_worktree,
                    "expected_version": expected_version,
                },
                evidence=audit_evidence,
            )
            return _control_response(
                workspace=workspace,
                operation=operation,
                message="workspace destroy callback ignored",
            )
        cleanup_event_payload = _event_payload(
            {
                **event_payload,
                "cleanup": cleanup_payload,
            },
            expected_version=None,
        )
        if not cleanup_result.ok:
            cleanup_message = _cleanup_failure_message(cleanup_result)
            bounded_cleanup_message = _bounded_operation_error_message(cleanup_message)
            failure_causality = await load_failure_causality_snapshot(self._session, workspace)
            primary_failure = (
                failure_causality.primary_failure if failure_causality is not None else None
            )
            previous_secondary_failures = (
                failure_causality.secondary_failures if failure_causality is not None else ()
            )
            secondary_failure = {
                "failure_reason": "cleanup_failure",
                "reason_code": "CLEANUP_FAILED",
                "message": bounded_cleanup_message,
                "cleanup": cleanup_payload,
            }
            failed_transition_payload: dict[str, Any]
            preserved_secondary_failure: dict[str, Any] = {}
            preserved_secondary_failures: list[dict[str, Any]] = []
            if primary_failure is not None:
                failed_transition_payload = build_preserved_failure_payload(
                    primary_failure,
                    secondary_failure=secondary_failure,
                    extra=cleanup_event_payload,
                    previous_secondary_failures=previous_secondary_failures,
                )
                preserved_secondary_failure = failed_transition_payload[SECONDARY_FAILURE_KEY]
                preserved_secondary_failures = failed_transition_payload[SECONDARY_FAILURES_KEY]
            else:
                failed_transition_payload = dict(cleanup_event_payload)
            if primary_failure is None:
                workspace.failure_reason = "cleanup_failure"
                workspace.failure_message = bounded_cleanup_message
            else:
                restore_primary_failure_row_fields(workspace, primary_failure)
            workspace_status = WorkspaceStatus(workspace.status)
            failed_reason_code = primary_failure_reason_code(
                primary_failure,
                fallback="CLEANUP_FAILED",
            )
            if WorkspaceStateMachine.can_transition(workspace_status, WorkspaceStatus.failed):
                await repo.transition(
                    workspace,
                    to=WorkspaceStatus.failed,
                    reason_code=failed_reason_code,
                    payload=failed_transition_payload,
                )
            elif workspace_status == WorkspaceStatus.failed:
                # The workspace is already failed, so transition() is not used
                # because there is no valid failed -> failed state-machine edge.
                # Emit the secondary-failure event so cleanup faults remain in
                # the internal causality event stream even when the original
                # failure row never carried durable primary evidence.
                secondary_failure_recorded_payload: dict[str, Any] = {
                    "synthetic": True,
                    SECONDARY_FAILURE_KEY: secondary_failure,
                    SECONDARY_FAILURES_KEY: [
                        *previous_secondary_failures,
                        secondary_failure,
                    ],
                }
                if primary_failure is not None:
                    secondary_failure_recorded_payload.update(failed_transition_payload)
                await repo.add_event(
                    workspace,
                    event_type=SECONDARY_FAILURE_RECORDED_EVENT_TYPE,
                    reason_code=failed_reason_code,
                    payload=secondary_failure_recorded_payload,
                )
            result_payload: dict[str, Any] = {
                "status": workspace.status,
                "cleanup": cleanup_payload,
            }
            if primary_failure is not None:
                result_payload[PRIMARY_FAILURE_KEY] = primary_failure
                result_payload[SECONDARY_FAILURE_KEY] = preserved_secondary_failure
                result_payload[SECONDARY_FAILURES_KEY] = preserved_secondary_failures
            operation_result = _with_secret_lease_result(result_payload, secret_lease_summary)
            await operations.finish(
                operation,
                status=OperationStatus.failed,
                error_code="CLEANUP_FAILED",
                error_message=bounded_cleanup_message,
                result=operation_result,
            )
            audit_payload: dict[str, Any] = {
                "cleanup": cleanup_payload,
                "error_message": bounded_cleanup_message,
            }
            if primary_failure is not None:
                audit_payload[PRIMARY_FAILURE_KEY] = primary_failure
                audit_payload[SECONDARY_FAILURE_KEY] = preserved_secondary_failure
                audit_payload[SECONDARY_FAILURES_KEY] = preserved_secondary_failures
            audit_evidence = _with_secret_lease_evidence(audit_payload, secret_lease_summary)
            await _add_control_audit_event(
                repo,
                workspace,
                operation=operation,
                action=OperationType.destroy.value,
                outcome="failed",
                reason_code="CLEANUP_FAILED",
                extra={
                    "force": force,
                    "remove_volumes": remove_volumes,
                    "remove_worktree": remove_worktree,
                    "expected_version": expected_version,
                },
                evidence=audit_evidence,
            )
            message = "workspace cleanup failed"
        else:
            if WorkspaceStateMachine.can_transition(
                WorkspaceStatus(workspace.status), WorkspaceStatus.destroyed
            ):
                await repo.transition(
                    workspace,
                    to=WorkspaceStatus.destroyed,
                    reason_code="DESTROYED",
                    payload=cleanup_event_payload,
                )
            if (
                workspace.compose_project_name is not None
                and not await has_terminal_runtime_released_event(self._session, workspace.id)
            ):
                await repo.add_event(
                    workspace,
                    event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                    reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
                    payload={
                        "compose_project_name": workspace.compose_project_name,
                        "workspace_status": workspace.status,
                        "cleanup": cleanup_payload,
                    },
                )
            operation_result = _with_secret_lease_result(
                {"status": workspace.status, "cleanup": cleanup_payload},
                secret_lease_summary,
            )
            await operations.finish(
                operation,
                status=OperationStatus.succeeded,
                result=operation_result,
            )
            audit_evidence = _with_secret_lease_evidence(
                {"cleanup": cleanup_payload},
                secret_lease_summary,
            )
            await _add_control_audit_event(
                repo,
                workspace,
                operation=operation,
                action=OperationType.destroy.value,
                outcome="succeeded",
                reason_code=_OPERATOR_DESTROY_REASON_CODE,
                extra={
                    "force": force,
                    "remove_volumes": remove_volumes,
                    "remove_worktree": remove_worktree,
                    "expected_version": expected_version,
                },
                evidence=audit_evidence,
            )
            message = "workspace destroyed"

        return _control_response(
            workspace=workspace,
            operation=operation,
            message=message,
        )

    async def _require_workspace(
        self,
        repo: WorkspaceRepository,
        workspace_id: str,
    ) -> Workspace:
        """Fetch a workspace by ID or raise WorkspaceNotFoundError."""
        workspace = await repo.get(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(workspace_id)
        return workspace

    async def _revoke_destroy_secret_leases(
        self,
        workspace: Workspace,
    ) -> dict[str, Any] | None:
        """Revoke all secret leases for a workspace being destroyed."""
        revoked = await SecretLeaseService(self._session).revoke_workspace_secret_leases(
            workspace,
            now=datetime.now(UTC),
            reason_code=TERMINAL_CLEANUP_REVOKE_REASON,
        )
        if not revoked:
            return None
        return secret_lease_revocation_summary(
            revoked,
            reason_code=TERMINAL_CLEANUP_REVOKE_REASON,
        )

    async def _require_workspace_for_update(
        self,
        repo: WorkspaceRepository,
        workspace_id: str,
    ) -> Workspace:
        """Fetch a workspace with a row-level lock or raise WorkspaceNotFoundError."""
        workspace = await repo.get_for_update(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(workspace_id)
        return workspace

    async def _prepare_operation(
        self,
        repo: WorkspaceRepository,
        operations: OperationRepository,
        *,
        workspace_id: str,
        operation_type: OperationType,
        payload: dict[str, object | None],
        idempotency_key: str | None,
        expected_version: int | None,
        active_payload_identity: dict[str, object | None] | None = None,
        idempotency_payload_identity: dict[str, object | None] | None = None,
        idempotency_identity_keys: frozenset[str] | None = None,
    ) -> _PreparedOperation:
        """Resolve idempotency, version, and active-operation coalescing before creating an operation."""
        if idempotency_key is not None:
            idempotency_key = idempotency_key.strip() or None
        if idempotency_key is not None:
            await operations.acquire_idempotency_key_lock(idempotency_key)
            existing = await operations.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                payload_matches = (
                    _payload_matches_idempotency_identity(
                        existing.payload,
                        identity=idempotency_payload_identity,
                        identity_keys=idempotency_identity_keys,
                    )
                    if idempotency_payload_identity is not None
                    else existing.payload == payload
                )
                if (
                    existing.workspace_id != workspace_id
                    or existing.type != operation_type.value
                    or not payload_matches
                ):
                    raise IdempotencyConflictError()
                workspace = await self._require_workspace(repo, workspace_id)
                return _PreparedOperation(
                    workspace=workspace,
                    replay=existing,
                    kind=_PreparedOperationKind.exact_replay,
                    idempotency_key=idempotency_key,
                )

        workspace = await self._require_workspace_for_update(repo, workspace_id)
        if expected_version is not None and workspace.version != expected_version:
            raise VersionConflictError(
                expected_version=expected_version,
                actual_version=workspace.version,
            )
        if active_payload_identity is not None:
            active = await operations.find_active_matching_payload(
                workspace_id=workspace_id,
                operation_type=operation_type,
                payload_identity=active_payload_identity,
            )
            if active is not None:
                return _PreparedOperation(
                    workspace=workspace,
                    replay=active,
                    kind=_PreparedOperationKind.active_coalesce,
                    idempotency_key=idempotency_key,
                )
        return _PreparedOperation(workspace=workspace, idempotency_key=idempotency_key)


from awf.service.controls_helpers import (  # noqa: E402
    _add_control_audit_event,
    _bounded_operation_error_message,
    _cancel_stale_pr_monitor_recovery_operations,
    _claim_reset_snapshot,
    _cleanup_failure_message,
    _cleanup_optional_string,
    _cleanup_reason_code,
    _cleanup_result_from_mapping,
    _cleanup_status,
    _cleanup_step_status,
    _cleanup_steps_from_mapping,
    _cleanup_string,
    _communicate,
    _control_response,
    _control_warning_payloads,
    _docker_process,
    _event_payload,
    _find_active_operation,
    _finish_stack_stop_failed_operation,
    _is_active,
    _is_pr_monitor_recovery_operation,
    _json_datetime,
    _normalize_cleanup_result,
    _operation_conflict_detail,
    _operation_payload,
    _operator_operation_payload,
    _payload_matches_idempotency_identity,
    _reset_failed_workspace_for_remonitor,
    _with_secret_lease_evidence,
    _with_secret_lease_result,
    _workspace_pr_operation_context,
    _workspace_rebase_operation_context,
    default_cleaner,
    stop_project_containers,
)

__all__ = [
    "ProjectStopper",
    "CleanerFactory",
    "WorkspaceControlService",
    "WorkspaceControlError",
    "WorkspaceNotFoundError",
    "ActiveWorkspaceDestroyError",
    "IdempotencyConflictError",
    "VersionConflictError",
    "WorkspaceStackStopError",
    "WorkspaceRemonitorMissingPrUrlError",
    "WorkspaceRemonitorStateError",
    "WorkspaceRefreshStateError",
    "WorkspaceValidateStateError",
    "WorkspaceValidateMissingPrUrlError",
    "WorkspaceRebaseMissingPrUrlError",
    "WorkspaceRebaseMissingCandidateError",
    "WorkspaceRebaseStateError",
    "WorkspaceRebaseActiveConflictError",
    "stop_project_containers",
    "_docker_process",
    "_communicate",
    "default_cleaner",
    "_reset_failed_workspace_for_remonitor",
    "_cancel_stale_pr_monitor_recovery_operations",
    "_is_pr_monitor_recovery_operation",
    "_control_response",
    "_control_warning_payloads",
    "_operator_operation_payload",
    "_operation_payload",
    "_with_secret_lease_result",
    "_with_secret_lease_evidence",
    "_workspace_pr_operation_context",
    "_workspace_rebase_operation_context",
    "_event_payload",
    "_normalize_cleanup_result",
    "_cleanup_result_from_mapping",
    "_cleanup_steps_from_mapping",
    "_cleanup_status",
    "_cleanup_step_status",
    "_cleanup_reason_code",
    "_cleanup_string",
    "_cleanup_optional_string",
    "_cleanup_failure_message",
    "_finish_stack_stop_failed_operation",
    "_bounded_operation_error_message",
    "_add_control_audit_event",
    "_claim_reset_snapshot",
    "_json_datetime",
    "_find_active_operation",
    "_operation_conflict_detail",
    "_payload_matches_idempotency_identity",
    "_is_active",
]
