"""Retained refresh and shutdown must not leave readable credential views."""

import asyncio
import fcntl
import os
import threading
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import text

from awf.control.worker.types import _ExecutionTaskKind
from awf.db.models import Workspace
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.receiver import Receiver
from awf.hosted_node.runtime_authority import WorkspaceAuthority
from awf.hosted_node.runtime_consumers import WorkspaceConsumers
from awf.hosted_node.runtime_views import RuntimeViews
from awf.hosted_node.runtime_worker import start_runtime
from awf.service.resource_capacity import LocalCapacityLimits
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_authority import bound_workspace as bound_workspace
from tests.unit.test_hosted_runtime_capacity import configured
from tests.unit.test_hosted_runtime_review_retained import deny_agent
from tests.unit.test_hosted_runtime_warm_views import owned_workspace as owned_workspace


async def test_authorized_cancel_after_separate_retained_sweep_clears_view(
    owned_workspace, tmp_path
):
    runtime, _, _ = owned_workspace
    consumers = WorkspaceConsumers(runtime, tmp_path)
    consumers.stop_workspace = AsyncMock()
    consumers.poll_seconds = 100
    entered = asyncio.Event()

    async def operation():
        async with consumers.scope("ws_cloud_stable"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(operation())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        initial = runtime.views.publication("ws_cloud_stable")
        inode = initial.path.stat().st_ino
        # The real worker's poll runs outside the active consumer's context.
        assert await consumers.admission_check()
        refreshed = runtime.views.publication("ws_cloud_stable")
        assert refreshed.token != initial.token
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        consumers.stop_workspace.assert_awaited_once_with("ws_cloud_stable", authority_lost=False)
        assert initial.path.stat().st_ino == inode
        assert not list(initial.path.iterdir())
        assert runtime.views.publication("ws_cloud_stable") is None
        assert await consumers.admission_check()
        assert not list(initial.path.iterdir())
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def runtime_settings(owned_workspace, tmp_path, monkeypatch):
    from awf.hosted_node import runtime_worker

    authority, _, factory = owned_workspace
    receiver = authority.receiver
    async with factory.begin() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        row.status = "blocked"
    engine = factory.kw["bind"]
    async with engine.connect() as connection:
        schema = await connection.scalar(text("select current_schema()"))
    url = engine.url.update_query_dict({"awf_search_path": schema}).render_as_string(
        hide_password=False
    )
    monkeypatch.setattr(
        runtime_worker,
        "detect_local_capacity",
        Mock(return_value=LocalCapacityLimits(cpu_cores=16, memory_gb=64)),
    )
    return replace(
        configured(tmp_path),
        database_url=url,
        github_token=None,
        work_dir=str(receiver.config.host.workspaces_root),
        worker_poll_interval_seconds=0.05,
        auto_cleanup_orphans=False,
        terminal_workspace_gc_enabled=False,
        claude_base_gc_enabled=False,
    )


async def test_normal_runtime_shutdown_clears_exited_retained_scope_before_store_close(
    owned_workspace, tmp_path, monkeypatch
):
    authority, _, _ = owned_workspace
    receiver = authority.receiver
    settings = await runtime_settings(owned_workspace, tmp_path, monkeypatch)
    async with start_runtime(receiver, settings) as runtime:
        async with runtime.consumers.scope("ws_cloud_stable"):
            pass
        publication = runtime.authority.views.publication("ws_cloud_stable")
        inode = publication.path.stat().st_ino
        assert list(publication.path.iterdir())
        assert not runtime.runtime.worker._execution_tasks
    assert runtime.task.done()
    await receiver.close()
    assert receiver.closed and receiver.store.closed
    assert publication.path.stat().st_ino == inode
    assert not list(publication.path.iterdir())
    assert not (publication.path.parent / "publication").exists()


@pytest.mark.parametrize("revoked", [False, True])
async def test_runtime_shutdown_joins_active_scope_without_inventing_authority_loss(
    owned_workspace, tmp_path, monkeypatch, revoked
):
    authority, value, _ = owned_workspace
    settings = await runtime_settings(owned_workspace, tmp_path, monkeypatch)
    entered = asyncio.Event()
    async with start_runtime(authority.receiver, settings) as runtime:
        runtime.consumers.stop_workspace = AsyncMock()
        runtime.consumers.poll_seconds = 100

        async def operation():
            async with runtime.consumers.scope("ws_cloud_stable"):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(operation())
        runtime.runtime.worker._track_execution_task(
            "ws_cloud_stable", task, kind=_ExecutionTaskKind.READY
        )
        await asyncio.wait_for(entered.wait(), 5)
        publication = authority.views.publication("ws_cloud_stable")
        inode = publication.path.stat().st_ino
        if revoked:
            await deny_agent(authority, value, "revoke", monkeypatch)
    assert runtime.task.done() and task.done()
    assert not runtime.runtime.worker._execution_tasks
    if revoked:
        runtime.consumers.stop_workspace.assert_awaited_once_with("ws_cloud_stable")
        assert not publication.path.exists()
    else:
        runtime.consumers.stop_workspace.assert_awaited_once_with(
            "ws_cloud_stable", authority_lost=False
        )
        assert publication.path.stat().st_ino == inode
        assert not list(publication.path.iterdir())


async def test_repeated_receiver_close_preserves_original_failure_without_retry(owned_workspace):
    runtime, _, _ = owned_workspace
    failure = OSError("synthetic database unavailable")
    runtime.receiver.factory = Mock()
    runtime.receiver.factory.begin.side_effect = failure
    with pytest.raises(OSError) as first:
        await runtime.receiver.close()
    with pytest.raises(OSError) as second:
        await runtime.receiver.close()
    assert first.value is failure and second.value is failure
    runtime.receiver.factory.begin.assert_called_once_with()


async def test_closed_publication_keeps_final_durable_association_recheck(
    owned_workspace, monkeypatch
):
    runtime, _, factory = owned_workspace
    await asyncio.to_thread(runtime.views.close_admission)
    original = runtime.receiver.status
    changed = False

    async def rebind(binding):
        nonlocal changed
        result = await original(binding)
        if not changed:
            changed = True
            async with factory.begin() as session:
                row = await session.get(Workspace, "ws_cloud_stable")
                row.agent = "claude_code"
        return result

    monkeypatch.setattr(runtime.receiver, "status", rebind)
    with pytest.raises(MaterialDeniedError, match="HOSTED_WORKSPACE_BINDING_MISMATCH"):
        await runtime.workspace("ws_cloud_stable")
    assert runtime.views.publication("ws_cloud_stable") is None


@pytest.mark.parametrize("missing", ["row", "database"])
async def test_receiver_shutdown_clears_recorded_views_without_workspace_query(
    owned_workspace, missing
):
    runtime, _, factory = owned_workspace
    await runtime.workspace("ws_cloud_stable")
    publication = runtime.views.publication("ws_cloud_stable")
    inode = publication.path.stat().st_ino
    if missing == "row":
        async with factory.begin() as session:
            await session.delete(await session.get(Workspace, "ws_cloud_stable"))
        await runtime.receiver.close()
    else:
        runtime.receiver.factory = Mock()
        runtime.receiver.factory.begin.side_effect = OSError("synthetic database unavailable")
        with pytest.raises(OSError, match="synthetic database unavailable"):
            await runtime.receiver.close()
    assert runtime.receiver.store.closed
    assert publication.path.stat().st_ino == inode
    assert not list(publication.path.iterdir())
    assert not (publication.path.parent / "publication").exists()


async def test_receiver_shutdown_joins_initial_writer_across_repeated_cancellation(
    owned_workspace, monkeypatch
):
    runtime, _, _ = owned_workspace
    receiver = runtime.receiver
    entered, release = threading.Event(), threading.Event()
    original = runtime.views._publication

    def blocked(parent, token=None):
        value = original(parent, token)
        if token is not None:
            entered.set()
            assert release.wait(5)
        return value

    monkeypatch.setattr(runtime.views, "_publication", blocked)
    writer = asyncio.create_task(runtime.workspace("ws_cloud_stable"))
    closer = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        closer = asyncio.create_task(receiver.close())
        await asyncio.sleep(0.02)
        closer.cancel()
        await asyncio.sleep(0.02)
        closer.cancel()
        assert not closer.done(), "shutdown must join its locked initial publisher"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closer, 5)
        await asyncio.gather(writer, return_exceptions=True)
        path = receiver.store.root / runtime.views._key("ws_cloud_stable") / "view"
        assert not path.exists() or not list(path.iterdir())
        assert not (path.parent / "publication").exists()
        assert receiver.store.closed
        with pytest.raises(MaterialDeniedError):
            runtime.views.publish(
                "ws_cloud_stable",
                {"openai.api-key": b"synthetic-one", "github.api-key": b"synthetic-gh"},
            )
    finally:
        release.set()
        await asyncio.gather(writer, *([closer] if closer else []), return_exceptions=True)


