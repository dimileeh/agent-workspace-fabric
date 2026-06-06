"""Unit tests for the ccusage usage collector (no real docker, no real CLI)."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from awf.common.commands import COMMAND_TIMEOUT_REASON, CommandResult, FakeCommandRunner
from awf.db.enums import AgentRuntime
from awf.service import usage_collection
from awf.service.usage_collection import CcusageCollector, _is_missing_binary, _RealClock
from awf.service.usage_store import (
    NormalizedUsage,
    UsageSnapshot,
    read_latest_usage_snapshot,
    write_usage_snapshot,
)

_COMPOSE_FILE = Path("/fake/compose.yml")


class FakeClock:
    """Deterministic clock: ``sleep`` blocks until the test calls ``tick``."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self._gate: asyncio.Queue[None] = asyncio.Queue()
        self._t = datetime(2026, 5, 22, tzinfo=UTC)

    def now(self) -> datetime:
        return self._t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await self._gate.get()
        self._t += timedelta(seconds=seconds)

    def tick(self) -> None:
        self._gate.put_nowait(None)


class _BlockingRunner:
    """``run_streaming`` records each call. The first calls return the matching
    ``passthrough`` result immediately (e.g. the run-start baseline read); every
    later call blocks until ``release`` is set."""

    def __init__(self, result: CommandResult, *, passthrough: Sequence[CommandResult] = ()) -> None:
        self.calls: list[list[str]] = []
        self.release = asyncio.Event()
        self._result = result
        self._passthrough = tuple(passthrough)

    async def run(self, args: list[str], **_kwargs: Any) -> CommandResult:  # pragma: no cover
        raise AssertionError("run() should not be called")

    async def run_streaming(self, args: list[str], **_kwargs: Any) -> CommandResult:
        index = len(self.calls)
        self.calls.append(list(args))
        if index < len(self._passthrough):
            return self._passthrough[index]
        await self.release.wait()
        return self._result


class _RaisingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def run(self, args: list[str], **_kwargs: Any) -> CommandResult:  # pragma: no cover
        raise RuntimeError("boom")

    async def run_streaming(self, args: list[str], **_kwargs: Any) -> CommandResult:
        self.calls.append(list(args))
        raise RuntimeError("boom")


async def _wait_for(predicate: Callable[[], bool], *, tries: int = 200) -> None:
    for _ in range(tries):
        if predicate():
            return
        # Real (tiny) delay, not sleep(0): snapshot writes now run via
        # asyncio.to_thread, so the poll must yield enough wall-time for the
        # worker thread to complete and post its result back to the loop.
        await asyncio.sleep(0.001)
    raise AssertionError("condition not reached")


def _ccusage_runner(*stdouts: str) -> FakeCommandRunner:
    runner = FakeCommandRunner()
    for stdout in stdouts:
        runner.queue_result(returncode=0, stdout=stdout)
    return runner


@pytest.mark.unit
@pytest.mark.parametrize(
    ("provider", "source"),
    [
        (AgentRuntime.claude_code, "claude"),
        (AgentRuntime.codex, "codex"),
        (AgentRuntime.gemini, "gemini"),
        (AgentRuntime.opencode, "opencode"),
    ],
)
async def test_ccusage_argv_per_provider(
    tmp_path: Path, provider: AgentRuntime, source: str
) -> None:
    runner = FakeCommandRunner()
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="proj",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_argv",
        provider=provider,
    )
    await ctx.finalize(status="success")

    args = runner.calls[0].args
    assert args[:2] == ["docker", "compose"]
    assert "-p" in args and "proj" in args
    assert "agent" in args
    # ``--config`` pins a neutral config (baked into the agent-runtime image) so
    # ccusage skips auto-discovery of user/project ccusage configs that could
    # otherwise filter per-run totals. The literal path must match the Dockerfile.
    assert args[-7:] == [
        "ccusage",
        source,
        "daily",
        "--json",
        "--offline",
        "--config",
        "/opt/awf/ccusage-neutral.json",
    ]


