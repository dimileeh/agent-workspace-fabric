"""Health readiness egress-audit task helper contracts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import awf.api.routes.health as health_route
from awf.api.app import create_app


@pytest.mark.unit
async def test_reset_egress_audit_summary_counts_task_cancels_app_lookup() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    state = SimpleNamespace()

    async def _leaked_lookup() -> dict[str, int]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    leaked_task = asyncio.create_task(_leaked_lookup())
    await asyncio.wait_for(started.wait(), timeout=1.0)
    health_route._track_egress_audit_summary_counts_task(state, leaked_task)

    try:
        health_route.reset_egress_audit_summary_counts_task(state)

        assert health_route._pending_egress_audit_summary_counts_task(state) is None
        await asyncio.wait_for(
            asyncio.gather(leaked_task, return_exceptions=True),
            timeout=1.0,
        )
        await asyncio.wait_for(cancelled.wait(), timeout=1.0)
    finally:
        if not leaked_task.done():
            leaked_task.cancel()
            await asyncio.wait_for(
                asyncio.gather(leaked_task, return_exceptions=True),
                timeout=1.0,
            )


@pytest.mark.unit
async def test_reset_egress_audit_summary_counts_task_consumes_completed_task() -> None:
    state = SimpleNamespace()

    async def _completed_lookup() -> dict[str, int]:
        return {"allowed": 1}

    task = asyncio.create_task(_completed_lookup())
    await task
    health_route._track_egress_audit_summary_counts_task(state, task)

    health_route.reset_egress_audit_summary_counts_task(state)

    assert getattr(state, health_route._EGRESS_AUDIT_SUMMARY_COUNTS_TASK_STATE_ATTR, None) is None


@pytest.mark.unit
async def test_pending_egress_audit_summary_counts_task_clears_completed_task() -> None:
    state = SimpleNamespace()

    async def _completed_lookup() -> dict[str, int]:
        return {"warn": 2}

    task = asyncio.create_task(_completed_lookup())
    await task
    setattr(state, health_route._EGRESS_AUDIT_SUMMARY_COUNTS_TASK_STATE_ATTR, task)

    assert health_route._pending_egress_audit_summary_counts_task(state) is None
    assert getattr(state, health_route._EGRESS_AUDIT_SUMMARY_COUNTS_TASK_STATE_ATTR, None) is None


@pytest.mark.unit
async def test_create_app_does_not_reset_other_app_egress_audit_lookup_task() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    app = create_app(use_lifespan=False)

    async def _lookup() -> dict[str, int]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(_lookup())
    await started.wait()
    health_route._track_egress_audit_summary_counts_task(app.state, task)

    try:
        new_app = create_app(use_lifespan=False)

        assert health_route._pending_egress_audit_summary_counts_task(app.state) is task
        assert health_route._pending_egress_audit_summary_counts_task(new_app.state) is None
        assert not cancelled.is_set()
    finally:
        health_route.reset_egress_audit_summary_counts_task(app.state)
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.unit
async def test_stale_egress_audit_lookup_callback_does_not_clear_current_task() -> None:
    release_stale = asyncio.Event()
    release_current = asyncio.Event()
    state = SimpleNamespace()

    async def _lookup(release: asyncio.Event) -> dict[str, int]:
        await release.wait()
        return {}

    stale_task = asyncio.create_task(_lookup(release_stale))
    current_task = asyncio.create_task(_lookup(release_current))

    try:
        health_route._track_egress_audit_summary_counts_task(state, stale_task)
        health_route._track_egress_audit_summary_counts_task(state, current_task)

        release_stale.set()
        await stale_task
        await asyncio.sleep(0)

        assert health_route._pending_egress_audit_summary_counts_task(state) is current_task
    finally:
        release_current.set()
        await asyncio.gather(stale_task, current_task, return_exceptions=True)


@pytest.mark.unit
async def test_drain_cancelled_task_result_defers_pending_task_consumption() -> None:
    release = asyncio.Event()

    async def _pending() -> None:
        await release.wait()

    task = asyncio.create_task(_pending())
    await health_route._drain_cancelled_task_result(task, timeout=0)

    assert not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
