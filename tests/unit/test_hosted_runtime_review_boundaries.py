"""Negative authority and lifecycle edges using durable receipts and real files."""

import asyncio
import base64
import os
import secrets
import stat
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from awf.common.commands import FakeCommandRunner
from awf.common.workspace_scope import workspace_consumer
from awf.db.models import Workspace
from awf.hosted_node.identity import MaterialDeniedError, WorkspaceScopeDeniedError
from awf.hosted_node.lifecycle_store import locked
from awf.hosted_node.models import Grant
from awf.hosted_node.runtime_consumers import HostedCommandRunner, WorkspaceConsumers
from awf.hosted_node.runtime_filesystem import share_checkout
from awf.hosted_node.runtime_schemas import POLICY_KEY, HostedWorkspace, association_matches
from awf.hosted_node.runtime_worker import start_runtime
from awf.hosted_node.schemas import Refresh
from awf.profiles.models import WorkspaceProfile
from awf.service.resource_capacity import LocalCapacityLimits
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_capacity import configured
from tests.unit.test_hosted_runtime_contract import hosted_create


@pytest.fixture
async def workspace(authority):
    runtime, value, factory = authority
    association = HostedWorkspace.model_validate(value["hosted"])
    async with factory.begin() as session:
        session.add(
            Workspace(
                id=association.workspace_id,
                status="requested",
                repo_url=value["repo"]["url"],
                branch_base="main",
                task_title="Boundary regression",
                task_prompt="No external execution",
                agent="codex",
                task_policy={POLICY_KEY: association.model_dump(mode="json")},
            )
        )
    return runtime, association, factory


@pytest.mark.parametrize("name", ["CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"])
async def test_profile_cannot_override_current_credential_environment(workspace, name):
    runtime, association, _ = workspace
    profile = WorkspaceProfile.model_validate(
        {"name": "boundary", "runtime": {"environment": {name: secrets.token_hex(16)}}}
    )
    with pytest.raises(MaterialDeniedError, match="HOSTED_PROFILE_AUTH_OVERRIDE"):
        await runtime.profile_mounts(profile, association.workspace_id)


@pytest.mark.parametrize(
    "change",
    [
        {"name": "unbound"},
        {"provider": "env"},
        {"kind": "env"},
        {"mode": "rw"},
        {"ref": "untrusted-reference"},
        {"target": "/run/awf/secrets/anthropic.api-key"},
    ],
)
async def test_profile_declaration_must_match_current_protected_binding(workspace, change):
    runtime, association, _ = workspace
    declaration = {
        "name": "agent",
        "provider": "protected",
        "kind": "mount",
        "mode": "ro",
        "target": association.credentials.agent.target,
    }
    profile = WorkspaceProfile.model_validate(
        {"name": "boundary", "secrets": [{**declaration, **change}]}
    )
    with pytest.raises(MaterialDeniedError, match="PROTECTED_DECLARATION_MISMATCH"):
        await runtime.profile_mounts(profile, association.workspace_id)


async def test_exact_explicit_profile_declarations_resolve_readonly_workspace_view(workspace):
    runtime, association, _ = workspace
    profile = WorkspaceProfile.model_validate(
        {
            "name": "boundary",
            "secrets": [
                {"name": role, "provider": "protected", "target": binding.target}
                for role, binding in (
                    ("agent", association.credentials.agent),
                    ("github", association.credentials.github),
                )
            ],
        }
    )
    result = await runtime.profile_mounts(profile, association.workspace_id)
    assert len(result.mounts) == 1
    assert result.mounts[0].mode == "ro"
    assert result.mounts[0].target == "/run/awf/secrets"
    assert set(Path(result.mounts[0].source).iterdir()) == {
        Path(result.mounts[0].source) / "openai.api-key",
        Path(result.mounts[0].source) / "github.api-key",
    }
    assert not result.environment