@pytest.mark.unit
async def test_baseline_subtracted_from_final_sample(tmp_path: Path) -> None:
    runner = _ccusage_runner(
        json.dumps({"totals": {"totalTokens": 5, "inputTokens": 3, "outputTokens": 2}}),
        json.dumps({"totals": {"totalTokens": 8, "inputTokens": 5, "outputTokens": 3}}),
    )
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_delta",
        provider=AgentRuntime.claude_code,
    )
    await ctx.finalize(status="success")

    snap = read_latest_usage_snapshot("ws_delta", work_dir=tmp_path)
    assert snap is not None
    assert snap.phase == "final"
    assert snap.status == "available"
    assert snap.total_tokens == 3  # 8 - 5 baseline
    assert snap.input_tokens == 2
    assert snap.output_tokens == 1
    assert snap.baseline == {
        "input_tokens": 3,
        "cached_input_tokens": None,
        "output_tokens": 2,
        "reasoning_output_tokens": None,
        "total_tokens": 5,
        "cost_estimate": None,
        "currency": None,
        "model": None,
    }


@pytest.mark.unit
async def test_prior_baseline_not_reused_fresh_capture_each_run(tmp_path: Path) -> None:
    # A prior snapshot anchored a baseline for this workspace, but it must NOT be
    # reused. The per-workspace auth copy persists across retries/recovery runs
    # (auth_mounts skips the copy when the target dir exists; GC only runs on
    # workspace completion), so the prior run's transcripts are still on disk and
    # ccusage at this run's start already reflects them. Here the persisted
    # baseline is 100 but the on-disk reading at run start is 120 (the prior run
    # added 20). Reusing 100 would report 130-100=30, inflating this run's total
    # by the prior run's usage; a fresh baseline (120) reports the true 130-120=10.
    write_usage_snapshot(
        UsageSnapshot(
            workspace_id="ws_reuse",
            provider="claude_code",
            ccusage_source="claude",
            status="available",
            phase="final",
            captured_at="2026-05-22T00:00:00+00:00",
            baseline={"total_tokens": 100},
        ),
        work_dir=tmp_path,
    )
    runner = _ccusage_runner(
        json.dumps({"totals": {"totalTokens": 120}}),  # fresh baseline at run start
        json.dumps({"totals": {"totalTokens": 130}}),  # final
    )
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_reuse",
        provider=AgentRuntime.claude_code,
    )
    await ctx.finalize(status="success")

    snap = read_latest_usage_snapshot("ws_reuse", work_dir=tmp_path)
    assert snap is not None
    # Fresh baseline (120) captured at run start, NOT the persisted 100: 130-120=10.
    assert snap.total_tokens == 10
    # Two ccusage calls: fresh baseline + final (no reuse short-circuit).
    assert len(runner.calls) == 2


@pytest.mark.unit
async def test_baseline_not_reused_when_provider_changed(tmp_path: Path) -> None:
    # A workspace can switch agents in place (provider recovery fallback), so a
    # prior baseline anchored for a different provider/source must not be reused:
    # subtracting it against an unrelated ccusage source would skew the delta.
    # The prior lifetime total is still valid workspace usage and must accumulate
    # with the new provider's fresh-baseline delta.
    write_usage_snapshot(
        UsageSnapshot(
            workspace_id="ws_switch",
            provider="claude_code",
            ccusage_source="claude",
            status="available",
            phase="final",
            captured_at="2026-05-22T00:00:00+00:00",
            total_tokens=20,
            baseline={"total_tokens": 100},
        ),
        work_dir=tmp_path,
    )
    runner = _ccusage_runner(
        json.dumps({"totals": {"totalTokens": 10}}),  # fresh codex baseline
        json.dumps({"totals": {"totalTokens": 17}}),  # codex final
    )
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_switch",
        provider=AgentRuntime.codex,
    )
    await ctx.finalize(status="success")

    snap = read_latest_usage_snapshot("ws_switch", work_dir=tmp_path)
    assert snap is not None
    assert snap.provider == "codex"
    assert snap.ccusage_source == "codex"
    # Stale claude baseline (100) ignored; a fresh codex baseline (10) is captured.
    assert snap.total_tokens == 27  # prior lifetime 20 + fresh-baseline delta 7
    assert len(runner.calls) == 2  # fresh baseline + final, not a single reused call


