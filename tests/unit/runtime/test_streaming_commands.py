"""Streaming command runner tests."""

from __future__ import annotations

import sys
import time

import pytest

from awf.common.commands import AsyncioSubprocessRunner, FakeCommandRunner


@pytest.mark.unit
async def test_asyncio_runner_streams_stdout_and_stderr_before_completion() -> None:
    runner = AsyncioSubprocessRunner()
    frames: list[tuple[str, str, float]] = []
    started = time.perf_counter()

    async def on_stdout(data: str) -> None:
        frames.append(("stdout", data, time.perf_counter()))

    async def on_stderr(data: str) -> None:
        frames.append(("stderr", data, time.perf_counter()))

    result = await runner.run_streaming(
        [
            sys.executable,
            "-c",
            (
                "import sys,time;"
                "sys.stdout.write('first\\n');sys.stdout.flush();"
                "time.sleep(0.15);"
                "sys.stderr.write('warn\\n');sys.stderr.flush();"
                "time.sleep(0.15);"
                "sys.stdout.write('last\\n');sys.stdout.flush()"
            ),
        ],
        on_stdout=on_stdout,
        on_stderr=on_stderr,
    )
    finished = time.perf_counter()

    assert result.returncode == 0
    assert result.stdout == "first\nlast\n"
    assert result.stderr == "warn\n"
    assert [fd for fd, _data, _at in frames] == ["stdout", "stderr", "stdout"]
    assert frames[0][2] - started < finished - frames[0][2]


@pytest.mark.unit
async def test_fake_runner_replays_streaming_callbacks() -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=7, stdout="out\n", stderr="err\n")
    stdout: list[str] = []
    stderr: list[str] = []

    result = await runner.run_streaming(
        ["example"],
        on_stdout=stdout.append,
        on_stderr=stderr.append,
        cwd="/tmp/work",
    )

    assert result.returncode == 7
    assert stdout == ["out\n"]
    assert stderr == ["err\n"]
    assert runner.calls[0].args == ["example"]
    assert runner.calls[0].cwd == "/tmp/work"
