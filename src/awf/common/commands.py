"""Async command runner abstraction.

Everything in AWF that shells out (git, docker compose, coding CLIs) goes
through this Protocol instead of calling ``asyncio.create_subprocess_exec``
directly. Two benefits:

1. Unit tests inject a fake runner (FakeCommandRunner below) that records
   calls and returns canned output. No real subprocess needed.
2. A single place to add cross-cutting concerns later (retries, timeouts,
   trace context propagation) without touching every call site.
"""

from __future__ import annotations

import asyncio
import codecs
import inspect
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

COMMAND_TIMEOUT_REASON = "COMMAND_TIMEOUT"
COMMAND_IDLE_TIMEOUT_REASON = "COMMAND_IDLE_TIMEOUT"
_TIMEOUT_RETURN_CODE = 124
_TERMINATE_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class CommandResult:
    """Result of a subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str
    reason_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class AsyncCommandRunner(Protocol):
    """Runs shell commands. Producton impl uses asyncio subprocess; tests fake it."""

    async def run(  # pragma: no cover - Protocol method declaration only.
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult: ...


StreamCallback = Callable[[str], Awaitable[None] | None]


class AsyncStreamingCommandRunner(AsyncCommandRunner, Protocol):
    """Optional extension for runners that can stream stdout/stderr chunks."""

    async def run_streaming(  # pragma: no cover - Protocol method declaration only.
        self,
        args: list[str],
        *,
        on_stdout: StreamCallback | None = None,
        on_stderr: StreamCallback | None = None,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
        wall_timeout_seconds: float | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> CommandResult: ...


class AsyncioSubprocessRunner:
    """Default runner — backed by ``asyncio.create_subprocess_exec``."""

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout_bytes, stderr_bytes = await proc.communicate(input=input_bytes)
        except asyncio.CancelledError:
            # A timeout wrapper (``asyncio.wait_for``) cancels this coroutine
            # mid-``communicate``. Without explicit teardown the spawned process
            # — e.g. a wedged ``docker compose exec`` client driving a toolchain
            # probe — would be left orphaned and accumulate across workspaces.
            # Terminate and reap it before propagating the cancellation.
            await _terminate_process(proc, asyncio.create_task(proc.wait()))
            raise
        assert proc.returncode is not None
        return CommandResult(
            returncode=proc.returncode,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
        )

    async def run_streaming(
        self,
        args: list[str],
        *,
        on_stdout: StreamCallback | None = None,
        on_stderr: StreamCallback | None = None,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
        wall_timeout_seconds: float | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> CommandResult:
        _validate_timeout("wall_timeout_seconds", wall_timeout_seconds)
        _validate_timeout("idle_timeout_seconds", idle_timeout_seconds)
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        last_output_at = started_at
        timeout_reason: str | None = None

        async def _feed_stdin() -> None:
            if input_bytes is None or proc.stdin is None:
                return
            with suppress(BrokenPipeError, ConnectionResetError):
                proc.stdin.write(input_bytes)
                await proc.stdin.drain()
            proc.stdin.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await proc.stdin.wait_closed()

        async def _emit(
            parts: list[str],
            callback: StreamCallback | None,
            text: str,
        ) -> None:
            if not text:
                return
            parts.append(text)
            if callback is not None:
                maybe_awaitable = callback(text)
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable

        async def _read_stream(
            reader: asyncio.StreamReader,
            parts: list[str],
            callback: StreamCallback | None,
        ) -> None:
            nonlocal last_output_at
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    await _emit(parts, callback, decoder.decode(b"", final=True))
                    return
                last_output_at = loop.time()
                await _emit(parts, callback, decoder.decode(chunk))

        async def _watchdog(wait_task: asyncio.Task[int]) -> None:
            nonlocal timeout_reason
            if wall_timeout_seconds is None and idle_timeout_seconds is None:
                return

            wall_deadline = (
                started_at + wall_timeout_seconds if wall_timeout_seconds is not None else None
            )

            while not wait_task.done():
                now = loop.time()
                idle_deadline = (
                    last_output_at + idle_timeout_seconds
                    if idle_timeout_seconds is not None
                    else None
                )

                if wall_deadline is not None and now >= wall_deadline:
                    timeout_reason = COMMAND_TIMEOUT_REASON
                    break
                if idle_deadline is not None and now >= idle_deadline:
                    timeout_reason = COMMAND_IDLE_TIMEOUT_REASON
                    break

                deadlines = [d for d in (wall_deadline, idle_deadline) if d is not None]
                sleep_for = max(min(deadlines) - now, 0.0)
                done, _pending = await asyncio.wait({wait_task}, timeout=sleep_for)
                if done:
                    return

            if timeout_reason is not None:
                await _terminate_process(proc, wait_task)

        wait_task = asyncio.create_task(proc.wait())
        tasks = [
            asyncio.create_task(_feed_stdin()),
            asyncio.create_task(_read_stream(proc.stdout, stdout_parts, on_stdout)),
            asyncio.create_task(_read_stream(proc.stderr, stderr_parts, on_stderr)),
            wait_task,
        ]
        watchdog_task = asyncio.create_task(_watchdog(wait_task))
        tasks.append(watchdog_task)
        try:
            await asyncio.gather(*tasks)
        finally:
            if proc.returncode is None:
                await _terminate_process(proc, wait_task)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        returncode = wait_task.result()
        if timeout_reason is not None:
            diagnostic = _timeout_diagnostic(
                timeout_reason,
                wall_timeout_seconds=wall_timeout_seconds,
                idle_timeout_seconds=idle_timeout_seconds,
            )
            await _emit(stderr_parts, on_stderr, diagnostic)
            returncode = _TIMEOUT_RETURN_CODE

        return CommandResult(
            returncode=returncode,
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
            reason_code=timeout_reason,
        )


@dataclass
class _RecordedCall:
    args: list[str]
    input_bytes: bytes | None
    cwd: str | None


class FakeCommandRunner:
    """Test double — records every invocation and returns canned results.

    Use ``queue_result(...)`` to push results that will be returned in FIFO
    order. Later tests can inspect ``self.calls`` for the recorded argv.
    """

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []
        self._queued: list[CommandResult] = []

    def queue_result(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        reason_code: str | None = None,
    ) -> None:
        self._queued.append(CommandResult(returncode, stdout, stderr, reason_code))

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        self.calls.append(_RecordedCall(args=list(args), input_bytes=input_bytes, cwd=cwd))
        if not self._queued:
            return CommandResult(returncode=0, stdout="", stderr="")
        return self._queued.pop(0)

    async def run_streaming(
        self,
        args: list[str],
        *,
        on_stdout: StreamCallback | None = None,
        on_stderr: StreamCallback | None = None,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
        wall_timeout_seconds: float | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> CommandResult:
        del wall_timeout_seconds, idle_timeout_seconds
        result = await self.run(args, input_bytes=input_bytes, cwd=cwd)
        if result.stdout and on_stdout is not None:
            maybe_awaitable = on_stdout(result.stdout)
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
        if result.stderr and on_stderr is not None:
            maybe_awaitable = on_stderr(result.stderr)
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
        return result


def _validate_timeout(name: str, value: float | None) -> None:
    if value is not None and value <= 0:
        raise ValueError(f"{name} must be positive")


async def _terminate_process(
    proc: asyncio.subprocess.Process,
    wait_task: asyncio.Task[int],
) -> None:
    if proc.returncode is not None:
        return

    wait_for_exit = wait_task
    if wait_task.done():
        wait_for_exit = asyncio.create_task(proc.wait())

    with suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(
            asyncio.shield(wait_for_exit),
            timeout=_TERMINATE_GRACE_SECONDS,
        )
    except TimeoutError:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
        await asyncio.shield(wait_for_exit)


def _timeout_diagnostic(
    reason_code: str,
    *,
    wall_timeout_seconds: float | None,
    idle_timeout_seconds: float | None,
) -> str:
    if reason_code == COMMAND_IDLE_TIMEOUT_REASON:
        return (
            f"command idle timeout after {_format_seconds(idle_timeout_seconds)}s without output\n"
        )
    return f"command wall timeout after {_format_seconds(wall_timeout_seconds)}s\n"


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:g}"
