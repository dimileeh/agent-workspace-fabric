"""Operator control: set a workspace's managed OpenRouter budget and resume it.

The new budget is frozen in ``task_policy.provider_budget`` (source
``operator``); a live managed key's limit is PATCHed to the new remaining
headroom first, so a failed management call leaves no state change. A workspace
parked on its provider budget resumes through the existing blocked-resume
machinery: a pre-PR park arms a directive for the executor, a monitor-origin
park returns to ``monitoring_pr``.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Final

from awf.adapters.managed_credentials import PROVIDER_MANAGEMENT_KEY_MISSING
from awf.api.schemas import WorkspaceControlResponse
from awf.common.agent_execution import PROVIDER_BUDGET_POLICY_KEY, managed_openrouter_frozen
from awf.control.blocked_transition import is_monitor_origin_block_resume_phase
from awf.control.provider_budget_park import PROVIDER_BUDGET_BLOCK_TYPE
from awf.control.state_machine import WorkspaceStateMachine
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.enums import ProviderManagedKeyStatus as KeyStatus
from awf.db.repositories import (
    OperationRepository,
    ProviderManagedKeyRepository,
    WorkspaceRepository,
)
from awf.runtime.operator_hints import build_pending_operator_hint_payload, utcnow
from awf.runtime.pr_monitor import OperatorHint
from awf.service.controls_errors import WorkspaceControlError
from awf.service.controls_helpers import (
    _add_control_audit_event,
    _control_response,
    _event_payload,
    _operator_operation_payload,
)
from awf.service.openrouter_management import (
    MANAGED_KEY_NOT_FOUND,
    OpenRouterManagementClient,
    OpenRouterManagementError,
)
from awf.service.openrouter_management import usd as quantize_usd
from awf.service.provider_managed_keys import _key_payload, reserved_usd, usd_text

PROVIDER_BUDGET_RAISED: Final[str] = "PROVIDER_BUDGET_RAISED"
PROVIDER_BUDGET_INVALID: Final[str] = "PROVIDER_BUDGET_INVALID"
PROVIDER_BUDGET_UNSUPPORTED: Final[str] = "PROVIDER_BUDGET_UNSUPPORTED"
PROVIDER_BUDGET_RESUME_REASON: Final[str] = "provider_budget_raised"
MAX_PROVIDER_BUDGET_USD: Final[Decimal] = Decimal(1_000_000)
_BUDGETABLE_STATUSES = frozenset(
    status for status in WorkspaceStatus if not WorkspaceStateMachine.is_callback_terminal(status)
)


class ProviderBudgetControlError(WorkspaceControlError):
    """A budget control rejection; ``http_status`` maps it for REST clients."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        http_status: int,
        detail: dict[str, object] | None = None,
    ) -> None:
        super().__init__(error_code=error_code, message=message, detail=detail)
        self.http_status = http_status


