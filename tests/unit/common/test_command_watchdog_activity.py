"""Idle watchdog liveness = output OR injected activity probe (issue #932).

A print-mode coding CLI (Claude Code runs with ``-p``) emits nothing until it
finishes, so ``last_output_at`` alone turns ``agent_idle_timeout_seconds`` into a
blind cap on an otherwise healthy run. ``run_streaming`` therefore accepts an
optional activity probe: when the *idle* deadline is reached the probe is asked
whether anything changed since the previous probe, and a positive answer resets
the idle clock. Uncertainty fails open — a probe that raises or answers ``None``
extends the window too, because "could not tell" is not "nothing moved". Only a
probe that positively reports no activity lets the idle timeout fire. The
**wall** deadline is never extended — it stays the hard cap.
"""

from __future__ import annotations

import asyncio
import sys

import pytest
import structlog

from awf.common.commands import (
    COMMAND_IDLE_TIMEOUT_REASON,
    COMMAND_TIMEOUT_REASON,
    AsyncioSubprocessRunner,
    FakeCommandRunner,
    _timeout_diagnostic,
)

_SILENT_CHILD = "import time; time.sleep(10)"
_QUICK_SILENT_CHILD = "import time; time.sleep(0.6)"
# Silent long enough for the idle deadline to fire, then speaks once and hushes.
_LATE_TALKER = "import time; time.sleep(0.3); print('alive', flush=True); time.sleep(10)"


@pytest.mark.unit
async def test_idle_watchdog_does_not_fire_while_the_probe_reports_activity() -> None:
    """A silent child that keeps changing the worktree is not idle."""
    runner = AsyncioSubprocessRunner()
    probe_calls = 0

    async def _probe() -> bool:
        nonlocal probe_calls
        probe_calls += 1
        return True

    with structlog.testing.capture_logs() as captured:
        result = await runner.run_streaming(
            [sys.executable, "-c", _QUICK_SILENT_CHILD],
            wall_timeout_seconds=30.0,
            idle_timeout_seconds=0.15,
            activity_probe=_probe,
        )

    assert result.returncode == 0
    assert result.reason_code is None
    assert probe_calls >= 1
    extensions = [
        entry
        for entry in captured
        if entry.get("event") == "command.idle_watchdog.activity_extended"
    ]
    assert extensions
    assert extensions[0]["extensions"] == 1


@pytest.mark.unit
async def test_idle_watchdog_fires_when_the_probe_reports_no_activity() -> None:
    """No output AND no worktree change is still idle."""
    runner = AsyncioSubprocessRunner()
    probe_calls = 0

    async def _probe() -> bool:
        nonlocal probe_calls
        probe_calls += 1
        return False

    result = await runner.run_streaming(
        [sys.executable, "-c", _SILENT_CHILD],
        wall_timeout_seconds=30.0,
        idle_timeout_seconds=0.2,
        activity_probe=_probe,
    )

    assert result.returncode == 124
    assert result.reason_code == COMMAND_IDLE_TIMEOUT_REASON
    assert probe_calls == 1
    assert "idle timeout after 0.2s without output or worktree activity" in result.stderr


@pytest.mark.unit
async def test_output_arriving_during_the_probe_defers_the_idle_timeout() -> None:
    """A scan is not instantaneous; output that lands during it still counts.

    The worktree probe runs off the event loop, so on a large tree the child can
    emit stdout while the scan is in flight. ``_read_stream`` updates the idle
    clock, so the watchdog must re-read it after the await instead of killing a
    run that just spoke.
    """
    runner = AsyncioSubprocessRunner()
    spoke = asyncio.Event()
    probe_calls = 0

    async def _probe() -> bool:
        nonlocal probe_calls
        probe_calls += 1
        # Stands in for a scan slow enough to be overtaken by the child.
        await spoke.wait()
        return False

    def _on_stdout(text: str) -> None:
        spoke.set()

    result = await runner.run_streaming(
        [sys.executable, "-c", _LATE_TALKER],
        on_stdout=_on_stdout,
        wall_timeout_seconds=30.0,
        idle_timeout_seconds=0.2,
        activity_probe=_probe,
    )

    assert "alive" in result.stdout
    # The first probe was overtaken by the child's output, so the run survived
    # it; the second — with the child genuinely silent — ended it.
    assert probe_calls == 2
    assert result.returncode == 124
    assert result.reason_code == COMMAND_IDLE_TIMEOUT_REASON


@pytest.mark.unit
async def test_wall_timeout_is_never_extended_by_the_activity_probe() -> None:
    """The hard cap stays hard even while the probe keeps reporting activity."""
    runner = AsyncioSubprocessRunner()

    def _probe() -> bool:
        return True

    result = await runner.run_streaming(
        [sys.executable, "-c", _SILENT_CHILD],
        wall_timeout_seconds=0.5,
        idle_timeout_seconds=0.1,
        activity_probe=_probe,
    )

    assert result.returncode == 124
    assert result.reason_code == COMMAND_TIMEOUT_REASON
    assert "wall timeout after 0.5s" in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize("error", [OSError("scandir failed"), TimeoutError("probe stalled")])
