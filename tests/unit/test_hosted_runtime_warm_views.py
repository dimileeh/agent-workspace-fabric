"""Cancellation clears secrets without stranding a retained bind mount."""

import asyncio
import threading
from unittest.mock import AsyncMock

import pytest

from awf.db.models import Workspace
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.receiver import Receiver
from awf.hosted_node.runtime_authority import WorkspaceAuthority
from awf.hosted_node.runtime_consumers import WorkspaceConsumers
from awf.hosted_node.runtime_views import owned_publication
from tests.unit.test_hosted_runtime_authority import (
    authority as authority,
)
from tests.unit.test_hosted_runtime_authority import (
    bound_workspace as bound_workspace,
)
from tests.unit.test_hosted_runtime_review_retained import deny_agent


@pytest.fixture
async def owned_workspace(bound_workspace):
    runtime, _, factory = bound_workspace
    async with factory.begin() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        row.node_id = runtime.receiver.identity.owner
    return bound_workspace


@pytest.mark.parametrize("authority_lost", [False, True])
async def test_stop_clears_current_view_and_only_authorized_resume_keeps_inode(
    owned_workspace, tmp_path, authority_lost
):
    runtime, _, _ = owned_workspace
    await runtime.workspace("ws_cloud_stable")
    path = runtime.receiver.store.root / runtime.views._key("ws_cloud_stable") / "view"
    inode = path.stat().st_ino
    consumers = WorkspaceConsumers(runtime, tmp_path)
    consumers.stop_workspace = AsyncMock()
    await consumers._stop("ws_cloud_stable", authority_lost=authority_lost)
    if authority_lost:
        assert not path.exists()
        return
    assert path.is_dir()
    assert path.stat().st_ino == inode
    assert not list(path.iterdir())
    assert await runtime.reconcile_workspace("ws_cloud_stable")
    assert not list(path.iterdir()), "retained sweep must not republish a cleared view"
    await runtime.workspace("ws_cloud_stable")
    assert path.stat().st_ino == inode
    assert (path / "github.api-key").read_bytes() == b"synthetic-github-key"


@pytest.mark.parametrize("successor", [False, True])
async def test_stale_retained_refresh_cannot_republish_clear_or_overwrite_successor(
    owned_workspace, monkeypatch, successor
):
    runtime, _, _ = owned_workspace
    await runtime.workspace("ws_cloud_stable")
    path = runtime.receiver.store.root / runtime.views._key("ws_cloud_stable") / "view"
    original = runtime.materials
    inode = path.stat().st_ino
    new_files = {"openai.api-key": b"synthetic-new", "github.api-key": b"synthetic-new-gh"}

    async def raced(*args, **kwargs):
        await runtime.retract("ws_cloud_stable", preserve_directory=True)
        if successor:
            runtime.views.publish("ws_cloud_stable", new_files)
        return await original(*args, **kwargs)

    monkeypatch.setattr(runtime, "materials", raced)
    await runtime.reconcile_workspace("ws_cloud_stable")
    assert path.stat().st_ino == inode
    if successor:
        assert (path / "github.api-key").read_bytes() == new_files["github.api-key"]
    else:
        assert not list(path.iterdir())


@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize("boundary", ["publication", "recheck"])
async def test_cancelled_initial_scope_publication_preserves_only_preexisting_mount(
    owned_workspace, tmp_path, monkeypatch, retained, boundary
):
    runtime, _, _ = owned_workspace
    if retained:
        await runtime.workspace("ws_cloud_stable")
    path = runtime.receiver.store.root / runtime.views._key("ws_cloud_stable") / "view"
    inode = path.stat().st_ino if retained else None
    published, release_writer = threading.Event(), threading.Event()
    checking = asyncio.Event()
    publish, status = runtime.views.publish, runtime.receiver.status
    active = None

    def write(*args, **kwargs):
        value = publish(*args, **kwargs)
        published.set()
        if boundary == "publication":
            assert release_writer.wait(5)
        return value

    async def check(binding):
        if asyncio.current_task() is active and published.is_set() and boundary == "recheck":
            checking.set()
            await asyncio.Event().wait()
        return await status(binding)

    monkeypatch.setattr(runtime.views, "publish", write)
    monkeypatch.setattr(runtime.receiver, "status", check)
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock()

    async def operation():
        async with scope.scope("ws_cloud_stable"):
            pytest.fail("cancelled initial publication must not enter consumer")

    active = asyncio.create_task(operation())
    try:
        if boundary == "publication":
            assert await asyncio.to_thread(published.wait, 5)
        else:
            await asyncio.wait_for(checking.wait(), 5)
        active.cancel()
        release_writer.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(active, 5)
        if retained:
            assert path.is_dir(), "cancellation during initial authorization stranded warm mount"
            assert path.stat().st_ino == inode
            assert not list(path.iterdir())
            await runtime.reconcile_workspace("ws_cloud_stable")
            assert not list(path.iterdir())
        else:
            assert not path.exists()
    finally:
        release_writer.set()
        if not active.done():
            active.cancel()
        await asyncio.gather(active, return_exceptions=True)


