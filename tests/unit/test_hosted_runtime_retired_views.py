"""Only empty views of durably retired boots may release material capacity."""

import asyncio
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from awf.db.models import Workspace
from awf.hosted_node.identity import MaterialDeniedError, WorkspaceScopeDeniedError
from awf.hosted_node.receiver import Receiver
from awf.hosted_node.runtime_consumers import WorkspaceConsumers
from awf.hosted_node.runtime_views import RuntimeViews
from awf.hosted_node.schemas import Install, Preparation
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_retained import retained

FILES = {"openai.api-key": b"synthetic-agent", "github.api-key": b"synthetic-github"}


async def test_restart_preserves_generic_material_workspace_id_contract(authority):
    runtime, payload, factory = authority
    binding = {
        **payload["hosted"]["credentials"]["agent"],
        "workspace_id": "foundation-only",
        "preparation_id": "foundation-preparation",
        "grant_id": "foundation-grant",
        "idempotency_key": "foundation-install",
    }
    await runtime.receiver.prepare(
        Preparation.model_validate({name: binding[name] for name in Preparation.model_fields})
    )
    await runtime.receiver.install(Install.model_validate({**binding, "material": "c3ludGhldGlj"}))
    await runtime.receiver.close()
    successor = await Receiver.start(runtime.receiver.config, factory)
    await successor.close()


async def test_receiver_restart_reclaims_empty_receipt_bound_view(authority):
    runtime, payload, factory = authority
    path = await retained(runtime, payload, factory, "ws_cloud_stable")
    inode = path.stat().st_ino
    await runtime.receiver.close()
    assert path.stat().st_ino == inode and not list(path.iterdir())
    successor = await Receiver.start(runtime.receiver.config, factory)
    try:
        assert not path.parent.exists()
    finally:
        await successor.close()


@pytest.mark.parametrize("state", ["empty", "readable", "token-only", "foreign"])
async def test_restart_reclaims_only_empty_known_retired_view(authority, state):
    runtime, payload, factory = authority
    path = await retained(runtime, payload, factory, "ws_cloud_stable")
    if state == "foreign":
        foreign = RuntimeViews(
            runtime.receiver.store,
            identity=runtime.receiver.identity.model_copy(update={"owner": "foreign/node"}),
        )
        publication = foreign.publish("ws_cloud_stable", FILES)
        foreign.remove_owned("ws_cloud_stable", publication)
        path = publication.path
    elif state == "readable":
        pass
    else:
        runtime.views.remove("ws_cloud_stable", preserve_directory=True)
        if state == "token-only":
            (path.parent / "publication").write_text("a" * 32)
            (path.parent / "publication").chmod(0o600)
    inode = path.stat().st_ino
    successor = await Receiver.start(runtime.receiver.config, factory)
    try:
        if state == "empty":
            assert not path.parent.exists()
        else:
            assert path.stat().st_ino == inode
            if state == "readable":
                assert (path / "github.api-key").read_bytes() == b"synthetic-github-key"
    finally:
        await successor.close()


async def test_empty_retired_reclamation_preserves_same_boot_mount_and_capacity(authority):
    runtime, _, _ = authority
    store, identity = runtime.receiver.store, runtime.receiver.identity
    same = runtime.views.publish("ws_warm", FILES)
    runtime.views.remove_owned("ws_warm", same)
    inode = same.path.stat().st_ino
    assert not runtime.views.retire_empty("ws_warm", current=identity)
    assert same.path.stat().st_ino == inode
    runtime.views.remove("ws_warm")
    # Keep the actual 1024 bound: material receipts already occupy two roots.
    remaining = 1024 - len(list(store.root.iterdir()))
    old = []
    for index in range(remaining):
        workspace_id = f"ws_retired_{index}"
        publication = runtime.views.publish(workspace_id, FILES)
        runtime.views.remove_owned(workspace_id, publication)
        old.append(workspace_id)
    with pytest.raises(MaterialDeniedError, match="MATERIAL_CAPACITY"):
        runtime.views.publish("ws_capacity", FILES)
    current = identity.model_copy(update={"boot_epoch": identity.boot_epoch + 1})
    for workspace_id in old:
        assert runtime.views.retire_empty(workspace_id, current=current)
    assert not runtime.views.retire_empty(old[0], current=current)
    assert runtime.views.publish("ws_capacity", FILES) is not None


