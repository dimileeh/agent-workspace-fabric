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
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    """Result of a subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class AsyncCommandRunner(Protocol):
    """Runs shell commands. Producton impl uses asyncio subprocess; tests fake it."""

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult: ...


StreamCallback = Callable[[str], Awaitable[None] | None]


class AsyncStreamingCommandRunner(AsyncCommandRunner, Protocol):
    """Optional extension for runners that can stream stdout/stderr chunks."""

    async def run_streaming(
        self,
        args: list[str],
        *,
        on_stdout: StreamCallback | None = None,
        on_stderr: StreamCallback | None = None,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
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
        stdout_bytes, stderr_bytes = await proc.communicate(input=input_bytes)
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
    ) -> CommandResult:
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

        async def _feed_stdin() -> None:
            if input_bytes is None or proc.stdin is None:
                return
            proc.stdin.write(input_bytes)
            await proc.stdin.drain()
            proc.stdin.close()

        async def _read_stream(
            reader: asyncio.StreamReader,
            parts: list[str],
            callback: StreamCallback | None,
        ) -> None:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                text = chunk.decode("utf-8", errors="replace")
                parts.append(text)
                if callback is not None:
                    maybe_awaitable = callback(text)
                    if inspect.isawaitable(maybe_awaitable):
                        await maybe_awaitable

        await asyncio.gather(
            _feed_stdin(),
            _read_stream(proc.stdout, stdout_parts, on_stdout),
            _read_stream(proc.stderr, stderr_parts, on_stderr),
        )
        returncode = await proc.wait()
        return CommandResult(
            returncode=returncode,
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
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

    def queue_result(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self._queued.append(CommandResult(returncode, stdout, stderr))

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
    ) -> CommandResult:
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
