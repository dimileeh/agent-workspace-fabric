"""Extracted Provisioner short-transaction guard and side-effect helpers.

Mechanically moved from ``awf.node.provisioner`` to keep that module under the
first-party line-count guardrail. Behavior is unchanged; the functions take
``self`` and are wired back onto :class:`~awf.node.provisioner.Provisioner` via
:class:`ProvisionerShortTxnHelpersMixin`.

These helpers share one shape: each opens a fresh ``self._session_factory()``
session, performs a guarded status read or side-effect write, and commits —
distinct from the main provisioning flow that threads a single claimed session.
``self._record_stale_action_skip`` stays on ``Provisioner`` and is reached via
the combined class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from awf.common.logging import get_logger
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    EgressAuditRepository,
    TaskAttemptRepository,
    WorkspaceRepository,
)
from awf.service.secret_leases import SecretLeaseService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from awf.db.enums import EgressDecision
    from awf.node.egress_policy import LocalEgressPlan
    from awf.profiles.models import WorkspaceProfile

_log = get_logger(__name__)


async def _verify_execution_claim_epoch(
    self: Any, workspace_id: str, execution_claim_epoch: int
) -> bool:
    """Return ``True`` only if the row is still ``provisioning`` at the expected epoch.

    Returns ``False`` when the workspace is gone, has left ``provisioning``,
    or a later claimant has advanced ``execution_claim_epoch`` past the
    value this provisioner was dispatched with — i.e. this worker has been
    fenced (D4). No row lock and no ``run_coroutine_threadsafe`` bridge: a
    single cheap indexed point-read on the event loop before ``launch()``.
    Reads only ``execution_claim_epoch`` (gated on ``status =
    'provisioning'``) instead of loading the full workspace row.
    """
    async with self._session_factory() as session:
        epoch = await WorkspaceRepository(session).read_provisioning_execution_claim_epoch(
            workspace_id
        )
        return epoch is not None and epoch == execution_claim_epoch


async def _record_egress_audit_if_current(
    self: Any,
    *,
    workspace_id: str,
    egress_plan: LocalEgressPlan,
    egress_decision: EgressDecision,
    destination_category: str,
    execution_claim_epoch: int | None = None,
) -> bool:
    """Record an egress audit only if the workspace is still in provisioning.

    ``execution_claim_epoch`` (when supplied) fences the write the same way the
    terminal ``_mark_failed`` transition does (D7): if a later claimant advanced
    ``execution_claim_epoch`` during the launch ``to_thread`` window, this
    provisioner has been fenced and must not commit an immutable egress audit —
    stale policy evidence against the new claimant's row. The row can still read
    ``provisioning`` because the new claimant re-enters that status, so the
    status check alone is insufficient.
    """
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        if ws is None:  # pragma: no cover - race with hard deletion
            _log.warning(
                "provisioner.skip_unknown",
                workspace_id=workspace_id,
                action="record_egress_audit",
            )
            return False
        if ws.status != WorkspaceStatus.provisioning.value:
            await self._record_stale_action_skip(
                repo,
                ws,
                action="record_egress_audit",
                expected=WorkspaceStatus.provisioning,
                reason_code="PROVISIONER_STALE_STATUS",
            )
            await session.commit()
            return False
        if execution_claim_epoch is not None and ws.execution_claim_epoch != execution_claim_epoch:
            _log.info(
                "provisioner.skip_fenced_epoch",
                workspace_id=workspace_id,
                action="record_egress_audit",
                expected_epoch=execution_claim_epoch,
                actual_epoch=ws.execution_claim_epoch,
            )
            return False
        await self._create_egress_audit_record(
            session,
            workspace_id=workspace_id,
            egress_plan=egress_plan,
            egress_decision=egress_decision,
            destination_category=destination_category,
        )
        await session.commit()
        return True


async def _create_egress_audit_record(
    self: Any,  # noqa: ARG001 - bound as a method; operates on the passed session
    session: AsyncSession,
    *,
    workspace_id: str,
    egress_plan: LocalEgressPlan,
    egress_decision: EgressDecision,
    destination_category: str,
) -> None:
    """Persist an egress audit record for the workspace's network policy decision."""
    attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace_id)
    await EgressAuditRepository(session).create(
        workspace_id=workspace_id,
        attempt_id=attempt.id if attempt is not None else None,
        policy_posture=egress_plan.mode.value,
        decision=egress_decision.value,
        destination_category=destination_category,
        reason_code=egress_plan.reason_code,
        details=dict(egress_plan.details),
    )


async def _issue_secret_leases(
    self: Any,
    workspace_id: str,
    profile: WorkspaceProfile,
) -> None:
    """Issue secret leases declared in the workspace profile."""
    if not profile.secrets:
        return
    try:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:
                return
            if ws.status != WorkspaceStatus.provisioning.value:
                await self._record_stale_action_skip(
                    repo,
                    ws,
                    action="issue_secret_leases",
                    expected=WorkspaceStatus.provisioning,
                    reason_code="PROVISIONER_STALE_STATUS",
                )
                await session.commit()
                return
            await SecretLeaseService(session).issue_profile_secret_leases(ws, profile)
            await session.commit()
    except Exception:
        _log.exception(
            "provisioner.secret_lease_issue_failed",
            workspace_id=workspace_id,
        )
        raise


async def _recheck_status(
    self: Any,
    workspace_id: str,
    *,
    expected: WorkspaceStatus,
    action: str,
    reason_code: str,
) -> bool:
    """Return True if the workspace is still in the expected status, False otherwise."""
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        if ws is None:  # pragma: no cover - race with hard deletion
            _log.warning(
                "provisioner.skip_unknown",
                workspace_id=workspace_id,
                action=action,
            )
            return False
        if ws.status == expected.value:
            return True
        await self._record_stale_action_skip(
            repo,
            ws,
            action=action,
            expected=expected,
            reason_code=reason_code,
        )
        await session.commit()
        return False


class ProvisionerShortTxnHelpersMixin:
    """Short-transaction guard/side-effect helpers delegated from ``Provisioner``."""

    _verify_execution_claim_epoch = _verify_execution_claim_epoch
    _record_egress_audit_if_current = _record_egress_audit_if_current
    _create_egress_audit_record = _create_egress_audit_record
    _issue_secret_leases = _issue_secret_leases
    _recheck_status = _recheck_status
