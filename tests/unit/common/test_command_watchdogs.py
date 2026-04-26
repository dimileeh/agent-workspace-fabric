"""Command-runner watchdog tests."""

from __future__ import annotations

import sys

import pytest

from awf.common.commands import AsyncioSubprocessRunner


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
