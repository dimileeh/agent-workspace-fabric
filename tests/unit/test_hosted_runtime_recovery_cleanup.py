"""Actual recovery, inactive cleanup and concurrent credential publication."""

import asyncio
import base64
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
from awf.hosted_node.runtime_views import RuntimeViews
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
            with pytest.raises(MaterialDeniedError, match="PROTECTED_MATERIAL_UNAVAILABLE"):
                await asyncio.wait_for(old_task, 5)
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
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    other = await retained(runtime, value, factory, "ws_other", other=True)
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
    assert not path.parent.exists()
    assert other.is_dir()


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
