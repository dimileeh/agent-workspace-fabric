"""Actual recovery, inactive cleanup and concurrent credential publication."""

import asyncio
import base64
import errno
import fcntl
import json
import os
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text

from awf.common.commands import CommandResult
from awf.db.models import Workspace
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.runtime_authority import WorkspaceAuthority
from awf.hosted_node.runtime_consumers import WorkspaceConsumers
from awf.hosted_node.runtime_views import (
    RuntimeViews,
    _publication_ownership,
    owned_publication,
    track_publications,
)
from awf.hosted_node.schemas import Binding, Refresh
from awf.node.cleanup import WorkspaceCleanupResult
from awf.service.controls import WorkspaceControlService
from awf.service.gc import run_workspace_filesystem_gc
from tests.unit.control.test_worker_parts.test_worker_part_025 import (
    _BlockingMonitorExecutor,
    _live_agent_snapshot,
    _RecordingRuntimeInspector,
)
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_retained import deny_agent, retained


@pytest.mark.parametrize("revoked", [False, True])
async def test_preserved_recovery_uses_exact_candidate_authority(
    authority, tmp_path, monkeypatch, revoked
):
    from awf.service import worker
    from tests.unit.service.test_worker import _settings

    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable", status="pushing")
    async with factory.begin() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        row.branch_name = "awf/ws_cloud_stable"
        row.remote_push_branch = row.branch_name
        row.base_commit = "a" * 40
    calls = []

    class Commands:
        async def run(self, args, **kwargs):
            calls.append((args, kwargs.get("env")))
            assert args[:3] == ["gh", "pr", "list"]
            assert consumers.workspace_id.get() == "ws_cloud_stable"
            async with factory.begin() as session:
                assert (
                    await session.scalar(
                        select(Workspace)
                        .where(Workspace.id == "ws_cloud_stable")
                        .with_for_update(nowait=True)
                    )
                    is not None
                )
            return CommandResult(
                0,
                json.dumps(
                    [
                        {
                            "url": "https://github.com/example/repo/pull/270",
                            "number": 270,
                            "headRefName": "awf/ws_cloud_stable",
                            "headRefOid": "e" * 40,
                            "headRepository": {"name": "repo"},
                            "headRepositoryOwner": {"login": "example"},
                        }
                    ]
                ),
                "",
            )

    monkeypatch.setattr(worker, "AsyncioSubprocessRunner", Commands)
    engine = factory.kw["bind"]
    async with engine.connect() as connection:
        schema = await connection.scalar(text("select current_schema()"))
    url = engine.url.update_query_dict({"awf_search_path": schema}).render_as_string(
        hide_password=False
    )
    settings = replace(
        _settings(tmp_path, database_url=url), node_id=runtime.receiver.identity.owner
    )
    consumers = WorkspaceConsumers(runtime, Path(settings.work_dir))
    built = worker.build_worker_runtime(settings, hosted=consumers)
    executor = _BlockingMonitorExecutor()
    built.worker._executor = executor
    built.worker._runtime_inspector = _RecordingRuntimeInspector(
        {"awf_ws_cloud_stable": _live_agent_snapshot()}
    )
    consumers.stop_workspace = AsyncMock()
    if revoked:
        resolve = built.worker._resolve_preserved_active_branch_open_pr

        async def revoke_before_resolver(**kwargs):
            await deny_agent(runtime, value, "revoke", monkeypatch)
            return await resolve(**kwargs)

        monkeypatch.setattr(
            built.worker, "_resolve_preserved_active_branch_open_pr", revoke_before_resolver
        )
    try:
        await built.worker.run_once()
        async with factory() as session:
            row = await session.get(Workspace, "ws_cloud_stable")
            assert row.status == ("pushing" if revoked else "monitoring_pr")
            assert row.pr_number == (None if revoked else 270)
        if revoked:
            assert not calls and not path.exists()
            consumers.stop_workspace.assert_awaited_once_with("ws_cloud_stable")
        else:
            await asyncio.wait_for(executor.started.wait(), 5)
            assert len(calls) == 1
            assert calls[0][1]["GH_TOKEN"] == "synthetic-github-key"
            consumers.stop_workspace.assert_not_awaited()
        assert consumers.workspace_id.get() is None
    finally:
        executor.release.set()
        await built.worker.wait_for_execution_tasks()
        await built.engine.dispose()


@pytest.mark.parametrize("operation", ["destroy", "gc"])
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("compose_metadata", [False, True])
async def test_completed_cleanup_retracts_view_without_republishing(
    authority, tmp_path, monkeypatch, operation, restart, compose_metadata
):
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable", status="cancelled")
    other = await retained(runtime, value, factory, "ws_other", other=True)
    if not compose_metadata:
        async with factory.begin() as session:
            row = await session.get(Workspace, "ws_cloud_stable")
            row.compose_project_name = None
            row.compose_file_path = None
    if operation == "destroy":
        async with factory.begin() as session:
            service = WorkspaceControlService(
                session,
                project_stopper=AsyncMock(),
                cleaner_factory=lambda: SimpleNamespace(
                    cleanup=AsyncMock(
                        return_value=WorkspaceCleanupResult.skipped(reason_code="synthetic")
                    )
                ),
            )
            result = await service.destroy_workspace(
                "ws_cloud_stable",
                force=False,
                remove_volumes=False,
                remove_worktree=True,
                idempotency_key="destroy",
            )
            assert result.status.value == "destroyed"
    else:
        work = tmp_path / "work"
        async with factory.begin() as session:
            row = await session.get(Workspace, "ws_cloud_stable")
            row.status = "completed"
            row.pr_url = "https://github.com/example/repo/pull/270"
            row.pr_merge_sha = "e" * 40
            row.pr_merged_at = datetime.now(UTC)
        await run_workspace_filesystem_gc(
            factory,
            work_dir=work,
            workspace_id="ws_cloud_stable",
            execute=True,
            ignore_retention=True,
        )
        async with factory() as session:
            row = await session.get(Workspace, "ws_cloud_stable")
            assert row.filesystem_reclaimed_at is not None
    if restart:
        runtime = WorkspaceAuthority(runtime.receiver)
    consumers = WorkspaceConsumers(runtime, tmp_path)
    consumers.stop_workspace = AsyncMock()
    publish = runtime.views.publish
    published = []

    def observe(workspace_id, files):
        published.append(workspace_id)
        return publish(workspace_id, files)

    monkeypatch.setattr(runtime.views, "publish", observe)
    assert await consumers.admission_check()
    assert "ws_cloud_stable" not in published
    assert not path.parent.exists()
    assert other.is_dir()
    consumers.stop_workspace.assert_not_awaited()
    with pytest.raises(MaterialDeniedError):
        await runtime.workspace("ws_cloud_stable")
    assert not path.exists()