async def test_retirement_serializes_with_inflight_publication(authority, monkeypatch):
    runtime, _, _ = authority
    publication = runtime.views.publish("ws_retired", FILES)
    runtime.views.remove_owned("ws_retired", publication)
    current = runtime.receiver.identity.model_copy(
        update={"boot_epoch": runtime.receiver.identity.boot_epoch + 1}
    )
    entered, release = threading.Event(), threading.Event()
    original = runtime.views._publication

    def hold_writer(parent, token=None):
        if token is not None:
            entered.set()
            assert release.wait(5)
        return original(parent, token)

    monkeypatch.setattr(runtime.views, "_publication", hold_writer)
    writer = asyncio.create_task(asyncio.to_thread(runtime.views.publish, "ws_retired", FILES))
    reaper = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        reaper = asyncio.create_task(
            asyncio.to_thread(runtime.views.retire_empty, "ws_retired", current=current)
        )
        await asyncio.sleep(0)
    finally:
        release.set()
        published = await writer
        if reaper is not None:
            assert not await reaper
    assert (published.path / "github.api-key").read_bytes() == FILES["github.api-key"]
    assert runtime.views.publication("ws_retired").token == published.token


@pytest.mark.parametrize("entry", ["secret", "pending", "symlink", "unexpected-parent"])
async def test_retirement_never_unlinks_nonempty_or_unknown_entries(authority, tmp_path, entry):
    runtime, _, _ = authority
    publication = runtime.views.publish("ws_retired", FILES)
    runtime.views.remove_owned("ws_retired", publication)
    path = publication.path
    if entry == "unexpected-parent":
        target = path.parent / "unrecognized"
    else:
        target = path / ("openai.api-key.pending" if entry == "pending" else "openai.api-key")
    if entry == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(b"synthetic-outside")
        target.symlink_to(outside)
    else:
        target.write_bytes(b"synthetic-retained")
    current = runtime.receiver.identity.model_copy(
        update={"boot_epoch": runtime.receiver.identity.boot_epoch + 1}
    )
    assert not runtime.views.retire_empty("ws_retired", current=current)
    assert target.exists() and path.is_dir()
    target.unlink()


@pytest.mark.parametrize("replacement", ["symlink", "permissions"])
async def test_retirement_rejects_unsafe_directory_without_touching_target(
    authority, tmp_path, replacement
):
    runtime, _, _ = authority
    publication = runtime.views.publish("ws_retired", FILES)
    runtime.views.remove_owned("ws_retired", publication)
    path = publication.path
    current = runtime.receiver.identity.model_copy(
        update={"boot_epoch": runtime.receiver.identity.boot_epoch + 1}
    )
    if replacement == "symlink":
        path.rmdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        path.symlink_to(outside, target_is_directory=True)
    else:
        path.chmod(0o777)
    try:
        with pytest.raises((OSError, MaterialDeniedError)):
            runtime.views.retire_empty("ws_retired", current=current)
        assert path.exists()
    finally:
        if replacement == "symlink":
            path.unlink()
        else:
            path.chmod(0o710)


@pytest.mark.parametrize("policy", [None, {}, {"hosted_node": None}])
async def test_missing_association_denies_after_current_view_cleanup(authority, tmp_path, policy):
    runtime, payload, factory = authority
    path = await retained(runtime, payload, factory, "ws_cloud_stable")
    async with factory.begin() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        row.task_policy = policy
    consumers = WorkspaceConsumers(runtime, tmp_path)
    consumers.stop_workspace = AsyncMock()
    with pytest.raises(WorkspaceScopeDeniedError, match="HOSTED_WORKSPACE_REQUIRED"):
        async with consumers.scope("ws_cloud_stable"):
            pytest.fail("unbound scope entered")
    consumers.stop_workspace.assert_awaited_once()
    assert not path.exists()


@pytest.mark.parametrize("stored", [{}, "invalid", {"workspace_id": "ws_foreign"}])
async def test_malformed_association_is_typed_and_never_guesses_cleanup_identity(
    authority, tmp_path, stored
):
    runtime, payload, factory = authority
    path = await retained(runtime, payload, factory, "ws_cloud_stable")
    foreign = RuntimeViews(
        runtime.receiver.store,
        identity=runtime.receiver.identity.model_copy(update={"owner": "foreign/node"}),
    )
    protected = foreign.publish("ws_cloud_stable", FILES)
    async with factory.begin() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        row.task_policy = {"hosted_node": stored}
    consumers = WorkspaceConsumers(runtime, tmp_path)
    consumers.stop_workspace = AsyncMock()
    with pytest.raises(MaterialDeniedError, match="^HOSTED_WORKSPACE_BINDING_MISMATCH$"):
        async with consumers.scope("ws_cloud_stable"):
            pytest.fail("malformed scope entered")
    consumers.stop_workspace.assert_awaited_once()
    assert not path.exists()
    assert (protected.path / "github.api-key").read_bytes() == FILES["github.api-key"]


def test_http_contract_does_not_describe_opt_in_runtime_as_always_closed():
    text = (Path(__file__).parents[2] / "docs/contracts/node-idle-gate.md").read_text()
    assert "`runtime_ready` is **always false** in this slice" not in text
    assert "`BootEvidence.runtime_ready`" in text
