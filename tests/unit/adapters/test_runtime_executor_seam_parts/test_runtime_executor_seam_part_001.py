"""Hosted runtime seam: watchdog verdicts across log-sink failures (#934).

The hosted execution path flushes buffered output and then closes its log sinks
around the classification that turns a watchdog timeout into an
``AgentRunError``. Both hops await, so either can raise — or be cancelled — over
that timeout, and the verdict protocol would then rewind to the rollback floor
and delete the timed-out run's work.

Split out of ``tests/unit/adapters/test_runtime_executor_seam.py`` to keep each
test module under the first-party 1500-line maintainability guardrail.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import structlog

import awf.adapters.base as base_module
from awf.adapters.base import AgentRunError
from awf.adapters.codex import CodexAdapter
from awf.adapters.runtime_executor import AgentRuntimeExecResult
from awf.common.commands import COMMAND_TIMEOUT_REASON, FakeCommandRunner
from tests.unit.adapters.test_runtime_executor_seam import (
    _COMPOSE_FILE,
    _COMPOSE_PROJECT,
    _PROMPT,
    _RecordingExecutor,
    _RecordingSinks,
)


class _CloseFailingSinks(_RecordingSinks):
    """Sinks that raise while the hosted run's ``finally`` closes them."""

    async def close(self) -> None:
        """Raise the failure the log store hits on close."""
        self.closed = True
        raise RuntimeError("log sink close failure")


class _CloseCancellingSinks(_RecordingSinks):
    """Sinks cancelled while the hosted run's ``finally`` closes them."""

    async def close(self) -> None:
        """Raise the cancellation delivered while the close awaits."""
        self.closed = True
        raise asyncio.CancelledError


class _FlushFailingSinks(_RecordingSinks):
    """Sinks that raise when the adapter flushes the buffered hosted output."""

    async def write_stdout(self, data: str) -> None:
        """Raise the failure the log store hits on the post-run flush."""
        raise RuntimeError("log sink flush failure")


class _FlushCancellingSinks(_RecordingSinks):
    """Sinks cancelled when the adapter flushes the buffered hosted output."""

    async def write_stdout(self, data: str) -> None:
        """Raise the cancellation delivered while the flush awaits."""
        raise asyncio.CancelledError


class _SinkLogStore:
    """Log store handing out one prepared sink object."""

    def __init__(self, sinks: _RecordingSinks) -> None:
        """Store the sink the adapter will be given."""
        self.sinks = sinks

    async def open_command_streams(self, **_kwargs: Any) -> _RecordingSinks:
        """Return the prepared sink."""
        return self.sinks