@pytest.mark.parametrize("fault", ["absent_anchor", "pending_anchor", "absent_current"])
async def test_missing_or_uninstalled_receipt_never_resolves_material(workspace, fault):
    runtime, association, factory = workspace
    anchor = association.credentials.agent
    async with factory.begin() as session:
        receipt = await runtime.receiver.receipt(session, anchor)
        assert receipt is not None
        if fault == "absent_anchor":
            await session.delete(receipt)
        elif fault == "pending_anchor":
            receipt.state = "pending"
        else:
            grant = await session.get(Grant, receipt.grant)
            assert grant is not None
            grant.current = secrets.token_hex(16)
    with pytest.raises(MaterialDeniedError, match="PROTECTED_MATERIAL_UNAVAILABLE"):
        await runtime.workspace(association.workspace_id)
    assert not (runtime.views.store.root / runtime.views._key(association.workspace_id)).exists()


@pytest.mark.parametrize("field", ["namespace", "revision", "fence", "authorization_generation"])
async def test_current_receipt_cannot_rebind_or_regress_the_durable_anchor(workspace, field):
    runtime, association, factory = workspace
    anchor = association.credentials.agent
    previous = await runtime.receiver.status(anchor)
    refreshed = anchor.model_copy(
        update={
            "revision": 2,
            "fence": 2,
            "idempotency_key": "boundary-refresh",
        }
    )
    await runtime.receiver.install(
        Refresh(
            **refreshed.model_dump(),
            predecessor=previous.receipt_id,
            material=base64.b64encode(secrets.token_hex(16).encode()).decode(),
        ),
        refresh=True,
    )
    assert await runtime._current(anchor) == refreshed
    previous = await runtime.receiver.status(refreshed)
    successor = refreshed.model_copy(
        update={
            "revision": 3,
            "fence": 3,
            "idempotency_key": "boundary-successor",
        }
    )
    await runtime.receiver.install(
        Refresh(
            **successor.model_dump(),
            predecessor=previous.receipt_id,
            material=base64.b64encode(secrets.token_hex(16).encode()).decode(),
        ),
        refresh=True,
    )
    # Corrupt only the synthetic DB's successor, keeping the actual anchor receipt.
    async with factory.begin() as session:
        row = await runtime.receiver.receipt(session, successor)
        assert row is not None
        value = dict(row.binding)
        value[field] = (
            "foreign-namespace"
            if field == "namespace"
            else (2 if field == "authorization_generation" else 1)
        )
        row.binding = value
    with pytest.raises(MaterialDeniedError, match="HOSTED_WORKSPACE_BINDING_MISMATCH"):
        await runtime._current(refreshed)


async def test_non_ascii_api_key_is_rejected_after_real_refresh(workspace):
    runtime, association, _ = workspace
    anchor = association.credentials.agent
    previous = await runtime.receiver.status(anchor)
    await runtime.receiver.install(
        Refresh(
            **{**anchor.model_dump(), "revision": 2, "fence": 2, "idempotency_key": "non-ascii"},
            predecessor=previous.receipt_id,
            material=base64.b64encode(secrets.token_bytes(16) + b"\xff").decode(),
        ),
        refresh=True,
    )
    with pytest.raises(MaterialDeniedError, match="INVALID_API_KEY_MATERIAL"):
        await runtime.workspace(association.workspace_id)


@pytest.mark.parametrize("phase", ["after_read", "after_publication"])
async def test_real_revocation_between_material_boundaries_denies_consumer(
    workspace, monkeypatch, phase
):
    runtime, association, _ = workspace
    original_status = runtime.receiver.status
    original_read = runtime.receiver.store.read
    original_publish = runtime.views.publish
    boundary = False
    revoked = False

    def read(*args):
        nonlocal boundary
        result = original_read(*args)
        if phase == "after_read":
            boundary = True
        return result

    def publish(*args):
        nonlocal boundary
        result = original_publish(*args)
        if phase == "after_publication":
            boundary = True
        return result

    async def status(binding):
        nonlocal revoked
        if boundary and not revoked:
            revoked = True
            await runtime.receiver.revoke(
                association.credentials.agent.model_copy(
                    update={
                        "idempotency_key": "boundary-revoke",
                        "fence": 2,
                    }
                )
            )
        return await original_status(binding)

    monkeypatch.setattr(runtime.receiver.store, "read", read)
    monkeypatch.setattr(runtime.views, "publish", publish)
    monkeypatch.setattr(runtime.receiver, "status", status)
    with pytest.raises(MaterialDeniedError, match="PROTECTED_MATERIAL_UNAVAILABLE"):
        await runtime.workspace(association.workspace_id)
    assert revoked
    assert not (
        runtime.views.store.root / runtime.views._key(association.workspace_id) / "view"
    ).exists()


