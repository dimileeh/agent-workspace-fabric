"""Tests for usage-sampling wiring in ``AgentAdapter.run``.

The adapter must drive the injected ``UsageSampler`` so the final sample is
recorded in every exit path (success, failure, timeout, cancellation) without
ever masking the agent outcome.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from awf.adapters import base as base_module
from awf.adapters.base import AgentRunError
from awf.adapters.codex import CodexAdapter
from awf.common.commands import COMMAND_TIMEOUT_REASON, CommandResult
from awf.db.enums import AgentRuntime

_COMPOSE_FILE = Path("/fake/compose.yml")


class _RecordingContext:
    def __init__(self, events: list[str], *, finalize_error: Exception | None = None) -> None:
        self._events = events
        self._finalize_error = finalize_error

    async def finalize(self, *, status: str) -> None:
        self._events.append(f"finalize:{status}")
        if self._finalize_error is not None:
            raise self._finalize_error


class _RecordingSampler:
    def __init__(
        self,
        events: list[str],
        *,
        start_error: Exception | None = None,
        finalize_error: Exception | None = None,
    ) -> None:
        self._events = events
        self._start_error = start_error
        self._finalize_error = finalize_error
        self.start_kwargs: dict[str, Any] | None = None

    async def start(self, **kwargs: Any) -> _RecordingContext:
        self._events.append("start")
        self.start_kwargs = kwargs
        if self._start_error is not None:
            raise self._start_error
        return _RecordingContext(self._events, finalize_error=self._finalize_error)


class _IsolatedRecordingContext(_RecordingContext):
    """Test double used by the surrounding scenario."""

    def __init__(
        self,
        events: list[str],
        *,
        capture_error: Exception | None = None,
        baseline_error: Exception | None = None,
        agent_completion_marker: str | None = None,
    ) -> None:
        """Initialize this test double."""
        super().__init__(events)
        self._capture_error = capture_error
        self._baseline_error = baseline_error
        self._agent_completion_marker = agent_completion_marker

    async def capture_final_before_cleanup(self, *, container_name: str) -> None:
        """Exercise the capture_final_before_cleanup test helper."""
        self._events.append(f"capture:{container_name}")
        if self._capture_error is not None:
            raise self._capture_error

    async def capture_baseline_before_agent(
        self, *, invocation: base_module.TrackedIsolatedComposeRun
    ) -> None:
        """Record the standalone baseline probe before the agent invocation."""
        assert "capture-baseline" in invocation.args
        self._events.append("baseline")
        if self._baseline_error is not None:
            raise self._baseline_error

    @property
    def baseline_cli_args(self) -> list[str]:
        """Return the standalone baseline probe command."""
        return ["capture-baseline"]

    @property
    def cli_args(self) -> list[str]:
        """Return the configured command-line arguments."""
        return ["agent-cli"]

    @property
    def volume_binds(self) -> tuple[tuple[Path, str], ...]:
        """Return the configured isolated-capture volume bindings."""
        return ((Path("/tmp/awf-usage-capture"), "/tmp/awf-ccusage"),)

    @property
    def agent_completion_marker(self) -> str | None:
        """Return the test-only marker emitted after the agent command exits."""
        return self._agent_completion_marker


class _IsolatedRecordingSampler(_RecordingSampler):
    """Test double used by the surrounding scenario."""

    def __init__(
        self,
        events: list[str],
        *,
        start_error: Exception | None = None,
        capture_error: Exception | None = None,
        baseline_error: Exception | None = None,
        agent_completion_marker: str | None = None,
    ) -> None:
        """Initialize this test double."""
        super().__init__(events)
        self._isolated_start_error = start_error
        self._capture_error = capture_error
        self._baseline_error = baseline_error
        self._agent_completion_marker = agent_completion_marker

    async def start_isolated(self, **kwargs: Any) -> _IsolatedRecordingContext:
        """Exercise the start_isolated test helper."""
        self._events.append("start_isolated")
        self.start_kwargs = kwargs
        if self._isolated_start_error is not None:
            raise self._isolated_start_error
        return _IsolatedRecordingContext(
            self._events,
            capture_error=self._capture_error,
            baseline_error=self._baseline_error,
            agent_completion_marker=self._agent_completion_marker,
        )


class _DelayedIsolatedRecordingSampler(_RecordingSampler):
    """Simulate an isolated capture worker that keeps running after cancellation."""

    def __init__(self, events: list[str], *, start_error: Exception | None = None) -> None:
        """Initialize this test double."""
        super().__init__(events)
        self._isolated_start_error = start_error
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def start_isolated(self, **kwargs: Any) -> _IsolatedRecordingContext:
        """Exercise the start_isolated test helper."""
        self._events.append("start_isolated")
        self.start_kwargs = kwargs

        async def _complete_capture_setup() -> _IsolatedRecordingContext:
            """Exercise the _complete_capture_setup test helper."""
            self.started.set()
            await self.release.wait()
            if self._isolated_start_error is not None:
                raise self._isolated_start_error
            return _IsolatedRecordingContext(self._events)

        return await asyncio.shield(asyncio.create_task(_complete_capture_setup()))


class _EventRunner:
    def __init__(self, events: list[str], *, result: CommandResult, cancel: bool = False) -> None:
        self._events = events
        self._result = result
        self._cancel = cancel
        self.calls: list[list[str]] = []
        self.streaming_calls: list[list[str]] = []

    async def run(self, args: list[str], **_kwargs: Any) -> CommandResult:
        # Targeted cleanup on timeout/cancellation funnels through here.
        self._events.append("cleanup")
        self.calls.append(list(args))
        return CommandResult(returncode=0, stdout="cleanup ok", stderr="")

    async def run_streaming(self, args: list[str], **_kwargs: Any) -> CommandResult:
        self._events.append("agent")
        self.streaming_calls.append(list(args))
        if self._cancel:
            raise asyncio.CancelledError
        return self._result


@pytest.mark.unit
async def test_sampler_started_before_agent_and_finalized_on_success() -> None:
    events: list[str] = []
    sampler = _RecordingSampler(events)
    runner = _EventRunner(events, result=CommandResult(returncode=0, stdout="ok", stderr=""))
    adapter = CodexAdapter(runner=runner, usage_sampler=sampler)

    result = await adapter.run(
        compose_project="proj",
        compose_file=_COMPOSE_FILE,
        prompt="do work",
        workspace_id="ws_ok",
    )

    assert result.ok
    assert events == ["start", "agent", "finalize:success"]
    assert sampler.start_kwargs is not None
    assert sampler.start_kwargs["provider"] is AgentRuntime.codex
    assert sampler.start_kwargs["compose_project"] == "proj"
    assert sampler.start_kwargs["workspace_id"] == "ws_ok"


@pytest.mark.unit
async def test_isolated_sampler_captures_usage_inside_clarification_container() -> None:
    """Verify isolated sampler captures usage inside clarification container."""
    events: list[str] = []
    sampler = _IsolatedRecordingSampler(events)
    runner = _EventRunner(events, result=CommandResult(returncode=0, stdout="ok", stderr=""))
    adapter = CodexAdapter(runner=runner, usage_sampler=sampler)

    result = await adapter.run(
        compose_project="proj",
        compose_file=_COMPOSE_FILE,
        prompt="do work",
        workspace_id="ws_isolated",
        isolated_worktree_host_path=Path("/worktrees/ws_isolated/reask"),
    )

    assert result.ok
    assert events == ["start_isolated", "baseline", "agent", "finalize:success"]
    assert sampler.start_kwargs is not None
    assert sampler.start_kwargs["provider"] is AgentRuntime.codex
    assert sampler.start_kwargs["cli_args"][:2] == ["codex", "exec"]
    args = runner.streaming_calls[0]
    assert "clarification" in args
    assert "/tmp/awf-usage-capture:/tmp/awf-ccusage:rw" in args
    assert "agent-cli" in args
    assert "capture-baseline" not in args


@pytest.mark.unit
async def test_isolated_final_usage_capture_is_not_charged_to_agent_timeout() -> None:
    """A final probe after CLI completion cannot turn success into a timeout."""

    marker = "\x1eagent-complete\x1f\n"
    events: list[str] = []

    class _FinalProbeRunner(_EventRunner):
        def __init__(self) -> None:
            super().__init__(events, result=CommandResult(returncode=0, stdout="", stderr=""))
            self.watchdog_timeouts: list[tuple[float | None, float | None]] = []

        async def run_streaming(
            self,
            args: list[str],
            *,
            on_stdout: Any = None,
            wall_timeout_seconds: float | None = None,
            idle_timeout_seconds: float | None = None,
            **_kwargs: Any,
        ) -> CommandResult:
            self._events.append("agent")
            self.streaming_calls.append(list(args))
            self.watchdog_timeouts.append((wall_timeout_seconds, idle_timeout_seconds))
            if wall_timeout_seconds is not None or idle_timeout_seconds is not None:
                return CommandResult(
                    returncode=124,
                    stdout="",
                    stderr="command wall timeout after 0.01s\n",
                    reason_code=COMMAND_TIMEOUT_REASON,
                )
            assert on_stdout is not None
            await on_stdout(marker[:7])
            await on_stdout(marker[7:])
            await asyncio.sleep(0.02)
            return CommandResult(returncode=0, stdout=marker, stderr="")

    sampler = _IsolatedRecordingSampler(events, agent_completion_marker=marker)
    runner = _FinalProbeRunner()
    adapter = CodexAdapter(
        runner=runner,
        usage_sampler=sampler,
        agent_wall_timeout_seconds=0.01,
        agent_idle_timeout_seconds=0.01,
    )

    result = await adapter.run(
        compose_project="proj",
        compose_file=_COMPOSE_FILE,
        prompt="do work",
        workspace_id="ws_isolated_final_usage_outside_timeout",
        isolated_worktree_host_path=Path(
            "/worktrees/ws_isolated_final_usage_outside_timeout/reask"
        ),
    )

    assert result.ok
    assert result.stdout == ""
    assert runner.watchdog_timeouts == [(None, None)]
    assert events == ["start_isolated", "baseline", "agent", "finalize:success"]


@pytest.mark.unit
async def test_isolated_agent_idle_timeout_precedes_completion_marker() -> None:
    """The dedicated watchdog still stops a silent CLI before it completes."""

    marker = "\x1eagent-complete\x1f"
    events: list[str] = []

    class _SilentAgentRunner(_EventRunner):
        def __init__(self) -> None:
            super().__init__(events, result=CommandResult(returncode=0, stdout="", stderr=""))
            self.cancelled = False
            self.watchdog_timeouts: list[tuple[float | None, float | None]] = []

        async def run_streaming(
            self,
            args: list[str],
            *,
            wall_timeout_seconds: float | None = None,
            idle_timeout_seconds: float | None = None,
            **_kwargs: Any,
        ) -> CommandResult:
            self._events.append("agent")
            self.streaming_calls.append(list(args))
            self.watchdog_timeouts.append((wall_timeout_seconds, idle_timeout_seconds))
            if wall_timeout_seconds is not None or idle_timeout_seconds is not None:
                return CommandResult(
                    returncode=124,
                    stdout="",
                    stderr="command idle timeout after 0.01s without output\n",
                    reason_code=base_module.COMMAND_IDLE_TIMEOUT_REASON,
                )
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    sampler = _IsolatedRecordingSampler(events, agent_completion_marker=marker)
    runner = _SilentAgentRunner()
    adapter = CodexAdapter(
        runner=runner,
        usage_sampler=sampler,
        agent_wall_timeout_seconds=1.0,
        agent_idle_timeout_seconds=0.01,
    )

    with pytest.raises(AgentRunError) as excinfo:
        await adapter.run(
            compose_project="proj",
            compose_file=_COMPOSE_FILE,
            prompt="do work",
            workspace_id="ws_isolated_idle_timeout",
            isolated_worktree_host_path=Path("/worktrees/ws_isolated_idle_timeout/reask"),
        )

    assert excinfo.value.reason_code == "AGENT_IDLE_TIMEOUT"
    assert runner.cancelled
    assert runner.watchdog_timeouts == [(None, None)]
    capture_event = next(event for event in events if event.startswith("capture:"))
    assert events.index(capture_event) < events.index("cleanup")
    assert events[-1] == "finalize:timeout"


@pytest.mark.unit
async def test_isolated_baseline_capture_failure_does_not_mask_agent_run() -> None:
    """A diagnostic baseline failure cannot prevent the clarification CLI from running."""
    events: list[str] = []
    sampler = _IsolatedRecordingSampler(events, baseline_error=RuntimeError("probe failed"))
    runner = _EventRunner(events, result=CommandResult(returncode=0, stdout="ok", stderr=""))
    adapter = CodexAdapter(runner=runner, usage_sampler=sampler)

    result = await adapter.run(
        compose_project="proj",
        compose_file=_COMPOSE_FILE,
        prompt="do work",
        workspace_id="ws_isolated_baseline_failure",
        isolated_worktree_host_path=Path("/worktrees/ws_isolated_baseline_failure/reask"),
    )

    assert result.ok
    assert events == ["start_isolated", "baseline", "agent", "finalize:success"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("sampler", "expected_events"),
    [
        (_RecordingSampler, ["agent"]),
        (
            lambda events: _IsolatedRecordingSampler(
                events, start_error=RuntimeError("isolated sampler down")
            ),
            ["start_isolated", "agent"],
        ),
    ],
)
async def test_isolated_run_does_not_fall_back_to_persistent_usage_sampling(
    sampler: Any, expected_events: list[str]
) -> None:
    """Verify isolated run does not fall back to persistent usage sampling."""
    events: list[str] = []
    runner = _EventRunner(events, result=CommandResult(returncode=0, stdout="ok", stderr=""))
    adapter = CodexAdapter(runner=runner, usage_sampler=sampler(events))

    result = await adapter.run(
        compose_project="proj",
        compose_file=_COMPOSE_FILE,
        prompt="do work",
        workspace_id="ws_isolated_sampling_unavailable",
        isolated_worktree_host_path=Path("/worktrees/ws_isolated_sampling_unavailable/reask"),
    )

    assert result.ok
    assert events == expected_events


@pytest.mark.unit
async def test_isolated_sampler_finalized_when_invocation_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify isolated sampler finalized when invocation construction fails."""
    events: list[str] = []
    sampler = _IsolatedRecordingSampler(events)
    runner = _EventRunner(events, result=CommandResult(returncode=0, stdout="ok", stderr=""))
    adapter = CodexAdapter(runner=runner, usage_sampler=sampler)

    def _fail_invocation_construction(**_kwargs: Any) -> None:
        """Simulate invocation construction for this test."""
        raise ValueError("invalid isolated invocation")

    monkeypatch.setattr(
        base_module, "build_isolated_tracked_compose_run", _fail_invocation_construction
    )

    with pytest.raises(ValueError, match="invalid isolated invocation"):
        await adapter.run(
            compose_project="proj",
            compose_file=_COMPOSE_FILE,
            prompt="do work",
            workspace_id="ws_isolated_builder_fail",
            isolated_worktree_host_path=Path("/worktrees/ws_isolated_builder_fail/reask"),
        )

    assert events == ["start_isolated", "finalize:failed"]


