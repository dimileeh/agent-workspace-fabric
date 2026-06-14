"""``POST /v1/service/gc`` worker-delegation tests (#582).

The capability-less API container reclaims **zero** of the dominant disk
consumers — the per-workspace Claude auth overlays + ``_shared/claude-base`` —
yet historically reported success. These tests prove ``execute`` now delegates
that reclaim to the worker and folds the worker's actual reclamation into the
response, fast-fails when no worker is available, times out structurally, and
leaves dry-run untouched (no worker trigger).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.repositories import WorkerHeartbeatRepository
from awf.db.session import make_session_factory

pytestmark = pytest.mark.usefixtures("mock_docker_cli_probe")

_NODE_ID = "local"


async def _seed_fresh_heartbeat(engine: AsyncEngine) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        await WorkerHeartbeatRepository(session).record_heartbeat(
            worker_id="worker-1",
            node_id=_NODE_ID,
            started_at=datetime.now(UTC),
            last_heartbeat_at=datetime.now(UTC),
            poll_interval_seconds=1.0,
        )
        await session.commit()


async def _no_pending_requests(engine: AsyncEngine) -> bool:
    """Return True when no ``service_gc_requests`` row was ever written."""
    factory = make_session_factory(engine)
    async with factory() as session:
        # ``claim_oldest_pending`` for a foreign node never matches our rows, so a
        # plain count via get-by-claim is unsuitable; query the table directly.
        from sqlalchemy import func, select

        from awf.db.models import ServiceGCRequest

        count = (
            await session.execute(select(func.count()).select_from(ServiceGCRequest))
        ).scalar_one()
        return count == 0


@pytest.fixture
def _stub_api_side_reclaim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the API-side reclaim to a deterministic zero-auth ``succeeded`` result.

    This isolates the delegation assertions from real filesystem GC: the API-side
    pass reclaims worktree/compose only (here: nothing), and crucially records the
    auth/claude-base paths as ``deleted_path_count: 0`` — proving the worker's
    fold is additive, not a double-count.
    """
    from unittest.mock import AsyncMock

    class _Result:
        def to_dict(self) -> dict[str, object]:
            return {
                "dry_run": False,
                "status": "succeeded",
                "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
                "candidate_count": 1,
                "preserved_count": 0,
                "deleted_path_count": 0,
                "total_estimated_bytes": 0,
            }

    monkeypatch.setattr(
        "awf.api.routes.service.run_service_workspace_gc",
        AsyncMock(return_value=_Result()),
    )


async def _run_worker_consumer(
    engine: AsyncEngine,
    report: dict[str, object],
) -> AsyncIterator[asyncio.Task[None]]:
    """Background task that consumes any pending gc trigger with a stub reaper."""
    factory = make_session_factory(engine)

    async def _terminal_reaper() -> dict[str, object]:
        return report

    worker = ControlWorker(
        session_factory=factory,
        provisioner=object(),  # type: ignore[arg-type]
        terminal_gc_reaper=_terminal_reaper,  # type: ignore[arg-type]
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_provisions=0,
            node_id=_NODE_ID,
        ),
    )

    async def _loop() -> None:
        while True:
            await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001
            await asyncio.sleep(0.02)

    return asyncio.create_task(_loop())


@pytest.mark.unit
async def test_execute_folds_worker_reclaim_into_headline(
    client: AsyncClient,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _stub_api_side_reclaim: None,
) -> None:
    """RED before fix: execute delegates to the worker and folds the real reclaim.

    Pre-fix the API reclaimed 0 auth/claude-base paths and wrote no trigger, so the
    response reported ``deleted_path_count: 0``. Post-fix the worker's reclaimed
    auth-overlay + claude-base bytes/paths fold into the headline.
    """
    monkeypatch.setenv("AWF_WORK_DIR", str(tmp_path / "service"))
    await _seed_fresh_heartbeat(engine)
    worker_report = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_path_count": 3,
        "total_estimated_bytes": 1_700_000_000,
    }
    consumer = await _run_worker_consumer(engine, worker_report)
    try:
        response = await client.post(
            "/v1/service/gc",
            json={"execute": True, "worker_delegation_timeout_seconds": 10},
        )
    finally:
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "succeeded"
    # Headline = API-side (0 auth) + worker reclaim (3 paths, ~1.7 GB).
    assert payload["deleted_path_count"] == 3
    assert payload["total_estimated_bytes"] == 1_700_000_000
    worker_reclaim = payload["worker_reclaim"]
    assert worker_reclaim["status"] == "completed"
    assert worker_reclaim["reason_code"] == "SERVICE_GC_WORKER_RECLAIMED"
    assert worker_reclaim["deleted_path_count"] == 3
    assert worker_reclaim["report"] == worker_report


