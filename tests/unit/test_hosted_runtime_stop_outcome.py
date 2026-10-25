"""Confirmed hosted teardown must not leave a durable running workspace."""

import asyncio
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from awf.api.app import configure_database, create_app
from awf.common.commands import AsyncioSubprocessRunner, CommandResult
from awf.common.config import Settings, get_settings
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import TaskAttempt, Workspace, WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.receiver import Receiver
from awf.hosted_node.runtime_consumers import WorkspaceConsumers
from awf.node.compose_manager import ComposeManager, ComposeTeardownResult
from awf.service import provider_readiness, worker
from tests.conftest import _ok_workspace_admission_disk_check
from tests.unit.node.test_provisioner_parts.test_provisioner_part_001 import (
    origin_repo as origin_repo,
)
from tests.unit.service.test_worker import _settings
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_retained import deny_agent, retained


async def settings_for(factory, tmp_path):
    engine = factory.kw["bind"]
    async with engine.connect() as connection:
        schema = await connection.scalar(text("select current_schema()"))
    url = engine.url.update_query_dict({"awf_search_path": schema}).render_as_string(
        hide_password=False
    )
    return replace(
        _settings(tmp_path, database_url=url),
        local_capacity_cpu_cores=12,
        local_capacity_memory_gb=32,
        worker_max_concurrent_executions=2,
        terminal_workspace_gc_enabled=False,
        claude_base_gc_enabled=False,
    )


@pytest.mark.parametrize("failure", ["revoke", "expiry"])
async def test_actual_worker_authority_loss_persists_failed_without_replay(
    authority, origin_repo, tmp_path, monkeypatch, failure
):
    runtime, payload, factory = authority
    settings = await settings_for(factory, tmp_path)
    work = Path(settings.work_dir)
    scope = WorkspaceConsumers(runtime, work)
    scope.poll_seconds = 0.01
    payload["repo"]["base_branch"] = "development"
    payload["task"]["auto_merge"] = False
    payload["workspace"] = {"profile_ref": None, "profile": {"name": "hosted-synthetic"}}
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    agent_calls, docker_calls = [], []
    spawn = asyncio.create_subprocess_exec

    async def local_spawn(*args, **kwargs):
        if args[0] == "docker":
            docker_calls.append(args)
            return await spawn(sys.executable, "-c", "pass", **kwargs)
        args = tuple(str(origin_repo) if item == payload["repo"]["url"] else item for item in args)
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", local_spawn)
    monkeypatch.setattr(
        provider_readiness,
        "_run_subprocess",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, 0, "synthetic-cli", ""),
    )
    real = AsyncioSubprocessRunner()

    class Effects:
        async def run(self, args, **kwargs):
            if args[0] == "git":
                return await real.run(args, **kwargs)
            assert args[0] == "docker", "revoked work must never reach forge"
            return CommandResult(0, "[]" if "ps" in args or "inspect" in args else "", "")

        async def run_streaming(self, args, **kwargs):
            if args[0] == "git":
                return await real.run_streaming(args, **kwargs)
            assert args[:2] == ["docker", "compose"]
            if "codex" in args:
                assert scope.workspace_id.get() == "ws_cloud_stable"
                assert kwargs["env"]["CODEX_API_KEY"] == "synthetic-agent-key"
                assert kwargs["env"]["GH_TOKEN"] == "synthetic-github-key"
                assert view.is_dir()
                agent_calls.append("entered")
                entered.set()
                try:
                    await asyncio.wait_for(release.wait(), 20)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                pytest.fail("revoked external agent must not return")
            return CommandResult(0, "", "")

    monkeypatch.setattr(worker, "AsyncioSubprocessRunner", Effects)
    built = worker.build_worker_runtime(settings, hosted=scope)
    view = runtime.receiver.store.root / runtime.views._key("ws_cloud_stable") / "view"
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    app.state.hosted_receiver, app.state.hosted_runtime = runtime.receiver, runtime
    app.state.workspace_admission_disk_check = _ok_workspace_admission_disk_check
    api_settings = Settings(
        _env_file=None, api_token="synthetic-operator", hosted_node=runtime.receiver.config
    )
    app.dependency_overrides[get_settings] = lambda: api_settings
    factory.configure(info={**factory.kw.get("info", {}), "awf_hosted_runtime": runtime})
    headers = {
        "Authorization": "Bearer synthetic-controller",
        "Idempotency-Key": "revocation-workflow",
        **runtime.receiver.identity.headers(),
    }
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/workspaces", json=payload, headers=headers)
            assert response.status_code == 202, response.text
            async with asyncio.timeout(30):
                await built.worker.run_once()
                await entered.wait()
                async with factory() as session:
                    row = await session.get(Workspace, "ws_cloud_stable")
                    assert row.status == "running" and row.execution_claimed_by is not None
                await deny_agent(runtime, payload, failure, monkeypatch)
                await built.worker.wait_for_execution_tasks()
            assert cancelled.is_set() and not view.exists()
            assert any("down" in args for args in docker_calls)
            release.set()
            for _ in range(2):
                await built.worker.run_once()
                await built.worker.wait_for_execution_tasks()
            async with factory() as session:
                row = await session.get(Workspace, "ws_cloud_stable")
                assert row.status == "failed", (row.status, row.failure_reason)
                assert row.failure_reason == "infrastructure_failure"
                assert row.execution_claimed_by is None
                assert row.pr_url is None and row.pr_number is None
                attempt = await session.scalar(
                    select(TaskAttempt).where(TaskAttempt.workspace_id == row.id)
                )
                assert attempt.status == "failed"
                events = list(
                    await session.scalars(
                        select(WorkspaceEvent).where(
                            WorkspaceEvent.workspace_id == row.id,
                            WorkspaceEvent.reason_code == "HOSTED_RUNTIME_STOPPED",
                        )
                    )
                )
                assert len(events) == 1 and events[0].new_state == "failed"
            status = await client.get(
                "/v1/workspaces/ws_cloud_stable",
                headers={**headers, "Authorization": "Bearer synthetic-operator"},
            )
            assert status.status_code == 200 and status.json()["status"] == "failed"
            assert agent_calls == ["entered"]
    finally:
        for task in built.worker._execution_tasks.values():
            if not task.done():
                task.cancel()
        await built.worker.wait_for_execution_tasks()
        await built.engine.dispose()


