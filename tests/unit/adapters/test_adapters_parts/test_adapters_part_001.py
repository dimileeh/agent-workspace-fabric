"""Codex adapter: watchdog-classification hand-off across teardown (#932/#934).

A timed-out agent run has to reach the caller *classified*, whatever happens to
it on the way out: the post-timeout cleanup can be cancelled, fail to spawn, or
be replaced by the usage-sampler finalize's own re-raised cancellation. Each of
those swaps the exception the caller finally sees, so every hop has to carry
the watchdog tag onto the replacement — otherwise the verdict protocol rewinds
to the rollback floor and deletes the timed-out run's work.

Split out of ``tests/unit/adapters/test_adapters.py`` to keep each test module
under the first-party 1500-line maintainability guardrail.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import structlog

from awf.adapters.codex import CodexAdapter
from awf.adapters.run_results import AgentRunError
from awf.common.commands import (
    COMMAND_IDLE_TIMEOUT_REASON,
    COMMAND_TIMEOUT_REASON,
    CommandResult,
    FakeCommandRunner,
    mark_masked_command_reason_code,
)
from awf.common.compose_exec import ComposeExecCleanupError
from tests.unit.adapters.test_adapters import (
    _COMPOSE_FILE,
    _COMPOSE_PROJECT,
    _PROMPT,
    _CancellingStreamingRunner,
    _RecordingSinks,
)


class _CancelledAfterTimeoutDiagnosticRunner:
    """Runner cancelled *after* its watchdog already classified the run.

    ``run_streaming`` writes its synthetic timeout diagnostic to the caller's
    log sink before returning, and that write awaits: a cancellation delivered
    there escapes carrying the command-level classification instead of the
    classified result (``mark_masked_command_reason_code``).
    """

    def __init__(self, *, reason_code: str | None, cleanup: str = "succeeds") -> None:
        """Initialize cleanup recording, its outcome and the masked classification."""
        self.cleanup_calls: list[list[str]] = []
        self._reason_code = reason_code
        self._cleanup = cleanup

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
        **kwargs: object,
    ) -> CommandResult:
        """Record the cancellation cleanup the adapter runs before re-raising."""
        del input_bytes, cwd
        self.cleanup_calls.append(list(args))
        assert "awf-cleanup" in args
        if self._cleanup == "spawn_error":
            raise OSError(12, "Cannot allocate memory")
        if self._cleanup == "fails":
            return CommandResult(returncode=1, stdout="", stderr="tagged process still alive")
        return CommandResult(returncode=0, stdout="awf cleanup: killed", stderr="")

    async def run_streaming(
        self,
        _args: list[str],
        **_kwargs: Any,
    ) -> CommandResult:
        """Raise the cancellation the runner's final sink write escapes with."""
        cancel_exc = asyncio.CancelledError()
        if self._reason_code is not None:
            mark_masked_command_reason_code(cancel_exc, self._reason_code)
        raise cancel_exc


class _CancelledDuringTimeoutCleanupRunner:
    """Runner whose *post-timeout* cleanup is cancelled while it awaits."""

    def __init__(self, *, sweep: str = "succeeds") -> None:
        """Initialize cleanup call recording and the shielded sweep's outcome."""
        self.cleanup_calls: list[list[str]] = []
        self._sweep = sweep

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
        **kwargs: object,
    ) -> CommandResult:
        """Cancel the tracked-exec cleanup the way a worker stop would."""
        del input_bytes, cwd
        self.cleanup_calls.append(list(args))
        assert "awf-cleanup" in args
        if len(self.cleanup_calls) == 1:
            raise asyncio.CancelledError
        # Retry under the shield: the adapter finishes the interrupted teardown
        # so the timed-out agent process cannot outlive the run.
        if self._sweep == "cancelled":
            raise asyncio.CancelledError
        if self._sweep == "fails":
            return CommandResult(returncode=1, stdout="", stderr="cleanup boom")
        return CommandResult(returncode=0, stdout="awf cleanup: killed", stderr="")

    async def run_streaming(
        self,
        _args: list[str],
        **_kwargs: Any,
    ) -> CommandResult:
        """Return a watchdog wall-timeout result, as a timed-out agent run does."""
        return CommandResult(
            returncode=124,
            stdout="",
            stderr="command wall timeout",
            reason_code="COMMAND_TIMEOUT",
        )


class _SpawnFailingTimeoutCleanupRunner:
    """Runner whose *post-timeout* cleanup cannot even be spawned.

    ``cleanup_compose_exec_invocation`` shells out, so the cleanup can fail
    before it ever judges the process tree — an ``OSError`` from a cleanup
    process that cannot be spawned, for instance.
    """

    def __init__(self, *, cancel_first: bool = False, error: Exception | None = None) -> None:
        """Initialize cleanup recording, first-call cancellation and the spawn error."""
        self.cleanup_calls: list[list[str]] = []
        self._cancel_first = cancel_first
        self._error = error or OSError(12, "Cannot allocate memory")

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
        **kwargs: object,
    ) -> CommandResult:
        """Fail to spawn the tracked-exec cleanup."""
        del input_bytes, cwd
        self.cleanup_calls.append(list(args))
        assert "awf-cleanup" in args
        if self._cancel_first and len(self.cleanup_calls) == 1:
            raise asyncio.CancelledError
        raise self._error

    async def run_streaming(
        self,
        _args: list[str],
        **_kwargs: Any,
    ) -> CommandResult:
        """Return a watchdog wall-timeout result, as a timed-out agent run does."""
        return CommandResult(
            returncode=124,
            stdout="",
            stderr="command wall timeout",
            reason_code="COMMAND_TIMEOUT",
        )