@pytest.mark.parametrize("contender", ["poll", "publisher"])
async def test_poll_and_active_refresh_are_serialized_by_real_material_lock(
    authority, tmp_path, monkeypatch, contender
):
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    consumers = WorkspaceConsumers(runtime, tmp_path)
    consumers.stop_workspace = AsyncMock()
    writer_blocked, release_writer, second_lock = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    opening, flock = os.open, fcntl.flock
    writer = None

    def open_file(name, flags, *args, **kwargs):
        nonlocal writer
        fd = opening(name, flags, *args, **kwargs)
        if name == "openai.api-key.pending" and writer is None:
            writer = threading.get_ident()
            writer_blocked.set()
            assert release_writer.wait(10)
        return fd

    def lock(fd, operation):
        if writer_blocked.is_set() and threading.get_ident() != writer:
            second_lock.set()
        return flock(fd, operation)

    monkeypatch.setattr(os, "open", open_file)
    monkeypatch.setattr(fcntl, "flock", lock)
    active = asyncio.create_task(runtime.workspace("ws_cloud_stable"))
    poll = None
    try:
        assert await asyncio.to_thread(writer_blocked.wait, 5)
        if contender == "poll":
            poll = asyncio.create_task(consumers.admission_check())
        else:
            views = RuntimeViews(runtime.receiver.store, identity=runtime.receiver.identity)
            poll = asyncio.create_task(
                asyncio.to_thread(
                    views.publish,
                    "ws_cloud_stable",
                    {
                        "openai.api-key": b"synthetic-agent-key",
                        "github.api-key": b"synthetic-github-key",
                    },
                )
            )
        assert await asyncio.to_thread(second_lock.wait, 5)
        assert not poll.done()
    finally:
        release_writer.set()
        await active
        if poll is not None:
            assert await poll
    consumers.stop_workspace.assert_not_awaited()
    assert (path / "openai.api-key").read_bytes() == b"synthetic-agent-key"
    assert (path / "github.api-key").read_bytes() == b"synthetic-github-key"
    assert not list(path.glob("*.pending"))
    await deny_agent(runtime, value, "revoke", monkeypatch)
    assert await consumers.admission_check()
    consumers.stop_workspace.assert_awaited_once_with("ws_cloud_stable")
    assert not path.exists()


@pytest.mark.parametrize("mutation", ["destroyed", "agent"])
async def test_cleanup_during_publication_cannot_resurrect_destroyed_or_rebound_view(
    authority, tmp_path, monkeypatch, mutation
):
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    started, release = threading.Event(), threading.Event()
    publish = runtime.views.publish

    def delayed(workspace_id, files):
        started.set()
        assert release.wait(10)
        return publish(workspace_id, files)

    monkeypatch.setattr(runtime.views, "publish", delayed)
    active = asyncio.create_task(runtime.workspace("ws_cloud_stable"))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        async with factory.begin() as session:
            row = await session.get(Workspace, "ws_cloud_stable")
            if mutation == "destroyed":
                row.status = "destroyed"
            else:
                row.agent = "claude_code"
        consumers = WorkspaceConsumers(runtime, tmp_path)
        consumers.stop_workspace = AsyncMock()
        assert await consumers.admission_check()
        assert not path.exists()
    finally:
        release.set()
    reason = (
        "HOSTED_WORKSPACE_INACTIVE"
        if mutation == "destroyed"
        else "HOSTED_WORKSPACE_BINDING_MISMATCH"
    )
    with pytest.raises(MaterialDeniedError, match=reason):
        await active
    assert not path.parent.exists()


async def test_inactive_reconciliation_cannot_remove_other_owner_view(authority, tmp_path):
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    async with factory.begin() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        row.status = "destroyed"
        row.node_id = "other-owner/nodeb"
    consumers = WorkspaceConsumers(runtime, tmp_path)
    consumers.stop_workspace = AsyncMock()
    assert await consumers.admission_check()
    await runtime.reconcile_workspace("ws_cloud_stable")
    await runtime.reconcile_workspace("ws_absent")
    assert path.is_dir()
    consumers.stop_workspace.assert_not_awaited()