@pytest.mark.unit
async def test_isolated_sampler_finalized_when_startup_is_cancelled() -> None:
    """Verify isolated sampler finalized when startup is cancelled."""
    events: list[str] = []
    sampler = _DelayedIsolatedRecordingSampler(events)
    runner = _EventRunner(events, result=CommandResult(returncode=0, stdout="ok", stderr=""))
    adapter = CodexAdapter(runner=runner, usage_sampler=sampler)

    run_task = asyncio.create_task(
        adapter.run(
            compose_project="proj",
            compose_file=_COMPOSE_FILE,
            prompt="do work",
            workspace_id="ws_isolated_cancel",
            isolated_worktree_host_path=Path("/worktrees/ws_isolated_cancel/reask"),
        )
    )
    await sampler.started.wait()
    run_task.cancel()
    await asyncio.sleep(0)
    cancellation_waits_for_cleanup = not run_task.done()
    sampler.release.set()

    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert cancellation_waits_for_cleanup
    assert events == ["start_isolated", "finalize:cancelled"]


@pytest.mark.unit
async def test_isolated_sampler_startup_failure_does_not_mask_cancellation() -> None:
    """Verify isolated sampler startup failure does not mask cancellation."""
    events: list[str] = []
    sampler = _DelayedIsolatedRecordingSampler(
        events, start_error=RuntimeError("isolated sampler down")
    )
    runner = _EventRunner(events, result=CommandResult(returncode=0, stdout="ok", stderr=""))
    adapter = CodexAdapter(runner=runner, usage_sampler=sampler)

    run_task = asyncio.create_task(
        adapter.run(
            compose_project="proj",
            compose_file=_COMPOSE_FILE,
            prompt="do work",
            workspace_id="ws_isolated_cancel_failure",
            isolated_worktree_host_path=Path("/worktrees/ws_isolated_cancel_failure/reask"),
        )
    )
    await sampler.started.wait()
    run_task.cancel()
    await asyncio.sleep(0)
    sampler.release.set()

    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert events == ["start_isolated"]