@pytest.mark.unit
async def test_second_run_accumulates_prior_lifetime_usage(tmp_path: Path) -> None:
    write_usage_snapshot(
        UsageSnapshot(
            workspace_id="ws_lifetime",
            provider="codex",
            ccusage_source="codex",
            status="available",
            phase="final",
            run_status="success",
            captured_at="2026-05-22T00:00:00+00:00",
            input_tokens=300,
            cached_input_tokens=50,
            output_tokens=200,
            reasoning_output_tokens=20,
            total_tokens=550,
            cost_estimate=0.50,
            currency="USD",
        ),
        work_dir=tmp_path,
    )
    runner = _ccusage_runner(
        json.dumps(
            {
                "totals": {
                    "inputTokens": 80,
                    "cachedInputTokens": 40,
                    "outputTokens": 20,
                    "reasoningOutputTokens": 10,
                    "totalTokens": 140,
                    "totalCost": 1.20,
                }
            }
        ),
        json.dumps(
            {
                "totals": {
                    "inputTokens": 90,
                    "cachedInputTokens": 42,
                    "outputTokens": 25,
                    "reasoningOutputTokens": 12,
                    "totalTokens": 157,
                    "totalCost": 1.37,
                }
            }
        ),
    )
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_lifetime",
        provider=AgentRuntime.codex,
    )
    await ctx.finalize(status="success")

    snap = read_latest_usage_snapshot("ws_lifetime", work_dir=tmp_path)
    assert snap is not None
    assert snap.input_tokens == 310
    assert snap.cached_input_tokens == 52
    assert snap.output_tokens == 205
    assert snap.reasoning_output_tokens == 22
    assert snap.total_tokens == 567
    assert snap.cost_estimate == pytest.approx(0.67)
    assert snap.run_delta == {
        "input_tokens": 10,
        "cached_input_tokens": 2,
        "output_tokens": 5,
        "reasoning_output_tokens": 2,
        "total_tokens": 17,
        "cost_estimate": pytest.approx(0.17),
        "currency": "USD",
        "model": None,
    }
    assert (
        snap.accumulated_usage_at_run_start
        == NormalizedUsage(
            input_tokens=300,
            cached_input_tokens=50,
            output_tokens=200,
            reasoning_output_tokens=20,
            total_tokens=550,
            cost_estimate=0.50,
            currency="USD",
        ).as_baseline_dict()
    )


@pytest.mark.unit
async def test_prior_snapshot_without_baseline_captures_fresh(tmp_path: Path) -> None:
    write_usage_snapshot(
        UsageSnapshot(
            workspace_id="ws_nobase",
            provider="claude_code",
            ccusage_source=None,
            status="unavailable",
            phase="final",
            captured_at="2026-05-22T00:00:00+00:00",
            reason="ccusage_source_unsupported",
        ),
        work_dir=tmp_path,
    )
    runner = _ccusage_runner(
        json.dumps({"totals": {"totalTokens": 10}}),
        json.dumps({"totals": {"totalTokens": 17}}),
    )
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_nobase",
        provider=AgentRuntime.claude_code,
    )
    await ctx.finalize(status="success")

    snap = read_latest_usage_snapshot("ws_nobase", work_dir=tmp_path)
    assert snap is not None
    assert snap.total_tokens == 7  # fresh baseline 10, final 17
    assert len(runner.calls) == 2


@pytest.mark.unit
async def test_safe_write_reading_reraises_cancellation(tmp_path: Path) -> None:
    collector = CcusageCollector(runner=FakeCommandRunner(), work_dir=tmp_path, clock=FakeClock())
    ctx = usage_collection._CcusageSampleContext(  # noqa: SLF001
        collector=collector,
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_cancel",
        provider=AgentRuntime.claude_code,
        source="claude",
        accumulated_usage_at_run_start=None,
    )

    async def _cancel_write(**_kwargs: object) -> None:
        raise asyncio.CancelledError

    ctx._write_reading = _cancel_write  # type: ignore[method-assign]  # noqa: SLF001

    with pytest.raises(asyncio.CancelledError):
        await ctx._safe_write_reading(  # noqa: SLF001
            usage=None,
            reason=usage_collection.REASON_COMMAND_FAILED,
            model=None,
            phase="live",
            run_status="running",
        )


@pytest.mark.unit
async def test_failed_baseline_does_not_leak_host_usage(tmp_path: Path) -> None:
    # Baseline capture times out, then the final read succeeds with a large total
    # that includes copied host history. Without a trustworthy baseline we must
    # report unavailable rather than subtracting against nothing and leaking it.
    runner = FakeCommandRunner()
    runner.queue_result(returncode=124, stdout="", stderr="", reason_code=COMMAND_TIMEOUT_REASON)
    # The baseline timeout triggers targeted compose-exec cleanup, which issues a
    # runner.run() that consumes the next FIFO slot (shared with run_streaming).
    runner.queue_result(returncode=0, stdout="awf cleanup: absent")
    runner.queue_result(returncode=0, stdout=json.dumps({"totals": {"totalTokens": 999}}))
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_baseline_fail",
        provider=AgentRuntime.claude_code,
    )
    await ctx.finalize(status="success")

    snap = read_latest_usage_snapshot("ws_baseline_fail", work_dir=tmp_path)
    assert snap is not None
    assert snap.phase == "final"
    assert snap.status == "unavailable"
    assert snap.reason == "ccusage_timeout"  # baseline failure reason, not "available"
    assert snap.total_tokens is None  # host total is NOT leaked as workspace usage


