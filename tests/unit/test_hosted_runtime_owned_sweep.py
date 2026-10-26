"""Owned-row sweep before Compose placement and throughout the real lifecycle."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Self
from unittest.mock import AsyncMock

import pytest

from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.receiver import Receiver
from awf.hosted_node.runtime_authority import WorkspaceAuthority
from awf.hosted_node.runtime_consumers import WorkspaceConsumers
from awf.hosted_node.runtime_views import RuntimeViews
from awf.hosted_node.schemas import Binding
from awf.node.git_manager import GitManager, GitOperationError
from awf.node.provisioner import Provisioner, ProvisionerConfig
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import (
    create_application as create_application,
)
from tests.unit.test_hosted_runtime_review_create import (
    create_client as create_client,
)
from tests.unit.test_hosted_runtime_review_retained import retained


async def deny(runtime, value, failure, monkeypatch):
    if failure == "expiry":
        now = datetime.now(UTC) + timedelta(minutes=11)

        class Clock(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> Self:
                return cls.fromtimestamp(now.timestamp(), tz)

        monkeypatch.setattr("awf.hosted_node.receiver.datetime", Clock)
        monkeypatch.setattr("awf.hosted_node.runtime_authority.datetime", Clock)
    elif failure != "current":
        role = "github" if failure == "github_revoke" else "agent"
        binding = value["hosted"]["credentials"][role]
        await runtime.receiver.revoke(
            Binding.model_validate({**binding, "idempotency_key": "sweep-revoke", "fence": 2})
        )


@pytest.mark.parametrize("failure", ["revoke", "github_revoke", "expiry"])
async def test_actual_checkout_failure_without_compose_is_swept(
    authority, create_client, tmp_path, monkeypatch, failure
):
    runtime, value, factory = authority
    client, headers = create_client
    response = await client.post(
        "/v1/workspaces", json=value, headers={**headers, "Idempotency-Key": "prelaunch"}
    )
    assert response.status_code == 202, response.text
    workspace_id = response.json()["workspace_id"]
    path = runtime.receiver.store.root / runtime.views._key(workspace_id) / "view"
    assert not path.exists()
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock()
    launcher = AsyncMock()
    spawn = asyncio.create_subprocess_exec
    calls = []

    async def local_failure(*args, **kwargs):
        assert args[:3] == ("git", "clone", "--mirror")
        assert path.is_dir()
        assert kwargs["env"]["GH_TOKEN"] == "synthetic-github-key"
        calls.append(args[:3])
        local_args = [
            str(tmp_path / "absent-origin") if arg == value["repo"]["url"] else arg for arg in args
        ]
        return await spawn(*local_args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", local_failure)
    provisioner = Provisioner(
        session_factory=factory,
        git=GitManager(tmp_path / "git", command_environment=scope.git_environment),
        config=ProvisionerConfig(node_id=runtime.receiver.identity.owner),
        stack_launcher=launcher,
        workspace_scope=scope.scope,
    )
    with pytest.raises(GitOperationError):
        await provisioner.provision(workspace_id)
    assert calls == [("git", "clone", "--mirror")]
    launcher.launch.assert_not_awaited()
    async with factory() as session:
        row = await session.get(Workspace, workspace_id)
        assert row.status == "failed"
        assert row.node_id == runtime.receiver.identity.owner
        assert row.compose_project_name is None and row.compose_file_path is None
        assert row.filesystem_reclaimed_at is None
    assert path.is_dir()
    scope.stop_workspace.assert_not_awaited()
    await deny(runtime, value, failure, monkeypatch)
    assert await scope.admission_check()
    assert not path.parent.exists(), "prelaunch views require the same authority sweep"
    scope.stop_workspace.assert_awaited_once_with(workspace_id)


@pytest.mark.parametrize("phase", [status.value for status in WorkspaceStatus] + ["reclaimed"])
@pytest.mark.parametrize("compose", [False, True])
@pytest.mark.parametrize("failure", ["current", "revoke", "expiry"])
async def test_every_owned_lifecycle_phase_obeys_current_authority(
    authority, tmp_path, monkeypatch, phase, compose, failure
):
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    other = await retained(runtime, value, factory, "ws_other", other=True)
    async with factory.begin() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        row.status = "completed" if phase == "reclaimed" else phase
        row.filesystem_reclaimed_at = datetime.now(UTC) if phase == "reclaimed" else None
        if not compose:
            row.compose_project_name = row.compose_file_path = None
    await deny(runtime, value, failure, monkeypatch)
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock()
    assert await scope.admission_check()
    inactive = phase in {"destroyed", "reclaimed"}
    assert path.is_dir() is (failure == "current" and not inactive)
    if failure != "current" and not inactive:
        scope.stop_workspace.assert_awaited_once_with("ws_cloud_stable")
    else:
        scope.stop_workspace.assert_not_awaited()
    assert other.is_dir()


async def test_pending_owned_row_does_not_gain_a_view_from_background_poll(
    authority, create_client, tmp_path
):
    runtime, value, factory = authority
    client, headers = create_client
    response = await client.post(
        "/v1/workspaces", json=value, headers={**headers, "Idempotency-Key": "pending"}
    )
    assert response.status_code == 202, response.text
    path = runtime.receiver.store.root / runtime.views._key("ws_cloud_stable") / "view"
    async with factory() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        assert row.status == "requested" and row.compose_project_name is None
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock()
    assert await scope.admission_check()
    assert not path.exists()
    scope.stop_workspace.assert_not_awaited()


@pytest.mark.parametrize("stop_failure", [False, True])
async def test_sweep_keeps_exact_64_row_paging_and_owner_binding(authority, tmp_path, stop_failure):
    runtime, value, factory = authority
    expected = [f"ws_page_{index:03}" for index in range(129)]
    async with factory.begin() as session:
        for workspace_id in [*expected, "ws_foreign", "ws_local"]:
            association = deepcopy(value["hosted"])
            association["workspace_id"] = workspace_id
            for binding in association["credentials"].values():
                binding["workspace_id"] = workspace_id
            session.add(
                Workspace(
                    id=workspace_id,
                    status="requested",
                    repo_url="https://github.com/example/repo.git",
                    branch_base="main",
                    task_title="Synthetic",
                    task_prompt="Synthetic",
                    agent="codex",
                    node_id="other/node"
                    if workspace_id == "ws_foreign"
                    else runtime.receiver.identity.owner,
                    task_policy={} if workspace_id == "ws_local" else {"hosted_node": association},
                )
            )
    scope = WorkspaceConsumers(runtime, tmp_path)

    async def reconcile(workspace_id):
        if stop_failure and workspace_id == expected[0]:
            raise MaterialDeniedError("SYNTHETIC_REVOKED_AUTHORITY")

    runtime.reconcile_workspace = AsyncMock(side_effect=reconcile)
    scope._stop = AsyncMock(side_effect=[OSError("synthetic stop failure")] * 3 + [None])
    pages = (
        (expected[:64], expected[64:127], expected[127:], expected[:63])
        if stop_failure
        else (expected[:64], expected[64:128], expected[128:], expected[:64])
    )
    for index, page in enumerate(pages):
        runtime.reconcile_workspace.reset_mock()
        assert await scope.admission_check() is (not stop_failure or index == 3)
        checked = list(dict.fromkeys(([expected[0]] if stop_failure and index else []) + page))
        assert [call.args[0] for call in runtime.reconcile_workspace.await_args_list] == checked
        assert len(checked) <= 64
        assert scope._retained_cursor == page[-1]
    assert scope._stop.await_count == (4 if stop_failure else 0)


@pytest.mark.parametrize("change", ["owner", "association", "agent", "credential_owner"])
async def test_listed_row_is_rechecked_for_owner_and_binding_before_reconciliation(
    authority, tmp_path, monkeypatch, change
):
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    other = await retained(runtime, value, factory, "ws_other", other=True)
    listing = runtime.retained_workspaces

    async def changed(after="", **kwargs):
        rows = await listing(after, **kwargs)
        if "ws_cloud_stable" not in rows:
            return rows
        async with factory.begin() as session:
            row = await session.get(Workspace, "ws_cloud_stable")
            if change == "owner":
                row.node_id = "other/node"
            elif change == "agent":
                row.agent = "claude_code"
            else:
                policy = deepcopy(row.task_policy)
                association = policy["hosted_node"]
                if change == "association":
                    association["workspace_id"] = "ws_other"
                for binding in association["credentials"].values():
                    if change == "association":
                        binding["workspace_id"] = "ws_other"
                    else:
                        binding["identity"]["owner"] = "other/node"
                row.task_policy = policy
        return rows

    monkeypatch.setattr(runtime, "retained_workspaces", changed)
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock()
    assert await scope.admission_check()
    assert path.exists() is (change == "owner")
    assert other.is_dir()
    if change == "owner":
        scope.stop_workspace.assert_not_awaited()
    else:
        scope.stop_workspace.assert_awaited_once_with("ws_cloud_stable")


async def test_new_boot_sweeps_prelaunch_view_without_compose(authority, tmp_path):
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable", status="failed")
    async with factory.begin() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        row.compose_project_name = row.compose_file_path = None
    successor = await Receiver.start(runtime.receiver.config, factory)
    try:
        scope = WorkspaceConsumers(WorkspaceAuthority(successor), tmp_path)
        scope.stop_workspace = AsyncMock()
        assert not await scope.admission_check()
        assert not path.exists()
        scope.stop_workspace.assert_awaited_once_with("ws_cloud_stable")
    finally:
        await successor.close()


async def test_failed_stop_does_not_starve_later_owned_credential_cleanup(
    authority, tmp_path, monkeypatch
):
    runtime, value, factory = authority
    first = await retained(runtime, value, factory, "ws_cloud_stable")
    second = await retained(runtime, value, factory, "ws_other", other=True)
    await deny(runtime, value, "revoke", monkeypatch)
    association, _ = await runtime.association("ws_other")
    await runtime.receiver.revoke(
        association.credentials.agent.model_copy(
            update={"idempotency_key": "other-revoke", "fence": 2}
        )
    )
    scope = WorkspaceConsumers(runtime, tmp_path)

    async def stop(workspace_id):
        if workspace_id == "ws_cloud_stable":
            raise OSError("synthetic exact stop failure")

    scope.stop_workspace = AsyncMock(side_effect=stop)
    assert not await scope.admission_check()
    assert not first.exists() and not second.exists()
    assert [call.args[0] for call in scope.stop_workspace.await_args_list] == [
        "ws_cloud_stable",
        "ws_other",
    ]
    scope.stop_workspace.side_effect = None
    scope.stop_workspace.reset_mock()
    assert await scope.admission_check()
    assert [call.args[0] for call in scope.stop_workspace.await_args_list] == [
        "ws_cloud_stable",
        "ws_other",
    ]


@pytest.mark.parametrize("change", ["deleted", "rebound"])
@pytest.mark.parametrize("cleanup", ["stop", "retract_recovers", "retract_broken"])
async def test_failed_stop_does_not_block_after_row_leaves_owner(
    authority, tmp_path, monkeypatch, change, cleanup
):
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    other = await retained(runtime, value, factory, "ws_healthy", other=True)
    await deny(runtime, value, "revoke", monkeypatch)
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock(
        side_effect=OSError("synthetic stop failure") if cleanup == "stop" else None
    )
    remove = runtime.views.remove
    broken = cleanup != "stop"

    def retract(workspace_id, **kwargs):
        if broken and workspace_id == "ws_cloud_stable":
            raise OSError("synthetic retraction failure")
        return remove(workspace_id, **kwargs)

    monkeypatch.setattr(runtime.views, "remove", retract)
    assert not await scope.admission_check()
    assert path.exists() is broken
    async with factory.begin() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        if change == "deleted":
            await session.delete(row)
        else:
            row.node_id = "other/node"
    successor = RuntimeViews(
        runtime.receiver.store,
        identity=runtime.receiver.identity.model_copy(update={"owner": "other/node"}),
    ).publish(
        "ws_cloud_stable",
        {
            "openai.api-key": b"synthetic-successor-agent",
            "github.api-key": b"synthetic-successor-forge",
        },
    )
    scope.stop_workspace.reset_mock()
    broken = cleanup == "retract_broken"
    assert await scope.admission_check() is (not broken)
    scope.stop_workspace.assert_not_awaited()
    assert path.exists() is broken
    assert bool(scope._retained_stop_failures) is broken
    assert successor.path.is_dir() and other.is_dir()
    broken = False
    assert await scope.admission_check()
    scope.stop_workspace.assert_not_awaited()
    assert not path.exists()
    assert not scope._retained_stop_failures
    assert successor.path.is_dir() and other.is_dir()


async def test_pending_cleanup_retry_is_bounded_without_starving_current_rows(
    authority, tmp_path, monkeypatch
):
    runtime, value, factory = authority
    healthy = await retained(runtime, value, factory, "ws_healthy", other=True)
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock()
    scope._retained_stop_failures = {f"ws_deleted_{index:03}" for index in range(65)}
    reconcile = runtime.reconcile_workspace
    checked = []

    async def observe(workspace_id):
        checked.append(workspace_id)
        return await reconcile(workspace_id)

    monkeypatch.setattr(runtime, "reconcile_workspace", observe)
    for remaining in (65, 49, 33, 17, 1):
        checked.clear()
        assert len(scope._retained_stop_failures) == remaining
        assert await scope.admission_check() is (remaining <= 16)
        assert len(checked) == min(remaining, 16) + 1
        assert "ws_healthy" in checked and healthy.is_dir()
    assert not scope._retained_stop_failures
    scope.stop_workspace.assert_not_awaited()