async def test_separate_authority_sweep_shares_scope_ownership(owned_workspace, tmp_path):
    runtime, _, _ = owned_workspace
    other = WorkspaceAuthority(runtime.receiver)
    consumers = WorkspaceConsumers(runtime, tmp_path)
    consumers.stop_workspace = AsyncMock()
    consumers.poll_seconds = 100
    entered = asyncio.Event()

    async def operation():
        async with consumers.scope("ws_cloud_stable"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(operation())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        initial = runtime.views.publication("ws_cloud_stable")
        assert await other.reconcile_workspace("ws_cloud_stable")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert not list(initial.path.iterdir())
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("boundary", ["entry", "body", "watch", "final"])
@pytest.mark.parametrize("revoke", [False, True])
async def test_sweep_ownership_through_every_scope_cancellation_boundary(
    owned_workspace, tmp_path, monkeypatch, boundary, revoke
):
    runtime, value, _ = owned_workspace
    consumers = WorkspaceConsumers(runtime, tmp_path)
    consumers.stop_workspace = AsyncMock()
    consumers.poll_seconds = 0.01 if boundary == "watch" else 100
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.workspace

    async def blocked(workspace_id):
        name = asyncio.current_task().get_name()
        if boundary == "final" and name.startswith("hosted-finalize-") and not entered.is_set():
            entered.set()
            await release.wait()
        material = await original(workspace_id)
        selected = (boundary == "entry" and name == "test-scope") or (
            boundary == "watch" and name.startswith("hosted-authority-")
        )
        if selected and not entered.is_set():
            entered.set()
            await release.wait()
        return material

    monkeypatch.setattr(runtime, "workspace", blocked)

    async def operation():
        async with consumers.scope("ws_cloud_stable"):
            if boundary == "body":
                entered.set()
            if boundary != "final":
                await asyncio.Event().wait()

    task = asyncio.create_task(operation(), name="test-scope")
    try:
        await asyncio.wait_for(entered.wait(), 5)
        publication = runtime.views.publication("ws_cloud_stable")
        inode = publication.path.stat().st_ino
        assert await WorkspaceConsumers(
            WorkspaceAuthority(runtime.receiver), tmp_path
        ).admission_check()
        refreshed = runtime.views.publication("ws_cloud_stable")
        assert refreshed.token != publication.token
        # Old-version cleanup must still preserve this same-owner newer write.
        runtime.views.remove("ws_cloud_stable", publication=publication, preserve_directory=True)
        assert list(publication.path.iterdir())
        if revoke:
            await deny_agent(runtime, value, "revoke", monkeypatch)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        if revoke:
            consumers.stop_workspace.assert_awaited_once_with("ws_cloud_stable")
            assert not publication.path.exists()
        else:
            consumers.stop_workspace.assert_awaited_once_with(
                "ws_cloud_stable", authority_lost=False
            )
            assert publication.path.stat().st_ino == inode
            assert not list(publication.path.iterdir())
            assert await consumers.admission_check()
            assert not list(publication.path.iterdir())
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_sweep_cannot_adopt_publication_created_during_association_read(
    owned_workspace, monkeypatch
):
    runtime, _, _ = owned_workspace
    await runtime.workspace("ws_cloud_stable")
    original = runtime.association
    successor = None

    async def rebound(workspace_id):
        nonlocal successor
        successor = runtime.views.publish(
            workspace_id,
            {"openai.api-key": b"synthetic-new", "github.api-key": b"synthetic-new-gh"},
        )
        return await original(workspace_id)

    monkeypatch.setattr(runtime, "association", rebound)
    assert await runtime.reconcile_workspace("ws_cloud_stable")
    assert runtime.views.publication("ws_cloud_stable").token == successor.token
    assert (successor.path / "github.api-key").read_bytes() == b"synthetic-new-gh"


async def test_sweep_cannot_adopt_unrecorded_publication(owned_workspace):
    runtime, _, _ = owned_workspace
    foreign = RuntimeViews(runtime.receiver.store, identity=runtime.receiver.identity)
    publication = foreign.publish(
        "ws_cloud_stable",
        {"openai.api-key": b"synthetic-foreign", "github.api-key": b"synthetic-foreign-gh"},
    )
    assert await runtime.reconcile_workspace("ws_cloud_stable")
    assert (publication.path.parent / "publication").read_text() == publication.token
    await runtime.receiver.close()
    assert (publication.path / "github.api-key").read_bytes() == b"synthetic-foreign-gh"


@pytest.mark.parametrize("successor_boot", [False, True])
async def test_shutdown_never_deletes_foreign_publication(owned_workspace, successor_boot):
    runtime, _, factory = owned_workspace
    await runtime.workspace("ws_cloud_stable")
    old = runtime.views.publication("ws_cloud_stable")
    successor = await Receiver.start(runtime.receiver.config, factory) if successor_boot else None
    views = (
        successor.views
        if successor
        else RuntimeViews(runtime.receiver.store, identity=runtime.receiver.identity)
    )
    newer = views.publish(
        "ws_cloud_stable",
        {"openai.api-key": b"synthetic-new", "github.api-key": b"synthetic-new-gh"},
    )
    try:
        await runtime.receiver.close()
        assert (newer.path / "github.api-key").read_bytes() == b"synthetic-new-gh"
        assert (newer.path.parent / "publication").read_text() == newer.token
        if successor_boot:
            assert not list(old.path.iterdir())
    finally:
        if successor:
            await successor.close()


async def test_waiting_flock_writer_cannot_outlive_cancelled_shutdown(owned_workspace):
    runtime, _, _ = owned_workspace
    store = runtime.receiver.store
    root = store._open_root()
    fcntl.flock(root, fcntl.LOCK_EX)
    writer = asyncio.create_task(
        asyncio.to_thread(
            runtime.views.publish,
            "ws_cloud_stable",
            {"openai.api-key": b"synthetic-one", "github.api-key": b"synthetic-gh"},
        )
    )
    runtime.views.track_writer(writer)
    closer = asyncio.create_task(runtime.receiver.close())
    try:
        await asyncio.sleep(0.02)
        closer.cancel()
        await asyncio.sleep(0.02)
        closer.cancel()
        assert not closer.done()
    finally:
        fcntl.flock(root, fcntl.LOCK_UN)
        os.close(root)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closer, 5)
    await asyncio.gather(writer, return_exceptions=True)
    path = store.root / runtime.views._key("ws_cloud_stable") / "view"
    assert not path.exists() or not list(path.iterdir())
    assert not (path.parent / "publication").exists()
    assert store.closed


