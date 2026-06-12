"""Command-runner watchdog tests."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

import awf.common.commands as commands
from awf.common.commands import (
    COMMAND_TIMEOUT_REASON,
    AsyncioSubprocessRunner,
    _format_seconds,
    _terminate_process,
    _timeout_diagnostic,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"wall_timeout_seconds": 0.0}, "wall_timeout_seconds must be positive"),
        ({"idle_timeout_seconds": -0.1}, "idle_timeout_seconds must be positive"),
    ],
)
async def test_asyncio_runner_rejects_non_positive_streaming_timeouts(
    kwargs: dict[str, float],
    message: str,
) -> None:
    runner = AsyncioSubprocessRunner()

    with pytest.raises(ValueError, match=message):
        await runner.run_streaming([sys.executable, "-c", "print('unused')"], **kwargs)


@pytest.mark.unit
async def test_asyncio_runner_wall_timeout_terminates_and_preserves_partial_output() -> None:
    runner = AsyncioSubprocessRunner()
    stdout: list[str] = []
    stderr: list[str] = []

    result = await runner.run_streaming(
        [
            sys.executable,
            "-c",
            ("import sys,time;sys.stdout.write('started\\n');sys.stdout.flush();time.sleep(10)"),
        ],
        on_stdout=stdout.append,
        on_stderr=stderr.append,
        wall_timeout_seconds=0.2,
    )

    assert result.returncode == 124
    assert result.reason_code == "COMMAND_TIMEOUT"
    assert result.stdout == "started\n"
    assert "wall timeout after 0.2s" in result.stderr
    assert stdout == ["started\n"]
    assert stderr == [result.stderr]


@pytest.mark.unit
async def test_asyncio_runner_idle_timeout_terminates_when_output_stalls() -> None:
    runner = AsyncioSubprocessRunner()
    stdout: list[str] = []
    stderr: list[str] = []

    result = await runner.run_streaming(
        [
            sys.executable,
            "-c",
            ("import sys,time;sys.stdout.write('first\\n');sys.stdout.flush();time.sleep(10)"),
        ],
        on_stdout=stdout.append,
        on_stderr=stderr.append,
        wall_timeout_seconds=10.0,
        idle_timeout_seconds=1.5,
    )

    assert result.returncode == 124
    assert result.reason_code == "COMMAND_IDLE_TIMEOUT"
    assert result.stdout == "first\n"
    assert "idle timeout after 1.5s without output" in result.stderr
    assert stdout == ["first\n"]
    assert stderr == [result.stderr]


@pytest.mark.unit
async def test_asyncio_runner_cancellation_terminates_subprocess(tmp_path: Path) -> None:
    runner = AsyncioSubprocessRunner()
    pid_file = tmp_path / "child.pid"

    task = asyncio.create_task(
        runner.run_streaming(
            [
                sys.executable,
                "-c",
                (
                    "import os,pathlib,sys,time;"
                    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));"
                    "sys.stdout.write('ready\\n');sys.stdout.flush();"
                    "time.sleep(30)"
                ),
                str(pid_file),
            ],
        )
    )
    pid = await _wait_for_pid_file(pid_file)

    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not _pid_exists(pid)
    finally:
        if _pid_exists(pid):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.unit
async def test_asyncio_runner_plain_run_terminates_subprocess_on_cancellation(
    tmp_path: Path,
) -> None:
    runner = AsyncioSubprocessRunner()
    pid_file = tmp_path / "child.pid"

    task = asyncio.create_task(
        runner.run(
            [
                sys.executable,
                "-c",
                (
                    "import os,pathlib,sys,time;"
                    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));"
                    "time.sleep(30)"
                ),
                str(pid_file),
            ],
        )
    )
    pid = await _wait_for_pid_file(pid_file)

    try:
        # A timeout wrapper (``asyncio.wait_for``) cancels ``run`` mid-flight;
        # the spawned process must be reaped, not left orphaned.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not _pid_exists(pid)
    finally:
        if _pid_exists(pid):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.unit
async def test_asyncio_runner_ignores_child_that_closes_stdin_early() -> None:
    runner = AsyncioSubprocessRunner()
    stdout: list[str] = []

    result = await runner.run_streaming(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdin.close(); print('closed')",
        ],
        input_bytes=b"input the child will not read",
        on_stdout=stdout.append,
    )

    assert result.returncode == 0
    assert result.stdout == "closed\n"
    assert "".join(stdout) == "closed\n"


@pytest.mark.unit
async def test_asyncio_runner_watchdog_exits_when_process_finishes_before_deadline() -> None:
    runner = AsyncioSubprocessRunner()

    result = await runner.run_streaming(
        [sys.executable, "-c", "print('quick')"],
        wall_timeout_seconds=10.0,
        idle_timeout_seconds=10.0,
    )

    assert result.returncode == 0
    assert result.reason_code is None
    assert result.stdout == "quick\n"
    assert result.stderr == ""


@pytest.mark.unit
async def test_terminate_process_noops_when_process_already_exited() -> None:
    class _AlreadyExitedProcess:
        returncode = 0

        def terminate(self) -> None:
            raise AssertionError("terminate should not be called")

        def kill(self) -> None:
            raise AssertionError("kill should not be called")

    async def _done() -> int:
        return 0

    wait_task = asyncio.create_task(_done())
    await wait_task

    await _terminate_process(_AlreadyExitedProcess(), wait_task)  # type: ignore[arg-type]


@pytest.mark.unit
async def test_terminate_process_kills_when_terminate_grace_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StubbornProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.terminated = False
            self.killed = False
            self._done = asyncio.Event()

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            self._done.set()

        async def wait(self) -> int:
            await self._done.wait()
            assert self.returncode is not None
            return self.returncode

    proc = _StubbornProcess()
    wait_task = asyncio.create_task(proc.wait())
    monkeypatch.setattr(commands, "_TERMINATE_GRACE_SECONDS", 0.01)

    await _terminate_process(proc, wait_task)  # type: ignore[arg-type]

    assert proc.terminated is True
    assert proc.killed is True
    assert await wait_task == -9


@pytest.mark.unit
async def test_terminate_process_skips_kill_when_process_exits_after_grace_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SlowExitProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.terminated = False
            self.killed = False

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            await asyncio.sleep(0.02)
            assert self.returncode is not None
            return self.returncode

    proc = _SlowExitProcess()
    wait_task = asyncio.create_task(proc.wait())
    monkeypatch.setattr(commands, "_TERMINATE_GRACE_SECONDS", 0.01)

    await _terminate_process(proc, wait_task)  # type: ignore[arg-type]

    assert proc.terminated is True
    assert proc.killed is False
    assert await wait_task == -15


@pytest.mark.unit
def test_timeout_diagnostic_formats_unknown_wall_timeout() -> None:
    assert _format_seconds(None) == "unknown"
    assert (
        _timeout_diagnostic(
            COMMAND_TIMEOUT_REASON,
            wall_timeout_seconds=None,
            idle_timeout_seconds=None,
        )
        == "command wall timeout after unknowns\n"
    )


@pytest.mark.unit
async def test_wait_for_pid_file_retries_when_file_disappears_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RacyPidPath:
        def __init__(self) -> None:
            self.values: list[str | FileNotFoundError] = [FileNotFoundError(), "123"]

        def exists(self) -> bool:
            return True

        def read_text(self) -> str:
            value = self.values.pop(0)
            if isinstance(value, FileNotFoundError):
                raise value
            return value

        def __str__(self) -> str:
            return "racy.pid"

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    assert await _wait_for_pid_file(_RacyPidPath()) == 123  # type: ignore[arg-type]


async def _wait_for_pid_file(path: Path) -> int:
    last_value: str | None = None
    for _ in range(200):
        try:
            last_value = path.read_text()
            try:
                return int(last_value)
            except ValueError:
                pass
        except FileNotFoundError:
            pass
        await asyncio.sleep(0.01)
    raise AssertionError(f"{path} did not contain a process id; last value={last_value!r}")


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