@pytest.mark.parametrize(
    "termination",
    ["superseded", "cancelled", "cancelled_body", "cancelled_finalcheck", "cancelled_stop"],
)
@pytest.mark.parametrize("same_material", [False, True])
async def test_older_cleanup_preserves_newer_publication_then_real_revocation_retracts(
    authority, tmp_path, monkeypatch, termination, same_material
):
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    other = await retained(runtime, value, factory, "ws_other", other=True)
    published = threading.Event()
    checking, release = asyncio.Event(), asyncio.Event()
    publish, status, workspace = runtime.views.publish, runtime.receiver.status, runtime.workspace
    old_task = None

    def observe(workspace_id, files):
        result = publish(workspace_id, files)
        published.set()
        return result

    async def check(binding):
        if (
            termination in {"superseded", "cancelled"}
            and asyncio.current_task() is old_task
            and published.is_set()
        ):
            checking.set()
            await release.wait()
        return await status(binding)

    monkeypatch.setattr(runtime.views, "publish", observe)
    monkeypatch.setattr(runtime.receiver, "status", check)
    old_consumers = WorkspaceConsumers(runtime, tmp_path)

    async def stop(workspace_id, **kwargs):
        if termination == "cancelled_stop":
            checking.set()
            await release.wait()

    old_consumers.stop_workspace = AsyncMock(side_effect=stop)

    async def check_final(workspace_id):
        if termination == "cancelled_finalcheck" and asyncio.current_task().get_name().startswith(
            "hosted-finalize-"
        ):
            checking.set()
            await release.wait()
        return await workspace(workspace_id)

    monkeypatch.setattr(runtime, "workspace", check_final)

    async def old_scope():
        async with old_consumers.scope("ws_cloud_stable"):
            if termination == "cancelled_body":
                checking.set()
                await asyncio.Event().wait()
            if termination == "cancelled_finalcheck":
                return
            if termination == "cancelled_stop":
                raise asyncio.CancelledError()
            pytest.fail("cancelled initial authorization entered the consumer")

    old_task = asyncio.create_task(
        runtime.workspace("ws_cloud_stable") if termination == "superseded" else old_scope()
    )
    try:
        await asyncio.wait_for(checking.wait(), 5)
        anchor = Binding.model_validate(value["hosted"]["credentials"]["agent"])
        predecessor = await status(anchor)
        key = b"synthetic-agent-key" if same_material else b"synthetic-newer-agent-key"
        refreshed = Refresh.model_validate(
            {
                **anchor.model_dump(),
                "idempotency_key": "overlap-refresh",
                "revision": 2,
                "fence": 2,
                "predecessor": predecessor.receipt_id,
                "material": base64.b64encode(key).decode(),
            }
        )
        assert (await runtime.receiver.install(refreshed, refresh=True)).status == "current"
        latest = await runtime.workspace("ws_cloud_stable")
        latest_publication = runtime.views.publication("ws_cloud_stable")
        assert latest.environment["CODEX_API_KEY"] == key.decode()
        assert (path / "openai.api-key").read_bytes() == key
        if termination != "superseded":
            old_task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(old_task, 5)
        else:
            release.set()
            followed = await asyncio.wait_for(old_task, 5)
            assert followed.environment["CODEX_API_KEY"] == key.decode()
        association, agent = await runtime.association("ws_cloud_stable")
        current = await runtime.materials(association, agent, current=True)
        assert current.environment["CODEX_API_KEY"] == key.decode()
        assert path.exists(), "An older request must not retract a newer authorized publication"
        assert (path / "openai.api-key").read_bytes() == key
        assert runtime.views.publication("ws_cloud_stable").token == latest_publication.token
        assert other.is_dir()

        await runtime.receiver.revoke(
            Binding.model_validate(
                {
                    **refreshed.model_dump(include=set(Binding.model_fields)),
                    "idempotency_key": "overlap-revoke",
                    "fence": 3,
                }
            )
        )
        consumers = WorkspaceConsumers(runtime, tmp_path)
        consumers.stop_workspace = AsyncMock()
        assert await consumers.admission_check()
        consumers.stop_workspace.assert_awaited_once_with("ws_cloud_stable")
        assert not path.parent.exists()
        assert other.is_dir()
    finally:
        release.set()
        if not old_task.done():
            old_task.cancel()
        await asyncio.gather(old_task, return_exceptions=True)


