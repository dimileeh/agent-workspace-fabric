"""Focused branch-coverage tests for control worker claim helpers."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import ControlWorker, WorkerConfig
from awf.control.worker.constants import (
    _STALE_ACTIVE_EXECUTION_EVENT_TYPE,
    _STALE_ACTIVE_EXECUTION_REASON_CODE,
)
from awf.control.worker.helpers import (
    _has_running_agent_runtime,
    _json_datetime,
    _monitor_claim_is_stale,
    _stale_active_execution_failure_message,
    _utc_datetime,
)
from awf.control.worker.types import _ActiveExecutionCandidate
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from tests.postgres import postgres_test_engine
from tests.unit.control.test_worker_coverage_edges_parts.test_worker_coverage_edges_part_001 import (
    _NoopProvisioner,
    _RefreshLoopWorker,
    _seed_status,
    _worker,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_stale_active_execution_can_fail_normalizes_latest_preserved_floor(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await _seed_status(
        factory, WorkspaceStatus.running, title="normalizes-preserved-floor"
    )
    now = datetime.now(UTC)
    status_started_at = now - timedelta(minutes=10)
    claim_expires_at = now - timedelta(minutes=5)
    latest_preserved_at = (now - timedelta(minutes=4)).replace(tzinfo=None)
    stale_at = now - timedelta(minutes=2)

    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        assert ws is not None
        ws.execution_claimed_by = "stale-worker"
        ws.execution_claim_expires_at = claim_expires_at
        state_events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.state_changed",
        )
        running_started = next(
            event for event in state_events if event.new_state == WorkspaceStatus.running.value
        )
        running_started.occurred_at = status_started_at
        stale = await repo.add_event(
            ws,
            event_type=_STALE_ACTIVE_EXECUTION_EVENT_TYPE,
            reason_code=_STALE_ACTIVE_EXECUTION_REASON_CODE,
            payload={"workspace_status": WorkspaceStatus.running.value},
        )
        stale.occurred_at = stale_at
        await session.commit()

    worker = ControlWorker(
        session_factory=factory,
        provisioner=_NoopProvisioner(),  # type: ignore[arg-type]
        config=WorkerConfig(active_execution_preservation_grace_seconds=0.0),
    )
    observed_floors: list[datetime] = []

    async def latest_preserved(
        session: AsyncSession,
        workspace_id: str,
        status: WorkspaceStatus,
        *,
        event_floor: datetime | None = None,
        match_active_execution_statuses: bool = False,
    ) -> datetime:
        del session, workspace_id, status, event_floor, match_active_execution_statuses
        return latest_preserved_at

    async def has_current_salvage_event(
        session: AsyncSession,
        workspace_id: str,
        *,
        event_type: str,
        reason_code: str,
        event_floor: datetime,
        workspace_status: WorkspaceStatus,
    ) -> bool:
        del session, workspace_id, event_type, reason_code, workspace_status
        observed_floors.append(event_floor)
        return event_floor == _utc_datetime(latest_preserved_at)

    monkeypatch.setattr(worker, "_latest_preserved_active_execution_at", latest_preserved)
    monkeypatch.setattr(worker, "_has_current_salvage_event", has_current_salvage_event)

    assert not await worker._stale_active_execution_can_fail(  # noqa: SLF001
        _ActiveExecutionCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.running,
            compose_project_name=f"awf_{workspace_id}",
        )
    )
    assert observed_floors == [_utc_datetime(latest_preserved_at)]


@pytest.mark.unit
def test_monitor_claim_staleness_and_json_datetime_handle_naive_datetimes() -> None:
    cutoff = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)

    assert _monitor_claim_is_stale(
        SimpleNamespace(
            monitor_claimed_by="worker",
            monitor_claim_expires_at=datetime(2026, 4, 27, 11, 59),
        ),
        cutoff,
    )
    assert _json_datetime(datetime(2026, 4, 27, 12, 1)) == "2026-04-27T12:01:00+00:00"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("loop_name", "raises", "expected_event"),
    [
        ("monitor", True, "worker.monitor_claim_refresh_failed"),
        ("monitor", False, "worker.monitor_claim_lost"),
        ("execution", True, "worker.execution_claim_refresh_failed"),
        ("execution", False, "worker.execution_claim_lost"),
    ],
)
async def test_claim_refresh_loops_stop_after_refresh_failure_or_lost_claim(
    loop_name: str,
    raises: bool,
    expected_event: str,
) -> None:
    worker = _RefreshLoopWorker(raises=raises, refreshed=False)
    loop = (
        worker._refresh_monitoring_pr_claim_loop
        if loop_name == "monitor"
        else worker._refresh_execution_claim_loop
    )

    with structlog.testing.capture_logs() as captured:
        await asyncio.wait_for(loop("ws_loop"), timeout=2)

    assert any(event.get("event") == expected_event for event in captured)
    if loop_name == "monitor":
        assert worker.monitor_refresh_calls == 1
        assert worker.execution_refresh_calls == 0
    else:
        assert worker.execution_refresh_calls == 1
        assert worker.monitor_refresh_calls == 0


@pytest.mark.unit
@pytest.mark.parametrize("loop_name", ["monitor", "execution"])
async def test_claim_refresh_loops_continue_after_successful_refresh(loop_name: str) -> None:
    worker = _RefreshLoopWorker(raises=False, refreshed=True)
    loop = (
        worker._refresh_monitoring_pr_claim_loop
        if loop_name == "monitor"
        else worker._refresh_execution_claim_loop
    )

    task = asyncio.create_task(loop("ws_loop"))
    await asyncio.wait_for(worker.refreshed_once.wait(), timeout=2)
    await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    if loop_name == "monitor":
        assert worker.monitor_refresh_calls == 1
        assert worker.execution_refresh_calls == 0
    else:
        assert worker.execution_refresh_calls == 1
        assert worker.monitor_refresh_calls == 0


@pytest.mark.unit
def test_stale_active_execution_failure_message_includes_runtime_reason() -> None:
    message = _stale_active_execution_failure_message(
        _ActiveExecutionCandidate(
            workspace_id="ws_runtime",
            status=WorkspaceStatus.running,
            compose_project_name="awf_ws_runtime",
        ),
        RuntimeSnapshot(stack_state="unavailable", reason=" docker unavailable \n", services=[]),
    )

    assert "compose runtime state is unavailable: docker unavailable" in message
    no_reason_message = _stale_active_execution_failure_message(
        _ActiveExecutionCandidate(
            workspace_id="ws_runtime",
            status=WorkspaceStatus.validating,
            compose_project_name="awf_ws_runtime",
        ),
        RuntimeSnapshot(stack_state="stopped", services=[]),
    )
    assert "compose runtime state is stopped." in no_reason_message


@pytest.mark.unit
def test_runtime_snapshot_requires_running_stack_before_agent_detection() -> None:
    assert not _has_running_agent_runtime(
        RuntimeSnapshot(
            stack_state="stopped",
            services=[
                RuntimeService(
                    name="agent",
                    container_id="agent-1",
                    image="awf-agent-runtime",
                    state="running",
                )
            ],
        )
    )


@pytest.mark.unit
def test_worker_utc_datetime_normalizes_naive_values() -> None:
    assert _utc_datetime(datetime(2026, 4, 27, 12, 0)) == datetime(
        2026,
        4,
        27,
        12,
        0,
        tzinfo=UTC,
    )


@pytest.mark.unit
async def test_wait_for_execution_tasks_removes_completed_tasks(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)
    worker._execution_tasks["ws_done"] = asyncio.create_task(asyncio.sleep(0))  # noqa: SLF001

    await worker.wait_for_execution_tasks()

    assert worker._execution_tasks == {}  # noqa: SLF001


@pytest.mark.unit
async def test_execution_and_monitor_claim_helpers_skip_already_running_task(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)
    task = asyncio.create_task(asyncio.sleep(30))
    worker._execution_tasks["ws_busy"] = task  # noqa: SLF001

    try:
        assert worker._dispatchable_execution_ids(  # noqa: SLF001
            ["ws_busy", "ws_next"],
            limit=2,
        ) == ["ws_next"]
        assert await worker._claim_monitoring_pr_ids(["ws_busy"], limit=1) == []  # noqa: SLF001
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.unit
async def test_dispatchable_execution_ids_stops_after_limit(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)

    assert worker._dispatchable_execution_ids(["ws_one", "ws_two"], limit=1) == ["ws_one"]  # noqa: SLF001