class _TeardownFailingAfterTimeoutRunner:
    """Runner whose post-classification teardown fails in an *ordinary* way.

    ``run_streaming`` classifies the watchdog verdict before it terminates and
    reaps the child, and that teardown can raise an ``OSError`` — or make the
    caller's log sink raise on the last flushed line. The runner tags what
    escapes with the command-level classification
    (``mark_masked_command_reason_code``) exactly as it tags a cancellation.
    """

    def __init__(self, *, reason_code: str | None, cleanup: str = "succeeds") -> None:
        """Initialize cleanup recording, its outcome and the masked classification."""
        self.cleanup_calls: list[list[str]] = []
        self._reason_code = reason_code
        self._cleanup = cleanup

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
        **kwargs: object,
    ) -> CommandResult:
        """Record the teardown cleanup the adapter runs before escalating."""
        del input_bytes, cwd
        self.cleanup_calls.append(list(args))
        assert "awf-cleanup" in args
        if self._cleanup == "spawn_error":
            raise OSError(12, "Cannot allocate memory")
        if self._cleanup == "cancelled" and len(self.cleanup_calls) == 1:
            raise asyncio.CancelledError
        if self._cleanup == "fails":
            return CommandResult(returncode=1, stdout="", stderr="tagged process still alive")
        return CommandResult(returncode=0, stdout="awf cleanup: killed", stderr="")

    async def run_streaming(
        self,
        _args: list[str],
        **_kwargs: Any,
    ) -> CommandResult:
        """Raise the ordinary failure the runner's teardown escapes with."""
        stream_exc = OSError("Cannot terminate process")
        if self._reason_code is not None:
            mark_masked_command_reason_code(stream_exc, self._reason_code)
        raise stream_exc


class _CloseFailingSinks(_RecordingSinks):
    """Log sinks that fail while being closed.

    The likeliest reason the close raises is the very failure being carried out
    of the run: a log sink that broke mid-stream stays broken when the adapter
    flushes it in its ``finally``.
    """

    async def close(self) -> None:
        """Raise the close failure that would displace the escaping exception."""
        self.closed = True
        raise RuntimeError("log sink close failure")


class _CloseFailingLogStore:
    """Log store whose sinks raise when the adapter closes them."""

    def __init__(self) -> None:
        """Initialize the close-failing sink."""
        self.sinks = _CloseFailingSinks()

    async def open_command_streams(self, **_kwargs: Any) -> _CloseFailingSinks:
        """Return the sink that raises on close."""
        return self.sinks


class _CloseCancellingSinks(_RecordingSinks):
    """Log sinks cancelled while the adapter's ``finally`` closes them.

    Flushing a sink awaits, so worker cancellation can land there — and the
    fresh ``CancelledError`` replaces whatever was escaping, tagged or not.
    """

    async def close(self) -> None:
        """Raise the cancellation delivered while the close awaits."""
        self.closed = True
        raise asyncio.CancelledError


class _CloseCancellingLogStore:
    """Log store whose sinks are cancelled when the adapter closes them."""

    def __init__(self) -> None:
        """Initialize the close-cancelling sink."""
        self.sinks = _CloseCancellingSinks()

    async def open_command_streams(self, **_kwargs: Any) -> _CloseCancellingSinks:
        """Return the sink that is cancelled on close."""
        return self.sinks


class _CancelReRaisingSampleContext:
    """Sampler context whose ``finalize`` re-raises a *fresh* cancellation.

    Mirrors ``UsageSampleContext.finalize``: it shields the final sample against
    a run that is being cancelled and then re-raises the ``CancelledError`` it
    consumed, so the object the adapter's ``finally`` sees is a different one
    from the cancellation it interrupted.
    """

    def __init__(self) -> None:
        """Record the status the adapter finalizes sampling with."""
        self.finalize_status: str | None = None

    async def finalize(self, *, status: str) -> None:
        """Re-raise a new cancellation the way the real finalize does."""
        self.finalize_status = status
        raise asyncio.CancelledError


class _CancelReRaisingSampler:
    """Usage sampler handing out a :class:`_CancelReRaisingSampleContext`."""

    def __init__(self) -> None:
        """Initialize the single context this sampler starts."""
        self.context = _CancelReRaisingSampleContext()

    async def start(self, **_kwargs: Any) -> _CancelReRaisingSampleContext:
        """Return the cancellation-re-raising sampling context."""
        return self.context


