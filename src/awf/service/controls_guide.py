"""Operator-guidance control extracted from :mod:`awf.service.controls`.

Keeps :class:`~awf.service.controls.WorkspaceControlService` under the
first-party file-size guardrail by housing the ``guide_workspace`` control
(issue #447) in a focused module. Behavior is unchanged; the method is wired
back onto the service through :class:`_WorkspaceGuideMixin`, mirroring the
delegate-mixin decomposition used by the executor/worker orchestrators.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import select

from awf.api.schemas import WorkspaceControlResponse
from awf.common.ids import new_operator_grant_id
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import OperatorGrantAuditRecord
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    WorkspaceRepository,
    owned_paths_overlap,
)
from awf.runtime.operator_hints import (
    build_pending_operator_hint_payload,
    persist_operator_hint,
    utcnow,
)
from awf.runtime.pr_monitor import OperatorHint
from awf.service.controls_errors import (
    _GUIDE_ELIGIBLE_STATUSES,
    WorkspaceGuideEmptyDirectiveError,
    WorkspaceGuideGrantNotAllowedError,
    WorkspaceGuideGrantReasonRequiredError,
    WorkspaceGuideInvalidGrantPathError,
    WorkspaceGuideMissingPrUrlError,
    WorkspaceGuidePolicyDowngradeRequiredError,
    WorkspaceGuideStateError,
)
from awf.service.controls_helpers import (
    _add_control_audit_event,
    _cancel_stale_pr_monitor_recovery_operations,
    _claim_reset_snapshot,
    _control_response,
    _event_payload,
    _operation_payload,
    _operator_operation_payload,
    _remonitor_current_head_sha,
    _reset_failed_workspace_for_remonitor,
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
    grants: list[str] | None = None,
    approve_policy_downgrade: bool = False,
    operator: str | None = None,
    idempotency_key: str | None = None,
    expected_version: int | None = None,
) -> WorkspaceControlResponse:
    """Inject an operator ``directive`` into a live or failed workspace.

    Purpose-named operator-guidance control (issue #447). It arms a PENDING
    :class:`OperatorHint` carrying the ``directive`` (the agent instruction,
    distinct from the audit ``reason``) so the monitor's next ``decide()``
    cycle re-engages the agent — even from a prior ``NotifyHuman`` wait —
    without cancelling/re-adopting. For ``monitoring_pr`` workspaces it
    mutates ``monitor_threads_addressed`` without changing status; for
    ``failed`` workspaces (issue #456) it resets to ``monitoring_pr``
    using the same state-reset as ``remonitor``."""

    repo = WorkspaceRepository(self._session)
    operations = OperationRepository(self._session)
    directive_text = (directive or "").strip()
    grant_inputs = sorted({grant.strip() for grant in (grants or []) if grant.strip()})
    if not directive_text and not grant_inputs:
        # REST strips whitespace at the schema boundary, but the MCP tool only
        # advertises an advisory ``minLength``; guard here so a blank directive
        # (with no grants) can never persist an empty operator hint that
        # re-engages the agent.
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
        extra={
            "directive": directive_text,
            "grants": grant_inputs,
            "approve_policy_downgrade": approve_policy_downgrade,
        },
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

    if current == WorkspaceStatus.blocked:
        # Pre-PR operator decision: arm scoped grants and/or a directive on the
        # paused workspace so the worker resume path can pick it up. No PR exists
        # yet, so the ``pr_url`` requirement is relaxed for this branch.
        return await _guide_blocked_workspace(
            self,
            repo=repo,
            operations=operations,
            workspace=workspace,
            prepared=prepared,
            directive_text=directive_text,
            reason_text=reason_text,
            grant_inputs=grant_inputs,
            approve_policy_downgrade=approve_policy_downgrade,
            operator=operator,
            operation_payload=operation_payload,
            expected_version=expected_version,
        )

    if grant_inputs:
        # Path grants only make sense for a blocked workspace.
        raise WorkspaceGuideGrantNotAllowedError(workspace)
    if not directive_text:
        raise WorkspaceGuideEmptyDirectiveError()
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
    #
    # Exception: a failed workspace has no live worker — any claims belong
    # to a dead worker and must be force-cleared so claim_monitoring_pr can
    # acquire the row immediately after the failed→monitoring_pr transition
    # (mirrors remonitor_workspace, which unconditionally nulls all claims).
    claims_reset = _reset_stale_monitor_execution_claims(workspace, now=utcnow())
    # For failed→monitoring_pr resets, resolve the effective head SHA using the
    # same logic as remonitor_workspace so the reopened merge candidate gets the
    # post-settle workspace SHA instead of the potentially stale candidate.head_sha.
    candidate_head_sha: str | None = None
    if current == WorkspaceStatus.failed:
        candidate_repo = MergeCandidateRepository(self._session)
        open_candidate = await candidate_repo.get_open_for_workspace_with_merge_inputs(workspace_id)
        candidate_head_sha = _remonitor_current_head_sha(workspace, open_candidate, monitor_state)
        if candidate_head_sha is None:
            latest = await candidate_repo.get_latest_for_workspace_with_merge_inputs(workspace_id)
            candidate_head_sha = latest.head_sha if latest is not None and latest.head_sha else None
    state_reset = await _reset_failed_workspace_for_remonitor(
        self._session, workspace, candidate_head_sha=candidate_head_sha
    )
    if state_reset is not None:
        remaining = _claim_reset_snapshot(workspace)
        workspace.monitor_claimed_by = None
        workspace.monitor_claim_expires_at = None
        workspace.execution_claimed_by = None
        workspace.execution_claim_expires_at = None
        for key, val in remaining.items():
            if val is not None:
                claims_reset[key] = val
    if state_reset is None:
        await repo.advance_workspace_version(workspace)
    event_payload: dict[str, object | None] = {
        "reason": reason,
        "directive": directive_text,
        "operation_id": operation.id,
        "claims_reset": claims_reset,
        "pending_operator_hint": pending_operator_hint,
    }
    if state_reset is not None:
        event_payload["state_reset"] = state_reset
    if cancelled_recovery_operations:
        event_payload["cancelled_recovery_operations"] = cancelled_recovery_operations
        event_payload["cancelled_recovery_reason_code"] = _OPERATOR_GUIDE_REASON_CODE
        event_payload["cancelled_recovery_requested_action"] = OperationType.guide.value
    event_payload = _event_payload(event_payload, expected_version=expected_version)
    if state_reset is not None:
        await repo.add_event_with_states(
            workspace,
            event_type="workspace.guide_requested",
            old_state=cast(str, state_reset["from"]),
            new_state=cast(str, state_reset["to"]),
            reason_code=_OPERATOR_GUIDE_REASON_CODE,
            payload=event_payload,
        )
    else:
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
    if state_reset is not None:
        result["state_reset"] = state_reset
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


_DEFAULT_GUIDE_OPERATOR = "operator"

# Matches the ``OperatorGrantAuditRecord.normalized_path`` column width. A grant
# path that exceeds it must be rejected as a 400 here rather than reaching the
# DB insert, where Postgres would raise a length error (500 + transaction
# rollback) for what is really an invalid operator request.
_MAX_GRANT_PATH_LENGTH = 1024


def _canonicalize_grant_path(path: str) -> str:
    """Normalize a grant glob to a repo-relative path, rejecting traversal.

    Mirrors the quality-gate path normalization (backslash → ``/``, strip
    leading ``./``) so grant globs match changed paths under the same matcher,
    and fails closed on anything that could escape the repo: absolute paths and
    any ``..`` segment are rejected."""
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized:
        raise WorkspaceGuideInvalidGrantPathError(path, "path is empty")
    if normalized.startswith("/"):
        raise WorkspaceGuideInvalidGrantPathError(path, "must be repo-relative (no leading '/')")
    if any(segment == ".." for segment in normalized.split("/")):
        raise WorkspaceGuideInvalidGrantPathError(path, "'..' traversal is not allowed")
    if len(normalized) > _MAX_GRANT_PATH_LENGTH:
        raise WorkspaceGuideInvalidGrantPathError(
            path, f"exceeds maximum length of {_MAX_GRANT_PATH_LENGTH} characters"
        )
    return normalized


async def _guide_blocked_workspace(
    self: Any,
    *,
    repo: WorkspaceRepository,
    operations: OperationRepository,
    workspace: Any,
    prepared: Any,
    directive_text: str,
    reason_text: str,
    grant_inputs: list[str],
    approve_policy_downgrade: bool,
    operator: str | None,
    operation_payload: dict[str, Any],
    expected_version: int | None,
) -> WorkspaceControlResponse:
    """Resolve a pre-PR ``blocked`` workspace via scoped grants and/or a directive.

    Operator-only: ``operator`` is set by the control plane, never by workspace
    input, and the agent has no control-plane egress to reach this surface. The
    grants are recorded as immutable, single-use, epoch-bound audit rows; the
    directive (if any) is armed in the dedicated ``pending_operator_hint`` column
    for the worker resume path. The workspace stays ``blocked`` — the worker's
    blocked-resume claim performs the ``blocked -> running`` transition.

    ``guide_workspace`` already rejects an empty directive with no grants (with
    ``WorkspaceGuideEmptyDirectiveError``) before dispatching here, so at least
    one of ``directive_text``/``grant_inputs`` is always present.
    """
    canonical_grants = sorted({_canonicalize_grant_path(path) for path in grant_inputs})
    if canonical_grants and not reason_text:
        raise WorkspaceGuideGrantReasonRequiredError()
    # Fail-fast: any grant that covers a recorded protected violation must carry
    # the policy-downgrade acknowledgement. The gate only suppresses a real
    # violation with an acked grant (a non-acked grant would re-block on resume),
    # so reject upfront rather than letting the operator queue a useless grant.
    if canonical_grants and not approve_policy_downgrade:
        violation_paths = [
            str(violation.get("path"))
            for violation in (workspace.block_violations or [])
            if isinstance(violation, dict) and violation.get("path")
        ]
        weakening = sorted(
            grant
            for grant in canonical_grants
            if any(owned_paths_overlap(violation_path, grant) for violation_path in violation_paths)
        )
        if weakening:
            raise WorkspaceGuidePolicyDowngradeRequiredError(weakening)

    operation = await operations.create(
        workspace_id=workspace.id,
        operation_type=OperationType.guide,
        status=OperationStatus.running,
        payload=operation_payload,
        idempotency_key=prepared.idempotency_key,
    )
    operator_id = (operator or "").strip() or _DEFAULT_GUIDE_OPERATOR
    now = utcnow()
    created_grants: list[dict[str, object]] = []
    revoked_grants: list[str] = []
    for grant_path in canonical_grants:
        self._session.add(
            OperatorGrantAuditRecord(
                id=new_operator_grant_id(),
                workspace_id=workspace.id,
                operator=operator_id,
                reason=reason_text,
                normalized_path=grant_path,
                block_epoch=workspace.block_epoch,
                approve_policy_downgrade=approve_policy_downgrade,
                created_at=now,
            )
        )
        created_grants.append(
            {"path": grant_path, "approve_policy_downgrade": approve_policy_downgrade}
        )

    pending_hint_payload: dict[str, object] | None = None
    if directive_text:
        hint = OperatorHint(
            reason=reason_text or directive_text,
            directive=directive_text,
            operation_id=operation.id,
            requested_at=now.isoformat(),
            reason_code=_OPERATOR_GUIDE_REASON_CODE,
            status="pending",
        )
        pending_hint_payload = build_pending_operator_hint_payload(hint)
        workspace.pending_operator_hint = pending_hint_payload
        if not canonical_grants:
            # Directive-only guide: revoke current-epoch grants armed by a prior
            # approve-and-keep guide. The blocked-resume path loads
            # ``_active_operator_grant_specs()`` for every protected-file gate
            # regardless of the directive, so a stale grant would still suppress a
            # violation on the same path even though the latest operator decision
            # was to revert/redo. Mirror of the grants-only branch clearing a
            # stale directive: the latest operator decision must win. A combined
            # guide (``canonical_grants`` non-empty) is exempt — it must not
            # revoke the grant it is recording in this same call.
            stale_grants = (
                (
                    await self._session.execute(
                        select(OperatorGrantAuditRecord).where(
                            OperatorGrantAuditRecord.workspace_id == workspace.id,
                            OperatorGrantAuditRecord.block_epoch == workspace.block_epoch,
                            OperatorGrantAuditRecord.consumed_at.is_(None),
                            OperatorGrantAuditRecord.revoked_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for stale_grant in stale_grants:
                stale_grant.revoked_at = now
                revoked_grants.append(stale_grant.normalized_path)
    else:
        # Grants-only guide: clear any directive armed by a prior guide. The
        # blocked-resume path prioritizes a stored directive over active grants
        # (``if hint.directive ... elif grant_specs``), so a stale revert/redo
        # directive left in place would override this fresh approve-and-keep
        # decision on resume. The latest operator decision must win.
        workspace.pending_operator_hint = None

    await repo.advance_workspace_version(workspace)
    event_payload = _event_payload(
        {
            "reason": reason_text or None,
            "directive": directive_text or None,
            "operation_id": operation.id,
            "operator": operator_id,
            "grants": created_grants,
            "revoked_grants": revoked_grants or None,
            "approve_policy_downgrade": approve_policy_downgrade,
            "block_epoch": workspace.block_epoch,
            "pending_operator_hint": pending_hint_payload,
        },
        expected_version=expected_version,
    )
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
        extra={
            "expected_version": expected_version,
            "directive": directive_text,
            "grants": created_grants,
            "revoked_grants": revoked_grants,
        },
    )
    result: dict[str, object | None] = {
        "status": workspace.status,
        "grants": created_grants,
        "block_epoch": workspace.block_epoch,
    }
    if pending_hint_payload is not None:
        result["pending_operator_hint"] = pending_hint_payload
    if revoked_grants:
        result["revoked_grants"] = revoked_grants
    await operations.finish(operation, status=OperationStatus.succeeded, result=result)
    return _control_response(
        workspace=workspace,
        operation=operation,
        message="workspace operator guidance recorded",
    )


class _WorkspaceGuideMixin:
    """Wires the extracted operator-guidance control onto the control service."""

    guide_workspace = guide_workspace