async def set_provider_budget(
    self: Any,
    workspace_id: str,
    *,
    usd: float | Decimal | str,
    reason: str | None = None,
    idempotency_key: str | None = None,
    expected_version: int | None = None,
    management_client: OpenRouterManagementClient | None = None,
) -> WorkspaceControlResponse:
    """Set the managed provider budget (USD) of one workspace and resume a park."""
    new_budget = _parse_budget(usd)
    repo = WorkspaceRepository(self._session)
    operations = OperationRepository(self._session)
    reason_text = (reason or "").strip() or None
    payload = _operator_operation_payload(
        reason=reason_text,
        reason_code=PROVIDER_BUDGET_RAISED,
        requested_action=OperationType.provider_budget.value,
        extra={"usd": usd_text(new_budget)},
    )
    prepared = await self._prepare_operation(
        repo,
        operations,
        workspace_id=workspace_id,
        operation_type=OperationType.provider_budget,
        payload=payload,
        idempotency_key=idempotency_key,
        expected_version=expected_version,
    )
    workspace = prepared.workspace
    if prepared.replay is not None:
        return _control_response(
            workspace=workspace, operation=prepared.replay, message="provider budget recorded"
        )
    policy = dict(workspace.task_policy or {})
    if not managed_openrouter_frozen(policy) or workspace.status not in {
        status.value for status in _BUDGETABLE_STATUSES
    }:
        raise ProviderBudgetControlError(
            error_code=PROVIDER_BUDGET_UNSUPPORTED,
            message="Only live workspaces with a managed OpenRouter selection have a budget.",
            http_status=409,
            detail={"status": workspace.status},
        )
    rows = await ProviderManagedKeyRepository(self._session).for_workspace(workspace.id)
    spend = sum((row.spend_usd for row in rows), Decimal(0))
    if new_budget <= spend:
        raise ProviderBudgetControlError(
            error_code=PROVIDER_BUDGET_INVALID,
            message="The provider budget must exceed the spend already recorded.",
            http_status=422,
            detail={"usd": usd_text(new_budget), "spend_usd": usd_text(spend)},
        )
    # Patch the live key before any DB write: a failed call leaves no state change.
    # A key still minting rechecks the budget under this row lock before activation;
    # if this transaction never commits, the key's next heartbeat (which waits on
    # this row lock) re-applies the committed limit.
    live = [row for row in rows if row.status == KeyStatus.active.value and row.key_hash]
    patched: dict[str, str] = {}
    for row in live:
        others = sum((reserved_usd(other) for other in rows if other is not row), Decimal(0))
        limit = max(row.spend_usd, quantize_usd(new_budget - others))
        if management_client is None:
            raise ProviderBudgetControlError(
                error_code=PROVIDER_MANAGEMENT_KEY_MISSING,
                message="A live managed key needs the OpenRouter management key to update.",
                http_status=502,
            )
        try:
            await management_client.update_key_limit(str(row.key_hash), limit)
        except OpenRouterManagementError as error:
            if error.reason_code == MANAGED_KEY_NOT_FOUND:
                # Already deleted (a finalize whose status write failed): the key is
                # deleted only after its usage is durable, so finalize the binding.
                now = utcnow()
                row.status = KeyStatus.deleted.value
                row.last_reason_code = MANAGED_KEY_NOT_FOUND
                row.lease_expires_at = None
                row.deleted_at = now
                row.updated_at = now
                await repo.add_event(
                    workspace,
                    event_type="provider.managed_key.released",
                    reason_code=MANAGED_KEY_NOT_FOUND,
                    payload=_key_payload(row),
                )
                continue
            raise ProviderBudgetControlError(
                error_code=error.reason_code,
                message="OpenRouter rejected the managed key limit update.",
                http_status=502,
                detail={"operation": error.operation, "http_status": error.http_status},
            ) from None
        row.limit_usd = limit
        patched[row.key_name] = usd_text(limit)

    budget = dict(policy.get(PROVIDER_BUDGET_POLICY_KEY) or {})
    old_budget = budget.get("limit_usd")
    budget.update(limit_usd=float(new_budget), source="operator")
    workspace.task_policy = {**policy, PROVIDER_BUDGET_POLICY_KEY: budget}
    operation = await operations.create(
        workspace_id=workspace.id,
        operation_type=OperationType.provider_budget,
        status=OperationStatus.running,
        payload=payload,
        idempotency_key=prepared.idempotency_key,
    )
    parked = (
        workspace.status == WorkspaceStatus.blocked.value
        and workspace.block_type == PROVIDER_BUDGET_BLOCK_TYPE
    )
    figures: dict[str, object | None] = {
        "old_limit_usd": usd_text(Decimal(str(old_budget))) if old_budget is not None else None,
        "new_limit_usd": usd_text(new_budget),
        "spend_usd": usd_text(spend),
        "patched_keys": patched,
        "resumed": parked,
        "operation_id": operation.id,
        "reason": reason_text,
    }
    await repo.add_event(
        workspace,
        event_type="provider.budget.raised",
        reason_code=PROVIDER_BUDGET_RAISED,
        payload=_event_payload(figures, expected_version=expected_version),
    )
    await _add_control_audit_event(
        repo,
        workspace,
        operation=operation,
        action=OperationType.provider_budget.value,
        outcome="succeeded",
        reason_code=PROVIDER_BUDGET_RAISED,
        extra={"expected_version": expected_version, **figures},
    )
    directive = (
        f"The operator raised this workspace's provider budget to {usd_text(new_budget)} USD. "
        "Continue the task from where it stopped; do not redo completed work."
    )
    if parked and is_monitor_origin_block_resume_phase(workspace.block_resume_phase):
        from awf.service.controls_guide import _guide_monitor_origin_blocked_workspace

        return await _guide_monitor_origin_blocked_workspace(
            self,
            repo=repo,
            operations=operations,
            workspace=workspace,
            operation=operation,
            directive_text=directive,
            reason_text=PROVIDER_BUDGET_RESUME_REASON,
            created_grants=[],
            created_grant_ids=set(),
            approve_policy_downgrade=False,
            operator_id="operator",
            now=utcnow(),
            expected_version=expected_version,
        )
    if parked:
        workspace.pending_operator_hint = build_pending_operator_hint_payload(
            OperatorHint(
                reason=PROVIDER_BUDGET_RESUME_REASON,
                directive=directive,
                operation_id=operation.id,
                requested_at=utcnow().isoformat(),
                reason_code=PROVIDER_BUDGET_RAISED,
                status="pending",
            )
        )
    await repo.advance_workspace_version(workspace)
    await operations.finish(
        operation, status=OperationStatus.succeeded, result={"status": workspace.status, **figures}
    )
    return _control_response(
        workspace=workspace, operation=operation, message="provider budget recorded"
    )


def _parse_budget(raw: float | Decimal | str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        value = Decimal(0)
    if isinstance(raw, bool) or not value.is_finite() or not 0 < value < MAX_PROVIDER_BUDGET_USD:
        raise ProviderBudgetControlError(
            error_code=PROVIDER_BUDGET_INVALID,
            message="The provider budget must be a USD amount above 0 and below 1,000,000.",
            http_status=422,
        )
    return quantize_usd(value)


class _ProviderBudgetControlMixin:
    """Wires the provider-budget control onto the workspace control service."""

    set_provider_budget = set_provider_budget
