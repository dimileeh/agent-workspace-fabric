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