async def test_partial_publication_failure_retracts_only_its_own_view(authority, monkeypatch):
    """Refresh write failure must clear credentials but keep the mounted inode.

    Docker bind-mounts pin the view directory inode. Destroying that directory on
    a transient publication OSError leaves the container mounted on an empty
    orphan while a later publish creates a different inode. Preserve the existing
    directory through failure cleanup so recovery can rewrite the same mount.
    """
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    other = await retained(runtime, value, factory, "ws_other", other=True)
    inode = path.stat().st_ino
    replace_file = os.replace

    def fail_second_file(source, destination, **kwargs):
        if destination == "github.api-key":
            raise OSError("synthetic publication failure")
        return replace_file(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", fail_second_file)
    with pytest.raises(OSError, match="synthetic publication failure"):
        runtime.views.publish(
            "ws_cloud_stable",
            {"openai.api-key": b"synthetic-next", "github.api-key": b"synthetic-gh-next"},
        )
    assert path.is_dir()
    assert path.stat().st_ino == inode
    assert not list(path.iterdir())
    assert not (path.parent / "publication").exists()
    assert other.is_dir()
    monkeypatch.setattr(os, "replace", replace_file)
    recovered = runtime.views.publish(
        "ws_cloud_stable",
        {"openai.api-key": b"synthetic-recovered", "github.api-key": b"synthetic-gh-recovered"},
    )
    assert recovered is not None
    assert recovered.path.stat().st_ino == inode
    assert (path / "openai.api-key").read_bytes() == b"synthetic-recovered"


async def test_failed_mounted_refresh_resets_ownership_so_retry_can_repopulate(
    authority, monkeypatch
):
    """Active-scope refresh failure must not stick ownership on a dead publication.

    Failure cleanup keeps the mounted directory inode and deletes the publication
    token. If ownership.publication still names that failed publication, a later
    materials() retry can refresh the stale token, get None, clear grace, and leave
    the retained directory empty. Reset ownership so a later publish can rewrite
    the same mount.
    """
    from awf.hosted_node import runtime_authority as authority_module
    from awf.hosted_node.runtime_views import PublishedView

    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    inode = path.stat().st_ino
    replace_file = os.replace

    def fail_second_file(source, destination, **kwargs):
        if destination == "github.api-key":
            raise OSError("synthetic mounted refresh failure")
        return replace_file(source, destination, **kwargs)

    with track_publications(runtime.views, "ws_cloud_stable"):
        ownership = owned_publication(runtime.views, "ws_cloud_stable")
        assert ownership is not None
        await runtime.workspace("ws_cloud_stable")
        assert ownership.publication is not None
        assert ownership.publication.path == path

        monkeypatch.setattr(os, "replace", fail_second_file)
        with pytest.raises(OSError, match="synthetic mounted refresh failure"):
            await runtime.workspace("ws_cloud_stable")
        assert path.is_dir()
        assert path.stat().st_ino == inode
        assert not list(path.iterdir())
        assert not (path.parent / "publication").exists()
        assert ownership.publication is None
        assert ownership.retry is not None

        monkeypatch.setattr(os, "replace", replace_file)
        # Re-stick a dead publication to prove materials refresh selection: a
        # snapshot cutover retry refreshes that token, gets None, and would leave
        # the mount empty unless ownership was reset by failure cleanup above.
        dead = PublishedView(path, "0" * 32, retained_directory=True, owner=ownership)
        ownership.publication = dead
        calls = {"n": 0}
        original_status = runtime._snapshot_status

        async def flaky_status(bindings):
            calls["n"] += 1
            if calls["n"] == 1:
                raise authority_module._SnapshotUnavailableError()
            return await original_status(bindings)

        async def keep_bindings(anchors, previous):
            return previous

        monkeypatch.setattr(runtime, "_snapshot_status", flaky_status)
        monkeypatch.setattr(runtime, "_snapshot_successor", keep_bindings)
        await runtime.workspace("ws_cloud_stable")
        assert not list(path.iterdir())
        assert ownership.publication is dead

        ownership.publication = None
        await runtime.workspace("ws_cloud_stable")
        assert path.stat().st_ino == inode
        assert (path / "openai.api-key").read_bytes() == b"synthetic-agent-key"
        assert (path / "github.api-key").read_bytes() == b"synthetic-github-key"
        assert ownership.publication is not None
        assert ownership.retry is None
        assert (path.parent / "publication").exists()


async def test_retry_marker_fsync_failure_clears_ownership_and_tracks_partial_marker(
    authority, tmp_path, monkeypatch
):
    """Retry-marker fsync failure must not leave ownership on a deleted token.

    A grace-eligible storage fault can fail the mounted refresh and then fail
    persisting publication.retry for the same reason. Ownership cleanup after
    that marker write must still run, and a partially written marker must be
    adopted so cancel can invalidate it once storage recovers.
    """
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    inode = path.stat().st_ino
    real_fsync = os.fsync

    def failing_fsync(fd):
        raise OSError("synthetic retry marker fsync failure")

    consumers = WorkspaceConsumers(runtime, tmp_path)
    consumers.stop_workspace = AsyncMock()
    with track_publications(runtime.views, "ws_cloud_stable"):
        ownership = owned_publication(runtime.views, "ws_cloud_stable")
        assert ownership is not None
        await runtime.workspace("ws_cloud_stable")
        assert ownership.publication is not None

        monkeypatch.setattr(os, "fsync", failing_fsync)
        with pytest.raises(OSError, match="synthetic retry marker fsync failure"):
            await runtime.workspace("ws_cloud_stable")
        assert path.is_dir()
        assert path.stat().st_ino == inode
        assert not list(path.iterdir())
        assert not (path.parent / "publication").exists()
        assert ownership.publication is None
        marker = path.parent / "publication.retry"
        assert marker.exists()
        generation = marker.read_text(encoding="ascii")
        assert len(generation) == 32
        assert ownership.retry == generation

        monkeypatch.setattr(os, "fsync", real_fsync)
        await consumers._stop("ws_cloud_stable", authority_lost=False)
        assert ownership.retry is None
        assert not marker.exists()

        await runtime.workspace("ws_cloud_stable")
        assert path.stat().st_ino == inode
        assert (path / "openai.api-key").read_bytes() == b"synthetic-agent-key"
        assert (path / "github.api-key").read_bytes() == b"synthetic-github-key"
        assert ownership.publication is not None
        assert ownership.retry is None
        assert (path.parent / "publication").exists()


async def test_unreadable_partial_retry_marker_is_cleared_with_ownership(authority, monkeypatch):
    """Invalid partial publication.retry must be cleared when persistence fails.

    If marker creation leaves an unreadable fence, ownership still drops the dead
    publication token and removes that fence so cancel/reconcile are not stuck on
    MaterialDeniedError from a corrupt retry file.
    """
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    inode = path.stat().st_ino
    replace_file = os.replace
    real_retry = runtime.views._retry_marker

    def fail_second_file(source, destination, **kwargs):
        if destination == "github.api-key":
            raise OSError("synthetic mounted refresh failure")
        return replace_file(source, destination, **kwargs)

    def corrupt_retry(parent, *, present=None):
        if present is True:
            fd = os.open(
                "publication.retry",
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=parent,
            )
            try:
                os.write(fd, b"bad")
            finally:
                os.close(fd)
            raise OSError("synthetic corrupt retry persist")
        return real_retry(parent, present=present)

    with track_publications(runtime.views, "ws_cloud_stable"):
        ownership = owned_publication(runtime.views, "ws_cloud_stable")
        assert ownership is not None
        await runtime.workspace("ws_cloud_stable")
        monkeypatch.setattr(os, "replace", fail_second_file)
        monkeypatch.setattr(runtime.views, "_retry_marker", corrupt_retry)
        with pytest.raises(OSError, match="synthetic mounted refresh failure"):
            await runtime.workspace("ws_cloud_stable")
        assert ownership.publication is None
        assert ownership.retry is None
        assert not (path.parent / "publication.retry").exists()

        monkeypatch.setattr(os, "replace", replace_file)
        monkeypatch.setattr(runtime.views, "_retry_marker", real_retry)
        await runtime.workspace("ws_cloud_stable")
        assert path.stat().st_ino == inode
        assert (path / "openai.api-key").read_bytes() == b"synthetic-agent-key"
        assert ownership.publication is not None


async def test_retry_marker_enospc_before_create_still_fences_retained_mount(
    authority, monkeypatch
):
    """ENOSPC on creating publication.retry must not leave an empty retained mount.

    After a mounted refresh clears credentials, unlink(publication)+create(retry)
    can also fail with ENOSPC before any retry token exists. The fallback read then
    returns None and clears ownership, so reconcile_workspace(publish_view=False)
    never restores the mount once storage recovers. Convert the live publication
    inode into the retry fence so the marker remains space-neutral under ENOSPC.
    """
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    inode = path.stat().st_ino
    replace_file = os.replace
    real_open = os.open

    def fail_second_file(source, destination, **kwargs):
        if destination == "github.api-key":
            raise OSError(errno.ENOSPC, "synthetic retained refresh ENOSPC")
        return replace_file(source, destination, **kwargs)

    def open_enospc_on_new_retry(path_or_fd, flags, *args, **kwargs):
        creating = bool(flags & os.O_CREAT)
        name = path_or_fd if isinstance(path_or_fd, str) else None
        if name == "publication.retry" and creating:
            dir_fd = kwargs.get("dir_fd")
            try:
                probe = real_open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                    dir_fd=dir_fd,
                )
            except FileNotFoundError as error:
                raise OSError(errno.ENOSPC, "synthetic retry create ENOSPC") from error
            else:
                os.close(probe)
        return real_open(path_or_fd, flags, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fail_second_file)
    monkeypatch.setattr(os, "open", open_enospc_on_new_retry)
    with pytest.raises(OSError, match="synthetic retained refresh ENOSPC"):
        await runtime.workspace("ws_cloud_stable")
    assert path.is_dir()
    assert path.stat().st_ino == inode
    assert not list(path.iterdir())
    assert not (path.parent / "publication").exists()
    marker = path.parent / "publication.retry"
    assert marker.exists(), "retained failure must leave a retry fence under ENOSPC"
    generation = marker.read_text(encoding="ascii")
    assert len(generation) == 32
    assert await runtime.publication("ws_cloud_stable") is None
    assert runtime.views.publication_retry("ws_cloud_stable") == generation

    monkeypatch.setattr(os, "replace", replace_file)
    monkeypatch.setattr(os, "open", real_open)
    assert await runtime.reconcile_workspace("ws_cloud_stable")
    assert path.stat().st_ino == inode
    assert (path / "openai.api-key").read_bytes() == b"synthetic-agent-key"
    assert (path / "github.api-key").read_bytes() == b"synthetic-github-key"
    assert (path.parent / "publication").exists()
    assert not marker.exists()


