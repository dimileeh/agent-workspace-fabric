"""Streaming command runner tests."""

from __future__ import annotations

import sys
import time

import pytest

from awf.common.commands import AsyncioSubprocessRunner, FakeCommandRunner


@pytest.mark.unit
async def test_asyncio_runner_captures_subprocess_output_and_input() -> None:
    runner = AsyncioSubprocessRunner()

    result = await runner.run(
        [
            sys.executable,
            "-c",
            "import sys; data=sys.stdin.read(); print(data.upper(), end='')",
        ],
        input_bytes=b"payload",
    )

    assert result.returncode == 0
    assert result.stdout == "PAYLOAD"
    assert result.stderr == ""


@pytest.mark.unit
async def test_asyncio_runner_streams_stdout_and_stderr_before_completion() -> None:
    runner = AsyncioSubprocessRunner()
    frames: list[tuple[str, str, float]] = []

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
    assert frames[0][2] < finished - 0.1


@pytest.mark.unit
async def test_asyncio_runner_preserves_utf8_split_across_stream_chunks() -> None:
    runner = AsyncioSubprocessRunner()
    stdout: list[str] = []

    result = await runner.run_streaming(
        [
            sys.executable,
            "-c",
            (
                "import sys,time;"
                "sys.stdout.buffer.write(b'before \\xf0\\x9f');"
                "sys.stdout.flush();"
                "time.sleep(0.05);"
                "sys.stdout.buffer.write(b'\\x98\\x80 after\\n');"
                "sys.stdout.flush()"
            ),
        ],
        on_stdout=stdout.append,
    )

    assert result.returncode == 0
    assert result.stdout == "before \U0001f600 after\n"
    assert "".join(stdout) == result.stdout


@pytest.mark.unit
async def test_asyncio_runner_rejects_non_positive_streaming_timeouts() -> None:
    runner = AsyncioSubprocessRunner()

    with pytest.raises(ValueError, match="wall_timeout_seconds must be positive"):
        await runner.run_streaming([sys.executable, "-c", "pass"], wall_timeout_seconds=0)

    with pytest.raises(ValueError, match="idle_timeout_seconds must be positive"):
        await runner.run_streaming([sys.executable, "-c", "pass"], idle_timeout_seconds=-1)


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


@pytest.mark.unit
async def test_fake_runner_replays_async_streaming_callbacks_and_records_input() -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="out\n", stderr="err\n")
    frames: list[tuple[str, str]] = []

    async def on_stdout(data: str) -> None:
        frames.append(("stdout", data))

    async def on_stderr(data: str) -> None:
        frames.append(("stderr", data))

    result = await runner.run_streaming(
        ["example", "--flag"],
        on_stdout=on_stdout,
        on_stderr=on_stderr,
        input_bytes=b"payload",
        cwd="/tmp/work",
        wall_timeout_seconds=10,
        idle_timeout_seconds=5,
    )

    assert result.returncode == 0
    assert frames == [("stdout", "out\n"), ("stderr", "err\n")]
    assert runner.calls[0].args == ["example", "--flag"]
    assert runner.calls[0].input_bytes == b"payload"
    assert runner.calls[0].cwd == "/tmp/work"
