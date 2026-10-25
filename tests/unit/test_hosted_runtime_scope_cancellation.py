"""Final authority cleanup must finish before a cancelled scope returns."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from awf.hosted_node.identity import MaterialDeniedError
from tests.unit.test_hosted_runtime_consumers import consumers


@pytest.mark.parametrize("boundary", ["authority", "stop", "retract"])
@pytest.mark.parametrize("revoked", [False, True])
async def test_repeated_cancel_joins_final_cleanup(tmp_path, monkeypatch, boundary, revoked):
    scope, authority = consumers(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    finalizing = False
    cleaned = False
    cancellation = asyncio.CancelledError("original watchdog")
    cancellation.command_reason_code = "AGENT_TIMEOUT"

    async def wait_at(name):
        assert scope.workspace_id.get() is None
        if boundary == name:
            entered.set()
            await release.wait()

    async def check(_workspace_id):
        if not finalizing:
            return authority.workspace.return_value
        await wait_at("authority")
        if revoked:
            raise MaterialDeniedError("GRANT_REVOKED")
        return authority.workspace.return_value

    async def stop(_workspace_id, **kwargs):
        await wait_at("stop")

    async def retract(_workspace_id, publication=None):
        nonlocal cleaned
        assert publication is (None if revoked else authority.publication.return_value)
        await wait_at("retract")
        cleaned = True

    authority.workspace.side_effect = check
    authority.retract.side_effect = retract
    authority.retract_publication.side_effect = retract
    scope.stop_workspace = AsyncMock(side_effect=stop)

    async def operation():
        nonlocal finalizing
        try:
            async with scope.scope("ws_exact"):
                finalizing = True
                raise cancellation
        finally:
            assert scope.workspace_id.get() is None

    task = asyncio.create_task(operation())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel("second cancel")
        await asyncio.sleep(0)
        task.cancel("third cancel")
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value is cancellation
        assert caught.value.command_reason_code == "AGENT_TIMEOUT"
        assert cleaned
        assert scope.workspace_id.get() is None
        if revoked:
            authority.retract.assert_awaited_once_with("ws_exact")
        else:
            authority.retract_publication.assert_awaited_once_with(
                "ws_exact", authority.publication.return_value
            )
        expected = {} if revoked else {"authority_lost": False}
        scope.stop_workspace.assert_awaited_once_with("ws_exact", **expected)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_first_cancel_during_final_check_is_not_swallowed(tmp_path, monkeypatch):
    scope, authority = consumers(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    finalizing = False

    async def check(_workspace_id):
        if finalizing:
            assert scope.workspace_id.get() is None
            entered.set()
            await release.wait()
        return authority.workspace.return_value

    authority.workspace.side_effect = check
    scope.stop_workspace = AsyncMock()

    async def operation():
        nonlocal finalizing
        async with scope.scope("ws_exact"):
            finalizing = True

    task = asyncio.create_task(operation())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel("late cancellation")
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError, match="late cancellation"):
            await task
        scope.stop_workspace.assert_awaited_once_with("ws_exact", authority_lost=False)
        authority.retract_publication.assert_awaited_once_with(
            "ws_exact", authority.publication.return_value
        )
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancel_after_final_check_before_parent_wakes_still_retracts(tmp_path, monkeypatch):
    scope, authority = consumers(tmp_path, monkeypatch)
    finalizing = False
    parent = None

    async def check(_workspace_id):
        if finalizing:
            parent.cancel("completed-check cancellation")
        return authority.workspace.return_value

    authority.workspace.side_effect = check
    scope.stop_workspace = AsyncMock()

    async def operation():
        nonlocal finalizing, parent
        parent = asyncio.current_task()
        async with scope.scope("ws_exact"):
            finalizing = True

    with pytest.raises(asyncio.CancelledError, match="completed-check cancellation"):
        await asyncio.create_task(operation())
    scope.stop_workspace.assert_awaited_once_with("ws_exact", authority_lost=False)
    authority.retract_publication.assert_awaited_once_with(
        "ws_exact", authority.publication.return_value
    )


@pytest.mark.parametrize("boundary", ["check", "stop", "retract"])
async def test_initial_cancel_joins_repeated_cleanup(tmp_path, monkeypatch, boundary):
    scope, authority = consumers(tmp_path, monkeypatch)
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    authority.association.return_value = (object(), "codex")

    async def initial(_workspace_id):
        entered.set()
        await asyncio.Event().wait()

    async def wait_at(name):
        if boundary == name:
            cleaning.set()
            await release.wait()

    async def check(*args, **kwargs):
        assert kwargs == {"current": True}
        await wait_at("check")

    async def stop(*args, **kwargs):
        assert kwargs == {"authority_lost": False}
        await wait_at("stop")

    async def retract(*args, **kwargs):
        assert args == ("ws_exact", authority.publication.return_value)
        assert not kwargs
        await wait_at("retract")

    authority.workspace.side_effect = initial
    authority.materials.side_effect = check
    authority.retract_publication.side_effect = retract
    scope.stop_workspace = AsyncMock(side_effect=stop)

    async def operation():
        async with scope.scope("ws_exact"):
            pytest.fail("cancelled initial scope entered")

    task = asyncio.create_task(operation())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel("original initial cancellation")
        await asyncio.wait_for(cleaning.wait(), 1)
        task.cancel("second")
        await asyncio.sleep(0)
        task.cancel("third")
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError, match="original initial cancellation"):
            await task
        authority.retract_publication.assert_awaited_once_with(
            "ws_exact", authority.publication.return_value
        )
        assert scope.workspace_id.get() is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
