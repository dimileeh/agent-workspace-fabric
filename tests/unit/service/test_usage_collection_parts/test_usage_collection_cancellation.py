"""Cancellation and error-path usage-sampling tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.db.enums import AgentRuntime
from awf.service.usage_collection import CcusageCollector
from awf.service.usage_store import read_latest_usage_snapshot
from tests.unit.service.test_usage_collection import (
    _COMPOSE_FILE,
    FakeClock,
    _BlockingRunner,
    _RaisingRunner,
    _wait_for,
)


@pytest.mark.unit
async def test_baseline_cancellation_propagates(tmp_path: Path) -> None:
    runner = _BlockingRunner(
        CommandResult(returncode=0, stdout=json.dumps({"totals": {"totalTokens": 1}}), stderr="")
    )
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    start_task = asyncio.ensure_future(
        collector.start(
            compose_project="p",
            compose_file=_COMPOSE_FILE,
            workspace_id="ws_bcancel",
            provider=AgentRuntime.claude_code,
        )
    )
    await _wait_for(lambda: len(runner.calls) == 1)  # baseline blocked in run_streaming
    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task


@pytest.mark.unit
async def test_live_sample_cancellation_propagates(tmp_path: Path) -> None:
    # Baseline is read fresh at run start (passthrough); the live sample blocks in
    # run_streaming so we can cancel it and assert the cancellation propagates.
    runner = _BlockingRunner(
        CommandResult(returncode=0, stdout=json.dumps({"totals": {"totalTokens": 9}}), stderr=""),
        passthrough=[
            CommandResult(
                returncode=0, stdout=json.dumps({"totals": {"totalTokens": 1}}), stderr=""
            )
        ],
    )
    clock = FakeClock()
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=clock)
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_lcancel",
        provider=AgentRuntime.claude_code,
    )
    await _wait_for(lambda: len(clock.sleeps) == 1)  # parked on sleep
    clock.tick()
    await _wait_for(lambda: len(runner.calls) == 2)  # live sample blocked in run_streaming

    assert ctx._task is not None
    ctx._task.cancel()  # cancellation must propagate, not be swallowed
    with pytest.raises(asyncio.CancelledError):
        await ctx._task


@pytest.mark.unit
async def test_sampler_errors_are_swallowed(tmp_path: Path) -> None:
    runner = _RaisingRunner()
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_raise",
        provider=AgentRuntime.claude_code,
    )
    # Neither the baseline error nor the final-sample error propagates.
    await ctx.finalize(status="failed")
    # The baseline read raised before anchoring a baseline, but the start path
    # still seeds an unavailable snapshot so a reused workspace id's prior-run
    # snapshot.json can't be reported as this run's usage during the window before
    # the first live tick. The final-sample error is swallowed (no later write),
    # so this seed stays the latest record on disk.
    seed = read_latest_usage_snapshot("ws_raise", work_dir=tmp_path)
    assert seed is not None
    assert seed.phase == "live"
    assert seed.run_status == "running"
    assert seed.status == "unavailable"
    assert seed.reason == "ccusage_command_failed"  # baseline failure, not a reading
    assert seed.total_tokens is None  # no prior-run metrics reported as this run's


@pytest.mark.unit
async def test_drain_pending_write_noops_without_pending_task(tmp_path: Path) -> None:
    collector = CcusageCollector(runner=FakeCommandRunner(), work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_no_pending_write",
        provider=AgentRuntime.claude_code,
    )

    ctx._pending_write = None
    await ctx._drain_pending_write()
    await ctx.finalize(status="success")
