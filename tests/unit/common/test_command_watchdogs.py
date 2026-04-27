"""Command-runner watchdog tests."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

from awf.common.commands import AsyncioSubprocessRunner


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
            (
                "import sys,time;"
                "sys.stdout.write('started\\n');sys.stdout.flush();"
                "time.sleep(10)"
            ),
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
            (
                "import sys,time;"
                "sys.stdout.write('first\\n');sys.stdout.flush();"
                "time.sleep(10)"
            ),
        ],
        on_stdout=stdout.append,
        on_stderr=stderr.append,
        wall_timeout_seconds=5.0,
        idle_timeout_seconds=0.2,
    )

    assert result.returncode == 124
    assert result.reason_code == "COMMAND_IDLE_TIMEOUT"
    assert result.stdout == "first\n"
    assert "idle timeout after 0.2s without output" in result.stderr
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
    await _wait_for_file(pid_file)
    pid = int(pid_file.read_text())

    try:
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
    assert stdout == ["closed\n"]


async def _wait_for_file(path: Path) -> None:
    for _ in range(200):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{path} was not created")


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
