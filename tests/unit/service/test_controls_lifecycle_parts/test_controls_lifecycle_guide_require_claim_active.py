"""Guide ``require_claim_active`` compare-and-act precondition (issue #1277)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import OperationRepository
from awf.hosted_node import lifecycle_operations as hosted_lifecycle
from awf.hosted_node.runtime_schemas import HostedControlAuthorization, HostedWorkspace
from awf.runtime.operator_hints import OPERATOR_HINT_STATE_KEY, operator_hint_from_threads
from awf.service.controls import IdempotencyConflictError
from awf.service.controls_errors import WorkspaceGuideClaimNotActiveError
from tests.postgres import postgres_test_session
from tests.unit.service.test_controls_lifecycle_parts.controls_lifecycle_helpers import (
    _events,
    _operations,
    _service,
    _workspace,
)
from tests.unit.test_hosted_runtime_contract import hosted_create


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


async def _monitoring_workspace(session: AsyncSession) -> object:
    workspace = await _workspace(session, status=WorkspaceStatus.monitoring_pr)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/77"
    workspace.pr_number = 77
    workspace.base_commit = "b" * 40
    workspace.monitor_last_commit_sha = "h" * 40
    await session.flush()
    return workspace


async def _failed_with_pr(session: AsyncSession) -> object:
    workspace = await _workspace(session, status=WorkspaceStatus.failed)
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/89"
    workspace.pr_number = 89
    workspace.base_commit = "b" * 40
    workspace.monitor_last_commit_sha = "h" * 40
    await session.flush()
    return workspace


async def _blocked_workspace(session: AsyncSession) -> object:
    workspace = await _workspace(session, status=WorkspaceStatus.blocked)
    workspace.block_reason_code = "QUALITY_GATE_POLICY_CHANGED"
    workspace.block_type = "protected_quality_gate"
    workspace.block_epoch = 1
    workspace.block_violations = [
        {"path": "pyproject.toml", "section": "tool.coverage", "line": 5, "reason": "weakened"}
    ]
    workspace.pr_url = None
    await session.flush()
    return workspace


def _snapshot(workspace: object) -> dict[str, object]:
    return {
        "status": workspace.status,
        "version": workspace.version,
        "monitor_claimed_by": workspace.monitor_claimed_by,
        "monitor_claim_expires_at": workspace.monitor_claim_expires_at,
        "execution_claimed_by": workspace.execution_claimed_by,
        "execution_claim_expires_at": workspace.execution_claim_expires_at,
        "monitor_threads_addressed": dict(workspace.monitor_threads_addressed or {}),
        "pending_operator_hint": workspace.pending_operator_hint,
    }


def _attach_hosted_association(workspace: Workspace) -> HostedWorkspace:
    """Bind a real hosted_node association to ``workspace`` for control_authority."""
    raw = hosted_create()["hosted"]
    raw["workspace_id"] = workspace.id
    for binding in raw["credentials"].values():
        binding["workspace_id"] = workspace.id
    stored = HostedWorkspace.model_validate(raw)
    workspace.task_policy = {"hosted_node": stored.model_dump(mode="json")}
    workspace.node_id = stored.credentials.agent.identity.owner
    return stored


def _patch_hosted_authority(monkeypatch: pytest.MonkeyPatch, stored: HostedWorkspace) -> AsyncMock:
    """Keep ``control_authority`` real; stub only materials + material lock."""
    materials = AsyncMock()
    lock = AsyncMock()
    authority = SimpleNamespace(
        receiver=SimpleNamespace(identity=stored.credentials.agent.identity),
        materials=materials,
    )
    monkeypatch.setattr(hosted_lifecycle, "authority_for", lambda *_args, **_kwargs: authority)
    monkeypatch.setattr(hosted_lifecycle, "lock_materials", lock)
    return lock


@pytest.mark.unit
async def test_require_claim_active_accepts_live_execution_claim(
    session: AsyncSession,
) -> None:
    workspace = await _monitoring_workspace(session)
    future = datetime.now(UTC) + timedelta(minutes=10)
    workspace.execution_claimed_by = "execution-worker"
    workspace.execution_claim_expires_at = future
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.guide_workspace(
        workspace.id,
        directive="steer the live monitor",
        require_claim_active=True,
        idempotency_key="guide-rca-exec-live",
        expected_version=workspace.version,
    )

    monitor_state = dict(workspace.monitor_threads_addressed or {})
    persisted = json.loads(monitor_state[OPERATOR_HINT_STATE_KEY])
    assert response.status == WorkspaceStatus.monitoring_pr
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert workspace.execution_claimed_by == "execution-worker"
    assert workspace.execution_claim_expires_at == future
    assert persisted["directive"] == "steer the live monitor"
    assert persisted["status"] == "pending"
    restored = operator_hint_from_threads(monitor_state)
    assert restored is not None
    assert restored.directive == "steer the live monitor"


@pytest.mark.unit
async def test_require_claim_active_accepts_live_monitor_claim(
    session: AsyncSession,
) -> None:
    workspace = await _monitoring_workspace(session)
    future = datetime.now(UTC) + timedelta(minutes=10)
    workspace.monitor_claimed_by = "monitor-worker"
    workspace.monitor_claim_expires_at = future
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    response = await service.guide_workspace(
        workspace.id,
        directive="steer via monitor claim",
        require_claim_active=True,
        idempotency_key="guide-rca-monitor-live",
        expected_version=workspace.version,
    )

    monitor_state = dict(workspace.monitor_threads_addressed or {})
    persisted = json.loads(monitor_state[OPERATOR_HINT_STATE_KEY])
    assert response.status == WorkspaceStatus.monitoring_pr
    assert workspace.monitor_claimed_by == "monitor-worker"
    assert workspace.monitor_claim_expires_at == future
    assert persisted["directive"] == "steer via monitor claim"
    assert persisted["status"] == "pending"


@pytest.mark.unit
@pytest.mark.parametrize(
    "claim_setup",
    [
        "expired",
        "null_expiry",
        "none",
    ],
)
async def test_require_claim_active_refuses_inactive_claims(
    session: AsyncSession,
    claim_setup: str,
) -> None:
    workspace = await _monitoring_workspace(session)
    if claim_setup == "expired":
        past = datetime.now(UTC) - timedelta(minutes=1)
        workspace.monitor_claimed_by = "monitor-worker"
        workspace.monitor_claim_expires_at = past
    elif claim_setup == "null_expiry":
        workspace.monitor_claimed_by = "monitor-worker"
        workspace.monitor_claim_expires_at = None
    await session.flush()
    before = _snapshot(workspace)
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceGuideClaimNotActiveError) as exc_info:
        await service.guide_workspace(
            workspace.id,
            directive="should refuse",
            require_claim_active=True,
            idempotency_key=f"guide-rca-{claim_setup}",
            expected_version=workspace.version,
        )

    assert exc_info.value.error_code == "WORKSPACE_GUIDE_CLAIM_NOT_ACTIVE"
    assert exc_info.value.detail == {
        "status": WorkspaceStatus.monitoring_pr.value,
        "claim_active": False,
    }
    assert _snapshot(workspace) == before
    assert await _operations(session, workspace.id) == []


@pytest.mark.unit
async def test_require_claim_active_refuses_failed_with_pr(
    session: AsyncSession,
) -> None:
    workspace = await _failed_with_pr(session)
    future = datetime.now(UTC) + timedelta(minutes=10)
    workspace.monitor_claimed_by = "dead-worker"
    workspace.monitor_claim_expires_at = future
    await session.flush()
    before = _snapshot(workspace)
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceGuideClaimNotActiveError) as exc_info:
        await service.guide_workspace(
            workspace.id,
            directive="do not remonitor",
            require_claim_active=True,
            idempotency_key="guide-rca-failed",
            expected_version=workspace.version,
        )

    assert exc_info.value.error_code == "WORKSPACE_GUIDE_CLAIM_NOT_ACTIVE"
    assert exc_info.value.detail == {
        "status": WorkspaceStatus.failed.value,
        "claim_active": True,
    }
    assert workspace.status == WorkspaceStatus.failed.value
    assert _snapshot(workspace) == before
    assert await _operations(session, workspace.id) == []


@pytest.mark.unit
async def test_require_claim_active_refuses_blocked(
    session: AsyncSession,
) -> None:
    workspace = await _blocked_workspace(session)
    before = _snapshot(workspace)
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceGuideClaimNotActiveError) as exc_info:
        await service.guide_workspace(
            workspace.id,
            directive="do not arm",
            require_claim_active=True,
            idempotency_key="guide-rca-blocked",
            expected_version=workspace.version,
        )

    assert exc_info.value.error_code == "WORKSPACE_GUIDE_CLAIM_NOT_ACTIVE"
    assert exc_info.value.detail == {
        "status": WorkspaceStatus.blocked.value,
        "claim_active": False,
    }
    assert workspace.status == WorkspaceStatus.blocked.value
    assert workspace.pending_operator_hint is None
    assert _snapshot(workspace) == before
    assert await _operations(session, workspace.id) == []


@pytest.mark.unit
async def test_require_claim_active_refuses_grants(
    session: AsyncSession,
) -> None:
    workspace = await _monitoring_workspace(session)
    future = datetime.now(UTC) + timedelta(minutes=10)
    workspace.monitor_claimed_by = "monitor-worker"
    workspace.monitor_claim_expires_at = future
    await session.flush()
    before = _snapshot(workspace)
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceGuideClaimNotActiveError) as exc_info:
        await service.guide_workspace(
            workspace.id,
            directive="steer",
            grants=["pyproject.toml"],
            reason="should refuse grants under rca",
            require_claim_active=True,
            idempotency_key="guide-rca-grants",
            expected_version=workspace.version,
        )

    assert exc_info.value.error_code == "WORKSPACE_GUIDE_CLAIM_NOT_ACTIVE"
    assert exc_info.value.detail == {
        "status": WorkspaceStatus.monitoring_pr.value,
        "claim_active": True,
    }
    assert _snapshot(workspace) == before
    assert await _operations(session, workspace.id) == []


@pytest.mark.unit
async def test_require_claim_active_refreshes_stale_identity_map_under_lock(
    session: AsyncSession,
) -> None:
    """Stale ORM claim fields must not defeat the locked compare-and-act check.

    ``workspace_for_payload`` and ``_prepare_operation`` load the row before the
    ``require_claim_active`` re-read. ``get_for_update`` acquires FOR UPDATE but
    does not ``populate_existing``, so a concurrent claim release committed into
    the same session (identity map still showing a live claim) would otherwise
    be accepted. Refresh under the lock before evaluating status/claim.
    """
    workspace = await _monitoring_workspace(session)
    future = datetime.now(UTC) + timedelta(minutes=10)
    workspace.monitor_claimed_by = "monitor-worker"
    workspace.monitor_claim_expires_at = future
    await session.flush()
    # Simulate another transaction releasing the claim without refreshing the
    # identity-mapped instance already resident in this session.
    await session.execute(
        update(Workspace)
        .where(Workspace.id == workspace.id)
        .values(monitor_claimed_by=None, monitor_claim_expires_at=None)
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    assert workspace.monitor_claimed_by == "monitor-worker"
    assert workspace.monitor_claim_expires_at == future
    before = _snapshot(workspace)
    service, _stopper, _cleaner = _service(session)

    with pytest.raises(WorkspaceGuideClaimNotActiveError) as exc_info:
        await service.guide_workspace(
            workspace.id,
            directive="should refuse stale claim",
            require_claim_active=True,
            idempotency_key="guide-rca-stale-identity",
            expected_version=workspace.version,
        )

    assert exc_info.value.error_code == "WORKSPACE_GUIDE_CLAIM_NOT_ACTIVE"
    assert exc_info.value.detail == {
        "status": WorkspaceStatus.monitoring_pr.value,
        "claim_active": False,
    }
    assert await _operations(session, workspace.id) == []
    # Refusal must not mutate the row; re-read under populate_existing to assert
    # the DB still has the released claim and no guide side effects.
    refreshed = await session.get(Workspace, workspace.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.monitor_claimed_by is None
    assert refreshed.monitor_claim_expires_at is None
    assert refreshed.status == before["status"]
    assert refreshed.version == before["version"]


@pytest.mark.unit
async def test_require_claim_active_legacy_payload_replays_default_false(
    session: AsyncSession,
) -> None:
    """Pre-upgrade guide ops omit ``require_claim_active``; default retry must replay.

    PRD §8.5: same idempotency key + unchanged request returns the original
    result. Legacy payloads lack the new flag; absence must equal the default
    False so an omitted/False retry still replays, while an explicit True retry
    still conflicts as a different request.
    """
    workspace = await _monitoring_workspace(session)
    legacy = await OperationRepository(session).create(
        workspace_id=workspace.id,
        operation_type=OperationType.guide,
        status=OperationStatus.succeeded,
        idempotency_key="guide-rca-legacy-replay",
        payload={
            "owner": "operator_api",
            "source": "operator_api",
            "reason": None,
            "reason_code": "OPERATOR_GUIDE",
            "requested_action": OperationType.guide.value,
            "directive": "legacy steer",
            "grants": [],
            "approve_policy_downgrade": False,
            # Pre-upgrade shape: require_claim_active absent (not False).
            "expected_version": workspace.version,
        },
    )
    legacy.result = {"status": workspace.status}
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    replay = await service.guide_workspace(
        workspace.id,
        directive="legacy steer",
        idempotency_key="guide-rca-legacy-replay",
        expected_version=workspace.version,
    )
    assert replay.operation_id == legacy.id

    with pytest.raises(IdempotencyConflictError):
        await service.guide_workspace(
            workspace.id,
            directive="legacy steer",
            require_claim_active=True,
            idempotency_key="guide-rca-legacy-replay",
            expected_version=workspace.version,
        )


@pytest.mark.unit
async def test_require_claim_active_exact_replay_after_claim_ends(
    session: AsyncSession,
) -> None:
    workspace = await _monitoring_workspace(session)
    future = datetime.now(UTC) + timedelta(minutes=10)
    workspace.monitor_claimed_by = "monitor-worker"
    workspace.monitor_claim_expires_at = future
    await session.flush()
    service, _stopper, _cleaner = _service(session)

    first = await service.guide_workspace(
        workspace.id,
        directive="steer once",
        require_claim_active=True,
        idempotency_key="guide-rca-replay",
        expected_version=workspace.version,
    )
    # Claim ends after acceptance; exact replay must still return the stored success.
    workspace.monitor_claimed_by = None
    workspace.monitor_claim_expires_at = None
    await session.flush()

    replay = await service.guide_workspace(
        workspace.id,
        directive="steer once",
        require_claim_active=True,
        idempotency_key="guide-rca-replay",
        expected_version=workspace.version - 1,
    )
    operations = await _operations(session, workspace.id)

    assert first.operation_id == replay.operation_id
    assert len(operations) == 1
    assert operations[0].status == "succeeded"


@pytest.mark.unit
async def test_require_claim_active_false_monitoring_pr_unchanged(
    session: AsyncSession,
) -> None:
    workspace = await _monitoring_workspace(session)
    service, _stopper, _cleaner = _service(session)

    response = await service.guide_workspace(
        workspace.id,
        directive="ordinary guide",
        require_claim_active=False,
        idempotency_key="guide-rca-false-monitoring",
        expected_version=workspace.version,
    )
    monitor_state = dict(workspace.monitor_threads_addressed or {})
    persisted = json.loads(monitor_state[OPERATOR_HINT_STATE_KEY])

    assert response.status == WorkspaceStatus.monitoring_pr
    assert persisted["directive"] == "ordinary guide"
    assert persisted["status"] == "pending"


@pytest.mark.unit
async def test_require_claim_active_absent_failed_resets(
    session: AsyncSession,
) -> None:
    workspace = await _failed_with_pr(session)
    service, _stopper, _cleaner = _service(session)

    response = await service.guide_workspace(
        workspace.id,
        directive="reset after failure",
        idempotency_key="guide-rca-absent-failed",
        expected_version=workspace.version,
    )

    assert response.status == WorkspaceStatus.monitoring_pr
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    events = await _events(session, workspace.id)
    guide_event = next(e for e in events if e.event_type == "workspace.guide_requested")
    assert guide_event.payload["state_reset"]["from"] == WorkspaceStatus.failed.value
    assert guide_event.payload["state_reset"]["to"] == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_require_claim_active_false_blocked_arms(
    session: AsyncSession,
) -> None:
    workspace = await _blocked_workspace(session)
    service, _stopper, _cleaner = _service(session)

    response = await service.guide_workspace(
        workspace.id,
        directive="arm blocked resume",
        require_claim_active=False,
        idempotency_key="guide-rca-false-blocked",
        expected_version=workspace.version,
    )

    assert response.status == WorkspaceStatus.blocked
    assert workspace.status == WorkspaceStatus.blocked.value
    assert workspace.pending_operator_hint is not None
    assert workspace.pending_operator_hint["directive"] == "arm blocked resume"


@pytest.mark.unit
async def test_require_claim_active_hosted_accepts_live_claim(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = await _monitoring_workspace(session)
    stored = _attach_hosted_association(workspace)
    future = datetime.now(UTC) + timedelta(minutes=10)
    workspace.monitor_claimed_by = "monitor-worker"
    workspace.monitor_claim_expires_at = future
    await session.flush()
    service, _stopper, _cleaner = _service(session)
    lock = _patch_hosted_authority(monkeypatch, stored)

    response = await service.guide_workspace(
        workspace.id,
        directive="hosted steer",
        require_claim_active=True,
        hosted_authorization=HostedControlAuthorization(
            namespace="tenant",
            actor="caller",
        ),
        idempotency_key="guide-rca-hosted-ok",
        expected_version=workspace.version,
    )

    monitor_state = dict(workspace.monitor_threads_addressed or {})
    persisted = json.loads(monitor_state[OPERATOR_HINT_STATE_KEY])
    assert response.status == WorkspaceStatus.monitoring_pr
    assert persisted["directive"] == "hosted steer"
    lock.assert_awaited_once()
    assert lock.await_args.args[2] == stored


@pytest.mark.unit
async def test_require_claim_active_hosted_refuses_without_claim(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = await _monitoring_workspace(session)
    stored = _attach_hosted_association(workspace)
    await session.flush()
    before = _snapshot(workspace)
    service, _stopper, _cleaner = _service(session)
    lock = _patch_hosted_authority(monkeypatch, stored)

    with pytest.raises(WorkspaceGuideClaimNotActiveError) as exc_info:
        await service.guide_workspace(
            workspace.id,
            directive="hosted refuse",
            require_claim_active=True,
            hosted_authorization=HostedControlAuthorization(
                namespace="tenant",
                actor="caller",
            ),
            idempotency_key="guide-rca-hosted-refuse",
            expected_version=workspace.version,
        )

    assert exc_info.value.error_code == "WORKSPACE_GUIDE_CLAIM_NOT_ACTIVE"
    assert _snapshot(workspace) == before
    assert await _operations(session, workspace.id) == []
    # Hosted authorization + locked refresh still ran before the claim refusal.
    lock.assert_awaited_once()
    assert lock.await_args.args[2] == stored