@pytest.mark.unit
async def test_sampler_finalized_failed_on_agent_error() -> None:
    events: list[str] = []
    sampler = _RecordingSampler(events)
    runner = _EventRunner(events, result=CommandResult(returncode=1, stdout="", stderr="boom"))
    adapter = CodexAdapter(runner=runner, usage_sampler=sampler)

    with pytest.raises(AgentRunError):
        await adapter.run(
            compose_project="proj",
            compose_file=_COMPOSE_FILE,
            prompt="do work",
            workspace_id="ws_fail",
        )

    assert events[-1] == "finalize:failed"


@pytest.mark.unit
async def test_sampler_finalized_timeout_on_agent_timeout() -> None:
    events: list[str] = []
    sampler = _RecordingSampler(events)
    runner = _EventRunner(
        events,
        result=CommandResult(
            returncode=124, stdout="", stderr="", reason_code=COMMAND_TIMEOUT_REASON
        ),
    )
    adapter = CodexAdapter(runner=runner, usage_sampler=sampler)

    with pytest.raises(AgentRunError) as excinfo:
        await adapter.run(
            compose_project="proj",
            compose_file=_COMPOSE_FILE,
            prompt="do work",
            workspace_id="ws_timeout",
        )

    assert excinfo.value.reason_code == "AGENT_TIMEOUT"
    assert events[-1] == "finalize:timeout"