@pytest.mark.unit
async def test_timeout_runs_targeted_compose_exec_cleanup(tmp_path: Path) -> None:
    # A timed-out ccusage exec only kills the local compose client; per the
    # build_tracked_compose_exec contract the in-container process tree may survive.
    # The collector must run targeted cleanup for the timed-out invocation so
    # orphaned ccusage processes can't accumulate across repeated timeouts. The
    # sample still stays reason-coded as ccusage_timeout.
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout=json.dumps({"totals": {"totalTokens": 1}}))  # baseline
    runner.queue_result(returncode=124, stdout="", stderr="", reason_code=COMMAND_TIMEOUT_REASON)
    runner.queue_result(returncode=0, stdout="awf cleanup: killed")  # targeted cleanup run()
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_timeout_cleanup",
        provider=AgentRuntime.claude_code,
    )
    await ctx.finalize(status="failed")

    snap = read_latest_usage_snapshot("ws_timeout_cleanup", work_dir=tmp_path)
    assert snap is not None
    assert snap.phase == "final"
    assert snap.status == "unavailable"
    assert snap.reason == "ccusage_timeout"  # cleanup failure must not change reason coding
    # Exactly one targeted cleanup exec was issued for the timed-out invocation
    # (the cleanup argv carries the awf-cleanup marker).
    cleanup_calls = [call for call in runner.calls if "awf-cleanup" in call.args]
    assert len(cleanup_calls) == 1


@pytest.mark.unit
async def test_fresh_workspace_no_records_baseline_reports_full_usage(tmp_path: Path) -> None:
    # ccusage runs cleanly at baseline but the fresh workspace has no prior usage,
    # so the later total is genuine workspace usage and must be reported in full.
    runner = _ccusage_runner(
        json.dumps({}),  # baseline: valid JSON, no usage records (fresh)
        json.dumps({"totals": {"totalTokens": 12}}),  # final: real workspace usage
    )
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_fresh",
        provider=AgentRuntime.claude_code,
    )
    await ctx.finalize(status="success")

    snap = read_latest_usage_snapshot("ws_fresh", work_dir=tmp_path)
    assert snap is not None
    assert snap.status == "available"
    assert snap.total_tokens == 12  # full usage; no host history to subtract
    assert snap.baseline is None  # nothing anchored, so the next run recaptures


@pytest.mark.unit
async def test_samples_at_sixty_second_interval_while_active(tmp_path: Path) -> None:
    clock = FakeClock()
    runner = _ccusage_runner(*[json.dumps({"totals": {"totalTokens": 1}})] * 8)
    collector = CcusageCollector(
        runner=runner, work_dir=tmp_path, clock=clock, interval_seconds=60.0
    )
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_cadence",
        provider=AgentRuntime.codex,
    )
    assert len(runner.calls) == 1  # baseline at start

    await _wait_for(lambda: len(clock.sleeps) == 1)
    clock.tick()
    await _wait_for(lambda: len(runner.calls) == 2)  # first live sample

    await _wait_for(lambda: len(clock.sleeps) == 2)
    clock.tick()
    await _wait_for(lambda: len(runner.calls) == 3)  # second live sample

    await _wait_for(lambda: len(clock.sleeps) == 3)
    await ctx.finalize(status="success")
    await _wait_for(lambda: len(runner.calls) == 4)  # final sample

    assert clock.sleeps == [60.0, 60.0, 60.0]
    snap = read_latest_usage_snapshot("ws_cadence", work_dir=tmp_path)
    assert snap is not None
    assert snap.phase == "final"


