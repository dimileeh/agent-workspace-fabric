"""Retained stacks keep authority enforcement outside an active consumer scope."""

import base64
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from awf.api.schemas import WorkspaceCreateRequest
from awf.db.models import Workspace
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.receiver import Receiver
from awf.hosted_node.runtime_authority import WorkspaceAuthority
from awf.hosted_node.runtime_consumers import WorkspaceConsumers
from awf.hosted_node.schemas import Binding, Install, Preparation
from tests.unit.test_hosted_runtime_authority import authority as authority


async def retained(runtime, value, factory, workspace_id, *, status="blocked", other=False):
    payload = deepcopy(value)
    payload["hosted"]["workspace_id"] = workspace_id
    for role, binding in payload["hosted"]["credentials"].items():
        binding["workspace_id"] = workspace_id
        if other:
            for field in ("idempotency_key", "preparation_id", "grant_id"):
                binding[field] = f"{role}-{workspace_id}"
            binding["expires_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
            await runtime.receiver.prepare(
                Preparation.model_validate({key: binding[key] for key in Preparation.model_fields})
            )
            await runtime.receiver.install(
                Install.model_validate(
                    {
                        **binding,
                        "material": base64.b64encode(f"synthetic-other-{role}".encode()).decode(),
                    }
                )
            )
    association = WorkspaceCreateRequest.model_validate(payload).hosted
    async with factory.begin() as session:
        session.add(
            Workspace(
                id=workspace_id,
                status=status,
                repo_url="https://github.com/example/repo.git",
                branch_base="main",
                task_title="Synthetic",
                task_prompt="Synthetic",
                agent="codex",
                node_id=runtime.receiver.identity.owner,
                compose_project_name=f"awf_{workspace_id}",
                compose_file_path=f"/work/compose/{workspace_id}/compose.yml",
                task_policy={"hosted_node": association.model_dump(mode="json")},
            )
        )
    await runtime.workspace(workspace_id)
    return runtime.receiver.store.root / runtime.views._key(workspace_id) / "view"


async def deny_agent(runtime, value, failure, monkeypatch):
    if failure == "revoke":
        binding = value["hosted"]["credentials"]["agent"]
        await runtime.receiver.revoke(
            Binding.model_validate({**binding, "idempotency_key": "revoked", "fence": 2})
        )
    else:
        from awf.hosted_node import runtime_authority

        now = datetime.now(UTC) + timedelta(minutes=11)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(now.timestamp(), tz=tz)

        monkeypatch.setattr(runtime_authority, "datetime", Clock)


@pytest.mark.parametrize("failure", ["revoke", "expiry"])
@pytest.mark.parametrize("restart", [False, True])
async def test_retained_scope_exit_is_reconciled_after_revocation_or_expiry(
    authority, tmp_path, monkeypatch, failure, restart
):
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    other = await retained(runtime, value, factory, "ws_other", other=True)
    scope = WorkspaceConsumers(runtime, tmp_path)
    async with scope.scope("ws_cloud_stable"):
        pass  # A normal blocked/paused exit intentionally keeps the warm stack.
    assert path.is_dir()
    await deny_agent(runtime, value, failure, monkeypatch)
    if restart:
        scope = WorkspaceConsumers(WorkspaceAuthority(runtime.receiver), tmp_path)
    scope.stop_workspace = AsyncMock()
    assert await scope.admission_check()
    scope.stop_workspace.assert_awaited_once_with("ws_cloud_stable")
    assert not path.exists()
    assert other.is_dir()
    await runtime.workspace("ws_other")


async def test_initial_resume_denial_stops_retained_stack_and_retracts_view(
    authority, tmp_path, monkeypatch
):
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock()
    await deny_agent(runtime, value, "revoke", monkeypatch)
    with pytest.raises(MaterialDeniedError):
        async with scope.scope("ws_cloud_stable"):
            pytest.fail("revoked resume must not execute")
    scope.stop_workspace.assert_awaited_once_with("ws_cloud_stable")
    assert not path.exists()


async def test_failed_stop_still_retracts_credentials_and_retries_on_next_poll(
    authority, tmp_path, monkeypatch
):
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock(side_effect=OSError("synthetic stop failure"))
    await deny_agent(runtime, value, "revoke", monkeypatch)
    assert not await scope.admission_check()
    assert not path.exists()
    scope.stop_workspace.side_effect = None
    assert await scope.admission_check()
    assert scope.stop_workspace.await_count == 2


async def test_retained_scan_wraps_and_rechecks_previously_authorized_workspace(
    authority, tmp_path, monkeypatch
):
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    other = await retained(runtime, value, factory, "ws_other", other=True)
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock()
    assert await scope.admission_check()
    scope.stop_workspace.assert_not_awaited()
    await deny_agent(runtime, value, "revoke", monkeypatch)
    assert await scope.admission_check()
    scope.stop_workspace.assert_awaited_once_with("ws_cloud_stable")
    assert not path.exists() and other.is_dir()


async def test_new_boot_reconciliation_removes_only_recorded_previous_boot_view(
    authority, tmp_path
):
    runtime, value, factory = authority
    old_path = await retained(runtime, value, factory, "ws_cloud_stable")
    successor = await Receiver.start(runtime.receiver.config, factory)
    try:
        current = WorkspaceAuthority(successor)
        scope = WorkspaceConsumers(current, tmp_path)
        scope.stop_workspace = AsyncMock()
        # Boot acceptance stays closed, but stale material/retained execution
        # must still be reconciled by the actual new owner's ordinary poll.
        assert not await scope.admission_check()
        scope.stop_workspace.assert_awaited_once_with("ws_cloud_stable")
        assert not old_path.exists()
    finally:
        await successor.close()


async def test_obsolete_process_cannot_stop_successor_or_remove_its_view(authority, tmp_path):
    runtime, value, factory = authority
    old_path = await retained(runtime, value, factory, "ws_cloud_stable")
    successor = await Receiver.start(runtime.receiver.config, factory)
    try:
        current = WorkspaceAuthority(successor)
        new_path = current.views.publish(
            "ws_cloud_stable", {"openai.api-key": b"new-agent", "github.api-key": b"new-github"}
        ).path
        scope = WorkspaceConsumers(runtime, tmp_path)
        stopped = []

        async def stop(workspace_id):
            await runtime.receiver.identity_status()
            stopped.append(workspace_id)

        scope.stop_workspace = stop
        assert not await scope.admission_check()
        assert not stopped and new_path.is_dir()
        assert not old_path.exists()
    finally:
        await successor.close()


@pytest.mark.parametrize("stop_succeeds", [False, True])
async def test_actual_worker_poll_hook_stops_exact_owned_stack_and_keeps_volumes(
    authority, tmp_path, monkeypatch, stop_succeeds
):
    from awf.node.compose_manager import ComposeManager, ComposeTeardownResult
    from awf.service.worker import build_worker_runtime
    from tests.unit.service.test_worker import _settings

    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    engine = factory.kw["bind"]
    async with engine.connect() as connection:
        schema = await connection.scalar(text("select current_schema()"))
    url = engine.url.update_query_dict({"awf_search_path": schema}).render_as_string(
        hide_password=False
    )
    settings = _settings(tmp_path, database_url=url)
    scope = WorkspaceConsumers(runtime, Path(settings.work_dir))
    teardown = AsyncMock(
        return_value=ComposeTeardownResult(
            status="succeeded" if stop_succeeds else "failed", reason_code="synthetic"
        )
    )
    monkeypatch.setattr(ComposeManager, "teardown_project", teardown)
    worker = build_worker_runtime(settings, hosted=scope)
    try:
        await deny_agent(runtime, value, "revoke", monkeypatch)
        assert await worker.worker._admission_check() is stop_succeeds
        teardown.assert_awaited_once_with(
            project_name="awf_ws_cloud_stable",
            compose_file=Path(settings.work_dir) / "compose/ws_cloud_stable/compose.yml",
            workspace_id="ws_cloud_stable",
            remove_volumes=False,
        )
        assert not path.exists()
        teardown.reset_mock()
        async with factory.begin() as session:
            workspace = await session.get(Workspace, "ws_cloud_stable")
            workspace.node_id = "other-owner/node"
        await scope.stop_workspace("ws_cloud_stable")
        teardown.assert_not_awaited()
    finally:
        await worker.engine.dispose()
