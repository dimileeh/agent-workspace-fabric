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


class _EventRunner:
    def __init__(self, events: list[str], *, result: CommandResult, cancel: bool = False) -> None:
        self._events = events
        self._result = result
        self._cancel = cancel
        self.calls: list[list[str]] = []

    async def run(self, args: list[str], **_kwargs: Any) -> CommandResult:
        # Targeted cleanup on timeout/cancellation funnels through here.
        self._events.append("cleanup")
        self.calls.append(list(args))
        return CommandResult(returncode=0, stdout="cleanup ok", stderr="")

    async def run_streaming(self, args: list[str], **_kwargs: Any) -> CommandResult:
        self._events.append("agent")
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
