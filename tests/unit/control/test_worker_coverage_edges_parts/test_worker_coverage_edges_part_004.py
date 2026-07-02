"""Focused branch-coverage tests for control worker dispatch helpers.

Split out of ``test_worker_coverage_edges_part_002`` to keep each test module
under the first-party 1500-line maintainability guardrail.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import claims as worker_claims
from awf.control.worker import dispatch_methods as worker_dispatch_methods
from awf.control.worker.constants import (
    _MONITOR_RECOVERY_OWNER,
    _MONITOR_RECOVERY_SOURCE,
)
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("reason_code", "expected"),
    [
        ("FORGE_NOT_SUPPORTED", True),
        ("BITBUCKET_AUTH_NOT_CONFIGURED", True),
        ("DEPRECATED_TASK_KIND", True),
        ("UNSUPPORTED_TASK_KIND", True),
        ("MONITOR_RECOVERY_COMPOSE_FAILED", True),
        ("MONITOR_RECOVERY_METADATA_MISSING", True),
        ("MONITOR_RECOVERY_FAILED", True),
        ("MONITOR_RECOVERY_STILL_RUNNING", False),
        ("OTHER_FAILURE", False),
        ("MONITOR_RECOVERY_CANCELLED", False),
    ],
)
def test_is_monitor_recovery_handoff_failure_reason(reason_code: str, expected: bool) -> None:
    """Verify is monitor recovery handoff failure reason."""
    assert (
        worker_dispatch_methods._is_monitor_recovery_handoff_failure_reason(reason_code)  # noqa: SLF001
        is expected
    )


@pytest.mark.unit
def test_monitor_recovery_handoff_failure_message_uses_workspace_failure_message() -> None:
    """Verify monitor recovery handoff failure message uses workspace failure message."""
    event = SimpleNamespace(payload={})
    workspace = SimpleNamespace(
        status=WorkspaceStatus.failed.value,
        failure_message="Monitor handoff aborted after validation failure.",
    )
    message = worker_dispatch_methods._monitor_recovery_handoff_failure_message(  # noqa: SLF001
        event,
        workspace=workspace,
        default_message="default",
    )
    assert message == "Monitor handoff aborted after validation failure."


@pytest.mark.unit
def test_monitor_recovery_handoff_failure_message_returns_default_when_payload_empty() -> None:
    """Verify monitor recovery handoff failure message returns default when payload empty."""
    event = SimpleNamespace(payload={})
    workspace = SimpleNamespace(status=WorkspaceStatus.monitoring_pr.value, failure_message=None)
    message = worker_dispatch_methods._monitor_recovery_handoff_failure_message(  # noqa: SLF001
        event,
        workspace=workspace,
        default_message="Monitor recovery handoff failed.",
    )
    assert message == "Monitor recovery handoff failed."


@pytest.mark.unit
async def test_cancel_monitor_claim_heartbeat_pops_matching_task_and_awaits_cancel() -> None:
    """Verify cancel monitor claim heartbeat pops matching task and awaits cancel."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _heartbeat() -> None:
        """Test helper for heartbeat."""
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    worker = SimpleNamespace(
        _monitor_claim_heartbeat_tasks={"ws_monitor": asyncio.create_task(_heartbeat())}
    )
    heartbeat = worker._monitor_claim_heartbeat_tasks["ws_monitor"]
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await worker_claims._cancel_monitor_claim_heartbeat(  # noqa: SLF001
        worker,
        "ws_monitor",
        heartbeat=heartbeat,
    )

    assert worker._monitor_claim_heartbeat_tasks == {}
    assert cancelled.is_set()
    assert heartbeat.cancelled()


@pytest.mark.unit
async def test_cancel_monitor_claim_heartbeat_noop_when_task_already_gone() -> None:
    """Verify cancel monitor claim heartbeat noop when task already gone."""
    worker = SimpleNamespace(_monitor_claim_heartbeat_tasks={})

    await worker_claims._cancel_monitor_claim_heartbeat(worker, "ws_missing")  # noqa: SLF001

    assert worker._monitor_claim_heartbeat_tasks == {}


@pytest.mark.unit
async def test_active_worker_restart_remonitor_operation_id_returns_running_operation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify active worker restart remonitor operation id returns running operation."""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@example.com:repo/app.git",
            branch_base="main",
            task_title="active worker restart remonitor lookup",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        operation = await OperationRepository(session).create(
            workspace_id=ws.id,
            operation_type=OperationType.remonitor,
            status=OperationStatus.running,
            payload={
                "source": _MONITOR_RECOVERY_SOURCE,
                "owner": _MONITOR_RECOVERY_OWNER,
            },
        )
        await session.commit()
        workspace_id = ws.id
        operation_id = operation.id

    async with factory() as session:
        found = await worker_claims._active_worker_restart_remonitor_operation_id(  # noqa: SLF001
            session,
            workspace_id,
        )

    assert found == operation_id


@pytest.mark.unit
async def test_active_worker_restart_remonitor_operation_id_prefers_newest_active_operation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """When multiple worker-restart remonitor ops are active, adopt the newest."""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@example.com:repo/app.git",
            branch_base="main",
            task_title="active worker restart remonitor newest",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        op_repo = OperationRepository(session)
        await op_repo.create(
            workspace_id=ws.id,
            operation_type=OperationType.remonitor,
            status=OperationStatus.running,
            payload={
                "source": _MONITOR_RECOVERY_SOURCE,
                "owner": _MONITOR_RECOVERY_OWNER,
            },
        )
        newer = await op_repo.create(
            workspace_id=ws.id,
            operation_type=OperationType.remonitor,
            status=OperationStatus.running,
            payload={
                "source": _MONITOR_RECOVERY_SOURCE,
                "owner": _MONITOR_RECOVERY_OWNER,
            },
        )
        await session.commit()
        workspace_id = ws.id
        newer_id = newer.id

    async with factory() as session:
        found = await worker_claims._active_worker_restart_remonitor_operation_id(  # noqa: SLF001
            session,
            workspace_id,
        )

    assert found == newer_id


@pytest.mark.unit
async def test_active_worker_restart_remonitor_operation_id_returns_none_for_fresh_previous_owner(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify active worker restart remonitor operation id returns none for fresh previous owner."""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@example.com:repo/app.git",
            branch_base="main",
            task_title="active worker restart remonitor fresh owner",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await OperationRepository(session).create(
            workspace_id=ws.id,
            operation_type=OperationType.remonitor,
            status=OperationStatus.running,
            payload={
                "source": _MONITOR_RECOVERY_SOURCE,
                "owner": _MONITOR_RECOVERY_OWNER,
            },
        )
        await session.commit()
        workspace_id = ws.id

    async with factory() as session:
        found = await worker_claims._active_worker_restart_remonitor_operation_id(  # noqa: SLF001
            session,
            workspace_id,
            previous_monitor_claimed_by="live-worker",
            fresh_worker_ids={"live-worker"},
        )

    assert found is None


@pytest.mark.unit
async def test_active_worker_restart_remonitor_operation_id_returns_none_when_absent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify active worker restart remonitor operation id returns none when absent."""
    async with factory() as session:
        assert (
            await worker_claims._active_worker_restart_remonitor_operation_id(  # noqa: SLF001
                session,
                "ws_missing",
            )
            is None
        )