async def test_retry_marker_enospc_on_rewrite_reuses_renamed_publication(authority, monkeypatch):
    """Rename already fences; O_TRUNC+rewrite must not reintroduce an ENOSPC hole.

    publication→publication.retry is space-neutral and leaves a valid 32-hex
    token. Truncating that inode then failing the generation write empties the
    marker; the fallback read cannot adopt it, cleanup clears the fence, and a
    later sweep sees neither a live publication nor a retry generation.
    """
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    inode = path.stat().st_ino
    replace_file = os.replace
    real_write = os.write
    block_retry_rewrite = False

    def fail_second_file(source, destination, **kwargs):
        nonlocal block_retry_rewrite
        if destination == "github.api-key":
            # Refresh already wrote a new publication token; fence conversion
            # must reuse that inode without another write under ENOSPC.
            block_retry_rewrite = True
            raise OSError(errno.ENOSPC, "synthetic retained refresh ENOSPC")
        return replace_file(source, destination, **kwargs)

    def write_enospc_after_refresh(fd, data, *args, **kwargs):
        if block_retry_rewrite:
            raise OSError(errno.ENOSPC, "synthetic retry rewrite ENOSPC")
        return real_write(fd, data, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fail_second_file)
    monkeypatch.setattr(os, "write", write_enospc_after_refresh)
    with pytest.raises(OSError, match="synthetic retained refresh ENOSPC"):
        await runtime.workspace("ws_cloud_stable")
    assert path.is_dir()
    assert path.stat().st_ino == inode
    assert not list(path.iterdir())
    assert not (path.parent / "publication").exists()
    marker = path.parent / "publication.retry"
    assert marker.exists(), "renamed publication must remain as the retry fence"
    generation = marker.read_text(encoding="ascii")
    assert len(generation) == 32
    assert all(char in "0123456789abcdef" for char in generation)
    assert await runtime.publication("ws_cloud_stable") is None
    assert runtime.views.publication_retry("ws_cloud_stable") == generation

    monkeypatch.setattr(os, "replace", replace_file)
    monkeypatch.setattr(os, "write", real_write)
    assert await runtime.reconcile_workspace("ws_cloud_stable")
    assert path.stat().st_ino == inode
    assert (path / "openai.api-key").read_bytes() == b"synthetic-agent-key"
    assert (path / "github.api-key").read_bytes() == b"synthetic-github-key"
    assert (path.parent / "publication").exists()
    assert not marker.exists()


async def test_remove_view_unlink_failure_still_fences_retained_refresh(authority, monkeypatch):
    """Credential unlink failure during cleanup must not strand an ownerless publication.

    A retained refresh can fail on fsync/replace and then raise again while
    ``_remove_view`` deletes credentials. Dropping ownership before a retry fence
    leaves the live publication token on disk; later reconcile refreshes an
    ownerless view (returns None) and clears grace without restoring materials.
    Still convert publication→retry when unlink fails so reconcile can republish.
    """
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    inode = path.stat().st_ino
    replace_file = os.replace
    real_unlink = os.unlink
    credential_unlinks = 0
    fail_cleanup_unlinks = False

    def fail_second_file(source, destination, **kwargs):
        if destination == "github.api-key":
            raise OSError("synthetic retained refresh fsync failure")
        return replace_file(source, destination, **kwargs)

    def fail_second_credential_unlink(name, *args, **kwargs):
        nonlocal credential_unlinks
        if (
            fail_cleanup_unlinks
            and isinstance(name, str)
            and name in {"openai.api-key", "anthropic.api-key", "github.api-key"}
            and kwargs.get("dir_fd") is not None
        ):
            credential_unlinks += 1
            if credential_unlinks >= 2:
                raise OSError("synthetic cleanup unlink failure")
        return real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fail_second_file)
    monkeypatch.setattr(os, "unlink", fail_second_credential_unlink)
    fail_cleanup_unlinks = True
    with pytest.raises(OSError, match="synthetic retained refresh fsync failure"):
        await runtime.workspace("ws_cloud_stable")
    fail_cleanup_unlinks = False
    assert path.is_dir()
    assert path.stat().st_ino == inode
    assert not (path.parent / "publication").exists()
    marker = path.parent / "publication.retry"
    assert marker.exists(), "remove_view failure must still leave a retry fence"
    assert await runtime.publication("ws_cloud_stable") is None
    assert runtime.views.publication_retry("ws_cloud_stable") == marker.read_text(encoding="ascii")

    monkeypatch.setattr(os, "replace", replace_file)
    monkeypatch.setattr(os, "unlink", real_unlink)
    assert await runtime.reconcile_workspace("ws_cloud_stable")
    assert path.stat().st_ino == inode
    assert (path / "openai.api-key").read_bytes() == b"synthetic-agent-key"
    assert (path / "github.api-key").read_bytes() == b"synthetic-github-key"
    assert (path.parent / "publication").exists()
    assert not marker.exists()