@pytest.mark.unit
async def test_isolated_timeout_captures_usage_before_forced_container_removal() -> None:
    """Verify isolated timeout captures usage before forced container removal."""
    events: list[str] = []
    sampler = _IsolatedRecordingSampler(events)
    runner = _EventRunner(
        events,
        result=CommandResult(
            returncode=124, stdout="", stderr="", reason_code=COMMAND_TIMEOUT_REASON
        ),
    )
    adapter = CodexAdapter(runner=runner, usage_sampler=sampler)

    with pytest.raises(AgentRunError) as excinfo:
        await adapter.run(
            compose_project="proj",
            compose_file=_COMPOSE_FILE,
            prompt="do work",
            workspace_id="ws_isolated_timeout_capture",
            isolated_worktree_host_path=Path("/worktrees/ws_isolated_timeout_capture/reask"),
        )

    assert excinfo.value.reason_code == "AGENT_TIMEOUT"
    capture_event = next(event for event in events if event.startswith("capture:"))
    assert events.index(capture_event) < events.index("cleanup")
    assert events[-1] == "finalize:timeout"


@pytest.mark.unit
async def test_isolated_cancellation_captures_usage_before_forced_container_removal() -> None:
    """Verify isolated cancellation captures usage before forced container removal."""
    events: list[str] = []
    sampler = _IsolatedRecordingSampler(events)
    runner = _EventRunner(
        events, result=CommandResult(returncode=0, stdout="", stderr=""), cancel=True
    )
    adapter = CodexAdapter(runner=runner, usage_sampler=sampler)

    with pytest.raises(asyncio.CancelledError):
        await adapter.run(
            compose_project="proj",
            compose_file=_COMPOSE_FILE,
            prompt="do work",
            workspace_id="ws_isolated_cancel_capture",
            isolated_worktree_host_path=Path("/worktrees/ws_isolated_cancel_capture/reask"),
        )

    capture_event = next(event for event in events if event.startswith("capture:"))
    assert events.index(capture_event) < events.index("cleanup")
    assert events[-1] == "finalize:cancelled"


