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
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

from awf.common.logging import get_logger

COMMAND_TIMEOUT_REASON = "COMMAND_TIMEOUT"
COMMAND_IDLE_TIMEOUT_REASON = "COMMAND_IDLE_TIMEOUT"
_TIMEOUT_RETURN_CODE = 124
_TERMINATE_GRACE_SECONDS = 5.0

_log = get_logger(__name__)


@dataclass(frozen=True)
class CommandResult:
    """Result of a subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str
    reason_code: str | None = None
    # Raw stdout when the runner captured bytes (e.g. AsyncioSubprocessRunner).
    # Callers that need exact pathname bytes from ``-z`` Git output must prefer
    # this over ``stdout``, which is UTF-8-decoded with ``errors="replace"``.
    stdout_bytes: bytes | None = None

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
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult: ...


StreamCallback = Callable[[str], Awaitable[None] | None]

ActivityProbe = Callable[[], Awaitable[bool | None] | bool | None]
"""Answers "did anything change since the previous probe?".

Injected by callers that can observe liveness the child's stdout cannot show —
an agent CLI in print mode writes files for an hour before emitting a byte. The
probe owns its own baseline: each call reports activity *since the last call*.

``None`` means "could not tell" and is treated as activity, so only a probe that
positively reports "nothing moved" lets the idle timeout fire; the wall timeout
stays the hard cap for a child that really is wedged. Kept generic here; the
concrete worktree-scanning implementation lives in
``awf.adapters.worktree_activity`` (AGENTS.md: core stays project-neutral).
"""


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
        activity_probe: ActivityProbe | None = None,
    ) -> CommandResult: ...


class AsyncioSubprocessRunner:
    """Default runner — backed by ``asyncio.create_subprocess_exec``."""

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        # Validate before spawning: an invalid (non-positive) timeout must fail
        # without launching — and then leaking — the child process.
        _validate_timeout("timeout_seconds", timeout_seconds)
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=None if env is None else dict(env),
        )
        wait_task = asyncio.create_task(proc.wait())
        try:
            if timeout_seconds is not None:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(input=input_bytes),
                    timeout=timeout_seconds,
                )
            else:
                stdout_bytes, stderr_bytes = await proc.communicate(input=input_bytes)
        except TimeoutError:
            await _terminate_process(proc, wait_task)
            diagnostic = _timeout_diagnostic(
                COMMAND_TIMEOUT_REASON,
                wall_timeout_seconds=timeout_seconds,
                idle_timeout_seconds=None,
            )
            return CommandResult(
                returncode=_TIMEOUT_RETURN_CODE,
                stdout="",
                stderr=diagnostic,
                reason_code=COMMAND_TIMEOUT_REASON,
            )
        except asyncio.CancelledError:
            # A timeout wrapper (``asyncio.wait_for``) cancels this coroutine
            # mid-``communicate``. Without explicit teardown the spawned process
            # — e.g. a wedged ``docker compose exec`` client driving a toolchain
            # probe — would be left orphaned and accumulate across workspaces.
            # Terminate and reap it before propagating the cancellation.
            await _terminate_process(proc, wait_task)
            raise
        finally:
            # On the success path ``wait_task`` is never awaited — ``communicate``
            # reaps the process itself — so cancel the bookkeeping task rather
            # than leave it orphaned in the event loop until GC. On the
            # timeout/cancel paths ``_terminate_process`` has already reaped it,
            # so this cancel is a no-op.
            wait_task.cancel()
        assert proc.returncode is not None
        return CommandResult(
            returncode=proc.returncode,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            stdout_bytes=stdout_bytes,
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
        activity_probe: ActivityProbe | None = None,
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

        async def _observed_activity() -> bool | None:
            """Ask the injected probe whether the watched state moved.

            Fails **open**: a probe that cannot answer returns ``None``
            ("unknown"), which the watchdog counts as activity. An unreadable or
            over-budget probe says nothing about whether the child is alive, and
            killing a healthy run on that non-answer is the #932 defect again.
            The wall deadline is the hard cap that still ends a wedged run.
            ``CancelledError`` is a ``BaseException`` and is deliberately not
            absorbed.
            """
            assert activity_probe is not None
            try:
                maybe_awaitable = activity_probe()
                observed = (
                    await maybe_awaitable
                    if inspect.isawaitable(maybe_awaitable)
                    else maybe_awaitable
                )
            except (OSError, TimeoutError) as exc:
                _log.warning(
                    "command.idle_watchdog.activity_probe_failed",
                    exc_type=type(exc).__name__,
                    idle_timeout_seconds=idle_timeout_seconds,
                )
                return None
            return None if observed is None else bool(observed)

        async def _watchdog(wait_task: asyncio.Task[int]) -> None:
            nonlocal timeout_reason, last_output_at
            if wall_timeout_seconds is None and idle_timeout_seconds is None:
                return

            wall_deadline = (
                started_at + wall_timeout_seconds if wall_timeout_seconds is not None else None
            )
            activity_extensions = 0

            while not wait_task.done():
                now = loop.time()
                idle_deadline = (
                    last_output_at + idle_timeout_seconds
                    if idle_timeout_seconds is not None
                    else None
                )

                # The wall deadline is checked first and is never extended: it
                # is the hard cap a live-but-looping agent must still hit.
                if wall_deadline is not None and now >= wall_deadline:
                    timeout_reason = COMMAND_TIMEOUT_REASON
                    break
                if idle_deadline is not None and now >= idle_deadline:
                    # Probe only at the deadline (not on a poll cadence) so the
                    # cost stays at one scan per idle window.
                    observed = await _observed_activity() if activity_probe is not None else False
                    if observed is not False:
                        # Observed activity *and* an unanswerable probe both
                        # extend: only a probe that positively reports "nothing
                        # moved" may let the idle timeout fire.
                        activity_extensions += 1
                        last_output_at = loop.time()
                        if observed is None:
                            _log.warning(
                                "command.idle_watchdog.activity_unknown_extended",
                                idle_timeout_seconds=idle_timeout_seconds,
                                extensions=activity_extensions,
                            )
                        else:
                            _log.info(
                                "command.idle_watchdog.activity_extended",
                                idle_timeout_seconds=idle_timeout_seconds,
                                extensions=activity_extensions,
                            )
                        continue
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
                activity_probe_enabled=activity_probe is not None,
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
    env: dict[str, str] | None
    timeout_seconds: float | None


_HANG_QUEUE_MARKER = object()


class FakeCommandRunner:
    """Test double — records every invocation and returns canned results.

    Use ``queue_result(...)`` to push results that will be returned in FIFO
    order. Later tests can inspect ``self.calls`` for the recorded argv.
    """

    def __init__(self) -> None:
        self.calls: list[_RecordedCall] = []
        self._queued: list[CommandResult | object] = []
        self._responders: list[tuple[Callable[[list[str]], bool], CommandResult]] = []

    def respond_when(
        self,
        predicate: Callable[[list[str]], bool],
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        reason_code: str | None = None,
    ) -> None:
        """Answer every command matching ``predicate`` without consuming the queue.

        Responders are checked in registration order before the FIFO queue. Use
        them for order-independent probe commands (for example the validation
        worktree's ``git ls-files`` / ``git config --get`` guards) so scripted
        queues only have to model the commands a test actually asserts on.
        """
        self._responders.append((predicate, CommandResult(returncode, stdout, stderr, reason_code)))

    def queue_result(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        reason_code: str | None = None,
    ) -> None:
        self._queued.append(CommandResult(returncode, stdout, stderr, reason_code))

    def queue_hang(self) -> None:
        """Queue a simulated hung subprocess for per-attempt timeout tests."""

        self._queued.append(_HANG_QUEUE_MARKER)

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        # Mirror AsyncioSubprocessRunner.run: reject a non-positive timeout so the
        # fake enforces the same fail-before-spawn input contract as production
        # and no invalid call is recorded.
        _validate_timeout("timeout_seconds", timeout_seconds)
        self.calls.append(
            _RecordedCall(
                args=list(args),
                input_bytes=input_bytes,
                cwd=cwd,
                env=None if env is None else dict(env),
                timeout_seconds=timeout_seconds,
            )
        )
        for predicate, canned in self._responders:
            if predicate(list(args)):
                return canned
        if not self._queued:
            return CommandResult(returncode=0, stdout="", stderr="")
        queued = self._queued.pop(0)
        if queued is _HANG_QUEUE_MARKER:
            if timeout_seconds is not None:
                await asyncio.sleep(timeout_seconds + 0.05)
                diagnostic = _timeout_diagnostic(
                    COMMAND_TIMEOUT_REASON,
                    wall_timeout_seconds=timeout_seconds,
                    idle_timeout_seconds=None,
                )
                return CommandResult(
                    returncode=_TIMEOUT_RETURN_CODE,
                    stdout="",
                    stderr=diagnostic,
                    reason_code=COMMAND_TIMEOUT_REASON,
                )
            await asyncio.sleep(3600.0)
            return CommandResult(returncode=0, stdout="", stderr="")
        assert isinstance(queued, CommandResult)
        return queued

    async def run_streaming(
        self,
        args: list[str],
        *,
        on_stdout: StreamCallback | None = None,
        on_stderr: StreamCallback | None = None,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        wall_timeout_seconds: float | None = None,
        idle_timeout_seconds: float | None = None,
        activity_probe: ActivityProbe | None = None,
    ) -> CommandResult:
        del wall_timeout_seconds, idle_timeout_seconds, activity_probe
        result = await self.run(
            args,
            input_bytes=input_bytes,
            cwd=cwd,
            env=env,
        )
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
    activity_probe_enabled: bool = False,
) -> str:
    if reason_code == COMMAND_IDLE_TIMEOUT_REASON:
        # Only widen the wording when a probe actually watched for activity, so
        # the message never claims a check the run did not perform.
        suffix = " or worktree activity" if activity_probe_enabled else ""
        return (
            f"command idle timeout after {_format_seconds(idle_timeout_seconds)}s "
            f"without output{suffix}\n"
        )
    return f"command wall timeout after {_format_seconds(wall_timeout_seconds)}s\n"


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:g}"
