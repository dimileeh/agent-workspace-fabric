"""Worker hooks: bounded periodic key reconciliation and terminal key finalization."""

from __future__ import annotations

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
        _config=SimpleNamespace(provider_key_reconcile_interval_seconds=interval),
    )


async def test_reconcile_runs_once_per_interval(monkeypatch):
    calls: list[int] = []
    clock = [1000.0]
    monkeypatch.setattr(provider_keys, "monotonic", lambda: clock[0])

    async def reconcile():
        calls.append(1)
        return {"deleted": 1}

    worker = _worker(reconciler=reconcile)
    await provider_keys._maybe_reconcile_openrouter_keys(worker)
    await provider_keys._maybe_reconcile_openrouter_keys(worker)
    clock[0] += 900.0
    await provider_keys._maybe_reconcile_openrouter_keys(worker)

    assert len(calls) == 2
    assert worker._next_provider_key_reconcile_at == clock[0] + 900.0


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
