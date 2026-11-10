"""Park managed-key provider failures in ``blocked`` instead of failing them.

``PROVIDER_BUDGET_EXHAUSTED``, ``PROVIDER_MANAGED_KEY_UNAVAILABLE`` and
``PROVIDER_MANAGEMENT_KEY_MISSING`` come from one workspace's managed OpenRouter
key. Failing would discard the spent work; retrying or falling back to another
provider would spend outside the operator's budget. Both failure sinks (the
executor's ``_mark_failed`` and the PR monitor's ``_terminate_failed``) divert
them here: the workspace keeps its worktree, warm stack and claim, and resumes
through the existing blocked-resume machinery once an operator raises the
budget (``awf workspace provider-budget``) or restores the key.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from awf.adapters.managed_credentials import PROVIDER_BUDGET_EXHAUSTED, PROVIDER_PARK_REASON_CODES
from awf.common.audit import redact_audit_text, redact_audit_value
from awf.common.logging import get_logger
from awf.control.blocked_transition import enter_blocked_for_protected_violation_in_session
from awf.db.enums import MONITOR_BLOCK_RESUME_PHASE_PREFIX, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository

PROVIDER_BUDGET_BLOCK_TYPE: Final[str] = "provider_budget"
PROVIDER_BUDGET_RESUME_PHASE: Final[str] = "provider_budget"
MONITOR_PROVIDER_BUDGET_RESUME_PHASE: Final[str] = (
    f"{MONITOR_BLOCK_RESUME_PHASE_PREFIX}provider_budget"
)
PROVIDER_BUDGET_EXHAUSTED_EVENT_TYPE: Final[str] = "provider.budget.exhausted"
PROVIDER_MANAGED_KEY_UNAVAILABLE_EVENT_TYPE: Final[str] = "provider.managed_key.unavailable"
_PRE_PR_PARKABLE = frozenset(
    {WorkspaceStatus.running, WorkspaceStatus.validating, WorkspaceStatus.pushing}
)
_log = get_logger(__name__)


def is_provider_park_reason(reason_code: object) -> bool:
    return reason_code in PROVIDER_PARK_REASON_CODES


async def park_for_provider_key(
    executor: Any,
    *,
    workspace_id: str,
    from_status: WorkspaceStatus,
    reason_code: str,
    message: str,
    details: Mapping[str, Any] | None,
    monitor_owner_id: str | None = None,
) -> bool:
    """Divert an executor failure into the provider-budget block; ``False`` = not parked."""
    payload = park_payload(reason_code, message, details)
    if from_status == WorkspaceStatus.monitoring_pr:
        async with executor._session_factory() as session:
            ws = await park_monitor_in_session(
                session,
                WorkspaceRepository(session),
                workspace_id=workspace_id,
                reason_code=reason_code,
                payload=payload,
                monitor_owner_id=monitor_owner_id,
            )
            if ws is None:
                return False
            await session.commit()
        return True
    if from_status not in _PRE_PR_PARKABLE:
        return False
    parked = bool(
        await executor.enter_blocked_for_protected_violation(
            workspace_id=workspace_id,
            from_status=from_status,
            violations=(),
            resume_phase=PROVIDER_BUDGET_RESUME_PHASE,
            block_type=PROVIDER_BUDGET_BLOCK_TYPE,
            block_reason_code=reason_code,
            transition_reason_code=reason_code,
            extra_payload=payload,
            stale_action="park_for_provider_key",
            recovery_finalize_reason_code=reason_code,
            recovery_finalize_message="A managed provider key paused the workspace mid-recovery.",
        )
    )
    if parked:
        async with executor._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            assert ws is not None  # just transitioned into blocked
            await _record_park_event(repo, ws, reason_code=reason_code, payload=payload)
            await session.commit()
    return parked


async def park_monitor_in_session(
    session: Any,
    repo: WorkspaceRepository,
    *,
    workspace_id: str,
    reason_code: str,
    payload: Mapping[str, Any],
    monitor_owner_id: str | None,
) -> Workspace | None:
    """``monitoring_pr -> blocked`` with the monitor-origin resume phase (not committed)."""
    ws = await enter_blocked_for_protected_violation_in_session(
        session,
        repo,
        workspace_id=workspace_id,
        from_status=WorkspaceStatus.monitoring_pr,
        violations=(),
        resume_phase=MONITOR_PROVIDER_BUDGET_RESUME_PHASE,
        block_type=PROVIDER_BUDGET_BLOCK_TYPE,
        block_reason_code=reason_code,
        transition_reason_code=reason_code,
        monitor_owner_id=monitor_owner_id,
        fence_unclaimed_monitor=True,
        extra_payload=payload,
    )
    if ws is not None:
        await _record_park_event(repo, ws, reason_code=reason_code, payload=payload)
    return ws


def park_payload(
    reason_code: str, message: str, details: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Non-secret park evidence: reason, redacted message and budget figures."""
    details = details or {}
    payload: dict[str, Any] = {
        "reason_code": reason_code,
        "message": redact_audit_text(message, limit=2000),
    }
    for key in ("provider_budget", "management_reason_code", "source_reason_code"):
        if details.get(key) is not None:
            payload[key] = redact_audit_value(details[key])
    return payload


async def _record_park_event(
    repo: WorkspaceRepository, ws: Workspace, *, reason_code: str, payload: Mapping[str, Any]
) -> None:
    await repo.add_event(
        ws,
        event_type=PROVIDER_BUDGET_EXHAUSTED_EVENT_TYPE
        if reason_code == PROVIDER_BUDGET_EXHAUSTED
        else PROVIDER_MANAGED_KEY_UNAVAILABLE_EVENT_TYPE,
        reason_code=reason_code,
        payload=dict(payload),
    )
    _log.warning(
        "provider.managed_key.workspace_parked",
        workspace_id=ws.id,
        reason_code=reason_code,
        block_resume_phase=ws.block_resume_phase,
    )