async def test_repeated_cancellation_waits_for_publisher_then_retracts(workspace, monkeypatch):
    runtime, association, _ = workspace
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = runtime.views.publish

    def publish(*args):
        entered.set()
        assert release.wait(timeout=5)
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(runtime.views, "publish", publish)
    pending = asyncio.create_task(runtime.workspace(association.workspace_id))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert finished.is_set()
    assert not (
        runtime.views.store.root / runtime.views._key(association.workspace_id) / "view"
    ).exists()


@pytest.mark.parametrize("fault", ["missing_policy", "different_workspace"])
async def test_persisted_workspace_must_have_its_own_association(workspace, fault):
    runtime, association, factory = workspace
    async with factory.begin() as session:
        row = await session.get(Workspace, association.workspace_id)
        assert row is not None
        if fault == "missing_policy":
            row.task_policy = {}
        else:
            value = association.model_dump(mode="json")
            value["workspace_id"] = "ws_other"
            for binding in value["credentials"].values():
                binding["workspace_id"] = "ws_other"
            row.task_policy = {POLICY_KEY: value}
    with pytest.raises(MaterialDeniedError, match="HOSTED_WORKSPACE_"):
        await runtime.workspace(association.workspace_id)


@pytest.mark.parametrize("stored", [None, {}, {"workspace_id": "ws_broken"}])
def test_replay_cannot_accept_missing_or_malformed_stored_association(stored):
    incoming = HostedWorkspace.model_validate(hosted_create()["hosted"])
    assert not association_matches(stored, incoming)
    assert association_matches(stored, None) is (stored is None)


async def test_nested_scope_reauthorizes_and_rejects_cross_workspace(workspace, tmp_path):
    runtime, association, _ = workspace
    scope = WorkspaceConsumers(runtime, tmp_path)
    workspace_id = association.workspace_id
    with pytest.raises(WorkspaceScopeDeniedError, match="PROTECTED_MATERIAL_UNAVAILABLE"):
        async with scope.scope(workspace_id):
            async with scope.scope(workspace_id):
                assert scope.workspace_id.get() == workspace_id
            with pytest.raises(MaterialDeniedError, match="WORKSPACE_SCOPE_MISMATCH"):
                async with scope.scope("ws_foreign"):
                    pytest.fail("cross-workspace body executed")
            await runtime.receiver.revoke(
                association.credentials.agent.model_copy(
                    update={
                        "idempotency_key": "nested-revoke",
                        "fence": 2,
                    }
                )
            )
            with pytest.raises(MaterialDeniedError):
                async with scope.scope(workspace_id):
                    pytest.fail("revoked nested body executed")
    assert scope.workspace_id.get() is None


@pytest.mark.parametrize(
    "args,reason",
    [
        (["--privileged", "agent", "true"], "UNSUPPORTED_HOSTED_EXEC_OPTION"),
        (["-T"], "HOSTED_EXEC_SERVICE_REQUIRED"),
        (["-w", "/workspace"], "HOSTED_EXEC_SERVICE_REQUIRED"),
    ],
)
async def test_malformed_compose_exec_never_reaches_delegate(workspace, tmp_path, args, reason):
    runtime, association, _ = workspace
    scope = WorkspaceConsumers(runtime, tmp_path)
    delegate = FakeCommandRunner()
    async with scope.scope(association.workspace_id):
        with pytest.raises(MaterialDeniedError, match=reason):
            await HostedCommandRunner(scope, delegate).run(["docker", "compose", "exec", *args])
    assert not delegate.calls


