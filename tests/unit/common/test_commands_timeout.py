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
@pytest.mark.parametrize("timeout_seconds", [0.0, -1.0])
async def test_fake_runner_rejects_non_positive_timeout(timeout_seconds: float) -> None:
    # The fake mirrors AsyncioSubprocessRunner.run: a non-positive timeout must
    # fail before the call is recorded, so tests written against the fake catch
    # the same invalid-input contract production enforces.
    runner = FakeCommandRunner()

    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        await runner.run(["gh", "version"], timeout_seconds=timeout_seconds)

    assert runner.calls == []


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
        # ``AsyncioSubprocessRunner.run`` creates exactly one task through the
        # module-level ``asyncio.create_task`` seam this monkeypatch replaces —
        # the ``proc.wait()`` bookkeeping task. ``create_subprocess_exec`` /
        # ``communicate`` use lower-level loop primitives, so they do not route
        # through here. Wrap every intercepted task rather than matching an
        # asyncio-internal coroutine name (``cr_code.co_name``): the
        # ``assert len(spies) == 1`` below still pins that exactly one
        # bookkeeping task is created — now catching a stray second one instead
        # of silently ignoring it — and ``cancel_calls == 1`` that it is torn
        # down once.
        task = real_create_task(coro, **kwargs)
        spy = _CancelSpy(task)
        spies.append(spy)
        return spy

    monkeypatch.setattr(commands_mod.asyncio, "create_task", _spying_create_task)

    runner = AsyncioSubprocessRunner()
    result = await runner.run(["/bin/echo", "hi"])

    assert result.returncode == 0
    assert len(spies) == 1
    assert spies[0].cancel_calls == 1