@pytest.mark.unit
async def test_isolated_capture_failure_does_not_mask_timeout_or_container_removal() -> None:
    """Verify isolated capture failure does not mask timeout or container removal."""
    events: list[str] = []
    sampler = _IsolatedRecordingSampler(events, capture_error=RuntimeError("capture failed"))
    runner = _EventRunner(
        events,
        result=CommandResult(
            returncode=124, stdout="", stderr="", reason_code=COMMAND_TIMEOUT_REASON
        ),
    )
    adapter = CodexAdapter(runner=runner, usage_sampler=sampler)

    with pytest.raises(AgentRunError) as excinfo:
        await adapter.run(
            compose_project="proj",
            compose_file=_COMPOSE_FILE,
            prompt="do work",
            workspace_id="ws_isolated_capture_failure",
            isolated_worktree_host_path=Path("/worktrees/ws_isolated_capture_failure/reask"),
        )

    assert excinfo.value.reason_code == "AGENT_TIMEOUT"
    capture_event = next(event for event in events if event.startswith("capture:"))
    assert events.index(capture_event) < events.index("cleanup")
    assert events[-1] == "finalize:timeout"


@pytest.mark.unit
async def test_isolated_timeout_cancellation_during_capture_still_removes_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify isolated timeout cancellation during capture still removes container."""
    events: list[str] = []
    sampler = _IsolatedRecordingSampler(events)
    runner = _EventRunner(
        events,
        result=CommandResult(
            returncode=124, stdout="", stderr="", reason_code=COMMAND_TIMEOUT_REASON
        ),
    )
    adapter = CodexAdapter(runner=runner, usage_sampler=sampler)
    capture_started = asyncio.Event()

    async def _block_capture(_self: _IsolatedRecordingContext, *, container_name: str) -> None:
        """Block capture for this test."""
        events.append(f"capture:{container_name}")
        capture_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(_IsolatedRecordingContext, "capture_final_before_cleanup", _block_capture)

    run_task = asyncio.create_task(
        adapter.run(
            compose_project="proj",
            compose_file=_COMPOSE_FILE,
            prompt="do work",
            workspace_id="ws_isolated_timeout_capture_cancelled",
            isolated_worktree_host_path=Path(
                "/worktrees/ws_isolated_timeout_capture_cancelled/reask"
            ),
        )
    )
    await asyncio.wait_for(capture_started.wait(), timeout=0.2)
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    capture_event = next(event for event in events if event.startswith("capture:"))
    assert events.index(capture_event) < events.index("cleanup")
    assert events[-1] == "finalize:cancelled"


@pytest.mark.unit
async def test_isolated_error_cancellation_during_capture_still_removes_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify isolated error cancellation during capture still removes container."""
    events: list[str] = []
    sampler = _IsolatedRecordingSampler(events)
    runner = _EventRunner(events, result=CommandResult(returncode=1, stdout="", stderr="boom"))
    adapter = CodexAdapter(runner=runner, usage_sampler=sampler)
    capture_started = asyncio.Event()

    async def _fail_agent(*_args: Any, **_kwargs: Any) -> CommandResult:
        """Simulate agent for this test."""
        events.append("agent")
        raise RuntimeError("agent execution failed")

    async def _block_capture(_self: _IsolatedRecordingContext, *, container_name: str) -> None:
        """Block capture for this test."""
        events.append(f"capture:{container_name}")
        capture_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "run_streaming", _fail_agent)
    monkeypatch.setattr(_IsolatedRecordingContext, "capture_final_before_cleanup", _block_capture)

    run_task = asyncio.create_task(
        adapter.run(
            compose_project="proj",
            compose_file=_COMPOSE_FILE,
            prompt="do work",
            workspace_id="ws_isolated_error_capture_cancelled",
            isolated_worktree_host_path=Path(
                "/worktrees/ws_isolated_error_capture_cancelled/reask"
            ),
        )
    )
    await asyncio.wait_for(capture_started.wait(), timeout=0.2)
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    capture_event = next(event for event in events if event.startswith("capture:"))
    assert events.index(capture_event) < events.index("cleanup")
    assert events[-1] == "finalize:cancelled"