async def test_caller_environment_cannot_override_current_scoped_authority(
    workspace, tmp_path, monkeypatch
):
    runtime, association, _ = workspace
    endpoint = f"unix://{tmp_path}/execution.sock"
    monkeypatch.setenv("DOCKER_HOST", endpoint)
    monkeypatch.setenv("DOCKER_CONTEXT", "unrelated-context")
    scope = WorkspaceConsumers(runtime, tmp_path)
    delegate = FakeCommandRunner()
    foreign = secrets.token_hex(16)
    async with scope.scope(association.workspace_id):
        await HostedCommandRunner(scope, delegate).run(
            ["git", "status"],
            env={
                "GH_TOKEN": foreign,
                "HOME": "/foreign",
                "TERM": "dumb",
                "DOCKER_HOST": "unix:///foreign.sock",
                "DOCKER_CONTEXT": "foreign-context",
            },
        )
    call = delegate.calls[0]
    protected = call.env["GH_TOKEN"] != foreign
    assert protected
    assert call.env["HOME"] == str(tmp_path / "hosted-empty-home")
    assert call.env["TERM"] == "dumb"
    assert call.env["DOCKER_HOST"] == endpoint
    assert "DOCKER_CONTEXT" not in call.env


async def test_local_workspace_consumer_keeps_arguments_and_result_without_hosted_scope():
    class Local:
        @workspace_consumer
        async def inspect(self, workspace_id, *, detail):
            return workspace_id, detail

    assert await Local().inspect("ws_local", detail="summary") == ("ws_local", "summary")


def _checkout(tmp_path):
    root = tmp_path / "git"
    mirror, worktree = root / "mirrors/repo.git", root / "worktrees/ws_boundary"
    mirror.mkdir(parents=True)
    worktree.mkdir(parents=True)
    return root, mirror, worktree


def test_unmanaged_linked_git_metadata_is_rejected_before_chmod(tmp_path):
    root, mirror, worktree = _checkout(tmp_path)
    outside = tmp_path / "foreign-metadata"
    outside.mkdir(mode=0o700)
    (worktree / ".git").write_text(f"gitdir: {outside}\n")
    with pytest.raises(MaterialDeniedError, match="UNMANAGED_WORKSPACE_PATH"):
        share_checkout(root, mirror, worktree, os.getegid())
    assert stat.S_IMODE(outside.stat().st_mode) == 0o700


def test_missing_linked_metadata_is_not_created_and_loose_objects_stay_readonly(tmp_path):
    root, mirror, worktree = _checkout(tmp_path)
    missing = mirror / "worktrees/missing"
    (worktree / ".git").write_text(f"gitdir: {missing}\n")
    objects = mirror / "objects/ab"
    objects.mkdir(parents=True)
    loose = objects / "object"
    loose.write_bytes(b"immutable-git-object")
    loose.chmod(0o444)
    share_checkout(root, mirror, worktree, os.getegid())
    assert not missing.exists()
    assert stat.S_IMODE(objects.stat().st_mode) == 0o2770
    assert stat.S_IMODE(loose.stat().st_mode) == 0o444
    assert loose.read_bytes() == b"immutable-git-object"


@pytest.mark.parametrize("kind", ["fifo", "hardlink"])
def test_special_or_shared_inode_checkout_file_is_not_made_agent_writable(tmp_path, kind):
    root, mirror, worktree = _checkout(tmp_path)
    target = worktree / "unsafe"
    if kind == "fifo":
        os.mkfifo(target, 0o600)
    else:
        original = tmp_path / "original"
        original.write_text("preserve")
        original.chmod(0o600)
        target.hardlink_to(original)
    before = target.stat()
    with pytest.raises(MaterialDeniedError, match="UNSAFE_WORKSPACE_FILE"):
        share_checkout(root, mirror, worktree, os.getegid())
    after = target.stat()
    assert (after.st_mode, after.st_uid, after.st_gid) == (
        before.st_mode,
        before.st_uid,
        before.st_gid,
    )


@pytest.mark.parametrize("workspace_id", ["cw_other", "ws_../escape", "ws_\u00e9"])
async def test_view_rejects_invalid_workspace_before_creating_directory(authority, workspace_id):
    runtime, _, _ = authority
    before = set(runtime.views.store.root.iterdir())
    with pytest.raises(MaterialDeniedError, match="INVALID_WORKSPACE_ID"):
        runtime.views.publish(
            workspace_id,
            {
                "openai.api-key": secrets.token_bytes(16),
                "github.api-key": secrets.token_bytes(16),
            },
        )
    assert set(runtime.views.store.root.iterdir()) == before


