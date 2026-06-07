"""Operator-guidance control extracted from :mod:`awf.service.controls`.

Keeps :class:`~awf.service.controls.WorkspaceControlService` under the
first-party file-size guardrail by housing the ``guide_workspace`` control
(issue #447) in a focused module. Behavior is unchanged; the method is wired
back onto the service through :class:`_WorkspaceGuideMixin`, mirroring the
delegate-mixin decomposition used by the executor/worker orchestrators.
"""

from __future__ import annotations

from typing import Any

from awf.api.schemas import WorkspaceControlResponse
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.runtime.operator_hints import (
    build_pending_operator_hint_payload,
    persist_operator_hint,
    utcnow,
)
from awf.runtime.pr_monitor import OperatorHint
from awf.service.controls_errors import (
    _GUIDE_ELIGIBLE_STATUSES,
    WorkspaceGuideEmptyDirectiveError,
    WorkspaceGuideMissingPrUrlError,
    WorkspaceGuideStateError,
)
from awf.service.controls_helpers import (
    _add_control_audit_event,
    _cancel_stale_pr_monitor_recovery_operations,
    _control_response,
    _event_payload,
    _operation_payload,
    _operator_operation_payload,
    _reset_stale_monitor_execution_claims,
    _workspace_pr_operation_context,
)

_OPERATOR_GUIDE_REASON_CODE = "OPERATOR_GUIDE"