@pytest.mark.unit
async def test_sampler_finalized_cancelled_on_cancellation() -> None:
    events: list[str] = []
    sampler = _RecordingSampler(events)
    runner = _EventRunner(
        events, result=CommandResult(returncode=0, stdout="", stderr=""), cancel=True
    )
    adapter = CodexAdapter(runner=runner, usage_sampler=sampler)

    with pytest.raises(asyncio.CancelledError):
        await adapter.run(
            compose_project="proj",
            compose_file=_COMPOSE_FILE,
            prompt="do work",
            workspace_id="ws_cancel",
        )

    assert events[-1] == "finalize:cancelled"


@pytest.mark.unit
async def test_sampler_start_error_does_not_mask_result() -> None:
    events: list[str] = []
    sampler = _RecordingSampler(events, start_error=RuntimeError("sampler down"))
    runner = _EventRunner(events, result=CommandResult(returncode=0, stdout="ok", stderr=""))
    adapter = CodexAdapter(runner=runner, usage_sampler=sampler)

    result = await adapter.run(
        compose_project="proj",
        compose_file=_COMPOSE_FILE,
        prompt="do work",
        workspace_id="ws_start_err",
    )

    assert result.ok
    # start failed -> no context -> no finalize event, agent still ran.
    assert events == ["start", "agent"]