@pytest.fixture
async def stop_worker(authority, tmp_path, monkeypatch):
    runtime, _, factory = authority
    settings = await settings_for(factory, tmp_path)
    scope = WorkspaceConsumers(runtime, Path(settings.work_dir))
    teardown = AsyncMock(
        return_value=ComposeTeardownResult(status="succeeded", reason_code="synthetic")
    )
    monkeypatch.setattr(ComposeManager, "teardown_project", teardown)
    built = worker.build_worker_runtime(settings, hosted=scope)
    try:
        yield scope, teardown
    finally:
        await built.engine.dispose()


@pytest.mark.parametrize("status", [status.value for status in WorkspaceStatus])
async def test_confirmed_stop_transitions_only_nonterminal_owned_rows(
    authority, stop_worker, status
):
    runtime, payload, factory = authority
    # Inactive rows are never republished; seed the live fixture before changing its status.
    await retained(runtime, payload, factory, "ws_cloud_stable", status="running")
    async with factory.begin() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        row.status = status
        row.execution_claimed_by, row.execution_claim_epoch = "owned-worker", 7
        row.monitor_claimed_by = "owned-monitor"
    scope, teardown = stop_worker
    await scope.stop_workspace("ws_cloud_stable")
    terminal = status in {"failed", "completed", "cancelled", "destroying", "destroyed"}
    async with factory() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        assert row.status == (status if terminal else "failed")
        assert row.failure_reason == (None if terminal else "infrastructure_failure")
        assert row.execution_claim_epoch == (7 if terminal else 8)
        assert row.execution_claimed_by == ("owned-worker" if terminal else None)
        assert row.monitor_claimed_by == ("owned-monitor" if terminal else None)
    teardown.assert_awaited_once()


