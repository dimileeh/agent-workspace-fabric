"""Worker hooks: bounded periodic key reconciliation and terminal key finalization."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import structlog

from awf.control.worker import provider_keys
from awf.service.openrouter_management import OpenRouterManagementError

pytestmark = pytest.mark.unit


def _worker(*, reconciler=None, finalizer=None, interval: float = 900.0):
    return SimpleNamespace(
        _provider_key_reconciler=reconciler,
        _provider_key_finalizer=finalizer,
        _next_provider_key_reconcile_at=0.0,
        _provider_key_reconcile_task=None,
        _config=SimpleNamespace(provider_key_reconcile_interval_seconds=interval),
    )


async def _settle(worker) -> None:
    task = worker._provider_key_reconcile_task
    if task is not None:
        await task


async def test_reconcile_runs_once_per_interval(monkeypatch):
    calls: list[int] = []
    clock = [1000.0]
    monkeypatch.setattr(provider_keys, "monotonic", lambda: clock[0])

    async def reconcile():
        calls.append(1)
        return {"deleted": 1}

    worker = _worker(reconciler=reconcile)
    await provider_keys._maybe_reconcile_openrouter_keys(worker)
    await _settle(worker)
    await provider_keys._maybe_reconcile_openrouter_keys(worker)
    await _settle(worker)
    clock[0] += 900.0
    await provider_keys._maybe_reconcile_openrouter_keys(worker)
    await _settle(worker)

    assert len(calls) == 2
    assert worker._next_provider_key_reconcile_at == clock[0] + 900.0


async def test_dispatch_never_waits_on_an_in_flight_sweep(monkeypatch):
    monkeypatch.setattr(provider_keys, "monotonic", lambda: 1000.0)
    started: list[int] = []
    release = asyncio.Event()

    async def reconcile():
        started.append(1)
        await release.wait()
        return {}

    worker = _worker(reconciler=reconcile)
    await asyncio.wait_for(provider_keys._maybe_reconcile_openrouter_keys(worker), timeout=1)
    await asyncio.sleep(0)
    sweep = worker._provider_key_reconcile_task
    # Still due (rescheduled only once a sweep ends), yet never a second sweep.
    await asyncio.wait_for(provider_keys._maybe_reconcile_openrouter_keys(worker), timeout=1)

    assert started == [1]
    assert worker._provider_key_reconcile_task is sweep
    assert not sweep.done()
    release.set()
    await sweep
    assert worker._next_provider_key_reconcile_at == 1900.0


async def test_stop_drains_or_cancels_an_in_flight_sweep(monkeypatch):
    monkeypatch.setattr(provider_keys, "monotonic", lambda: 10.0)
    release = asyncio.Event()
    finished: list[int] = []

    async def reconcile():
        await release.wait()
        finished.append(1)
        return {}

    worker = _worker(reconciler=reconcile, interval=60.0)
    await provider_keys._stop_provider_key_reconcile(worker)  # nothing in flight
    await provider_keys._maybe_reconcile_openrouter_keys(worker)
    await asyncio.sleep(0)
    await provider_keys._stop_provider_key_reconcile(worker)
    assert worker._provider_key_reconcile_task.cancelled()
    assert finished == []
    assert worker._next_provider_key_reconcile_at == 70.0

    worker._next_provider_key_reconcile_at = 0.0
    await provider_keys._maybe_reconcile_openrouter_keys(worker)
    asyncio.get_running_loop().call_soon(release.set)
    await provider_keys._stop_provider_key_reconcile(worker, drain=True)
    assert finished == [1]
    assert not worker._provider_key_reconcile_task.cancelled()


async def test_without_a_management_key_the_sweep_is_off():
    worker = _worker()
    await provider_keys._maybe_reconcile_openrouter_keys(worker)
    await provider_keys._finalize_terminal_provider_keys(worker, "ws_x")
    assert worker._next_provider_key_reconcile_at == 0.0


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            OpenRouterManagementError("PROVIDER_MANAGEMENT_API_UNAUTHORIZED", operation="list"),
            "PROVIDER_MANAGEMENT_API_UNAUTHORIZED",
        ),
        (RuntimeError("database gone"), "PROVIDER_MANAGED_KEY_RECONCILE_FAILED"),
    ],
)
async def test_failures_are_logged_rescheduled_and_never_raised(monkeypatch, error, reason):
    monkeypatch.setattr(provider_keys, "monotonic", lambda: 50.0)

    async def reconcile():
        raise error

    async def finalize(_workspace_id):
        raise error

    worker = _worker(reconciler=reconcile, finalizer=finalize, interval=60.0)
    with structlog.testing.capture_logs() as logs:
        await provider_keys._maybe_reconcile_openrouter_keys(worker)
        await _settle(worker)
        await provider_keys._finalize_terminal_provider_keys(worker, "ws_x")

    assert worker._next_provider_key_reconcile_at == 110.0
    assert [(log["event"], log["reason_code"]) for log in logs] == [
        ("worker.provider_key_reconcile_failed", reason),
        ("worker.provider_key_finalize_failed", reason),
    ]


async def test_terminal_finalize_delegates_to_the_broker():
    finalized: list[str] = []

    async def finalize(workspace_id):
        finalized.append(workspace_id)
        return {"finalized": 1, "failed": 0}

    await provider_keys._finalize_terminal_provider_keys(_worker(finalizer=finalize), "ws_y")

    assert finalized == ["ws_y"]