async def test_warm_clear_does_not_preserve_previous_boot_binding(owned_workspace):
    runtime, _, factory = owned_workspace
    await runtime.workspace("ws_cloud_stable")
    old = runtime.receiver.store.root / runtime.views._key("ws_cloud_stable") / "view"
    successor = await Receiver.start(runtime.receiver.config, factory)
    try:
        current = WorkspaceAuthority(successor)
        path = current.views.publish(
            "ws_cloud_stable",
            {"openai.api-key": b"synthetic-new", "github.api-key": b"synthetic-gh"},
        ).path
        inode = path.stat().st_ino
        await current.retract("ws_cloud_stable", preserve_directory=True)
        assert path.stat().st_ino == inode and not list(path.iterdir())
        assert not old.exists()
    finally:
        await successor.close()


@pytest.mark.parametrize("same_material", [False, True])
async def test_stale_warm_clear_and_refresh_preserve_new_publication(authority, same_material):
    runtime, _, _ = authority
    files = {"openai.api-key": b"synthetic-one", "github.api-key": b"synthetic-gh"}
    old = runtime.views.publish("ws_exact", files)
    if not same_material:
        files["openai.api-key"] = b"synthetic-two"
    current = runtime.views.publish("ws_exact", files)
    runtime.views.remove("ws_exact", publication=old, preserve_directory=True)
    assert runtime.views.refresh("ws_exact", files, old) is None
    assert runtime.views.publication("ws_exact").token == current.token
    assert (current.path / "openai.api-key").read_bytes() == files["openai.api-key"]
    runtime.views.remove("ws_exact", publication=current, preserve_directory=True)
    assert runtime.views.publication("ws_exact") is None
    assert not list(current.path.iterdir())
    with pytest.raises(MaterialDeniedError, match="INVALID_RUNTIME_VIEW"):
        runtime.views.refresh("ws_foreign", files, current)


@pytest.mark.parametrize("invalid", [None, "symlink", "oversize", "unknown"])
async def test_warm_clear_validates_all_entries_before_removing_pending_bytes(authority, invalid):
    runtime, _, _ = authority
    publication = runtime.views.publish(
        "ws_exact", {"openai.api-key": b"synthetic-one", "github.api-key": b"synthetic-gh"}
    )
    path = publication.path
    pending = path / ("foreign" if invalid == "unknown" else "openai.api-key.pending")
    if invalid == "symlink":
        pending.symlink_to(path / "openai.api-key")
    else:
        pending.write_bytes(b"x" * (65537 if invalid == "oversize" else 2))
        pending.chmod(0o400)
    if invalid:
        with pytest.raises(MaterialDeniedError):
            runtime.views.remove("ws_exact", preserve_directory=True)
        assert (path / "github.api-key").read_bytes() == b"synthetic-gh"
        assert runtime.views.publication("ws_exact").token == publication.token
        pending.unlink()
    else:
        inode = path.stat().st_ino
        runtime.views.remove("ws_exact", preserve_directory=True)
        assert path.stat().st_ino == inode and not list(path.iterdir())
        assert runtime.views.publication("ws_exact") is None