@pytest.mark.parametrize(
    "change",
    ["node", "claim", "epoch", "monitor", "binding", "version", "status", "boot", "missing"],
)
async def test_stop_does_not_transition_a_successor_after_teardown(authority, stop_worker, change):
    runtime, payload, factory = authority
    await retained(runtime, payload, factory, "ws_cloud_stable", status="running")
    scope, teardown = stop_worker
    successor = None

    async def changed(**kwargs):
        nonlocal successor
        if change == "boot":
            successor = await Receiver.start(runtime.receiver.config, factory)
        else:
            async with factory.begin() as session:
                row = await session.get(Workspace, "ws_cloud_stable")
                if change == "missing":
                    await session.delete(row)
                elif change == "node":
                    row.node_id = "successor/node"
                elif change == "claim":
                    row.execution_claimed_by = "successor-worker"
                elif change == "epoch":
                    row.execution_claim_epoch += 1
                elif change == "monitor":
                    row.monitor_claimed_by = "successor-monitor"
                elif change == "binding":
                    policy = deepcopy(row.task_policy)
                    policy["hosted_node"]["credentials"]["agent"]["fence"] += 1
                    row.task_policy = policy
                elif change == "version":
                    row.version += 1
                else:
                    await WorkspaceRepository(session).transition(
                        row, to=WorkspaceStatus.cancelled, reason_code="OPERATOR_CANCELLED"
                    )
        return ComposeTeardownResult(status="succeeded", reason_code="synthetic")

    teardown.side_effect = changed
    try:
        if change == "boot":
            with pytest.raises(MaterialDeniedError):
                await scope.stop_workspace("ws_cloud_stable")
        else:
            await scope.stop_workspace("ws_cloud_stable")
        async with factory() as session:
            row = await session.get(Workspace, "ws_cloud_stable")
            if change == "missing":
                assert row is None
                return
            assert row.status == ("cancelled" if change == "status" else "running")
            assert row.failure_reason is None
    finally:
        if successor is not None:
            await successor.close()


@pytest.mark.parametrize("reason", ["AGENT_TIMEOUT", "AGENT_IDLE_TIMEOUT"])
async def test_stop_preserves_existing_primary_timeout_causality(authority, stop_worker, reason):
    runtime, payload, factory = authority
    await retained(runtime, payload, factory, "ws_cloud_stable", status="running")
    async with factory.begin() as session:
        repo = WorkspaceRepository(session)
        row = await repo.get("ws_cloud_stable")
        row.failure_reason = FailureReason.agent_failure.value
        row.failure_message = "Synthetic primary watchdog timeout"
        await repo.add_event(
            row,
            event_type="workspace.state_changed",
            reason_code=reason,
            payload={"primary_failure": {"reason_code": reason}},
        )
        row.events[-1].new_state = "failed"
    scope, _ = stop_worker
    await scope.stop_workspace("ws_cloud_stable")
    async with factory() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        assert row.status == "failed" and row.failure_reason == "agent_failure"
        assert row.failure_message == "Synthetic primary watchdog timeout"
        events = list(
            await session.scalars(
                select(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == row.id)
                .order_by(WorkspaceEvent.event_order)
            )
        )
        assert len(events) == 2 and events[-1].reason_code == reason
        assert events[-1].payload["secondary_failure"]["reason_code"] == "HOSTED_RUNTIME_STOPPED"


async def test_failed_teardown_never_claims_a_durable_stopped_outcome(authority, stop_worker):
    runtime, payload, factory = authority
    await retained(runtime, payload, factory, "ws_cloud_stable", status="running")
    scope, teardown = stop_worker
    teardown.return_value = ComposeTeardownResult(status="failed", reason_code="synthetic")
    with pytest.raises(MaterialDeniedError, match="HOSTED_RUNTIME_STOP_FAILED"):
        await scope.stop_workspace("ws_cloud_stable")
    async with factory() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        assert row.status == "running" and row.failure_reason is None