@pytest.mark.unit
async def test_live_snapshots_written_during_run(tmp_path: Path) -> None:
    clock = FakeClock()
    runner = _ccusage_runner(
        json.dumps({"totals": {"totalTokens": 2}}),  # baseline
        json.dumps({"totals": {"totalTokens": 9}}),  # live sample
    )
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=clock)
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_live",
        provider=AgentRuntime.codex,
    )
    # start() seeds an immediate zero-delta "running" snapshot from the baseline
    # reading (so a reused workspace id can't surface a prior run's totals before
    # the first live sample). It lands synchronously, before the first tick.
    seed = read_latest_usage_snapshot("ws_live", work_dir=tmp_path)
    assert seed is not None
    assert seed.phase == "live"
    assert seed.total_tokens == 0  # baseline 2 - baseline 2

    await _wait_for(lambda: len(clock.sleeps) == 1)
    clock.tick()
    # The write trails the ccusage call (it now runs via asyncio.to_thread), and
    # the seed is already on disk, so wait on the live *delta* landing rather than
    # mere snapshot presence.
    await _wait_for(
        lambda: (
            (snap := read_latest_usage_snapshot("ws_live", work_dir=tmp_path)) is not None
            and snap.total_tokens == 7
        )
    )

    snap = read_latest_usage_snapshot("ws_live", work_dir=tmp_path)
    assert snap is not None
    assert snap.phase == "live"
    assert snap.total_tokens == 7  # 9 - 2 baseline

    await ctx.finalize(status="success")


@pytest.mark.unit
async def test_start_seed_preserves_prior_lifetime_usage(tmp_path: Path) -> None:
    # A prior run left a final snapshot with metrics for this workspace id, which
    # retries/recovery reuse. At start — before the first live sample — the fresh
    # baseline seed must preserve the prior lifetime total instead of resetting
    # the workspace-level LLM usage display to zero.
    write_usage_snapshot(
        UsageSnapshot(
            workspace_id="ws_seed",
            provider="claude_code",
            ccusage_source="claude",
            status="available",
            phase="final",
            run_status="success",
            captured_at="2026-05-22T00:00:00+00:00",
            input_tokens=300,
            output_tokens=200,
            total_tokens=500,
        ),
        work_dir=tmp_path,
    )
    runner = _ccusage_runner(json.dumps({"totals": {"totalTokens": 120}}))  # fresh baseline only
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_seed",
        provider=AgentRuntime.claude_code,
    )

    # No live tick yet: the seed has already replaced the prior snapshot on disk.
    seed = read_latest_usage_snapshot("ws_seed", work_dir=tmp_path)
    assert seed is not None
    assert seed.phase == "live"
    assert seed.run_status == "running"
    assert seed.status == "available"
    assert seed.total_tokens == 500
    assert seed.run_delta == {
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "reasoning_output_tokens": None,
        "total_tokens": 0,
        "cost_estimate": None,
        "currency": None,
        "model": None,
    }
    assert len(runner.calls) == 1  # only the baseline exec; the seed reuses it

    await ctx.finalize(status="cancelled")


@pytest.mark.unit
async def test_start_seed_preserves_prior_usage_when_baseline_unavailable(
    tmp_path: Path,
) -> None:
    # Same reused-id scenario, but the run-start baseline read times out, so no
    # trustworthy baseline is anchored. The seed must still preserve the prior
    # lifetime total while surfacing the baseline failure reason.
    write_usage_snapshot(
        UsageSnapshot(
            workspace_id="ws_seed_fail",
            provider="claude_code",
            ccusage_source="claude",
            status="available",
            phase="final",
            captured_at="2026-05-22T00:00:00+00:00",
            total_tokens=500,
        ),
        work_dir=tmp_path,
    )
    runner = FakeCommandRunner()
    runner.queue_result(returncode=124, stdout="", stderr="", reason_code=COMMAND_TIMEOUT_REASON)
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_seed_fail",
        provider=AgentRuntime.claude_code,
    )

    seed = read_latest_usage_snapshot("ws_seed_fail", work_dir=tmp_path)
    assert seed is not None
    assert seed.phase == "live"
    assert seed.run_status == "running"
    assert seed.status == "available"
    assert seed.reason == "ccusage_timeout"  # baseline failure reason, not "available"
    assert seed.total_tokens == 500

    await ctx.finalize(status="failed")


@pytest.mark.unit
async def test_final_failure_after_live_sample_preserves_latest_lifetime_usage(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout=json.dumps({"totals": {"totalTokens": 10}}))
    runner.queue_result(returncode=0, stdout=json.dumps({"totals": {"totalTokens": 18}}))
    runner.queue_result(returncode=124, stdout="", stderr="", reason_code=COMMAND_TIMEOUT_REASON)
    runner.queue_result(returncode=0, stdout="awf cleanup: killed")
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=clock)
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_live_then_fail",
        provider=AgentRuntime.claude_code,
    )

    await _wait_for(lambda: len(clock.sleeps) == 1)
    clock.tick()
    await _wait_for(
        lambda: (
            (snap := read_latest_usage_snapshot("ws_live_then_fail", work_dir=tmp_path)) is not None
            and snap.total_tokens == 8
        )
    )

    await ctx.finalize(status="failed")

    snap = read_latest_usage_snapshot("ws_live_then_fail", work_dir=tmp_path)
    assert snap is not None
    assert snap.phase == "final"
    assert snap.status == "available"
    assert snap.reason == "ccusage_timeout"
    assert snap.total_tokens == 8


