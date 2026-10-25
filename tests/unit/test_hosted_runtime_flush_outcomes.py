"""Final redacted log delivery cannot replace a command's primary outcome."""

import asyncio
import sys
from unittest.mock import AsyncMock

import pytest
import structlog.testing

from awf.adapters.failure_reasons import (
    _failure_reason_for_result,
    masked_agent_timeout_reason_code,
)
from awf.common import commands
from awf.common.commands import (
    COMMAND_IDLE_TIMEOUT_REASON,
    COMMAND_TIMEOUT_REASON,
    AsyncioSubprocessRunner,
    CommandResult,
    mark_masked_command_reason_code,
)
from awf.hosted_node.runtime_consumers import HostedCommandRunner
from tests.unit.test_hosted_runtime_consumers import consumers


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("reason", [COMMAND_TIMEOUT_REASON, COMMAND_IDLE_TIMEOUT_REASON])
async def test_real_watchdog_result_survives_hosted_final_sink_failure(
    tmp_path, monkeypatch, stream, reason
):
    scope, _ = consumers(tmp_path, monkeypatch)
    received = []

    async def fail(text):
        received.append(text)
        raise OSError("synthetic sink failure scoped-agent")

    runner = HostedCommandRunner(scope, AsyncioSubprocessRunner())
    with structlog.testing.capture_logs() as logs:
        async with scope.scope("ws_timeout"):
            result = await runner.run_streaming(
                [
                    sys.executable,
                    "-c",
                    f"import sys,time; sys.{stream}.write('scoped-agent tail'); sys.{stream}.flush(); time.sleep(30)",
                ],
                **{f"on_{stream}": fail},
                wall_timeout_seconds=0.3 if reason == COMMAND_TIMEOUT_REASON else 5,
                idle_timeout_seconds=0.3 if reason == COMMAND_IDLE_TIMEOUT_REASON else None,
            )
    assert result.returncode == 124
    assert result.reason_code == reason
    assert _failure_reason_for_result(result) == (
        "AGENT_TIMEOUT" if reason == COMMAND_TIMEOUT_REASON else "AGENT_IDLE_TIMEOUT"
    )
    assert received
    assert "scoped-agent" not in repr(received)
    assert "scoped-agent" not in repr(logs)
    assert "scoped-agent" not in result.stdout + result.stderr


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("kind", ["tagged_error", "tagged_cancel", "error", "cancel", "success"])
async def test_final_sink_failure_preserves_primary_exception_or_success_failure(
    tmp_path, monkeypatch, stream, kind
):
    scope, _ = consumers(tmp_path, monkeypatch)
    delegate = AsyncMock()
    primary = (
        asyncio.CancelledError("primary cancellation")
        if "cancel" in kind
        else OSError("primary teardown")
    )
    if kind.startswith("tagged"):
        mark_masked_command_reason_code(primary, COMMAND_IDLE_TIMEOUT_REASON)
    sink = RuntimeError("synthetic final sink scoped-github")

    async def run(*args, **kwargs):
        await kwargs[f"on_{stream}"]("scoped-github tail")
        if kind != "success":
            raise primary
        return CommandResult(0, "scoped-github", "")

    def fail(text):
        assert "scoped-github" not in text
        raise sink

    delegate.run_streaming.side_effect = run
    with structlog.testing.capture_logs() as logs:
        async with scope.scope("ws_timeout"):
            with pytest.raises(type(sink if kind == "success" else primary)) as caught:
                await HostedCommandRunner(scope, delegate).run_streaming(
                    ["synthetic"], **{f"on_{stream}": fail}
                )
    assert caught.value is (sink if kind == "success" else primary)
    assert masked_agent_timeout_reason_code(caught.value) == (
        "AGENT_IDLE_TIMEOUT" if kind.startswith("tagged") else None
    )
    assert "scoped-github" not in repr(logs)


@pytest.mark.parametrize("reason", [None, COMMAND_TIMEOUT_REASON, COMMAND_IDLE_TIMEOUT_REASON])
@pytest.mark.parametrize("primary", [None, "error", "cancel"])
async def test_new_cancellation_during_final_flush_keeps_timeout_classification(
    tmp_path, monkeypatch, reason, primary
):
    scope, _ = consumers(tmp_path, monkeypatch)
    delegate = AsyncMock()
    cancellation = asyncio.CancelledError("final sink cancellation")

    async def run(*args, **kwargs):
        await kwargs["on_stdout"]("scoped-agent tail")
        if primary is not None:
            error = asyncio.CancelledError("primary") if primary == "cancel" else OSError("primary")
            if reason is not None:
                mark_masked_command_reason_code(error, reason)
            raise error
        return CommandResult(124 if reason else 0, "scoped-agent tail", "", reason_code=reason)

    async def cancel(text):
        assert "scoped-agent" not in text
        raise cancellation

    delegate.run_streaming.side_effect = run
    with pytest.raises(asyncio.CancelledError) as caught:
        async with scope.scope("ws_timeout"):
            await HostedCommandRunner(scope, delegate).run_streaming(
                ["synthetic"], on_stdout=cancel
            )
    assert caught.value is cancellation
    assert getattr(caught.value, "command_reason_code", None) == reason