async def test_remove_view_and_retry_fence_failure_preserves_ownership(authority, monkeypatch):
    """When unlink and retry fencing both fail, keep ownership of the live token.

    Without a retry marker or live ownership, publication() returns an ownerless
    view and every subsequent refresh fails the owner fence, so credentials that
    were deleted before the unlink error never come back.
    """
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    inode = path.stat().st_ino
    replace_file = os.replace
    real_unlink = os.unlink
    real_retry = runtime.views._retry_marker
    credential_unlinks = 0
    fail_cleanup_unlinks = False

    def fail_second_file(source, destination, **kwargs):
        if destination == "github.api-key":
            raise OSError("synthetic retained refresh fsync failure")
        return replace_file(source, destination, **kwargs)

    def fail_second_credential_unlink(name, *args, **kwargs):
        nonlocal credential_unlinks
        if (
            fail_cleanup_unlinks
            and isinstance(name, str)
            and name in {"openai.api-key", "anthropic.api-key", "github.api-key"}
            and kwargs.get("dir_fd") is not None
        ):
            credential_unlinks += 1
            if credential_unlinks >= 2:
                raise OSError("synthetic cleanup unlink failure")
        return real_unlink(name, *args, **kwargs)

    def fail_retry_fence(parent, *, present=None):
        if present is True:
            raise OSError("synthetic retry fence failure")
        return real_retry(parent, present=present)

    with track_publications(runtime.views, "ws_cloud_stable"):
        ownership = owned_publication(runtime.views, "ws_cloud_stable")
        assert ownership is not None
        await runtime.workspace("ws_cloud_stable")
        assert ownership.publication is not None

        monkeypatch.setattr(os, "replace", fail_second_file)
        monkeypatch.setattr(os, "unlink", fail_second_credential_unlink)
        monkeypatch.setattr(runtime.views, "_retry_marker", fail_retry_fence)
        fail_cleanup_unlinks = True
        with pytest.raises(OSError, match="synthetic retained refresh fsync failure"):
            await runtime.workspace("ws_cloud_stable")
        fail_cleanup_unlinks = False
        assert path.is_dir()
        assert path.stat().st_ino == inode
        assert (path.parent / "publication").exists()
        assert not (path.parent / "publication.retry").exists()
        assert ownership.publication is not None
        assert runtime.views._owners.get("ws_cloud_stable") is ownership
        assert await runtime.publication("ws_cloud_stable") is ownership.publication

        monkeypatch.setattr(os, "replace", replace_file)
        monkeypatch.setattr(os, "unlink", real_unlink)
        monkeypatch.setattr(runtime.views, "_retry_marker", real_retry)
        await runtime.workspace("ws_cloud_stable")
        assert path.stat().st_ino == inode
        assert (path / "openai.api-key").read_bytes() == b"synthetic-agent-key"
        assert (path / "github.api-key").read_bytes() == b"synthetic-github-key"
        assert ownership.publication is not None
        assert (path.parent / "publication").exists()