@pytest.mark.unit
@pytest.mark.parametrize(
    ("result", "expected_reason"),
    [
        (
            CommandResult(returncode=124, stdout="", stderr="", reason_code=COMMAND_TIMEOUT_REASON),
            "ccusage_timeout",
        ),
        (CommandResult(returncode=2, stdout="", stderr="boom"), "ccusage_command_failed"),
        (CommandResult(returncode=127, stdout="", stderr=""), "ccusage_unavailable"),
        (
            CommandResult(returncode=1, stdout="", stderr="sh: ccusage: command not found"),
            "ccusage_unavailable",
        ),
        (CommandResult(returncode=0, stdout="garbage{", stderr=""), "ccusage_invalid_json"),
        (CommandResult(returncode=0, stdout="{}", stderr=""), "ccusage_no_records"),
    ],
)
async def test_final_sample_reason_codes(
    tmp_path: Path, result: CommandResult, expected_reason: str
) -> None:
    runner = FakeCommandRunner()
    # Baseline reads a clean total; the final read returns the failure-shaped result.
    runner.queue_result(returncode=0, stdout=json.dumps({"totals": {"totalTokens": 1}}))
    runner._queued.append(result)  # noqa: SLF001 - intentional canned final result
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_reason",
        provider=AgentRuntime.gemini,
    )
    await ctx.finalize(status="failed")

    snap = read_latest_usage_snapshot("ws_reason", work_dir=tmp_path)
    assert snap is not None
    assert snap.phase == "final"
    assert snap.status == "unavailable"
    assert snap.reason == expected_reason


@pytest.mark.unit
async def test_unsupported_provider_records_reason_without_running_ccusage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(usage_collection, "provider_ccusage_source", lambda _provider: None)
    runner = FakeCommandRunner()
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_unsupported",
        provider=AgentRuntime.codex,
    )
    assert runner.calls == []  # no ccusage invocation for an unsupported source
    live = read_latest_usage_snapshot("ws_unsupported", work_dir=tmp_path)
    assert live is not None
    assert live.reason == "ccusage_source_unsupported"
    assert live.status == "unavailable"
    assert live.ccusage_source is None

    await ctx.finalize(status="success")
    final = read_latest_usage_snapshot("ws_unsupported", work_dir=tmp_path)
    assert final is not None
    assert final.phase == "final"
    assert final.reason == "ccusage_source_unsupported"
    assert runner.calls == []


@pytest.mark.unit
async def test_unsupported_provider_preserves_prior_lifetime_usage(tmp_path: Path) -> None:
    write_usage_snapshot(
        UsageSnapshot(
            workspace_id="ws_unsupported_prior",
            provider="codex",
            ccusage_source="codex",
            status="available",
            phase="final",
            captured_at="2026-05-22T00:00:00+00:00",
            total_tokens=123,
        ),
        work_dir=tmp_path,
    )
    collector = CcusageCollector(runner=FakeCommandRunner(), work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_unsupported_prior",
        provider=AgentRuntime.cursor,
    )
    await ctx.finalize(status="success")

    snap = read_latest_usage_snapshot("ws_unsupported_prior", work_dir=tmp_path)
    assert snap is not None
    assert snap.phase == "final"
    assert snap.status == "available"
    assert snap.reason == "ccusage_source_unsupported"
    assert snap.total_tokens == 123


@pytest.mark.unit
async def test_cursor_records_unsupported_source_until_ccusage_adds_cursor(
    tmp_path: Path,
) -> None:
    """Cursor usage records unsupported ccusage source snapshots."""
    runner = FakeCommandRunner()
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_cursor_usage",
        provider=AgentRuntime.cursor,
    )

    assert runner.calls == []
    await ctx.finalize(status="success")
    snap = read_latest_usage_snapshot("ws_cursor_usage", work_dir=tmp_path)
    assert snap is not None
    assert snap.provider == "cursor"
    assert snap.phase == "final"
    assert snap.ccusage_source is None
    assert snap.reason == "ccusage_source_unsupported"
    assert snap.status == "unavailable"