@pytest.mark.parametrize("boundary", ["association", "admission"])
@pytest.mark.parametrize("verdict", ["authorized", "denied", "uncertain"])
async def test_initial_prepublication_cancel_clears_retained_view_with_final_authority(
    owned_workspace, tmp_path, monkeypatch, boundary, verdict
):
    runtime, value, _ = owned_workspace
    await runtime.workspace("ws_cloud_stable")
    path = runtime.receiver.store.root / runtime.views._key("ws_cloud_stable") / "view"
    inode = path.stat().st_ino
    entered = asyncio.Event()
    original = getattr(runtime, boundary)
    calls = 0

    async def blocked_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await asyncio.Event().wait()
        if verdict == "uncertain":
            raise TimeoutError("synthetic unavailable authority")
        return await original(*args, **kwargs)

    monkeypatch.setattr(runtime, boundary, blocked_once)
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock()

    async def operation():
        async with scope.scope("ws_cloud_stable"):
            pytest.fail("cancelled initial authorization entered the consumer")

    task = asyncio.create_task(operation())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if verdict == "denied":
            await deny_agent(runtime, value, "revoke", monkeypatch)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        expected = {"authority_lost": False} if verdict == "authorized" else {}
        scope.stop_workspace.assert_awaited_once_with("ws_cloud_stable", **expected)
        assert runtime.views.publication("ws_cloud_stable") is None
        if verdict == "authorized":
            assert path.stat().st_ino == inode and not list(path.iterdir())
            await runtime.reconcile_workspace("ws_cloud_stable")
            assert not list(path.iterdir())
            await runtime.workspace("ws_cloud_stable")
            assert path.stat().st_ino == inode
        else:
            assert not path.exists()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancel_clears_latest_owned_write_when_thread_returns_out_of_order(
    owned_workspace, tmp_path, monkeypatch
):
    runtime, _, _ = owned_workspace
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock()
    scope.poll_seconds = 100
    first_written, release = threading.Event(), threading.Event()
    publish = runtime.views.publish
    calls = 0
    child = None

    def delayed_return(*args, **kwargs):
        nonlocal calls
        calls += 1
        first = calls == 1
        result = publish(*args, **kwargs)
        if first:
            first_written.set()
            assert release.wait(5)
        return result

    try:
        with pytest.raises(asyncio.CancelledError):
            async with scope.scope("ws_cloud_stable"):
                monkeypatch.setattr(runtime.views, "publish", delayed_return)
                child = asyncio.create_task(runtime.workspace("ws_cloud_stable"))
                assert await asyncio.to_thread(first_written.wait, 5)
                await runtime.workspace("ws_cloud_stable")
                release.set()
                await child
                raise asyncio.CancelledError()
        path = runtime.receiver.store.root / runtime.views._key("ws_cloud_stable") / "view"
        assert path.is_dir() and not list(path.iterdir())
        assert runtime.views.publication("ws_cloud_stable") is None
    finally:
        release.set()
        if child is not None:
            await asyncio.gather(child, return_exceptions=True)


async def test_queued_watcher_cannot_publish_over_successor_after_scope_closes(
    owned_workspace, tmp_path, monkeypatch
):
    runtime, value, _ = owned_workspace
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock()
    scope.poll_seconds = 0.01
    in_body = asyncio.Event()
    queued, release = threading.Event(), threading.Event()
    publish = runtime.views.publish
    owner = None
    calls = 0

    def queued_writer(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            queued.set()
            assert release.wait(5)
        return publish(*args, **kwargs)

    async def operation():
        nonlocal owner
        async with scope.scope("ws_cloud_stable"):
            owner = owned_publication(runtime.views, "ws_cloud_stable")
            monkeypatch.setattr(runtime.views, "publish", queued_writer)
            in_body.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(operation())
    try:
        await asyncio.wait_for(in_body.wait(), 5)
        assert await asyncio.to_thread(queued.wait, 5)
        task.cancel()
        async with asyncio.timeout(5):
            while not owner.closing:
                await asyncio.sleep(0)
        # This caller is outside the old scope, as a real successor is.
        await runtime.workspace("ws_cloud_stable")
        successor = runtime.views.publication("ws_cloud_stable")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert runtime.views.publication("ws_cloud_stable").token == successor.token
        assert (successor.path / "github.api-key").read_bytes() == b"synthetic-github-key"
        await deny_agent(runtime, value, "revoke", monkeypatch)
        assert await scope.admission_check()
        assert not successor.path.exists()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