class TestCodexAdapterTimeoutClassification:
    """Timeout classification survives every teardown hop that can replace it."""

    @pytest.mark.unit
    async def test_cancelled_timeout_cleanup_tags_the_watchdog_classification(self) -> None:
        """Cancellation inside the post-timeout cleanup keeps the timeout classification.

        The cleanup runs *before* the ``AgentRunError`` is raised and it awaits,
        so worker cancellation there escapes with the run already classified as a
        watchdog timeout but nothing published. Callers that preserve timed-out
        work instead of rolling it back (#932/#934) read the tag off the
        cancellation, exactly as they read it off a cleanup failure
        (PRRT_kwDOSJAM6s6f0n6B).
        """
        runner = _CancelledDuringTimeoutCleanupRunner()
        adapter = CodexAdapter(runner=runner)  # type: ignore[arg-type]

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(asyncio.CancelledError) as exc,
        ):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_timeout_cleanup_cancelled",
            )

        assert getattr(exc.value, "agent_reason_code", None) == "AGENT_TIMEOUT"
        # The interrupted teardown is finished under a shield: leaving it
        # abandoned would let the timed-out agent keep writing into the worktree
        # the caller is preserving.
        assert len(runner.cleanup_calls) == 2
        assert any(
            event.get("event") == "agent.run.timeout_cleanup_cancelled"
            and event.get("reason_code") == "AGENT_TIMEOUT"
            and event.get("workspace_id") == "ws_timeout_cleanup_cancelled"
            for event in captured
        )
        assert not any(
            event.get("event") == "agent.run.timeout_cleanup_cancelled_sweep_failed"
            for event in captured
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("sweep", "sweep_error"),
        [("fails", "ComposeExecCleanupError"), ("cancelled", "CancelledError")],
    )
    async def test_cancelled_timeout_cleanup_sweep_never_displaces_the_cancellation(
        self,
        sweep: str,
        sweep_error: str,
    ) -> None:
        """A failed or re-cancelled sweep still surfaces the tagged cancellation.

        The caller re-raises the tagged ``CancelledError`` right after the sweep,
        so a sweep error must be logged rather than propagated — otherwise it
        would replace the very classification the tag carries
        (PRRT_kwDOSJAM6s6f0n6B).
        """
        runner = _CancelledDuringTimeoutCleanupRunner(sweep=sweep)
        adapter = CodexAdapter(runner=runner)  # type: ignore[arg-type]

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(asyncio.CancelledError) as exc,
        ):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_timeout_cleanup_sweep_failed",
            )

        assert getattr(exc.value, "agent_reason_code", None) == "AGENT_TIMEOUT"
        assert len(runner.cleanup_calls) == 2
        assert any(
            event.get("event") == "agent.run.timeout_cleanup_cancelled_sweep_failed"
            and event.get("reason_code") == "AGENT_TIMEOUT"
            and event.get("sweep_error") == sweep_error
            for event in captured
        )
        assert any(
            event.get("event") == "agent.run.timeout_cleanup_cancelled" for event in captured
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("command_reason_code", "agent_reason_code"),
        [
            (COMMAND_TIMEOUT_REASON, "AGENT_TIMEOUT"),
            (COMMAND_IDLE_TIMEOUT_REASON, "AGENT_IDLE_TIMEOUT"),
        ],
    )
    async def test_cancelled_timeout_diagnostic_republishes_the_watchdog_tag(
        self,
        command_reason_code: str,
        agent_reason_code: str,
    ) -> None:
        """A cancellation the runner classified reaches the preserve path tagged.

        The runner tags the cancellation in *command* vocabulary; the adapter
        republishes it in agent vocabulary so the verdict protocol's cancellation
        handler preserves the timed-out run's work instead of rewinding over it
        (PRRT_kwDOSJAM6s6f7rCe).
        """
        runner = _CancelledAfterTimeoutDiagnosticRunner(reason_code=command_reason_code)
        adapter = CodexAdapter(runner=runner)  # type: ignore[arg-type]

        with pytest.raises(asyncio.CancelledError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_timeout_diagnostic_cancelled",
            )

        assert getattr(exc.value, "agent_reason_code", None) == agent_reason_code
        # The exec stack is still torn down: the cancellation cleanup runs before
        # the tagged error is re-raised.
        assert len(runner.cleanup_calls) == 1

    @pytest.mark.unit
    async def test_cancelled_stream_without_watchdog_tag_stays_untagged(self) -> None:
        """An ordinary cancellation gains no timeout classification it never earned."""
        runner = _CancellingStreamingRunner()
        adapter = CodexAdapter(runner=runner)  # type: ignore[arg-type]

        with pytest.raises(asyncio.CancelledError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_stream_cancelled_no_timeout",
            )

        assert getattr(exc.value, "agent_reason_code", None) is None

    @pytest.mark.unit
    async def test_cancelled_timeout_diagnostic_cleanup_failure_keeps_the_tag(self) -> None:
        """A cleanup failure that displaces the tagged cancellation carries the tag.

        The teardown this branch runs before re-raising can find the tracked
        process still alive, and the ``ComposeExecCleanupError`` it then raises
        replaces the cancellation the watchdog tag rode out on. Untagged it reads
        as an ordinary cleanup failure and the verdict protocol resets to the
        rollback floor, deleting the timed-out run's work (PRRT_kwDOSJAM6s6f8cgU).
        """
        runner = _CancelledAfterTimeoutDiagnosticRunner(
            reason_code=COMMAND_TIMEOUT_REASON,
            cleanup="fails",
        )
        adapter = CodexAdapter(runner=runner)  # type: ignore[arg-type]

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(ComposeExecCleanupError) as exc,
        ):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_diagnostic_cancel_cleanup_failed",
            )

        assert exc.value.reason_code == "EXEC_PROCESS_CLEANUP_FAILED"
        assert exc.value.agent_reason_code == "AGENT_TIMEOUT"
        assert "tagged process still alive" in str(exc.value)
        assert any(
            event.get("event") == "agent.run.timeout_cleanup_failed"
            and event.get("reason_code") == "AGENT_TIMEOUT"
            and event.get("workspace_id") == "ws_diagnostic_cancel_cleanup_failed"
            for event in captured
        )

    @pytest.mark.unit
    async def test_cancelled_timeout_diagnostic_cleanup_spawn_error_escalates_tagged(
        self,
    ) -> None:
        """A cleanup that cannot spawn here escalates tagged, as the timeout path does.

        Same masking hazard from the other side: this teardown shells out, so it
        can fail before it judges the process tree, and that raw error would
        escape untagged past the verdict protocol's generic branch
        (PRRT_kwDOSJAM6s6f8cgU).
        """
        runner = _CancelledAfterTimeoutDiagnosticRunner(
            reason_code=COMMAND_IDLE_TIMEOUT_REASON,
            cleanup="spawn_error",
        )
        adapter = CodexAdapter(runner=runner)  # type: ignore[arg-type]

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(ComposeExecCleanupError) as exc,
        ):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_diagnostic_cancel_cleanup_error",
            )

        assert exc.value.reason_code == "EXEC_PROCESS_CLEANUP_FAILED"
        assert exc.value.agent_reason_code == "AGENT_IDLE_TIMEOUT"
        assert isinstance(exc.value.__cause__, OSError)
        assert "Cannot allocate memory" in str(exc.value)
        assert any(
            event.get("event") == "agent.run.timeout_cleanup_error"
            and event.get("reason_code") == "AGENT_IDLE_TIMEOUT"
            and event.get("cleanup_error") == "OSError"
            for event in captured
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("cleanup", "expected_error"),
        [("fails", ComposeExecCleanupError), ("spawn_error", OSError)],
    )
    async def test_cancelled_stream_cleanup_failure_stays_untagged(
        self,
        cleanup: str,
        expected_error: type[Exception],
    ) -> None:
        """An untagged cancellation's cleanup failure earns no timeout tag.

        Nothing classified this run, so the failure surfaces exactly as it did
        before — no watchdog classification is invented for it.
        """
        runner = _CancelledAfterTimeoutDiagnosticRunner(reason_code=None, cleanup=cleanup)
        adapter = CodexAdapter(runner=runner)  # type: ignore[arg-type]

        with pytest.raises(expected_error) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_stream_cancel_cleanup_failed",
            )

        assert getattr(exc.value, "agent_reason_code", None) is None

    @pytest.mark.unit
    async def test_unexpected_timeout_cleanup_error_escalates_tagged(self) -> None:
        """A cleanup that cannot be spawned still reaches the preserve path.

        The post-timeout cleanup shells out, so it can fail with an ordinary
        exception — an ``OSError`` from a cleanup process that cannot be
        spawned — instead of the ``ComposeExecCleanupError`` it raises when the
        process tree survives. Left raw it escapes untagged, and the verdict
        protocol's generic handler rewinds to the rollback floor, deleting the
        timed-out run's edits and commits. Escalate it as the cleanup failure it
        is, carrying the watchdog classification (PRRT_kwDOSJAM6s6f1JoH).
        """
        runner = _SpawnFailingTimeoutCleanupRunner()
        adapter = CodexAdapter(runner=runner)  # type: ignore[arg-type]

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(ComposeExecCleanupError) as exc,
        ):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_timeout_cleanup_error",
            )

        assert exc.value.reason_code == "EXEC_PROCESS_CLEANUP_FAILED"
        assert exc.value.agent_reason_code == "AGENT_TIMEOUT"
        assert isinstance(exc.value.__cause__, OSError)
        # The escalation replaces the original error everywhere but the
        # traceback chain, so it must carry *why* the cleanup could not run —
        # the operator sees this message and this log line, not the __cause__.
        assert "Cannot allocate memory" in str(exc.value)
        assert any(
            event.get("event") == "agent.run.timeout_cleanup_error"
            and event.get("reason_code") == "AGENT_TIMEOUT"
            and event.get("cleanup_error") == "OSError"
            and "Cannot allocate memory" in str(event.get("cleanup_error_detail"))
            and event.get("workspace_id") == "ws_timeout_cleanup_error"
            for event in captured
        )

    @pytest.mark.unit
    async def test_unexpected_timeout_cleanup_error_detail_is_redacted(self) -> None:
        """The preserved cleanup detail is redacted before it is logged or raised.

        A spawn failure can quote the command environment, so the detail this
        escalation now carries goes through ``redact_secrets`` like every other
        runtime log field (PRRT_kwDOSJAM6s6f1JoH).
        """
        secret = "Bearer sk-ant-notarealtoken0123456789"
        runner = _SpawnFailingTimeoutCleanupRunner(
            error=OSError(f"exec failed with env AUTH={secret}")
        )
        adapter = CodexAdapter(runner=runner)  # type: ignore[arg-type]

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(ComposeExecCleanupError) as exc,
        ):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_timeout_cleanup_error_secret",
            )

        assert "notarealtoken" not in str(exc.value)
        assert "exec failed with env" in str(exc.value)
        details = [
            str(event.get("cleanup_error_detail"))
            for event in captured
            if event.get("event") == "agent.run.timeout_cleanup_error"
        ]
        assert details
        assert all("notarealtoken" not in detail for detail in details)

    @pytest.mark.unit
    async def test_cancelled_timeout_cleanup_sweep_error_never_displaces_the_cancellation(
        self,
    ) -> None:
        """An unexpected sweep error is logged, not propagated.

        Same masking hazard from the other side: the shielded sweep can fail to
        spawn too, and that raw error would replace the tagged ``CancelledError``
        the caller re-raises right after it (PRRT_kwDOSJAM6s6f1JoH).
        """
        runner = _SpawnFailingTimeoutCleanupRunner(cancel_first=True)
        adapter = CodexAdapter(runner=runner)  # type: ignore[arg-type]

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(asyncio.CancelledError) as exc,
        ):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_timeout_cleanup_sweep_error",
            )

        assert getattr(exc.value, "agent_reason_code", None) == "AGENT_TIMEOUT"
        assert len(runner.cleanup_calls) == 2
        assert any(
            event.get("event") == "agent.run.timeout_cleanup_cancelled_sweep_failed"
            and event.get("reason_code") == "AGENT_TIMEOUT"
            and event.get("sweep_error") == "OSError"
            for event in captured
        )
        assert any(
            event.get("event") == "agent.run.timeout_cleanup_cancelled" for event in captured
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("command_reason_code", "agent_reason_code"),
        [
            (COMMAND_TIMEOUT_REASON, "AGENT_TIMEOUT"),
            (COMMAND_IDLE_TIMEOUT_REASON, "AGENT_IDLE_TIMEOUT"),
        ],
    )
    async def test_timed_out_stream_teardown_failure_escalates_tagged(
        self,
        command_reason_code: str,
        agent_reason_code: str,
    ) -> None:
        """An ordinary post-classification teardown failure reaches the preserve path.

        The runner tags ordinary failures out of that window as well as
        cancellations, but only the cancellation branch republished the tag, so
        the ``OSError`` escaped as an unclassified failure and the verdict
        protocol's generic branch rewound the timed-out run's work
        (PRRT_kwDOSJAM6s6f9RQl).
        """
        runner = _TeardownFailingAfterTimeoutRunner(reason_code=command_reason_code)
        adapter = CodexAdapter(runner=runner)  # type: ignore[arg-type]

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(ComposeExecCleanupError) as exc,
        ):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_stream_teardown_failed",
            )

        assert exc.value.reason_code == "EXEC_PROCESS_CLEANUP_FAILED"
        assert exc.value.agent_reason_code == agent_reason_code
        assert isinstance(exc.value.__cause__, OSError)
        assert "Cannot terminate process" in str(exc.value)
        # The failed teardown is the one that should have killed the child, so the
        # tracked exec is still torn down before the escalation is raised.
        assert len(runner.cleanup_calls) == 1
        # That cleanup *succeeded* here — only the run teardown failed — so neither
        # the message nor the event may blame it and send an operator to debug a
        # step that worked.
        assert "run teardown failed after the watchdog timeout" in str(exc.value)
        assert "cleanup could not run" not in str(exc.value)
        assert any(
            event.get("event") == "agent.run.timeout_cleanup_error"
            and event.get("reason_code") == agent_reason_code
            and event.get("failure_summary") == "run teardown failed after the watchdog timeout"
            and event.get("cleanup_error") == "OSError"
            and event.get("workspace_id") == "ws_stream_teardown_failed"
            for event in captured
        )

    @pytest.mark.unit
    async def test_untagged_stream_failure_stays_an_ordinary_failure(self) -> None:
        """A failure no watchdog classified surfaces unchanged, with no cleanup.

        Without a verdict the exception *is* the run's outcome; inventing a
        timeout classification for it would preserve work no timeout produced.
        """
        runner = _TeardownFailingAfterTimeoutRunner(reason_code=None)
        adapter = CodexAdapter(runner=runner)  # type: ignore[arg-type]

        with pytest.raises(OSError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_stream_failure_untagged",
            )

        assert getattr(exc.value, "agent_reason_code", None) is None
        assert runner.cleanup_calls == []

    @pytest.mark.unit
    async def test_timed_out_stream_teardown_cleanup_failure_keeps_the_tag(self) -> None:
        """A cleanup that finds the process alive replaces the escalation, tagged.

        The ``ComposeExecCleanupError`` displaces the escalation this branch would
        otherwise raise, so it has to carry the classification itself or the
        timed-out run's work is rewound anyway (PRRT_kwDOSJAM6s6f9RQl).
        """
        runner = _TeardownFailingAfterTimeoutRunner(
            reason_code=COMMAND_TIMEOUT_REASON,
            cleanup="fails",
        )
        adapter = CodexAdapter(runner=runner)  # type: ignore[arg-type]

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(ComposeExecCleanupError) as exc,
        ):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_stream_teardown_cleanup_failed",
            )

        assert exc.value.agent_reason_code == "AGENT_TIMEOUT"
        assert "tagged process still alive" in str(exc.value)
        assert isinstance(exc.value.__cause__, OSError)
        assert any(
            event.get("event") == "agent.run.timeout_cleanup_failed"
            and event.get("reason_code") == "AGENT_TIMEOUT"
            and event.get("workspace_id") == "ws_stream_teardown_cleanup_failed"
            for event in captured
        )

    @pytest.mark.unit
    async def test_timed_out_stream_teardown_cleanup_spawn_error_escalates_tagged(self) -> None:
        """A cleanup that cannot spawn here escalates tagged, as the timeout path does."""
        runner = _TeardownFailingAfterTimeoutRunner(
            reason_code=COMMAND_IDLE_TIMEOUT_REASON,
            cleanup="spawn_error",
        )
        adapter = CodexAdapter(runner=runner)  # type: ignore[arg-type]

        with pytest.raises(ComposeExecCleanupError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_stream_teardown_cleanup_error",
            )

        assert exc.value.agent_reason_code == "AGENT_IDLE_TIMEOUT"
        assert isinstance(exc.value.__cause__, OSError)
        assert "Cannot allocate memory" in str(exc.value)

    @pytest.mark.unit
    async def test_timed_out_stream_teardown_cleanup_cancellation_keeps_the_tag(self) -> None:
        """Cancellation of that cleanup carries the tag and still finishes the sweep."""
        runner = _TeardownFailingAfterTimeoutRunner(
            reason_code=COMMAND_TIMEOUT_REASON,
            cleanup="cancelled",
        )
        adapter = CodexAdapter(runner=runner)  # type: ignore[arg-type]

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(asyncio.CancelledError) as exc,
        ):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_stream_teardown_cleanup_cancelled",
            )

        assert getattr(exc.value, "agent_reason_code", None) == "AGENT_TIMEOUT"
        assert len(runner.cleanup_calls) == 2
        assert any(
            event.get("event") == "agent.run.timeout_cleanup_cancelled"
            and event.get("reason_code") == "AGENT_TIMEOUT"
            and event.get("workspace_id") == "ws_stream_teardown_cleanup_cancelled"
            for event in captured
        )

    @pytest.mark.unit
    async def test_log_sink_close_failure_never_displaces_the_tagged_escalation(self) -> None:
        """A raising sink close leaves the tagged escalation in place.

        The adapter closes its log sinks in a ``finally``, so a close that raises
        replaces the ``ComposeExecCleanupError`` the stream-failure path just
        escalated — and the sink is the likeliest thing to have broken the run in
        the first place. Untagged, that replacement sends the verdict protocol's
        generic branch down the rollback floor and the timed-out run's work is
        deleted (PRRT_kwDOSJAM6s6f9jKk).
        """
        runner = _TeardownFailingAfterTimeoutRunner(reason_code=COMMAND_TIMEOUT_REASON)
        log_store = _CloseFailingLogStore()
        adapter = CodexAdapter(
            runner=runner,  # type: ignore[arg-type]
            log_store=log_store,  # type: ignore[arg-type]
        )

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(ComposeExecCleanupError) as exc,
        ):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_stream_teardown_close_failed",
            )

        assert exc.value.agent_reason_code == "AGENT_TIMEOUT"
        assert "Cannot terminate process" in str(exc.value)
        assert log_store.sinks.closed is True
        assert any(
            event.get("event") == "agent.run.timeout_log_close_failed"
            and event.get("reason_code") == "AGENT_TIMEOUT"
            and event.get("close_error") == "RuntimeError"
            and event.get("workspace_id") == "ws_stream_teardown_close_failed"
            for event in captured
        )

    @pytest.mark.unit
    async def test_log_sink_close_failure_never_displaces_the_tagged_cancellation(self) -> None:
        """The same close failure leaves a tagged cancellation in place too."""
        runner = _CancelledAfterTimeoutDiagnosticRunner(reason_code=COMMAND_IDLE_TIMEOUT_REASON)
        log_store = _CloseFailingLogStore()
        adapter = CodexAdapter(
            runner=runner,  # type: ignore[arg-type]
            log_store=log_store,  # type: ignore[arg-type]
        )

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(asyncio.CancelledError) as exc,
        ):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_cancelled_timeout_close_failed",
            )

        assert getattr(exc.value, "agent_reason_code", None) == "AGENT_IDLE_TIMEOUT"
        assert any(
            event.get("event") == "agent.run.timeout_log_close_failed"
            and event.get("reason_code") == "AGENT_IDLE_TIMEOUT"
            and event.get("workspace_id") == "ws_cancelled_timeout_close_failed"
            for event in captured
        )

    @pytest.mark.unit
    async def test_log_sink_close_failure_surfaces_without_a_watchdog_verdict(self) -> None:
        """With nothing classified in flight the close failure is the run's outcome."""
        runner = FakeCommandRunner()
        runner.queue_result(returncode=0, stdout="agent done")
        log_store = _CloseFailingLogStore()
        adapter = CodexAdapter(
            runner=runner,
            log_store=log_store,  # type: ignore[arg-type]
        )

        with pytest.raises(RuntimeError, match="log sink close failure"):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_close_failed_no_timeout",
            )

    @pytest.mark.unit
    async def test_usage_finalize_cancellation_keeps_the_watchdog_tag(self) -> None:
        """A re-raised finalize cancellation still carries the timeout tag.

        ``run`` finalizes usage sampling in a ``finally``, and that finalize
        re-raises the cancellation it consumed while shielding the final sample.
        The fresh ``CancelledError`` replaces the tagged one the post-timeout
        cleanup raised, so the tag must be carried across the replacement or the
        verdict handler rolls back the timed-out run's work
        (PRRT_kwDOSJAM6s6f1F2D).
        """
        runner = _CancelledDuringTimeoutCleanupRunner()
        sampler = _CancelReRaisingSampler()
        adapter = CodexAdapter(
            runner=runner,  # type: ignore[arg-type]
            usage_sampler=sampler,  # type: ignore[arg-type]
        )

        with pytest.raises(asyncio.CancelledError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_timeout_finalize_cancelled",
            )

        assert sampler.context.finalize_status == "cancelled"
        assert getattr(exc.value, "agent_reason_code", None) == "AGENT_TIMEOUT"

    @pytest.mark.unit
    async def test_usage_finalize_cancellation_untagged_when_no_timeout(self) -> None:
        """An ordinary cancellation gains no watchdog tag from the finalize hop."""
        runner = _CancellingStreamingRunner()
        sampler = _CancelReRaisingSampler()
        adapter = CodexAdapter(
            runner=runner,  # type: ignore[arg-type]
            usage_sampler=sampler,  # type: ignore[arg-type]
        )

        with pytest.raises(asyncio.CancelledError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_finalize_cancelled_no_timeout",
            )

        assert sampler.context.finalize_status == "cancelled"
        assert getattr(exc.value, "agent_reason_code", None) is None

    @pytest.mark.unit
    async def test_usage_finalize_cancellation_keeps_the_timeout_error_tag(self) -> None:
        """The finalize hop carries the tag off a timed-out ``AgentRunError`` too.

        The mainline timeout path finishes its cleanup and raises
        ``AgentRunError(AGENT_TIMEOUT)``; the finally's re-raised finalize
        cancellation replaces *that* exception just as effectively, so an
        untagged cancellation would send the verdict handler down the rollback
        path that deletes the timed-out run's work (PRRT_kwDOSJAM6s6f1F2D).
        """
        runner = FakeCommandRunner()
        runner.queue_result(
            returncode=124,
            stderr="command wall timeout",
            reason_code="COMMAND_TIMEOUT",
        )
        runner.queue_result(returncode=0, stdout="cleanup ok")
        sampler = _CancelReRaisingSampler()
        adapter = CodexAdapter(runner=runner, usage_sampler=sampler)  # type: ignore[arg-type]

        with pytest.raises(asyncio.CancelledError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_timeout_error_finalize_cancelled",
            )

        assert sampler.context.finalize_status == "timeout"
        assert getattr(exc.value, "agent_reason_code", None) == "AGENT_TIMEOUT"

    @pytest.mark.unit
    async def test_usage_finalize_cancellation_keeps_the_cleanup_failure_tag(self) -> None:
        """The finalize hop carries the tag off a masking cleanup failure too.

        A post-timeout cleanup that cannot prove the process tree is gone raises
        a ``ComposeExecCleanupError`` tagged with the watchdog classification.
        The re-raised finalize cancellation replaces it, so the tag has to travel
        onto the replacement (PRRT_kwDOSJAM6s6f1F2D).
        """
        runner = FakeCommandRunner()
        runner.queue_result(
            returncode=124,
            stderr="command wall timeout",
            reason_code="COMMAND_TIMEOUT",
        )
        runner.queue_result(returncode=1, stderr="tagged process still alive")
        sampler = _CancelReRaisingSampler()
        adapter = CodexAdapter(runner=runner, usage_sampler=sampler)  # type: ignore[arg-type]

        with pytest.raises(asyncio.CancelledError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_cleanup_failure_finalize_cancelled",
            )

        assert sampler.context.finalize_status == "failed"
        assert getattr(exc.value, "agent_reason_code", None) == "AGENT_TIMEOUT"

    @pytest.mark.unit
    async def test_returned_timeout_survives_a_failing_log_sink_close(self) -> None:
        """A returned watchdog verdict is not displaced by the sink close either.

        The ordinary way a timeout arrives is ``run_streaming`` *returning* a
        classified result, and that result is not turned into an ``AgentRunError``
        until after the sinks are closed. A close that raises in that ``finally``
        therefore escapes as an ordinary error before the timeout is ever
        published, and the verdict protocol's generic branch rewinds to the
        rollback floor and deletes the timed-out run's work
        (PRRT_kwDOSJAM6s6f9xkl).
        """
        runner = FakeCommandRunner()
        runner.queue_result(
            returncode=124,
            stderr="command wall timeout",
            reason_code=COMMAND_TIMEOUT_REASON,
        )
        runner.queue_result(returncode=0, stdout="awf cleanup: killed")
        log_store = _CloseFailingLogStore()
        adapter = CodexAdapter(
            runner=runner,
            log_store=log_store,  # type: ignore[arg-type]
        )

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(AgentRunError) as exc,
        ):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_returned_timeout_close_failed",
            )

        assert exc.value.reason_code == "AGENT_TIMEOUT"
        assert log_store.sinks.closed is True
        assert any(
            event.get("event") == "agent.run.timeout_log_close_failed"
            and event.get("reason_code") == "AGENT_TIMEOUT"
            and event.get("close_error") == "RuntimeError"
            and event.get("workspace_id") == "ws_returned_timeout_close_failed"
            for event in captured
        )

    @pytest.mark.unit
    async def test_returned_timeout_tags_a_cancelled_log_sink_close(self) -> None:
        """A cancellation from the sink close carries the returned timeout tag.

        ``except Exception`` never sees a ``CancelledError``, so a cancellation
        delivered while the close awaits escapes untagged and the verdict
        protocol's cancellation handler rewinds over the timed-out run's work
        (PRRT_kwDOSJAM6s6f9xkl).
        """
        runner = FakeCommandRunner()
        runner.queue_result(
            returncode=124,
            stderr="command idle timeout",
            reason_code=COMMAND_IDLE_TIMEOUT_REASON,
        )
        log_store = _CloseCancellingLogStore()
        adapter = CodexAdapter(
            runner=runner,
            log_store=log_store,  # type: ignore[arg-type]
        )

        with pytest.raises(asyncio.CancelledError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_returned_timeout_close_cancelled",
            )

        assert getattr(exc.value, "agent_reason_code", None) == "AGENT_IDLE_TIMEOUT"

    @pytest.mark.unit
    async def test_returned_timeout_close_cancellation_still_kills_the_tracked_exec(self) -> None:
        """That cancellation still tears the timed-out exec down before it escapes.

        The returned verdict's cleanup runs *after* the sinks close, so a
        cancellation escaping the close skips it entirely: the tag protects the
        timed-out run's work from the rollback, but the agent the watchdog gave up
        on keeps running inside the container and writing into the very worktree
        the caller is about to preserve (PRRT_kwDOSJAM6s6f97MR).
        """
        runner = FakeCommandRunner()
        runner.queue_result(
            returncode=124,
            stderr="command wall timeout",
            reason_code=COMMAND_TIMEOUT_REASON,
        )
        runner.queue_result(returncode=0, stdout="awf cleanup: killed")
        log_store = _CloseCancellingLogStore()
        adapter = CodexAdapter(
            runner=runner,
            log_store=log_store,  # type: ignore[arg-type]
        )

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(asyncio.CancelledError) as exc,
        ):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_returned_timeout_close_cancelled_sweep",
            )

        assert getattr(exc.value, "agent_reason_code", None) == "AGENT_TIMEOUT"
        assert [call.args for call in runner.calls if "awf-cleanup" in call.args] != []
        assert any(
            event.get("event") == "agent.run.timeout_log_close_cancelled"
            and event.get("reason_code") == "AGENT_TIMEOUT"
            and event.get("workspace_id") == "ws_returned_timeout_close_cancelled_sweep"
            for event in captured
        )

    @pytest.mark.unit
    async def test_cancelled_run_close_cancellation_does_not_repeat_the_cleanup(self) -> None:
        """A path that already tore the exec down is not swept a second time.

        The cancelled-run handler cleans up before it re-raises, so the sweep this
        close performs belongs only to the *returned* verdict; repeating it would
        shell out again for a process that is already gone.
        """
        runner = _CancelledAfterTimeoutDiagnosticRunner(reason_code=COMMAND_TIMEOUT_REASON)
        log_store = _CloseCancellingLogStore()
        adapter = CodexAdapter(
            runner=runner,  # type: ignore[arg-type]
            log_store=log_store,  # type: ignore[arg-type]
        )

        with pytest.raises(asyncio.CancelledError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_cancelled_run_close_cancelled",
            )

        assert getattr(exc.value, "agent_reason_code", None) == "AGENT_TIMEOUT"
        assert len(runner.cleanup_calls) == 1

    @pytest.mark.unit
    async def test_masked_timeout_tags_a_cancelled_log_sink_close(self) -> None:
        """The tag the exception handlers derived travels onto that cancellation too."""
        runner = _TeardownFailingAfterTimeoutRunner(reason_code=COMMAND_TIMEOUT_REASON)
        log_store = _CloseCancellingLogStore()
        adapter = CodexAdapter(
            runner=runner,  # type: ignore[arg-type]
            log_store=log_store,  # type: ignore[arg-type]
        )

        with pytest.raises(asyncio.CancelledError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_masked_timeout_close_cancelled",
            )

        assert getattr(exc.value, "agent_reason_code", None) == "AGENT_TIMEOUT"

    @pytest.mark.unit
    async def test_cancelled_log_sink_close_untagged_without_a_watchdog_verdict(self) -> None:
        """With nothing classified in flight the close cancellation gains no tag."""
        runner = FakeCommandRunner()
        runner.queue_result(returncode=0, stdout="agent done")
        log_store = _CloseCancellingLogStore()
        adapter = CodexAdapter(
            runner=runner,
            log_store=log_store,  # type: ignore[arg-type]
        )

        with pytest.raises(asyncio.CancelledError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_close_cancelled_no_timeout",
            )

        assert getattr(exc.value, "agent_reason_code", None) is None

    @pytest.mark.unit
    async def test_ordinary_failure_result_leaves_the_close_failure_in_place(self) -> None:
        """A non-timeout result is no watchdog verdict, so the close failure surfaces."""
        runner = FakeCommandRunner()
        runner.queue_result(returncode=1, stderr="agent exploded")
        log_store = _CloseFailingLogStore()
        adapter = CodexAdapter(
            runner=runner,
            log_store=log_store,  # type: ignore[arg-type]
        )

        with pytest.raises(RuntimeError, match="log sink close failure"):
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_failure_result_close_failed",
            )

    @pytest.mark.unit
    async def test_usage_finalize_cancellation_untagged_for_plain_failure(self) -> None:
        """A non-timeout agent failure hands the finalize hop nothing to carry."""
        runner = FakeCommandRunner()
        runner.queue_result(returncode=1, stderr="agent exploded")
        sampler = _CancelReRaisingSampler()
        adapter = CodexAdapter(runner=runner, usage_sampler=sampler)  # type: ignore[arg-type]

        with pytest.raises(asyncio.CancelledError) as exc:
            await adapter.run(
                compose_project=_COMPOSE_PROJECT,
                compose_file=_COMPOSE_FILE,
                prompt=_PROMPT,
                workspace_id="ws_failure_finalize_cancelled",
            )

        assert sampler.context.finalize_status == "failed"
        assert getattr(exc.value, "agent_reason_code", None) is None