async def test_probe_failure_fails_open_and_warns(error: Exception) -> None:
    """A probe that cannot answer must not license killing a live run (#932).

    The probe failing says nothing about the child, so the idle window is
    extended and the non-answer is warned about. The wall deadline still ends
    the run, so an always-failing probe cannot make the process immortal.
    """
    runner = AsyncioSubprocessRunner()

    async def _probe() -> bool:
        raise error

    with structlog.testing.capture_logs() as captured:
        result = await runner.run_streaming(
            [sys.executable, "-c", _SILENT_CHILD],
            wall_timeout_seconds=0.6,
            idle_timeout_seconds=0.15,
            activity_probe=_probe,
        )

    assert result.returncode == 124
    assert result.reason_code == COMMAND_TIMEOUT_REASON
    failures = [
        entry
        for entry in captured
        if entry.get("event") == "command.idle_watchdog.activity_probe_failed"
    ]
    assert failures
    assert failures[0]["exc_type"] == type(error).__name__
    extensions = [
        entry
        for entry in captured
        if entry.get("event") == "command.idle_watchdog.activity_unknown_extended"
    ]
    assert extensions
    assert extensions[0]["extensions"] == 1


@pytest.mark.unit
async def test_unknown_probe_answer_extends_the_idle_window() -> None:
    """``None`` — the truncated-walk answer — counts as activity, not idleness."""
    runner = AsyncioSubprocessRunner()
    calls = 0

    def _probe() -> bool | None:
        nonlocal calls
        calls += 1
        return None if calls < 2 else False

    with structlog.testing.capture_logs() as captured:
        result = await runner.run_streaming(
            [sys.executable, "-c", _SILENT_CHILD],
            wall_timeout_seconds=30.0,
            idle_timeout_seconds=0.15,
            activity_probe=_probe,
        )

    assert result.returncode == 124
    assert result.reason_code == COMMAND_IDLE_TIMEOUT_REASON
    # The unknown answer bought exactly one more window; the definite "no
    # activity" that followed fired the timeout.
    assert calls == 2
    unknown = [
        entry
        for entry in captured
        if entry.get("event") == "command.idle_watchdog.activity_unknown_extended"
    ]
    assert len(unknown) == 1
    assert unknown[0]["extensions"] == 1


@pytest.mark.unit
async def test_sync_probe_return_value_is_accepted() -> None:
    """A plain (non-awaitable) probe result is honoured too."""
    runner = AsyncioSubprocessRunner()
    calls = 0

    def _probe() -> bool:
        nonlocal calls
        calls += 1
        return calls < 2

    result = await runner.run_streaming(
        [sys.executable, "-c", _SILENT_CHILD],
        wall_timeout_seconds=30.0,
        idle_timeout_seconds=0.15,
        activity_probe=_probe,
    )

    assert result.returncode == 124
    assert result.reason_code == COMMAND_IDLE_TIMEOUT_REASON
    assert calls == 2


@pytest.mark.unit
async def test_no_probe_keeps_the_existing_idle_diagnostic_wording() -> None:
    """Without a probe the runner behaves exactly as before."""
    runner = AsyncioSubprocessRunner()

    result = await runner.run_streaming(
        [sys.executable, "-c", _SILENT_CHILD],
        wall_timeout_seconds=30.0,
        idle_timeout_seconds=0.2,
    )

    assert result.reason_code == COMMAND_IDLE_TIMEOUT_REASON
    assert result.stderr == "command idle timeout after 0.2s without output\n"


@pytest.mark.unit
def test_timeout_diagnostic_mentions_worktree_activity_only_with_a_probe() -> None:
    assert (
        _timeout_diagnostic(
            COMMAND_IDLE_TIMEOUT_REASON,
            wall_timeout_seconds=None,
            idle_timeout_seconds=5.0,
        )
        == "command idle timeout after 5s without output\n"
    )
    assert (
        _timeout_diagnostic(
            COMMAND_IDLE_TIMEOUT_REASON,
            wall_timeout_seconds=None,
            idle_timeout_seconds=5.0,
            activity_probe_enabled=True,
        )
        == "command idle timeout after 5s without output or worktree activity\n"
    )


@pytest.mark.unit
async def test_fake_command_runner_accepts_the_activity_probe_kwarg() -> None:
    """The test double mirrors the production streaming signature."""
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="done\n")

    def _probe() -> bool:  # pragma: no cover - never invoked by the fake
        raise AssertionError("the fake must not run the probe")

    result = await runner.run_streaming(["echo", "done"], activity_probe=_probe)

    assert result.returncode == 0
    assert result.stdout == "done\n"