async def test_store_waiter_rechecks_closed_after_acquiring_root_lock(owned_workspace, monkeypatch):
    runtime, _, _ = owned_workspace
    store = runtime.receiver.store
    entered, release = threading.Event(), threading.Event()
    original = store._open_root
    writer_id = None

    def delayed_open():
        if threading.get_ident() == writer_id:
            entered.set()
            assert release.wait(5)
        return original()

    def write():
        nonlocal writer_id
        writer_id = threading.get_ident()
        return store.install("a" * 32, "b" * 32, b"synthetic-delayed")

    monkeypatch.setattr(store, "_open_root", delayed_open)
    writer = asyncio.create_task(asyncio.to_thread(write))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await runtime.receiver.close()
        release.set()
        with pytest.raises(MaterialDeniedError):
            await asyncio.wait_for(writer, 5)
        assert not store.path("a" * 32, "b" * 32).exists()
    finally:
        release.set()
        await asyncio.gather(writer, return_exceptions=True)


async def test_shutdown_reports_unsafe_view_but_clears_other_recorded_views(owned_workspace):
    runtime, _, _ = owned_workspace
    files = {"openai.api-key": b"synthetic-one", "github.api-key": b"synthetic-gh"}
    unsafe = runtime.views.publish("ws_bad", files)
    safe = runtime.views.publish("ws_good", files)
    (unsafe.path.parent / "publication").chmod(0o644)
    with pytest.raises(ExceptionGroup, match="Runtime view retraction failed") as errors:
        await runtime.receiver.close()
    assert isinstance(errors.value.exceptions[0], MaterialDeniedError)
    assert not list(safe.path.iterdir())
    assert list(unsafe.path.iterdir()), "unsafe ownership must not authorize blind deletion"
    assert runtime.receiver.store.closed