@pytest.mark.unit
async def test_grok_records_unsupported_ccusage_source_without_running_ccusage(
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_grok_usage",
        provider=AgentRuntime.grok,
    )
    await ctx.finalize(status="success")

    snap = read_latest_usage_snapshot("ws_grok_usage", work_dir=tmp_path)
    assert runner.calls == []
    assert snap is not None
    assert snap.phase == "final"
    assert snap.status == "unavailable"
    assert snap.reason == "ccusage_source_unsupported"
    assert snap.ccusage_source is None


@pytest.mark.unit
async def test_finalize_is_idempotent(tmp_path: Path) -> None:
    runner = _ccusage_runner(
        json.dumps({"totals": {"totalTokens": 1}}),
        json.dumps({"totals": {"totalTokens": 4}}),
    )
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_idem",
        provider=AgentRuntime.opencode,
    )
    await ctx.finalize(status="success")
    calls_after_first = len(runner.calls)
    await ctx.finalize(status="success")
    assert len(runner.calls) == calls_after_first  # second finalize is a no-op


@pytest.mark.unit
async def test_finalize_completes_final_sample_under_cancellation(tmp_path: Path) -> None:
    # Baseline is read fresh at run start (passthrough, totalTokens=10); the final
    # sample (totalTokens=50) blocks in run_streaming so we can cancel mid-finalize.
    runner = _BlockingRunner(
        CommandResult(returncode=0, stdout=json.dumps({"totals": {"totalTokens": 50}}), stderr=""),
        passthrough=[
            CommandResult(
                returncode=0, stdout=json.dumps({"totals": {"totalTokens": 10}}), stderr=""
            )
        ],
    )
    clock = FakeClock()
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=clock)
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_cancel",
        provider=AgentRuntime.claude_code,
    )
    assert len(runner.calls) == 1  # fresh baseline captured at run start

    await _wait_for(lambda: len(clock.sleeps) == 1)  # live loop parked on sleep
    finalize_task = asyncio.ensure_future(ctx.finalize(status="cancelled"))
    await _wait_for(lambda: len(runner.calls) == 2)  # final sample blocked in run_streaming

    finalize_task.cancel()  # cancel the agent run mid-finalize
    for _ in range(5):
        await asyncio.sleep(0)
    runner.release.set()  # let the shielded final sample complete
    await finalize_task

    snap = read_latest_usage_snapshot("ws_cancel", work_dir=tmp_path)
    assert snap is not None
    assert snap.phase == "final"
    assert snap.total_tokens == 40  # 50 - 10 baseline


@pytest.mark.unit
async def test_final_write_drains_inflight_live_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A live-sample snapshot write runs in a worker thread that can't be
    # cancelled. If finalize cancelled the loop and raced ahead to its own final
    # write while that live write was still mid-rename, the two renames would race
    # and a late live rename could clobber the final snapshot (the store is
    # latest-wins). finalize must instead drain the in-flight live write first, so
    # the final write is never even started until the live write has landed.
    real_write = usage_collection.write_usage_snapshot
    block_live = threading.Event()  # enabled once the start-time seed write is done
    live_entered = threading.Event()  # worker signals it is blocked mid live write
    release_live = threading.Event()  # test releases the blocked live write
    final_started = threading.Event()  # worker signals the final write has begun

    def instrumented_write(snapshot: UsageSnapshot, *, work_dir: Path) -> Path:
        if snapshot.phase == "live" and block_live.is_set():
            live_entered.set()
            release_live.wait()
        if snapshot.phase == "final":
            final_started.set()
        return real_write(snapshot, work_dir=work_dir)

    monkeypatch.setattr(usage_collection, "write_usage_snapshot", instrumented_write)

    clock = FakeClock()
    runner = _ccusage_runner(
        json.dumps({"totals": {"totalTokens": 2}}),  # baseline (+ start-time seed write)
        json.dumps({"totals": {"totalTokens": 9}}),  # live sample
        json.dumps({"totals": {"totalTokens": 12}}),  # final sample
    )
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=clock)
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_drain",
        provider=AgentRuntime.claude_code,
    )

    # The seed write already landed during start(); only block the live sample.
    block_live.set()
    await _wait_for(lambda: len(clock.sleeps) == 1)
    clock.tick()
    await _wait_for(live_entered.is_set)  # live write is now blocked in its worker thread

    finalize_task = asyncio.ensure_future(ctx.finalize(status="success"))
    await _wait_for(lambda: ctx._task is not None and ctx._task.done())  # loop cancelled

    # The default executor has many workers, so without draining the final write
    # would run on its own thread and race the blocked live write's rename. Give
    # finalize ample yields: the final write must not start while the live write
    # is still in flight (each sleep lets finalize/the worker threads progress).
    for _ in range(50):
        await asyncio.sleep(0.002)
        assert not final_started.is_set(), "final write started before the live write drained"
    assert not finalize_task.done()  # blocked draining the in-flight live write

    release_live.set()  # let the in-flight live write finish its rename
    await finalize_task

    snap = read_latest_usage_snapshot("ws_drain", work_dir=tmp_path)
    assert snap is not None
    # The final write ran only after the live write landed, so it wins latest-wins.
    assert snap.phase == "final"
    assert snap.run_status == "success"
    assert snap.total_tokens == 10  # final 12 - baseline 2


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