@pytest.mark.parametrize("size", [0, 65537])
async def test_view_rejects_empty_or_oversize_material_before_writing(authority, size):
    runtime, _, _ = authority
    before = set(runtime.views.store.root.iterdir())
    with pytest.raises(MaterialDeniedError, match="INVALID_RUNTIME_VIEW_MATERIAL"):
        runtime.views.publish(
            "ws_boundary",
            {
                "openai.api-key": secrets.token_bytes(size),
                "github.api-key": secrets.token_bytes(16),
            },
        )
    assert set(runtime.views.store.root.iterdir()) == before


@pytest.mark.parametrize("operation", ["publish", "remove"])
async def test_unexpected_view_file_fails_closed_without_deleting_or_replacing(
    authority, operation
):
    runtime, _, _ = authority
    files = {"openai.api-key": secrets.token_bytes(16), "github.api-key": secrets.token_bytes(16)}
    path = runtime.views.publish("ws_boundary", files).path
    path.chmod(0o710 | 0o200)
    unexpected = path / "unexpected"
    unexpected.write_bytes(b"preserve")
    path.chmod(0o710)
    before = {child.name: child.read_bytes() for child in path.iterdir()}
    with pytest.raises(MaterialDeniedError, match="INVALID_RUNTIME_VIEW_TARGET"):
        if operation == "publish":
            runtime.views.publish("ws_boundary", files)
        else:
            runtime.views.remove("ws_boundary")
    unchanged = {child.name: child.read_bytes() for child in path.iterdir()} == before
    assert unchanged
    path.chmod(0o710 | 0o200)
    unexpected.unlink()
    path.chmod(0o710)


@pytest.mark.parametrize("disabled", ["config", "lifecycle"])
async def test_disabled_runtime_never_builds_worker(authority, tmp_path, monkeypatch, disabled):
    from awf.hosted_node import runtime_worker

    runtime, _, _ = authority
    build = Mock(side_effect=AssertionError("disabled runtime built worker"))
    monkeypatch.setattr(runtime_worker, "build_worker_runtime", build)
    if disabled == "config":
        runtime.receiver.config = runtime.receiver.config.model_copy(
            update={"runtime_enabled": False}
        )
    else:
        runtime.receiver.lifecycle = None
    with pytest.raises(MaterialDeniedError, match="HOSTED_RUNTIME_DISABLED"):
        await runtime.admission()
    with pytest.raises(MaterialDeniedError, match="HOSTED_RUNTIME_DISABLED"):
        async with start_runtime(runtime.receiver, configured(tmp_path)):
            pytest.fail("disabled runtime started")
    build.assert_not_called()


async def test_worker_admission_and_shutdown_use_real_heartbeat_and_cancel_owned_task(
    authority, tmp_path, monkeypatch
):
    from awf.hosted_node import runtime_worker

    runtime, _, factory = authority
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
    settings = replace(
        configured(tmp_path),
        database_url=url,
        github_token=None,
        work_dir=str(runtime.receiver.config.host.workspaces_root),
        worker_poll_interval_seconds=0.02,
        auto_cleanup_orphans=False,
        terminal_workspace_gc_enabled=False,
        claude_base_gc_enabled=False,
    )
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def outstanding_execution():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with start_runtime(runtime.receiver, settings) as running:
        for _ in range(100):
            if await running.is_ready(runtime.receiver.identity):
                break
            await asyncio.sleep(0.02)
        await running.admission()
        task = asyncio.create_task(outstanding_execution())
        running.runtime.worker._execution_tasks["ws_boundary"] = task
        await asyncio.wait_for(entered.wait(), 2)
        async with factory.begin() as session:
            row = await locked(session, runtime.receiver.identity)
            row.phase = "draining"
        with pytest.raises(MaterialDeniedError, match="HOSTED_RUNTIME_UNAVAILABLE"):
            await running.admission()
    assert running.task.done()
    assert task.cancelled() and cancelled.is_set()
    assert not await running.is_ready(runtime.receiver.identity)
