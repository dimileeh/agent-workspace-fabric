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


@pytest.mark.unit
async def test_successful_run_cancels_wait_bookkeeping_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``proc.wait()`` bookkeeping task is torn down on the normal
    success path instead of being left orphaned in the event loop.

    Without the ``finally: wait_task.cancel()`` teardown, the task created to
    back timeout/cancellation handling survives the happy path untouched until
    GC reaps it.
    """

    class _CancelSpy:
        def __init__(self, task: asyncio.Task[int]) -> None:
            self._task = task
            self.cancel_calls = 0

        def cancel(self, *args: object, **kwargs: object) -> bool:
            self.cancel_calls += 1
            return self._task.cancel(*args, **kwargs)

        def __getattr__(self, name: str) -> object:
            return getattr(self._task, name)

        def __await__(self):  # type: ignore[no-untyped-def]
            return self._task.__await__()

    import awf.common.commands as commands_mod

    spies: list[_CancelSpy] = []
    real_create_task = commands_mod.asyncio.create_task

    def _spying_create_task(coro, **kwargs):  # type: ignore[no-untyped-def]
        task = real_create_task(coro, **kwargs)
        if getattr(getattr(coro, "cr_code", None), "co_name", "") == "wait":
            spy = _CancelSpy(task)
            spies.append(spy)
            return spy
        return task

    monkeypatch.setattr(commands_mod.asyncio, "create_task", _spying_create_task)

    runner = AsyncioSubprocessRunner()
    result = await runner.run(["/bin/echo", "hi"])

    assert result.returncode == 0
    assert len(spies) == 1
    assert spies[0].cancel_calls == 1