class TestHostedTimeoutSurvivesLogSinkFailures:
    """Hosted watchdog verdicts must reach the caller through log-sink failures.

    The hosted path flushes buffered output and then closes its sinks around the
    classification that turns a watchdog timeout into an ``AgentRunError``. Both
    hops await, so either can raise — or be cancelled — over that timeout, and the
    verdict protocol's generic and cancellation branches then rewind to the
    rollback floor and delete the timed-out run's work. The local
    ``_run_agent_cli`` path already carries its classification across those hops;
    the hosted path must do the same (PRRT_kwDOSJAM6s6f-7fU).
    """

    @staticmethod
    def _timeout_adapter(sinks: _RecordingSinks) -> CodexAdapter:
        """Build a hosted adapter whose executor returns a watchdog timeout."""
        executor = _RecordingExecutor(
            result=AgentRuntimeExecResult(
                returncode=124,
                stdout="partial hosted stdout",
                stderr="hosted watchdog fired",
                timeout_reason=COMMAND_TIMEOUT_REASON,
            )
        )
        return CodexAdapter(
            runner=FakeCommandRunner(),
            default_model="gpt-5",
            log_store=_SinkLogStore(sinks),  # type: ignore[arg-type]
            runtime_executor=executor,
        )

    @pytest.mark.unit
    async def test_hosted_timeout_survives_a_failing_log_sink_close(self) -> None:
        sinks = _CloseFailingSinks()
        adapter = self._timeout_adapter(sinks)

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(AgentRunError) as exc,
        ):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_hosted_timeout_close_failed",
            )

        assert exc.value.reason_code == "AGENT_TIMEOUT"
        assert sinks.closed is True
        assert any(
            event.get("event") == "agent.run.timeout_log_close_failed"
            and event.get("reason_code") == "AGENT_TIMEOUT"
            and event.get("close_error") == "RuntimeError"
            and event.get("workspace_id") == "ws_hosted_timeout_close_failed"
            for event in captured
        )

    @pytest.mark.unit
    async def test_hosted_timeout_tags_a_cancelled_log_sink_close(self) -> None:
        sinks = _CloseCancellingSinks()
        adapter = self._timeout_adapter(sinks)

        with pytest.raises(asyncio.CancelledError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_hosted_timeout_close_cancelled",
            )

        assert getattr(exc.value, "agent_reason_code", None) == "AGENT_TIMEOUT"

    @pytest.mark.unit
    async def test_hosted_idle_timeout_tags_a_cancelled_log_sink_close(self) -> None:
        from awf.common.commands import COMMAND_IDLE_TIMEOUT_REASON

        executor = _RecordingExecutor(
            result=AgentRuntimeExecResult(
                returncode=124,
                stdout="partial",
                stderr="hosted idle watchdog fired",
                timeout_reason=COMMAND_IDLE_TIMEOUT_REASON,
            )
        )
        sinks = _CloseCancellingSinks()
        adapter = CodexAdapter(
            runner=FakeCommandRunner(),
            default_model="gpt-5",
            log_store=_SinkLogStore(sinks),  # type: ignore[arg-type]
            runtime_executor=executor,
        )

        with pytest.raises(asyncio.CancelledError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_hosted_idle_timeout_close_cancelled",
            )

        assert getattr(exc.value, "agent_reason_code", None) == "AGENT_IDLE_TIMEOUT"

    @pytest.mark.unit
    async def test_hosted_failure_leaves_the_close_failure_in_place(self) -> None:
        """A non-timeout hosted failure is no watchdog verdict to protect."""
        executor = _RecordingExecutor(
            result=AgentRuntimeExecResult(
                returncode=1,
                stdout="",
                stderr="hosted agent exploded",
            )
        )
        sinks = _CloseFailingSinks()
        adapter = CodexAdapter(
            runner=FakeCommandRunner(),
            default_model="gpt-5",
            log_store=_SinkLogStore(sinks),  # type: ignore[arg-type]
            runtime_executor=executor,
        )

        with pytest.raises(RuntimeError, match="log sink close failure"):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_hosted_failure_close_failed",
            )

    @pytest.mark.unit
    async def test_hosted_timeout_survives_a_failing_buffered_flush(self) -> None:
        """The pre-classification flush cannot escape ahead of the timeout either."""
        sinks = _FlushFailingSinks()
        adapter = self._timeout_adapter(sinks)

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(AgentRunError) as exc,
        ):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_hosted_timeout_flush_failed",
            )

        assert exc.value.reason_code == "AGENT_TIMEOUT"
        assert exc.value.result.stderr == "hosted watchdog fired"
        assert sinks.closed is True
        assert any(
            event.get("event") == "agent.run.hosted.timeout_log_flush_failed"
            and event.get("reason_code") == "AGENT_TIMEOUT"
            and event.get("flush_error") == "RuntimeError"
            and event.get("workspace_id") == "ws_hosted_timeout_flush_failed"
            for event in captured
        )

    @pytest.mark.unit
    async def test_hosted_timeout_tags_a_cancelled_buffered_flush(self) -> None:
        sinks = _FlushCancellingSinks()
        adapter = self._timeout_adapter(sinks)

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(asyncio.CancelledError) as exc,
        ):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_hosted_timeout_flush_cancelled",
            )

        assert getattr(exc.value, "agent_reason_code", None) == "AGENT_TIMEOUT"
        assert any(
            event.get("event") == "agent.run.hosted.timeout_log_flush_cancelled"
            and event.get("reason_code") == "AGENT_TIMEOUT"
            and event.get("workspace_id") == "ws_hosted_timeout_flush_cancelled"
            for event in captured
        )

    @pytest.mark.unit
    async def test_hosted_flush_failure_surfaces_without_a_watchdog_verdict(self) -> None:
        """With nothing classified in flight the flush failure is the run's outcome."""
        executor = _RecordingExecutor(
            result=AgentRuntimeExecResult(returncode=0, stdout="hosted stdout", stderr="")
        )
        sinks = _FlushFailingSinks()
        adapter = CodexAdapter(
            runner=FakeCommandRunner(),
            default_model="gpt-5",
            log_store=_SinkLogStore(sinks),  # type: ignore[arg-type]
            runtime_executor=executor,
        )

        with pytest.raises(RuntimeError, match="log sink flush failure"):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_hosted_flush_failed_no_timeout",
            )

    @pytest.mark.unit
    async def test_hosted_flush_cancellation_untagged_without_a_watchdog_verdict(self) -> None:
        """A close/flush cancellation gains no tag when nothing was classified."""
        executor = _RecordingExecutor(
            result=AgentRuntimeExecResult(returncode=0, stdout="hosted stdout", stderr="")
        )
        sinks = _FlushCancellingSinks()
        adapter = CodexAdapter(
            runner=FakeCommandRunner(),
            default_model="gpt-5",
            log_store=_SinkLogStore(sinks),  # type: ignore[arg-type]
            runtime_executor=executor,
        )

        with pytest.raises(asyncio.CancelledError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_hosted_flush_cancelled_no_timeout",
            )

        assert getattr(exc.value, "agent_reason_code", None) is None

    @pytest.mark.unit
    async def test_hosted_tag_is_dropped_when_a_reason_is_not_a_watchdog_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only watchdog codes may claim the caller's timeout preservation.

        The hosted timeout vocabulary is owned by ``runtime_executor``; a reason
        added there that normalizes to an ordinary failure must produce no tag,
        so a non-timeout failure can never make the verdict protocol preserve a
        worktree it should rewind. Mirrors the local path's
        ``_WATCHDOG_TIMEOUT_REASON_CODES`` screen.
        """
        monkeypatch.setattr(
            base_module,
            "_HOSTED_TIMEOUT_REASONS",
            frozenset({COMMAND_TIMEOUT_REASON, "COMMAND_NOT_A_TIMEOUT"}),
        )

        assert (
            base_module._hosted_watchdog_timeout_reason_code(
                AgentRuntimeExecResult(
                    returncode=124,
                    stdout="",
                    stderr="",
                    timeout_reason="COMMAND_NOT_A_TIMEOUT",
                )
            )
            is None
        )
        assert (
            base_module._hosted_watchdog_timeout_reason_code(
                AgentRuntimeExecResult(
                    returncode=124,
                    stdout="",
                    stderr="",
                    timeout_reason=COMMAND_TIMEOUT_REASON,
                )
            )
            == "AGENT_TIMEOUT"
        )