async def test_retained_reconcile_republishes_after_failed_mounted_refresh(authority, monkeypatch):
    """Retained reconcile must restore a warm mount after a fenced refresh failure.

    Failure cleanup keeps the mounted inode and deletes the live publication token.
    Retained reconciliation decides publish_view from on-disk publication state, not
    active-scope ownership, so without a recoverable retry marker the next sweep
    would authority-check with publish_view=False, clear grace, and leave the mount
    empty. Preserve a fenced marker so only that workspace is republished in place.
    """
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    other = await retained(runtime, value, factory, "ws_other_stable", other=True)
    inode = path.stat().st_ino
    other_token = (await runtime.publication("ws_other_stable")).token
    replace_file = os.replace

    def fail_second_file(source, destination, **kwargs):
        if destination == "github.api-key":
            raise OSError("synthetic retained refresh failure")
        return replace_file(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", fail_second_file)
    with pytest.raises(OSError, match="synthetic retained refresh failure"):
        await runtime.workspace("ws_cloud_stable")
    assert path.is_dir()
    assert path.stat().st_ino == inode
    assert not list(path.iterdir())
    assert not (path.parent / "publication").exists()
    assert (path.parent / "publication.retry").exists()
    assert await runtime.publication("ws_cloud_stable") is None

    monkeypatch.setattr(os, "replace", replace_file)
    assert await runtime.reconcile_workspace("ws_cloud_stable")
    assert path.stat().st_ino == inode
    assert (path / "openai.api-key").read_bytes() == b"synthetic-agent-key"
    assert (path / "github.api-key").read_bytes() == b"synthetic-github-key"
    assert (path.parent / "publication").exists()
    assert not (path.parent / "publication.retry").exists()
    assert await runtime.publication("ws_cloud_stable") is not None
    assert (await runtime.publication("ws_other_stable")).token == other_token
    assert (other / "openai.api-key").read_bytes() == b"synthetic-other-agent"


@pytest.mark.parametrize("change", ["retracted", "successor"])
async def test_retained_retry_publish_respects_generation_fence(authority, monkeypatch, change):
    """Retained reconcile must not republish after concurrent retract or successor.

    Observing publication.retry as a boolean before authority reads is not a write
    fence: cancellation cleanup can clear the mount, or another consumer can publish
    a successor, while materials() awaits. The first publish must validate the exact
    retry generation under the publication lock.
    """
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    inode = path.stat().st_ino
    replace_file = os.replace

    def fail_second_file(source, destination, **kwargs):
        if destination == "github.api-key":
            raise OSError("synthetic retained refresh failure")
        return replace_file(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", fail_second_file)
    with pytest.raises(OSError, match="synthetic retained refresh failure"):
        await runtime.workspace("ws_cloud_stable")
    monkeypatch.setattr(os, "replace", replace_file)
    assert (path.parent / "publication.retry").exists()
    generation = (path.parent / "publication.retry").read_text(encoding="ascii")
    assert len(generation) == 32

    entered, release = asyncio.Event(), asyncio.Event()
    original_current = runtime._current

    async def pause_current(anchor):
        if not entered.is_set():
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
        return await original_current(anchor)

    monkeypatch.setattr(runtime, "_current", pause_current)
    task = asyncio.create_task(runtime.reconcile_workspace("ws_cloud_stable"))
    await asyncio.wait_for(entered.wait(), 5)
    successor = None
    if change == "retracted":
        runtime.views.remove("ws_cloud_stable", preserve_directory=True)
        assert not (path.parent / "publication.retry").exists()
    else:
        successor = runtime.views.publish(
            "ws_cloud_stable",
            {
                "openai.api-key": b"synthetic-successor",
                "github.api-key": b"synthetic-successor",
            },
        )
        assert successor is not None
        assert not (path.parent / "publication.retry").exists()
    release.set()
    assert await asyncio.wait_for(task, 5) is True
    assert runtime.views.publication_retry("ws_cloud_stable") != generation
    if change == "retracted":
        assert await runtime.publication("ws_cloud_stable") is None
        assert path.is_dir() and path.stat().st_ino == inode
        assert not list(path.iterdir())
        assert not (path.parent / "publication.retry").exists()
    else:
        published = await runtime.publication("ws_cloud_stable")
        assert published is not None and published.token == successor.token
        assert (path / "openai.api-key").read_bytes() == b"synthetic-successor"
        assert (path / "github.api-key").read_bytes() == b"synthetic-successor"


@pytest.mark.parametrize("successor", [False, True])
async def test_cancel_invalidates_failed_publication_retry_without_touching_successor(
    authority, tmp_path, monkeypatch, successor
):
    """Cancel after a retained refresh failure must fence-invalidate publication.retry.

    Failure cleanup clears ownership.publication but leaves a retry generation. Cancel
    _stop(authority_lost=False) still holds ContextVar ownership and previously called
    retract_publication(None), which skipped removal and left the marker for reconcile
    to republish. Keep fenced ownership of the retry generation so cancel invalidates
    that exact marker without removing a successor live publication.
    """
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    inode = path.stat().st_ino
    replace_file = os.replace

    def fail_second_file(source, destination, **kwargs):
        if destination == "github.api-key":
            raise OSError("synthetic cancel retry failure")
        return replace_file(source, destination, **kwargs)

    consumers = WorkspaceConsumers(runtime, tmp_path)
    consumers.stop_workspace = AsyncMock()
    with track_publications(runtime.views, "ws_cloud_stable"):
        ownership = owned_publication(runtime.views, "ws_cloud_stable")
        assert ownership is not None
        await runtime.workspace("ws_cloud_stable")
        monkeypatch.setattr(os, "replace", fail_second_file)
        with pytest.raises(OSError, match="synthetic cancel retry failure"):
            await runtime.workspace("ws_cloud_stable")
        monkeypatch.setattr(os, "replace", replace_file)
        assert ownership.publication is None
        assert ownership.retry is not None
        generation = ownership.retry
        assert (path.parent / "publication.retry").read_text(encoding="ascii") == generation
        published = None
        if successor:
            # Sweep/reconcile has no scope ContextVar; clear it briefly so publish
            # adopts a distinct ownership the way an out-of-band successor would.
            cleared = _publication_ownership.set(None)
            try:
                published = runtime.views.publish(
                    "ws_cloud_stable",
                    {
                        "openai.api-key": b"synthetic-successor",
                        "github.api-key": b"synthetic-successor",
                    },
                    expected_retry=generation,
                )
            finally:
                _publication_ownership.reset(cleared)
            assert published is not None
            assert ownership.publication is None
            assert ownership.retry == generation
            assert not (path.parent / "publication.retry").exists()
        await consumers._stop("ws_cloud_stable", authority_lost=False)
        assert ownership.retry is None
    assert path.is_dir() and path.stat().st_ino == inode
    if successor:
        assert (await runtime.publication("ws_cloud_stable")).token == published.token
        assert (path / "openai.api-key").read_bytes() == b"synthetic-successor"
        assert (path / "github.api-key").read_bytes() == b"synthetic-successor"
    else:
        assert not (path.parent / "publication.retry").exists()
        assert await runtime.publication("ws_cloud_stable") is None
        assert not list(path.iterdir())
        assert await runtime.reconcile_workspace("ws_cloud_stable")
        assert not list(path.iterdir()), "cancel must not leave a retry for reconcile republish"


async def test_cancel_deletes_leftover_credentials_before_clearing_retry_marker(
    authority, tmp_path, monkeypatch
):
    """Cancel must unlink leftover credentials before dropping publication.retry.

    Retained refresh cleanup can fence a retry marker even when credential unlink
    fails mid-way. invalidate_retry used to clear only the marker, leaving readable
    keys in the warm mount that later sweeps skip (no publication, no retry).
    """
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    inode = path.stat().st_ino
    replace_file = os.replace
    real_unlink = os.unlink
    credential_unlinks = 0
    fail_cleanup_unlinks = False

    def fail_second_file(source, destination, **kwargs):
        if destination == "github.api-key":
            raise OSError("synthetic cancel leftover refresh failure")
        return replace_file(source, destination, **kwargs)

    def fail_second_credential_unlink(name, *args, **kwargs):
        nonlocal credential_unlinks
        if (
            fail_cleanup_unlinks
            and isinstance(name, str)
            and name in {"openai.api-key", "anthropic.api-key", "github.api-key"}
            and kwargs.get("dir_fd") is not None
        ):
            credential_unlinks += 1
            if credential_unlinks >= 2:
                raise OSError("synthetic cleanup unlink failure")
        return real_unlink(name, *args, **kwargs)

    consumers = WorkspaceConsumers(runtime, tmp_path)
    consumers.stop_workspace = AsyncMock()
    with track_publications(runtime.views, "ws_cloud_stable"):
        ownership = owned_publication(runtime.views, "ws_cloud_stable")
        assert ownership is not None
        await runtime.workspace("ws_cloud_stable")
        monkeypatch.setattr(os, "replace", fail_second_file)
        monkeypatch.setattr(os, "unlink", fail_second_credential_unlink)
        fail_cleanup_unlinks = True
        with pytest.raises(OSError, match="synthetic cancel leftover refresh failure"):
            await runtime.workspace("ws_cloud_stable")
        fail_cleanup_unlinks = False
        monkeypatch.setattr(os, "replace", replace_file)
        monkeypatch.setattr(os, "unlink", real_unlink)
        assert ownership.publication is None
        assert ownership.retry is not None
        assert list(path.iterdir()), "partial cleanup must leave readable leftover material"
        await consumers._stop("ws_cloud_stable", authority_lost=False)
        assert ownership.retry is None
    assert path.is_dir() and path.stat().st_ino == inode
    assert not (path.parent / "publication.retry").exists()
    assert not list(path.iterdir()), "cancel must delete leftover credentials with the marker"
    assert await runtime.publication("ws_cloud_stable") is None


async def test_cancel_preserves_retry_marker_when_leftover_credential_unlink_fails(
    authority, tmp_path, monkeypatch
):
    """If cancel cannot delete leftovers, keep publication.retry for retryable cleanup."""
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    replace_file = os.replace
    real_unlink = os.unlink
    credential_unlinks = 0
    fail_cleanup_unlinks = False
    fail_cancel_unlinks = False

    def fail_second_file(source, destination, **kwargs):
        if destination == "github.api-key":
            raise OSError("synthetic cancel preserve refresh failure")
        return replace_file(source, destination, **kwargs)

    def fail_credential_unlink(name, *args, **kwargs):
        nonlocal credential_unlinks
        if (
            (fail_cleanup_unlinks or fail_cancel_unlinks)
            and isinstance(name, str)
            and name in {"openai.api-key", "anthropic.api-key", "github.api-key"}
            and kwargs.get("dir_fd") is not None
        ):
            credential_unlinks += 1
            if fail_cleanup_unlinks and credential_unlinks >= 2:
                raise OSError("synthetic cleanup unlink failure")
            if fail_cancel_unlinks:
                raise OSError("synthetic cancel unlink failure")
        return real_unlink(name, *args, **kwargs)

    consumers = WorkspaceConsumers(runtime, tmp_path)
    consumers.stop_workspace = AsyncMock()
    with track_publications(runtime.views, "ws_cloud_stable"):
        ownership = owned_publication(runtime.views, "ws_cloud_stable")
        assert ownership is not None
        await runtime.workspace("ws_cloud_stable")
        monkeypatch.setattr(os, "replace", fail_second_file)
        monkeypatch.setattr(os, "unlink", fail_credential_unlink)
        fail_cleanup_unlinks = True
        with pytest.raises(OSError, match="synthetic cancel preserve refresh failure"):
            await runtime.workspace("ws_cloud_stable")
        fail_cleanup_unlinks = False
        monkeypatch.setattr(os, "replace", replace_file)
        generation = ownership.retry
        assert generation is not None
        leftovers = list(path.iterdir())
        assert leftovers
        credential_unlinks = 0
        fail_cancel_unlinks = True
        await consumers._stop("ws_cloud_stable", authority_lost=False)
        fail_cancel_unlinks = False
        monkeypatch.setattr(os, "unlink", real_unlink)
        assert ownership.retry == generation
        assert (path.parent / "publication.retry").read_text(encoding="ascii") == generation
        assert list(path.iterdir()) == leftovers
    monkeypatch.setattr(os, "unlink", real_unlink)
    assert runtime.views.invalidate_retry("ws_cloud_stable", generation) is True
    assert not (path.parent / "publication.retry").exists()
    assert not list(path.iterdir())


async def test_initial_publication_failure_removes_unmounted_view(authority, monkeypatch):
    """A brand-new view is not bind-mounted yet, so failure may remove it entirely."""
    runtime, _, _ = authority
    replace_file = os.replace

    def fail_second_file(source, destination, **kwargs):
        if destination == "github.api-key":
            raise OSError("synthetic initial publication failure")
        return replace_file(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", fail_second_file)
    with pytest.raises(OSError, match="synthetic initial publication failure"):
        runtime.views.publish(
            "ws_fresh",
            {"openai.api-key": b"synthetic-agent", "github.api-key": b"synthetic-github"},
        )
    key = runtime.views._key("ws_fresh")
    assert not (runtime.receiver.store.root / key).exists()


async def test_publication_fence_is_shared_by_view_instances_and_workspace_bound(authority):
    runtime, _, _ = authority
    files = {"openai.api-key": b"synthetic-agent", "github.api-key": b"synthetic-github"}
    first = runtime.views.publish("ws_first", files)
    second_views = RuntimeViews(runtime.receiver.store, identity=runtime.receiver.identity)
    second = second_views.publish("ws_first", files)
    assert first.path == second.path and first.token != second.token
    assert (second.path.parent / "publication").stat().st_mode & 0o777 == 0o600
    assert {child.name for child in second.path.iterdir()} == set(files)
    runtime.views.remove("ws_first", publication=first)
    assert (second.path / "openai.api-key").read_bytes() == files["openai.api-key"]
    with pytest.raises(MaterialDeniedError, match="INVALID_RUNTIME_VIEW"):
        runtime.views.remove("ws_other", publication=second)
    assert second.path.is_dir()
    runtime.views.remove("ws_first", publication=second)
    assert not second.path.parent.exists()
    runtime.views.remove("ws_first", publication=second)


@pytest.mark.parametrize("mutation", ["missing", "mode", "value", "characters", "hardlink"])
async def test_publication_fence_uncertainty_never_deletes_view_but_revoke_still_can(
    authority, mutation
):
    runtime, _, _ = authority
    publication = runtime.views.publish(
        "ws_first", {"openai.api-key": b"synthetic-agent", "github.api-key": b"synthetic-github"}
    )
    marker = publication.path.parent / "publication"
    extra = runtime.receiver.store.root / "extra-link"
    if mutation == "missing":
        marker.unlink()
    elif mutation == "mode":
        marker.chmod(0o644)
    elif mutation == "value":
        marker.write_text("not-a-publication-token")
    elif mutation == "characters":
        marker.write_text("z" * 32)
    else:
        extra.hardlink_to(marker)
    try:
        if mutation == "missing":
            runtime.views.remove("ws_first", publication=publication)
        else:
            with pytest.raises(MaterialDeniedError, match="INVALID_RUNTIME_VIEW"):
                runtime.views.remove("ws_first", publication=publication)
        assert (publication.path / "openai.api-key").read_bytes() == b"synthetic-agent"
        runtime.views.remove("ws_first")
        assert not publication.path.parent.exists()
    finally:
        extra.unlink(missing_ok=True)