@pytest.mark.unit
async def test_finalization_reached_when_start_cancelled() -> None:
    # Cancellation during baseline capture makes CcusageCollector.start re-raise
    # CancelledError. Since that is a BaseException (not caught by
    # _start_usage_sampling's except-Exception guard), the call must sit inside
    # the run try/finally so the cancelled exit path still reaches finalization
    # (a no-op for the missing context) instead of escaping the guard.
    events: list[str] = []
    sampler = _RecordingSampler(events, start_error=asyncio.CancelledError())
    runner = _EventRunner(events, result=CommandResult(returncode=0, stdout="ok", stderr=""))
    adapter = CodexAdapter(runner=runner, usage_sampler=sampler)

    finalize_calls: list[tuple[Any, str]] = []
    original_finalize = adapter._finalize_usage_sampling

    async def _recording_finalize(
        sampler_ctx: Any, *, status: str, workspace_id: str | None
    ) -> None:
        finalize_calls.append((sampler_ctx, status))
        await original_finalize(sampler_ctx, status=status, workspace_id=workspace_id)

    adapter._finalize_usage_sampling = _recording_finalize  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await adapter.run(
            compose_project="proj",
            compose_file=_COMPOSE_FILE,
            prompt="do work",
            workspace_id="ws_cancel_start",
        )

    # start() raised before the agent ran, yet finalization still ran — with no
    # context (None) and the cancelled status.
    assert events == ["start"]
    assert finalize_calls == [(None, "cancelled")]


@pytest.mark.unit
async def test_sampler_finalize_error_does_not_mask_agent_error() -> None:
    events: list[str] = []
    sampler = _RecordingSampler(events, finalize_error=RuntimeError("finalize down"))
    runner = _EventRunner(events, result=CommandResult(returncode=1, stdout="", stderr="boom"))
    adapter = CodexAdapter(runner=runner, usage_sampler=sampler)

    # The agent failure must surface even though finalize raised.
    with pytest.raises(AgentRunError):
        await adapter.run(
            compose_project="proj",
            compose_file=_COMPOSE_FILE,
            prompt="do work",
            workspace_id="ws_fin_err",
        )
    assert events[-1] == "finalize:failed"


@pytest.mark.unit
async def test_no_sampler_runs_agent_without_sampling() -> None:
    events: list[str] = []
    runner = _EventRunner(events, result=CommandResult(returncode=0, stdout="ok", stderr=""))
    adapter = CodexAdapter(runner=runner)

    result = await adapter.run(
        compose_project="proj",
        compose_file=_COMPOSE_FILE,
        prompt="do work",
        workspace_id="ws_none",
    )

    assert result.ok
    assert events == ["agent"]


@pytest.mark.unit
async def test_sampler_skipped_without_workspace_id() -> None:
    events: list[str] = []
    sampler = _RecordingSampler(events)
    runner = _EventRunner(events, result=CommandResult(returncode=0, stdout="ok", stderr=""))
    adapter = CodexAdapter(runner=runner, usage_sampler=sampler)

    result = await adapter.run(
        compose_project="proj",
        compose_file=_COMPOSE_FILE,
        prompt="do work",
    )

    assert result.ok
    # No workspace_id -> sampler is not started.
    assert events == ["agent"]
