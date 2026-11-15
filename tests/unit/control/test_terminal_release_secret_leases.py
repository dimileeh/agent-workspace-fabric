"""A terminal workspace's secret leases are revoked when its runtime release is recorded."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from awf.control.worker import WorkerConfig, cleanup
from awf.control.worker.terminal_owned_rows import REASON, reap_terminal_owned_rows
from awf.control.worker.types import _TerminalRuntimeCandidate
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent, WorkspaceSecretLease
from awf.db.session import make_session_factory
from awf.hosted_node.lifecycle_store import quiescence_blockers
from awf.node.cleanup import WorkspaceCleanupResult
from awf.service.secret_leases import TERMINAL_RUNTIME_RELEASE_REVOKE_REASON
from tests.unit.hosted_node.test_quiescence_blockers import (
    lease,
    released_event,
    seed_workspace,
)

pytestmark = pytest.mark.unit

NODE = "hosted/node-a"
WS = "ws_term_release"
RELEASE_EVENT = "workspace.terminal_runtime_released"


@pytest.fixture
async def factory(engine):
    return make_session_factory(engine)


@pytest.fixture(autouse=True)
def _no_planning_resume(monkeypatch):
    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        cleanup, "_resume_blocked_planning_scope_auto_retry_after_runtime_release", _noop
    )


def release_worker(factory):
    worker = SimpleNamespace(_session_factory=factory, _log_transient_db_retry=lambda *_: None)
    worker._has_terminal_runtime_release_event = partial(
        cleanup._has_terminal_runtime_release_event, worker
    )
    return worker


async def record_release(factory, *, listed_status: str) -> None:
    candidate = _TerminalRuntimeCandidate(
        workspace_id=WS,
        status=WorkspaceStatus(listed_status),
        compose_project_name=f"awf_{WS}",
        compose_file_path=None,
        repo_url="https://example.invalid/repo.git",
    )
    await cleanup._record_terminal_runtime_released(
        release_worker(factory), candidate, WorkspaceCleanupResult.skipped()
    )


async def lease_rows(factory):
    async with factory() as session:
        return list(
            await session.scalars(
                select(WorkspaceSecretLease).where(WorkspaceSecretLease.workspace_id == WS)
            )
        )


async def events(factory, event_type):
    async with factory() as session:
        return list(
            await session.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == WS, WorkspaceEvent.event_type == event_type
                )
            )
        )


async def blocker_names(factory):
    async with factory() as session:
        return {blocker.name for blocker in await quiescence_blockers(session)}


@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_terminal_release_revokes_active_leases_and_clears_quiescence(factory, status):
    async with factory.begin() as session:
        await seed_workspace(session, WS, status=status, compose_project_name=f"awf_{WS}")
        for row in (
            lease(WS, status="issued"),
            lease(WS, status="mounted"),
            lease(WS, status="expired"),
        ):
            session.add(row)
            await session.flush()
    assert "SECRET_LEASE_ACTIVE" in await blocker_names(factory)

    await record_release(factory, listed_status=status)

    leases = await lease_rows(factory)
    assert len(leases) == 3
    for row in leases:
        assert row.status == "revoked"
        assert row.revoke_reason_code == TERMINAL_RUNTIME_RELEASE_REVOKE_REASON
        assert row.revoked_at is not None
    assert len(await events(factory, RELEASE_EVENT)) == 1
    audits = await events(factory, "workspace.secret_lease")
    assert len(audits) == 3
    assert {(e.payload["action"], e.payload["revoke_reason_code"]) for e in audits} == {
        ("revoke", "TERMINAL_RUNTIME_RELEASED")
    }
    assert "SECRET_LEASE_ACTIVE" not in await blocker_names(factory)


async def test_release_already_recorded_still_revokes_active_leases(factory):
    async with factory.begin() as session:
        await seed_workspace(session, WS, status="completed")
        session.add(released_event(WS))
        await session.flush()
        session.add(lease(WS, status="mounted"))

    await record_release(factory, listed_status="completed")

    [row] = await lease_rows(factory)
    assert (row.status, row.revoke_reason_code) == (
        "revoked",
        TERMINAL_RUNTIME_RELEASE_REVOKE_REASON,
    )
    assert len(await events(factory, RELEASE_EVENT)) == 1
    assert "SECRET_LEASE_ACTIVE" not in await blocker_names(factory)


async def test_resumed_workspace_keeps_its_leases(factory):
    # Listed as ``failed``, then guide/remonitor resumed it before the release lock.
    async with factory.begin() as session:
        await seed_workspace(session, WS, status="monitoring_pr")
        session.add(lease(WS, status="mounted"))

    await record_release(factory, listed_status="failed")

    [row] = await lease_rows(factory)
    assert (row.status, row.revoke_reason_code, row.revoked_at) == ("mounted", None, None)
    assert await events(factory, RELEASE_EVENT) == []
    assert await events(factory, "workspace.secret_lease") == []


async def test_reaper_still_retires_lease_left_active_by_crash(factory):
    # Terminal, but the release transaction never ran (crash), so the lease stays issued.
    now = datetime.now(UTC)
    async with factory.begin() as session:
        await seed_workspace(session, WS, status="failed", node_id=NODE)
        session.add(lease(WS, status="issued"))
        await session.flush()
        await session.execute(
            update(Workspace)
            .where(Workspace.id == WS)
            .values(updated_at=now - timedelta(minutes=11))
        )
    assert "SECRET_LEASE_ACTIVE" in await blocker_names(factory)

    worker = SimpleNamespace(
        _session_factory=factory, _config=WorkerConfig(node_id=NODE), _execution_tasks={}
    )
    await reap_terminal_owned_rows(worker, limit=200)

    [row] = await lease_rows(factory)
    assert (row.status, row.revoke_reason_code) == ("revoked", REASON)
    assert REASON != TERMINAL_RUNTIME_RELEASE_REVOKE_REASON
    assert "SECRET_LEASE_ACTIVE" not in await blocker_names(factory)
