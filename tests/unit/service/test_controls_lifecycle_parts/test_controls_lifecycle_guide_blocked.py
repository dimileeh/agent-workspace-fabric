"""Behavior tests for ``guide`` resolving a pre-PR ``blocked`` workspace."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.ids import new_operator_grant_id
from awf.db.enums import WorkspaceStatus
from awf.db.models import OperatorGrantAuditRecord, Workspace
from awf.runtime.operator_hints import pre_pr_operator_hint_from_payload, utcnow
from awf.service.controls import (
    WorkspaceGuideEmptyDirectiveError,
    WorkspaceGuideGrantNotAllowedError,
    WorkspaceGuideGrantReasonRequiredError,
    WorkspaceGuideInvalidGrantPathError,
    WorkspaceGuidePolicyDowngradeRequiredError,
)
from tests.postgres import postgres_test_session
from tests.unit.service.test_controls_lifecycle_parts.controls_lifecycle_helpers import (
    _service,
    _workspace,
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


async def _blocked_workspace(
    session: AsyncSession,
    *,
    block_epoch: int = 1,
    violations: list[dict[str, object]] | None = None,
) -> Workspace:
    workspace = await _workspace(session, status=WorkspaceStatus.blocked)
    workspace.block_reason_code = "QUALITY_GATE_POLICY_CHANGED"
    workspace.block_type = "protected_quality_gate"
    workspace.block_epoch = block_epoch
    workspace.block_violations = (
        violations
        if violations is not None
        else [
            {"path": "pyproject.toml", "section": "tool.coverage", "line": 5, "reason": "weakened"}
        ]
    )
    # A pre-PR blocked workspace has no PR yet.
    workspace.pr_url = None
    await session.flush()
    return workspace


async def _grants(session: AsyncSession, workspace_id: str) -> list[OperatorGrantAuditRecord]:
    rows = await session.execute(
        select(OperatorGrantAuditRecord).where(
            OperatorGrantAuditRecord.workspace_id == workspace_id
        )
    )
    return list(rows.scalars().all())


@pytest.mark.unit
async def test_guide_blocked_eligible_without_pr_url_arms_directive(
    session: AsyncSession,
) -> None:
    workspace = await _blocked_workspace(session)
    service, _stopper, _cleaner = _service(session)

    response = await service.guide_workspace(
        workspace.id,
        directive="revert the pyproject change and add a real test",
        reason="operator asked for a revert",
        idempotency_key="guide-blocked-directive",
        expected_version=workspace.version,
    )

    # No PR url required; status stays blocked (worker resume does the transition).
    assert response.status == WorkspaceStatus.blocked
    assert workspace.status == WorkspaceStatus.blocked.value
    hint = pre_pr_operator_hint_from_payload(workspace.pending_operator_hint)
    assert hint is not None
    assert hint.directive == "revert the pyproject change and add a real test"
    assert hint.status == "pending"


@pytest.mark.unit
async def test_guide_blocked_persists_epoch_scoped_grant(session: AsyncSession) -> None:
    workspace = await _blocked_workspace(session, block_epoch=3)
    service, _stopper, _cleaner = _service(session)

    await service.guide_workspace(
        workspace.id,
        directive="",
        reason="approved benign config split",
        grants=["pyproject.toml"],
        approve_policy_downgrade=True,
        operator="alice@example.com",
        idempotency_key="guide-blocked-grant",
        expected_version=workspace.version,
    )

    grants = await _grants(session, workspace.id)
    assert len(grants) == 1
    grant = grants[0]
    assert grant.normalized_path == "pyproject.toml"
    assert grant.block_epoch == 3  # scoped to the current block instance
    assert grant.approve_policy_downgrade is True
    assert grant.operator == "alice@example.com"
    assert grant.reason == "approved benign config split"
    assert grant.consumed_at is None


@pytest.mark.unit
async def test_guide_blocked_grants_only_clears_stale_directive(session: AsyncSession) -> None:
    # A prior directive guide armed a revert directive on the blocked workspace.
    workspace = await _blocked_workspace(session, block_epoch=3)
    workspace.pending_operator_hint = {
        "reason": "revert it",
        "directive": "revert the pyproject change",
        "status": "pending",
        "reason_code": "OPERATOR_GUIDE",
    }
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    # The operator changes their mind and issues a grants-only approve-and-keep.
    await service.guide_workspace(
        workspace.id,
        directive="",
        reason="actually keep it",
        grants=["pyproject.toml"],
        approve_policy_downgrade=True,
        operator="alice@example.com",
        idempotency_key="guide-blocked-grant-clears-hint",
        expected_version=workspace.version,
    )

    # The stale directive must be cleared so the resume path honors the grant
    # instead of re-applying the old revert directive (which it prioritizes).
    assert pre_pr_operator_hint_from_payload(workspace.pending_operator_hint) is None
    grants = await _grants(session, workspace.id)
    assert len(grants) == 1
    assert grants[0].normalized_path == "pyproject.toml"


@pytest.mark.unit
async def test_guide_blocked_directive_only_revokes_stale_grant(session: AsyncSession) -> None:
    # A prior approve-and-keep guide recorded an active current-epoch grant.
    workspace = await _blocked_workspace(session, block_epoch=3)
    session.add(
        OperatorGrantAuditRecord(
            id=new_operator_grant_id(),
            workspace_id=workspace.id,
            operator="alice@example.com",
            reason="approved keeping it",
            normalized_path="pyproject.toml",
            block_epoch=3,
            approve_policy_downgrade=True,
            created_at=utcnow(),
        )
    )
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    # The operator changes their mind and issues a directive-only revert.
    await service.guide_workspace(
        workspace.id,
        directive="revert the pyproject change after all",
        reason="actually revert it",
        operator="alice@example.com",
        idempotency_key="guide-blocked-directive-revokes-grant",
        expected_version=workspace.version,
    )

    # The directive is armed, and the stale grant must be revoked so the resume
    # path's protected-file gates no longer honor it (mirror of the grants-only
    # branch clearing a stale directive): the latest operator decision wins.
    hint = pre_pr_operator_hint_from_payload(workspace.pending_operator_hint)
    assert hint is not None
    assert hint.directive == "revert the pyproject change after all"
    grants = await _grants(session, workspace.id)
    assert len(grants) == 1
    assert grants[0].revoked_at is not None


@pytest.mark.unit
async def test_guide_blocked_directive_with_new_grant_keeps_fresh_grant(
    session: AsyncSession,
) -> None:
    # A combined guide (grant + directive) must NOT revoke the grant it just
    # recorded in the same call — only a directive-ONLY decision supersedes
    # prior grants.
    workspace = await _blocked_workspace(
        session,
        block_epoch=2,
        violations=[{"path": "pyproject.toml", "section": "x", "line": 1, "reason": "weakened"}],
    )
    service, _stopper, _cleaner = _service(session)

    await service.guide_workspace(
        workspace.id,
        directive="also fix the unrelated module",
        reason="keep this config and fix the other file",
        grants=["docs/CONTRIBUTING.md"],
        idempotency_key="guide-blocked-directive-plus-grant",
        expected_version=workspace.version,
    )

    grants = await _grants(session, workspace.id)
    assert len(grants) == 1
    assert grants[0].normalized_path == "docs/CONTRIBUTING.md"
    assert grants[0].revoked_at is None


@pytest.mark.unit
async def test_guide_blocked_weakening_grant_requires_ack(session: AsyncSession) -> None:
    workspace = await _blocked_workspace(session)
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceGuidePolicyDowngradeRequiredError):
        await service.guide_workspace(
            workspace.id,
            directive="",
            reason="please keep it",
            grants=["pyproject.toml"],  # matches a recorded violation, no ack
            idempotency_key="guide-blocked-noack",
            expected_version=workspace.version,
        )
    assert await _grants(session, workspace.id) == []


@pytest.mark.unit
async def test_guide_blocked_benign_grant_does_not_require_ack(session: AsyncSession) -> None:
    # The granted path does not match any recorded violation, so no ack needed.
    workspace = await _blocked_workspace(
        session,
        violations=[{"path": "pyproject.toml", "section": "x", "line": 1, "reason": "weakened"}],
    )
    service, _stopper, _cleaner = _service(session)

    await service.guide_workspace(
        workspace.id,
        directive="",
        reason="grant an unrelated config file",
        grants=["docs/CONTRIBUTING.md"],
        idempotency_key="guide-blocked-benign",
        expected_version=workspace.version,
    )
    grants = await _grants(session, workspace.id)
    assert len(grants) == 1
    assert grants[0].approve_policy_downgrade is False


@pytest.mark.unit
async def test_guide_blocked_grant_requires_reason(session: AsyncSession) -> None:
    workspace = await _blocked_workspace(session)
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceGuideGrantReasonRequiredError):
        await service.guide_workspace(
            workspace.id,
            directive="",
            reason=None,
            grants=["pyproject.toml"],
            approve_policy_downgrade=True,
            idempotency_key="guide-blocked-noreason",
            expected_version=workspace.version,
        )


@pytest.mark.unit
@pytest.mark.parametrize("bad_path", ["../etc/passwd", "/abs/path", "a/../../b"])
async def test_guide_blocked_rejects_unsafe_grant_path(
    session: AsyncSession, bad_path: str
) -> None:
    workspace = await _blocked_workspace(session)
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceGuideInvalidGrantPathError):
        await service.guide_workspace(
            workspace.id,
            directive="",
            reason="trying traversal",
            grants=[bad_path],
            approve_policy_downgrade=True,
            idempotency_key=f"guide-blocked-bad-{bad_path}",
            expected_version=workspace.version,
        )


@pytest.mark.unit
async def test_guide_blocked_requires_directive_or_grant(session: AsyncSession) -> None:
    workspace = await _blocked_workspace(session)
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceGuideEmptyDirectiveError):
        await service.guide_workspace(
            workspace.id,
            directive="",
            reason="nothing to do",
            idempotency_key="guide-blocked-empty",
            expected_version=workspace.version,
        )


@pytest.mark.unit
async def test_guide_grants_rejected_on_monitoring_pr(session: AsyncSession) -> None:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/9"
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceGuideGrantNotAllowedError):
        await service.guide_workspace(
            workspace.id,
            directive="do the thing",
            reason="r",
            grants=["pyproject.toml"],
            approve_policy_downgrade=True,
            idempotency_key="guide-monitoring-grant",
            expected_version=workspace.version,
        )
