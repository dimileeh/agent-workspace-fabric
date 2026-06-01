"""Scoped coverage tests for :mod:`awf.common.commands`.

These exercise the ``AsyncioSubprocessRunner.run_streaming`` cleanup path
(line 217 — cancelling tasks that are still pending when ``gather`` propagates
an exception) and the watchdog early-exit branches (177->198, 198->exit) using
an injected fake subprocess so no real process, network, or sleeping is needed.
"""

from __future__ import annotations

import asyncio

import pytest

import awf.common.commands as commands
from awf.common.commands import AsyncioSubprocessRunner


class _FakeStreamReader:
    """Minimal ``asyncio.StreamReader`` stand-in feeding canned chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _FakeProcess:
    """Fake subprocess whose ``wait()`` only completes when released.

    This lets a test hold ``proc.wait()`` (and therefore ``wait_task``) pending
    while another task raises, forcing the ``finally`` cleanup in
    ``run_streaming`` to cancel the still-running tasks.
    """

    def __init__(
        self,
        *,
        stdout_chunks: list[bytes],
        stderr_chunks: list[bytes] | None = None,
        finish_immediately: bool = False,
    ) -> None:
        self.stdin = None
        self.stdout = _FakeStreamReader(stdout_chunks)
        self.stderr = _FakeStreamReader(stderr_chunks or [])
        self.returncode: int | None = 0 if finish_immediately else None
        self.terminated = False
        self.killed = False
        self._release = asyncio.Event()
        if finish_immediately:
            self._release.set()

    def release(self) -> None:
        self._release.set()

    async def wait(self) -> int:
        await self._release.wait()
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._release.set()

    def kill(self) -> None:  # pragma: no cover - defensive, not expected here
        self.killed = True
        self.returncode = -9
        self._release.set()


def _patch_exec(monkeypatch: pytest.MonkeyPatch, proc: _FakeProcess) -> None:
    async def _fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> _FakeProcess:
        return proc

    monkeypatch.setattr(
        commands.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )


@pytest.mark.unit
async def test_run_streaming_cancels_pending_tasks_when_callback_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising stdout callback makes ``gather`` propagate while ``wait`` is
    still pending, so the ``finally`` block cancels the not-yet-done tasks
    (the ``not task.done()`` true branch + ``task.cancel()`` on line 217)."""

    proc = _FakeProcess(stdout_chunks=[b"boom-chunk"])
    _patch_exec(monkeypatch, proc)
    runner = AsyncioSubprocessRunner()

    class _BoomError(RuntimeError):
        pass

    def _exploding_on_stdout(_text: str) -> None:
        raise _BoomError("callback failed")

    with pytest.raises(_BoomError, match="callback failed"):
        await runner.run_streaming(
            ["irrelevant"],
            on_stdout=_exploding_on_stdout,
        )

    # The fake process never completed on its own; the cleanup path must have
    # terminated it (returncode set by terminate / cancellation handling).
    assert proc.returncode is not None
    assert proc.terminated is True


@pytest.mark.unit
async def test_run_streaming_watchdog_exits_without_terminate_when_already_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the process is already finished by the time the watchdog runs, the
    while-loop is skipped (177->198) and no timeout is recorded, so the
    watchdog returns without terminating (198->exit)."""

    proc = _FakeProcess(stdout_chunks=[b"done\n"], finish_immediately=True)
    _patch_exec(monkeypatch, proc)
    runner = AsyncioSubprocessRunner()

    result = await runner.run_streaming(
        ["irrelevant"],
        wall_timeout_seconds=30.0,
        idle_timeout_seconds=30.0,
    )

    assert result.returncode == 0
    assert result.reason_code is None
    assert result.stdout == "done\n"
    assert proc.terminated is False
