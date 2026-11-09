"""Work-slot visibility in capacity metrics and ``awf service status`` (slot-release, §6.2 F).

The execution concurrency lane counts broker-owned work-slot leases: waiting PR
monitors and blocked/recovering workspaces hold none, so they are not in use;
queued invocations join ready rows in ``queued``. The status ``work_slots`` check
is informational and always ``ok``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.config import Settings
from awf.common.work_slot import MONITOR_WAITING_FOR_WORK_SLOT_REASON_CODE
from awf.db.enums import WorkSlotLeasePhase, WorkspaceStatus
from awf.db.session import make_engine, make_session_factory
from awf.service import status_work_slots
from awf.service.config import ServiceSettings
from awf.service.disk import DiskCheck
from awf.service.metrics import summarize_resource_saturation
from awf.service.metrics_capacity import _work_slot_summary
from awf.service.metrics_types import WorkSlotSummary
from awf.service.status_work_slots import (
    WORK_SLOTS_OK_REASON,
    WORK_SLOTS_UNAVAILABLE_REASON,
    _default_work_slot_summary,
    collect_work_slot_status,
)
from tests.postgres import postgres_test_engine, postgres_test_url
from tests.unit.control._worker_work_slot_support import add_lease, create_in_status

pytestmark = pytest.mark.unit

_HOLDER = "control-worker-visibility"


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _service_settings(tmp_path: Path, database_url: str) -> ServiceSettings:
    return ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url=database_url,
        docker_host=f"unix://{tmp_path / 'docker.sock'}",
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work"),
        api_token=None,
        github_token=None,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        worker_max_concurrent_executions=2,
    )


async def _seed_leases(factory: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    running = await create_in_status(factory, WorkspaceStatus.running, "running")
    adopted = await create_in_status(factory, WorkspaceStatus.running, "adopted")
    waiter = await create_in_status(factory, WorkspaceStatus.monitoring_pr, "waiter")
    huge = await create_in_status(factory, WorkspaceStatus.monitoring_pr, "huge")
    stale = await create_in_status(factory, WorkspaceStatus.monitoring_pr, "stale")
    await add_lease(factory, running, holder=_HOLDER, cleanup_unproven=True)
    await add_lease(factory, adopted, holder=_HOLDER, phase=WorkSlotLeasePhase.adopted)
    await add_lease(
        factory, waiter, holder=_HOLDER, phase=WorkSlotLeasePhase.waiting, ticket_offset=-30
    )
    await add_lease(factory, huge, holder=_HOLDER, phase=WorkSlotLeasePhase.unsatisfiable)
    # An expired ticket (its invocation stopped re-checking) is not a waiter.
    await add_lease(
        factory,
        stale,
        holder=_HOLDER,
        phase=WorkSlotLeasePhase.waiting,
        expires_in=-1,
        ticket_offset=-120,
    )
    return {"waiter": waiter}


async def test_work_slot_summary_counts_every_lease_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_leases(session_factory)
    now = datetime.now(UTC)

    async with session_factory() as session:
        summary = await _work_slot_summary(session, node_id="local", limit=3, now=now)

    assert summary.limit == 3
    assert summary.in_use == 2
    assert summary.available == 1
    assert summary.waiting == 1
    assert summary.unsatisfiable == 1
    assert summary.adopted == 1
    assert summary.cleanup_unproven == 1
    assert summary.oldest_wait_seconds is not None
    assert 25 <= summary.oldest_wait_seconds <= 120
    assert summary.reason_code == MONITOR_WAITING_FOR_WORK_SLOT_REASON_CODE

    async with session_factory() as session:
        empty = await _work_slot_summary(session, node_id="other-node", limit=3, now=now)
    # Legacy null-node rows count against every node, like the old admission scope.
    assert empty.in_use == 2


async def test_execution_lane_counts_leases_not_waiting_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Idle monitors and paused workspaces without a lease are not execution in_use.
    for status in (
        WorkspaceStatus.monitoring_pr,
        WorkspaceStatus.blocked,
        WorkspaceStatus.ready,
    ):
        await create_in_status(session_factory, status, f"idle-{status.value}")
    await _seed_leases(session_factory)
    settings = Settings(
        _env_file=None, work_dir="/tmp/awf-work", worker_max_concurrent_executions=4
    )

    summary = await summarize_resource_saturation(
        session_factory,
        settings=settings,
        disk_check=DiskCheck(
            path="/tmp/awf-work",
            checked_path="/tmp",
            total_bytes=100,
            used_bytes=10,
            free_bytes=90,
            percent_free=90.0,
            threshold_bytes=1,
            ok=True,
            status="ok",
            reason="SUFFICIENT_DISK",
        ),
        now=datetime.now(UTC),
    )

    assert summary.work_slots.in_use == 2
    assert summary.concurrency.execution.in_use == 2
    # One ready row plus the waiting and unsatisfiable invocations.
    assert summary.concurrency.execution.queued == 3
    assert summary.concurrency.execution.available == 2


async def test_status_check_reports_waiters_and_stays_ok(tmp_path: Path) -> None:
    summary = WorkSlotSummary(
        limit=2,
        in_use=2,
        available=0,
        waiting=3,
        unsatisfiable=1,
        adopted=1,
        cleanup_unproven=0,
        oldest_wait_seconds=42.0,
        reason_code=MONITOR_WAITING_FOR_WORK_SLOT_REASON_CODE,
    )

    async def _lookup(_settings: ServiceSettings) -> WorkSlotSummary:
        return summary

    check = await collect_work_slot_status(
        _service_settings(tmp_path, "postgresql+asyncpg://unused"), summary_lookup=_lookup
    )

    assert check["ok"] is True
    assert check["status"] == "waiting"
    assert check["reason"] == MONITOR_WAITING_FOR_WORK_SLOT_REASON_CODE
    assert check["detail"] == "2/2 work slots in use; 3 waiting; 1 unsatisfiable"
    assert (check["in_use"], check["waiting"], check["unsatisfiable"]) == (2, 3, 1)
    assert check["oldest_wait_seconds"] == 42.0

    async def _idle(_settings: ServiceSettings) -> WorkSlotSummary:
        return replace(summary, in_use=0, waiting=0, unsatisfiable=0, reason_code=None)

    idle = await collect_work_slot_status(
        _service_settings(tmp_path, "postgresql+asyncpg://unused"), summary_lookup=_idle
    )
    assert (idle["ok"], idle["status"], idle["reason"]) == (True, "ok", WORK_SLOTS_OK_REASON)


async def test_status_check_degrades_but_never_fails_when_lookup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _down(_settings: ServiceSettings) -> WorkSlotSummary:
        raise OperationalError("SELECT", {}, Exception("password=hunter2 db down"))

    check = await collect_work_slot_status(
        _service_settings(tmp_path, "postgresql+asyncpg://unused"), summary_lookup=_down
    )

    assert check["ok"] is True
    assert check["status"] == "unavailable"
    assert check["reason"] == WORK_SLOTS_UNAVAILABLE_REASON
    assert "hunter2" not in str(check["detail"])

    async def _slow(_settings: ServiceSettings) -> WorkSlotSummary:
        await asyncio.sleep(60)
        raise AssertionError("unreachable")  # pragma: no cover

    monkeypatch.setattr(status_work_slots, "_CHECK_TIMEOUT_SECONDS", 0.01)
    slow = await collect_work_slot_status(
        _service_settings(tmp_path, "postgresql+asyncpg://unused"), summary_lookup=_slow
    )
    assert (slow["ok"], slow["reason"]) == (True, WORK_SLOTS_UNAVAILABLE_REASON)


async def test_default_status_lookup_reads_the_local_node_leases(tmp_path: Path) -> None:
    async with postgres_test_url() as database_url:
        engine = make_engine(database_url)
        try:
            factory = make_session_factory(engine)
            busy = await create_in_status(factory, WorkspaceStatus.running, "busy")
            await add_lease(factory, busy, holder=_HOLDER)
        finally:
            await engine.dispose()

        summary = await _default_work_slot_summary(_service_settings(tmp_path, database_url))

    assert (summary.limit, summary.in_use, summary.available) == (2, 1, 1)
    assert summary.reason_code is None
    assert summary.oldest_wait_seconds is None