@pytest.mark.unit
async def test_safe_write_reading_logs_non_cancel_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    collector = CcusageCollector(runner=FakeCommandRunner(), work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_safe_write_error",
        provider=AgentRuntime.claude_code,
    )

    async def _raise_write(**_kwargs: object) -> None:
        raise RuntimeError("write failed")

    monkeypatch.setattr(ctx, "_write_reading", _raise_write)
    await ctx._safe_write_reading(
        usage=None,
        reason="unavailable",
        model=None,
        phase="live",
        run_status="running",
    )
    await ctx.finalize(status="failed")


@pytest.mark.unit
async def test_timeout_cleanup_logs_non_cancel_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_cleanup_error",
        provider=AgentRuntime.claude_code,
    )

    async def _raise_cleanup(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(usage_collection, "cleanup_compose_exec_invocation", _raise_cleanup)
    invocation = usage_collection.TrackedComposeExec(
        args=["docker", "compose", "exec"],
        invocation_id="inv",
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        service="agent",
        workdir="/workspace",
        source="usage",
        label="awf=usage",
        wrapper_script="wrapper",
        cleanup_script="cleanup",
    )
    await ctx._cleanup_timed_out_invocation(invocation)
    await ctx.finalize(status="failed")


@pytest.mark.unit
async def test_timeout_cleanup_reraises_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_cleanup_cancel",
        provider=AgentRuntime.claude_code,
    )

    async def _raise_cleanup(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(usage_collection, "cleanup_compose_exec_invocation", _raise_cleanup)
    invocation = usage_collection.TrackedComposeExec(
        args=["docker", "compose", "exec"],
        invocation_id="inv",
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        service="agent",
        workdir="/workspace",
        source="usage",
        label="awf=usage",
        wrapper_script="wrapper",
        cleanup_script="cleanup",
    )

    with pytest.raises(asyncio.CancelledError):
        await ctx._cleanup_timed_out_invocation(invocation)

    await ctx.finalize(status="failed")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (CommandResult(returncode=127, stdout="", stderr=""), True),
        # The exact "command not found" shell phrase counts even on a non-127 exit.
        (CommandResult(returncode=1, stdout="", stderr="bash: ccusage: command not found"), True),
        # A non-127 "no such file" is NOT a missing binary: a real missing binary
        # exits 127 (covered above), whereas ccusage (Node) emits this phrase for
        # ENOENT on a missing config/data file while the binary is present, so it
        # must stay REASON_COMMAND_FAILED rather than REASON_UNAVAILABLE.
        (
            CommandResult(
                returncode=1,
                stdout="",
                stderr="Error: ENOENT: no such file or directory, open "
                "'/opt/awf/ccusage-neutral.json'",
            ),
            False,
        ),
        (CommandResult(returncode=1, stdout="", stderr="boom"), False),
        # A bare "<x> not found" on stderr is an app-level error (e.g. ccusage
        # "source not found"), not a missing binary — a real missing binary
        # exits 127 (covered above).
        (CommandResult(returncode=1, stdout="", stderr="source not found"), False),
        # App-level "not found" in stdout (non-127, clean stderr) is a command
        # failure, not a missing binary.
        (CommandResult(returncode=1, stdout="usage record not found", stderr=""), False),
    ],
)
def test_is_missing_binary(result: CommandResult, expected: bool) -> None:
    assert _is_missing_binary(result) is expected


@pytest.mark.unit
async def test_real_clock_defaults(tmp_path: Path) -> None:
    # Constructing without a clock uses the real clock.
    collector = CcusageCollector(runner=FakeCommandRunner(), work_dir=tmp_path)
    assert isinstance(collector._clock, _RealClock)
    assert isinstance(collector._clock.now(), datetime)
    await collector._clock.sleep(0)