@pytest.mark.parametrize("outcome", ["timeout", "success", "error", "cancel"])
async def test_both_sinks_are_attempted_without_replacing_primary_outcome(
    tmp_path, monkeypatch, outcome
):
    scope, _ = consumers(tmp_path, monkeypatch)
    delegate = AsyncMock()
    primary = asyncio.CancelledError("primary") if outcome == "cancel" else OSError("primary")
    failures = [RuntimeError("stdout sink"), ValueError("stderr sink")]
    attempted = []

    async def run(*args, **kwargs):
        await kwargs["on_stdout"]("scoped-agent partial")
        await kwargs["on_stderr"]("scoped-github partial")
        if outcome in {"error", "cancel"}:
            raise primary
        return CommandResult(
            124 if outcome == "timeout" else 0,
            "",
            "",
            reason_code=COMMAND_TIMEOUT_REASON if outcome == "timeout" else None,
        )

    def fail(index, text):
        assert "scoped-agent" not in text and "scoped-github" not in text
        attempted.append(index)
        raise failures[index]

    delegate.run_streaming.side_effect = run
    async with scope.scope("ws_timeout"):
        call = HostedCommandRunner(scope, delegate).run_streaming(
            ["synthetic"],
            on_stdout=lambda text: fail(0, text),
            on_stderr=lambda text: fail(1, text),
        )
        if outcome == "timeout":
            assert (await call).reason_code == COMMAND_TIMEOUT_REASON
        else:
            expected = failures[0] if outcome == "success" else primary
            with pytest.raises(type(expected)) as caught:
                await call
            assert caught.value is expected
    assert attempted == [0, 1]


@pytest.mark.parametrize("reason", [COMMAND_TIMEOUT_REASON, COMMAND_IDLE_TIMEOUT_REASON])
async def test_actual_tagged_teardown_error_survives_final_flush(tmp_path, monkeypatch, reason):
    scope, _ = consumers(tmp_path, monkeypatch)
    started = []
    teardown_error = OSError("primary reap failure")

    async def fail_teardown(process, wait_task):
        started.append(process)
        raise teardown_error

    async def fail_sink(text):
        assert "scoped-agent" not in text
        raise RuntimeError("secondary log failure")

    monkeypatch.setattr(commands, "_terminate_process", fail_teardown)
    try:
        async with scope.scope("ws_timeout"):
            with pytest.raises(OSError) as caught:
                await HostedCommandRunner(scope, AsyncioSubprocessRunner()).run_streaming(
                    [
                        sys.executable,
                        "-c",
                        "import sys,time; sys.stdout.write('scoped-agent tail'); sys.stdout.flush(); time.sleep(30)",
                    ],
                    on_stdout=fail_sink,
                    wall_timeout_seconds=0.3 if reason == COMMAND_TIMEOUT_REASON else 5,
                    idle_timeout_seconds=0.3 if reason == COMMAND_IDLE_TIMEOUT_REASON else None,
                )
        assert caught.value is teardown_error
        assert masked_agent_timeout_reason_code(caught.value) == (
            "AGENT_TIMEOUT" if reason == COMMAND_TIMEOUT_REASON else "AGENT_IDLE_TIMEOUT"
        )
    finally:
        for process in set(started):
            if process.returncode is None:
                process.kill()
            await process.wait()


async def test_task_cancellation_while_final_sink_waits_preserves_classified_result(
    tmp_path, monkeypatch
):
    scope, _ = consumers(tmp_path, monkeypatch)
    delegate = AsyncMock()
    entered = asyncio.Event()

    async def run(*args, **kwargs):
        await kwargs["on_stdout"]("scoped-agent tail")
        return CommandResult(124, "", "", reason_code=COMMAND_TIMEOUT_REASON)

    async def waiting_sink(text):
        assert "scoped-agent" not in text
        entered.set()
        await asyncio.Event().wait()

    async def execute():
        async with scope.scope("ws_timeout"):
            await HostedCommandRunner(scope, delegate).run_streaming(
                ["synthetic"], on_stdout=waiting_sink
            )

    delegate.run_streaming.side_effect = run
    task = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert masked_agent_timeout_reason_code(caught.value) == "AGENT_TIMEOUT"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
