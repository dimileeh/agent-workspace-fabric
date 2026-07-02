"""Per-attempt subprocess timeout coverage for command runners."""

from __future__ import annotations

import asyncio

import pytest

from awf.common.commands import (
    _TIMEOUT_RETURN_CODE,
    COMMAND_TIMEOUT_REASON,
    AsyncioSubprocessRunner,
    FakeCommandRunner,
)


@pytest.mark.unit
async def test_runner_run_timeout_kills_hung_subprocess() -> None:
    runner = FakeCommandRunner()
    runner.queue_hang()

    result = await runner.run(["gh", "api", "graphql"], timeout_seconds=0.05)

    assert result.returncode == _TIMEOUT_RETURN_CODE
    assert result.reason_code == COMMAND_TIMEOUT_REASON
    assert "timeout" in result.stderr.lower()


@pytest.mark.unit
async def test_asyncio_subprocess_runner_honors_timeout() -> None:
    runner = AsyncioSubprocessRunner()
    result = await runner.run(
        ["sleep", "10"],
        timeout_seconds=0.1,
    )

    assert result.returncode == _TIMEOUT_RETURN_CODE
    assert result.reason_code == COMMAND_TIMEOUT_REASON


@pytest.mark.unit
async def test_fake_runner_without_timeout_hang_can_be_cancelled() -> None:
    runner = FakeCommandRunner()
    runner.queue_hang()

    task = asyncio.create_task(runner.run(["gh", "version"]))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
