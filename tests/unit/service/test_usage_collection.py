"""Unit tests for the ccusage usage collector (no real docker, no real CLI)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from awf.common.commands import COMMAND_TIMEOUT_REASON, CommandResult, FakeCommandRunner
from awf.db.enums import AgentRuntime
from awf.service import usage_collection
from awf.service.usage_collection import CcusageCollector, _is_missing_binary, _RealClock
from awf.service.usage_store import (
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
    """``run_streaming`` records the call then blocks until ``release`` is set."""

    def __init__(self, result: CommandResult) -> None:
        self.calls: list[list[str]] = []
        self.release = asyncio.Event()
        self._result = result

    async def run(self, args: list[str], **_kwargs: Any) -> CommandResult:  # pragma: no cover
        raise AssertionError("run() should not be called")

    async def run_streaming(self, args: list[str], **_kwargs: Any) -> CommandResult:
        self.calls.append(list(args))
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
        await asyncio.sleep(0)
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
    assert args[-5:] == ["ccusage", source, "daily", "--json", "--offline"]


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
        "output_tokens": 2,
        "total_tokens": 5,
        "cost_estimate": None,
        "currency": None,
        "model": None,
    }


@pytest.mark.unit
async def test_persisted_baseline_reused_across_runs(tmp_path: Path) -> None:
    # A prior snapshot already anchored a baseline for this workspace.
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
    runner = _ccusage_runner(json.dumps({"totals": {"totalTokens": 130}}))
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
    # delta uses the *persisted* baseline (100), not a fresh one: 130 - 100 = 30.
    assert snap.total_tokens == 30
    # The final sample was the only ccusage call (baseline reused, no extra call).
    assert len(runner.calls) == 1


@pytest.mark.unit
async def test_baseline_not_reused_when_provider_changed(tmp_path: Path) -> None:
    # A workspace can switch agents in place (provider recovery fallback), so a
    # prior baseline anchored for a different provider/source must not be reused:
    # subtracting it against an unrelated ccusage source would skew the delta.
    write_usage_snapshot(
        UsageSnapshot(
            workspace_id="ws_switch",
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
    assert snap.total_tokens == 7  # fresh baseline 10, final 17
    assert len(runner.calls) == 2  # fresh baseline + final, not a single reused call


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
async def test_failed_baseline_does_not_leak_host_usage(tmp_path: Path) -> None:
    # Baseline capture times out, then the final read succeeds with a large total
    # that includes copied host history. Without a trustworthy baseline we must
    # report unavailable rather than subtracting against nothing and leaking it.
    runner = FakeCommandRunner()
    runner.queue_result(returncode=124, stdout="", stderr="", reason_code=COMMAND_TIMEOUT_REASON)
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
    await _wait_for(lambda: len(clock.sleeps) == 1)
    clock.tick()
    await _wait_for(lambda: len(runner.calls) == 2)

    snap = read_latest_usage_snapshot("ws_live", work_dir=tmp_path)
    assert snap is not None
    assert snap.phase == "live"
    assert snap.total_tokens == 7  # 9 - 2 baseline

    await ctx.finalize(status="success")


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
            CommandResult(returncode=1, stdout="", stderr="sh: ccusage: not found"),
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
    write_usage_snapshot(
        UsageSnapshot(
            workspace_id="ws_cancel",
            provider="claude_code",
            ccusage_source="claude",
            status="available",
            phase="final",
            captured_at="2026-05-22T00:00:00+00:00",
            baseline={"total_tokens": 10},
        ),
        work_dir=tmp_path,
    )
    runner = _BlockingRunner(
        CommandResult(returncode=0, stdout=json.dumps({"totals": {"totalTokens": 50}}), stderr="")
    )
    clock = FakeClock()
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=clock)
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_cancel",
        provider=AgentRuntime.claude_code,
    )
    assert runner.calls == []  # baseline reused from prior snapshot

    await _wait_for(lambda: len(clock.sleeps) == 1)  # live loop parked on sleep
    finalize_task = asyncio.ensure_future(ctx.finalize(status="cancelled"))
    await _wait_for(lambda: len(runner.calls) == 1)  # final sample blocked in run_streaming

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
    write_usage_snapshot(
        UsageSnapshot(
            workspace_id="ws_lcancel",
            provider="claude_code",
            ccusage_source="claude",
            status="available",
            phase="final",
            captured_at="2026-05-22T00:00:00+00:00",
            baseline={"total_tokens": 1},
        ),
        work_dir=tmp_path,
    )
    runner = _BlockingRunner(
        CommandResult(returncode=0, stdout=json.dumps({"totals": {"totalTokens": 9}}), stderr="")
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
    await _wait_for(lambda: len(runner.calls) == 1)  # live sample blocked in run_streaming

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
    assert read_latest_usage_snapshot("ws_raise", work_dir=tmp_path) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (CommandResult(returncode=127, stdout="", stderr=""), True),
        (CommandResult(returncode=1, stdout="", stderr="ccusage: not found"), True),
        (CommandResult(returncode=1, stdout="", stderr="boom"), False),
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