async def guide_workspace(
    self: Any,
    workspace_id: str,
    *,
    directive: str,
    reason: str | None = None,
    idempotency_key: str | None = None,
    expected_version: int | None = None,
) -> WorkspaceControlResponse:
    """Inject an operator ``directive`` into a live monitoring workspace.

    Purpose-named operator-guidance control (issue #447). It arms a PENDING
    :class:`OperatorHint` carrying the ``directive`` (the agent instruction,
    distinct from the audit ``reason``) so the monitor's next ``decide()``
    cycle re-engages the agent — even from a prior ``NotifyHuman`` wait —
    without cancelling/re-adopting. It reuses the same OperatorHint engine
    as ``remonitor`` but never changes workspace status (it mutates
    ``monitor_threads_addressed`` while staying ``monitoring_pr``)."""

    repo = WorkspaceRepository(self._session)
    operations = OperationRepository(self._session)
    directive_text = (directive or "").strip()
    if not directive_text:
        # REST strips whitespace at the schema boundary, but the MCP tool only
        # advertises an advisory ``minLength``; guard here so a blank directive
        # can never persist an empty operator hint that re-engages the agent.
        raise WorkspaceGuideEmptyDirectiveError()
    reason_text = (reason or "").strip()
    workspace_for_payload = await self._require_workspace(repo, workspace_id)
    base_payload = _operator_operation_payload(
        # Hash the *stripped* reason so the idempotency identity matches the
        # persisted hint (which stores ``reason_text``). REST strips at the
        # schema boundary, but a direct Python/MCP caller retrying with a
        # whitespace-only variant (" foo " then "foo") would otherwise hash
        # differently and conflict instead of replaying the cached operation.
        reason=reason_text or None,
        reason_code=_OPERATOR_GUIDE_REASON_CODE,
        requested_action=OperationType.guide.value,
        extra={"directive": directive_text},
    )
    # Persist the PR/head context for provenance, but keep it OUT of the
    # idempotency identity. The monitor may push/record a new head between the
    # first request and a same-key retry (e.g. after a lost first response), so
    # a volatile ``source_head_sha``/``source_base_sha`` must not turn a
    # legitimate safe-retry into IDEMPOTENCY_CONFLICT. The identity is the
    # stable directive/reason payload only, mirroring ``request_rebase_workspace``.
    operation_payload = _operation_payload(
        {
            **base_payload,
            **_workspace_pr_operation_context(workspace_for_payload),
        },
        expected_version=expected_version,
    )
    idempotency_payload = _operation_payload(base_payload, expected_version=expected_version)
    prepared = await self._prepare_operation(
        repo,
        operations,
        workspace_id=workspace_id,
        operation_type=OperationType.guide,
        payload=operation_payload,
        idempotency_key=idempotency_key,
        expected_version=expected_version,
        idempotency_payload_identity=idempotency_payload,
        idempotency_identity_keys=frozenset({*base_payload.keys(), "expected_version"}),
    )
    workspace = prepared.workspace
    if prepared.replay is not None:
        return _control_response(
            workspace=workspace,
            operation=prepared.replay,
            message="workspace operator guidance recorded",
        )

    current = WorkspaceStatus(workspace.status)
    if current not in _GUIDE_ELIGIBLE_STATUSES:
        raise WorkspaceGuideStateError(workspace)
    if not workspace.pr_url:
        raise WorkspaceGuideMissingPrUrlError(workspace)

    operation = await operations.create(
        workspace_id=workspace_id,
        operation_type=OperationType.guide,
        status=OperationStatus.running,
        payload=operation_payload,
        idempotency_key=prepared.idempotency_key,
    )
    monitor_state = dict(workspace.monitor_threads_addressed or {})
    hint = OperatorHint(
        reason=reason_text or directive_text,
        directive=directive_text,
        operation_id=operation.id,
        requested_at=utcnow().isoformat(),
        reason_code=_OPERATOR_GUIDE_REASON_CODE,
        status="pending",
    )
    persist_operator_hint(monitor_state, hint)
    workspace.monitor_threads_addressed = monitor_state
    pending_operator_hint = build_pending_operator_hint_payload(hint)
    # Re-engaging the monitor with a fresh directive must pre-empt any
    # in-flight PR-monitor validate_only/rebase_only recovery op (same guard
    # remonitor applies); otherwise the stale recovery cycle keeps running
    # alongside the new directive cycle and conflicts on this workspace.
    cancelled_recovery_operations = await _cancel_stale_pr_monitor_recovery_operations(
        operations,
        workspace_id=workspace.id,
        reason_code=_OPERATOR_GUIDE_REASON_CODE,
        requested_action=OperationType.guide.value,
    )
    # guide only persists a pending directive; it must not evict a *live*
    # monitor/execution lease. Nulling an unexpired lease would make the row
    # immediately re-claimable and let a second worker start a duplicate
    # monitor while the original loop is still running. Clear stale leases
    # only; a live lease keeps ownership and picks up the directive on its
    # next monitor cycle.
    claims_reset = _reset_stale_monitor_execution_claims(workspace, now=utcnow())
    await repo.advance_workspace_version(workspace)
    event_payload: dict[str, object | None] = {
        "reason": reason,
        "directive": directive_text,
        "operation_id": operation.id,
        "claims_reset": claims_reset,
        "pending_operator_hint": pending_operator_hint,
    }
    if cancelled_recovery_operations:
        event_payload["cancelled_recovery_operations"] = cancelled_recovery_operations
        event_payload["cancelled_recovery_reason_code"] = _OPERATOR_GUIDE_REASON_CODE
        event_payload["cancelled_recovery_requested_action"] = OperationType.guide.value
    event_payload = _event_payload(event_payload, expected_version=expected_version)
    await repo.add_event(
        workspace,
        event_type="workspace.guide_requested",
        reason_code=_OPERATOR_GUIDE_REASON_CODE,
        payload=event_payload,
    )
    await _add_control_audit_event(
        repo,
        workspace,
        operation=operation,
        action=OperationType.guide.value,
        outcome="succeeded",
        reason_code=_OPERATOR_GUIDE_REASON_CODE,
        extra={"expected_version": expected_version, "directive": directive_text},
    )
    result: dict[str, object | None] = {
        "status": workspace.status,
        "claims_reset": claims_reset,
        **_workspace_pr_operation_context(workspace),
    }
    if cancelled_recovery_operations:
        result["cancelled_recovery_operations"] = cancelled_recovery_operations
    await operations.finish(
        operation,
        status=OperationStatus.succeeded,
        result=result,
    )
    return _control_response(
        workspace=workspace,
        operation=operation,
        message="workspace operator guidance recorded",
    )


class _WorkspaceGuideMixin:
    """Wires the extracted operator-guidance control onto the control service."""

    guide_workspace = guide_workspace