@pytest.mark.unit
async def test_dry_run_writes_no_trigger_and_is_unchanged(
    client: AsyncClient,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_WORK_DIR", str(tmp_path / "service"))
    await _seed_fresh_heartbeat(engine)

    response = await client.post("/v1/service/gc", json={"min_age_hours": 24})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["status"] == "dry_run"
    # No worker trigger row written on a dry-run plan.
    assert "worker_reclaim" not in payload or payload["worker_reclaim"] is None
    assert await _no_pending_requests(engine)


@pytest.mark.unit
async def test_execute_worker_unavailable_does_not_block_or_succeed(
    client: AsyncClient,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _stub_api_side_reclaim: None,
) -> None:
    """No fresh heartbeat → fast structured error, not success, and no long wait."""
    monkeypatch.setenv("AWF_WORK_DIR", str(tmp_path / "service"))

    loop = asyncio.get_running_loop()
    started = loop.time()
    response = await client.post(
        # A large delegation budget must NOT be waited out when the worker is down.
        "/v1/service/gc",
        json={"execute": True, "worker_delegation_timeout_seconds": 30},
    )
    elapsed = loop.time() - started

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "partial"
    assert payload["reason_code"] == "SERVICE_GC_WORKER_UNAVAILABLE"
    # API-side reclaim still reported (here 0, but the field is present).
    assert payload["deleted_path_count"] == 0
    assert payload["worker_reclaim"]["status"] == "unavailable"
    # Fast-fail: nowhere near the 30s budget.
    assert elapsed < 5.0


@pytest.mark.unit
async def test_route_handler_folds_outcome_directly(
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _stub_api_side_reclaim: None,
) -> None:
    """Call the route handler directly so the fold/return continuation is exercised.

    Driving it through the ASGI transport hides the post-``await`` fold/return from
    coverage when the delegation polls (a known coverage+asyncio gap); a direct
    call traces it. No worker heartbeat → fast worker-unavailable, no polling.
    """
    monkeypatch.setenv("AWF_WORK_DIR", str(tmp_path / "service"))
    from awf.api.routes.service import trigger_service_gc
    from awf.api.schemas import ServiceGCRequest

    response = await trigger_service_gc(
        ServiceGCRequest(execute=True),
        session_factory=make_session_factory(engine),
    )

    assert response.status == "partial"
    assert response.reason_code == "SERVICE_GC_WORKER_UNAVAILABLE"
    assert response.worker_reclaim is not None
    assert response.worker_reclaim.status == "unavailable"


@pytest.mark.unit
async def test_execute_times_out_when_worker_never_completes(
    client: AsyncClient,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _stub_api_side_reclaim: None,
) -> None:
    """Fresh heartbeat but the request never finishes → structured timeout."""
    monkeypatch.setenv("AWF_WORK_DIR", str(tmp_path / "service"))
    await _seed_fresh_heartbeat(engine)
    # No worker consumer is running, so the pending row is never completed.

    response = await client.post(
        "/v1/service/gc",
        json={"execute": True, "worker_delegation_timeout_seconds": 0.2},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "partial"
    assert payload["reason_code"] == "SERVICE_GC_WORKER_DELEGATION_TIMEOUT"
    assert payload["worker_reclaim"]["status"] == "timeout"
