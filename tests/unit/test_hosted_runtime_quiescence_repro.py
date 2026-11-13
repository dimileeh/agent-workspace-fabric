"""A hosted workspace failed at the PR-monitor handoff must leave the node parkable."""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from awf.api.app import configure_database, create_app
from awf.common.commands import AsyncioSubprocessRunner, CommandResult
from awf.common.config import Settings, get_settings
from awf.db.enums import OperationStatus, OperationType
from awf.db.models import (
    CallbackDelivery,
    MergeCandidate,
    Operation,
    ResourceReservation,
    ServiceGCRequest,
    TaskAttempt,
    ValidationRun,
    Workspace,
    WorkspaceEvent,
    WorkspaceSecretLease,
)
from awf.db.repositories import OperationRepository
from awf.hosted_node.lifecycle_store import database_quiescent
from awf.hosted_node.runtime_consumers import WorkspaceConsumers
from awf.runtime.pr_monitor_runner import runner as monitor_runner
from awf.service import provider_readiness, worker
from tests.conftest import _ok_workspace_admission_disk_check
from tests.integration.runtime._pr_monitor_runner_fixtures import _pr_payload
from tests.unit.node.test_provisioner_parts.test_provisioner_part_001 import (
    origin_repo as origin_repo,
)
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_retained import deny_agent, retained
from tests.unit.test_hosted_runtime_stop_outcome import settings_for
from tests.unit.test_hosted_runtime_stop_outcome import stop_worker as stop_worker


async def leftovers(session) -> list[tuple[str, str]]:
    """Name every row that keeps the database non-quiescent, without ids."""
    found: list[tuple[str, str]] = []
    for model, live in (
        (TaskAttempt, TaskAttempt.status.not_in(("completed", "failed", "cancelled", "destroyed"))),
        (Operation, Operation.status.not_in(("succeeded", "failed", "cancelled"))),
        (ValidationRun, ValidationRun.status.not_in(("succeeded", "failed"))),
        (ResourceReservation, ResourceReservation.released_at.is_(None)),
        (ServiceGCRequest, ServiceGCRequest.status.not_in(("completed", "failed", "expired"))),
        (CallbackDelivery, CallbackDelivery.status.not_in(("succeeded", "failed", "skipped"))),
        (MergeCandidate, MergeCandidate.status.not_in(("merged", "closed"))),
        (WorkspaceSecretLease, WorkspaceSecretLease.status.not_in(("revoked", "released"))),
    ):
        for row in await session.scalars(select(model).where(live)):
            status = getattr(row, "status", "held")
            found.append((model.__tablename__, f"{getattr(row, 'type', '-')}:{status}"))
    return found


@pytest.mark.parametrize("failure", ["revoke", "expiry"])
async def test_monitoring_pr_credential_renewal_denied_leaves_node_quiescent(
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
    entered, release = asyncio.Event(), asyncio.Event()
    spawn = asyncio.create_subprocess_exec

    async def local_spawn(*args, **kwargs):
        if args[0] == "docker":
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
            if args[:3] == ["gh", "pr", "create"]:
                return CommandResult(0, "https://github.com/example/repo/pull/42\n", "")
            if args[:3] == ["gh", "api", "graphql"]:
                pending = _pr_payload(check_state="PENDING", merge_state_status="BLOCKED")
                return CommandResult(0, pending, "")
            return CommandResult(0, "[]" if "ps" in args or "inspect" in args else "", "")

        async def run_streaming(self, args, **kwargs):
            if args[0] == "git":
                return await real.run_streaming(args, **kwargs)
            if "codex" in args:
                (work / "git/worktrees/ws_cloud_stable/implemented.txt").write_text(
                    "synthetic implemented change\n"
                )
            return CommandResult(0, "", "")

    monkeypatch.setattr(worker, "AsyncioSubprocessRunner", Effects)
    original_init = monitor_runner.PullRequestMonitorRunner.__init__

    async def check_wait(_seconds):
        # The PR monitor parks in its operation-backed CI wait until authority is lost.
        entered.set()
        await release.wait()

    def with_held_sleep(self, **kwargs):
        original_init(self, **{**kwargs, "sleep": check_wait})

    monkeypatch.setattr(monitor_runner.PullRequestMonitorRunner, "__init__", with_held_sleep)
    built = worker.build_worker_runtime(settings, hosted=scope)
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
        "Idempotency-Key": "quiescence-repro",
        **runtime.receiver.identity.headers(),
    }
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/workspaces", json=payload, headers=headers)
            assert response.status_code == 202, response.text
            await built.worker.run_once()
            waiter = asyncio.create_task(entered.wait())
            try:
                tasks = list(built.worker._execution_tasks.values())
                await asyncio.wait([waiter, *tasks], return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            async with factory() as session:
                row = await session.get(Workspace, "ws_cloud_stable")
                assert entered.is_set(), (row.status, row.failure_reason, row.failure_message)
                assert row.status == "monitoring_pr"

            await deny_agent(runtime, payload, failure, monkeypatch)
            await built.worker.wait_for_execution_tasks()
            release.set()
            for _ in range(2):
                await built.worker.run_once()
                await built.worker.wait_for_execution_tasks()

        async with factory() as session:
            row = await session.get(Workspace, "ws_cloud_stable")
            assert row.status == "failed", (row.status, row.failure_reason)
            assert row.failure_reason == "infrastructure_failure"
            assert await session.scalar(
                select(WorkspaceEvent.id).where(
                    WorkspaceEvent.workspace_id == row.id,
                    WorkspaceEvent.reason_code == "HOSTED_RUNTIME_STOPPED",
                )
            )
            assert await leftovers(session) == []
            assert await database_quiescent(session)
    finally:
        release.set()
        for task in built.worker._execution_tasks.values():
            if not task.done():
                task.cancel()
        await built.worker.wait_for_execution_tasks()
        await built.engine.dispose()


async def test_hosted_stop_settles_monitor_operation_and_validation(authority, stop_worker):
    runtime, payload, factory = authority
    await retained(runtime, payload, factory, "ws_cloud_stable", status="monitoring_pr")
    async with factory.begin() as session:
        monitor = await OperationRepository(session).create(
            workspace_id="ws_cloud_stable",
            operation_type=OperationType.monitor_state,
            status=OperationStatus.running,
            payload={"owner": "pr_monitor", "source_head_sha": "a" * 40},
        )
        unrelated = await OperationRepository(session).create(
            workspace_id="ws_cloud_stable",
            operation_type=OperationType.validate,
            status=OperationStatus.succeeded,
        )
        session.add(
            ValidationRun(
                id="validation-hosted-stop",
                workspace_id="ws_cloud_stable",
                tier=1,
                command_set_hash="b" * 64,
                status="running",
            )
        )
    scope, _ = stop_worker
    await scope.stop_workspace("ws_cloud_stable")
    async with factory() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        assert row.status == "failed" and row.failure_reason == "infrastructure_failure"
        operation = await session.get(Operation, monitor.id)
        assert operation.status == "failed"
        assert operation.error_code == "HOSTED_RUNTIME_STOPPED"
        assert operation.finished_at is not None
        assert operation.payload["source_head_sha"] == "a" * 40
        assert (await session.get(Operation, unrelated.id)).status == "succeeded"
        validation = await session.get(ValidationRun, "validation-hosted-stop")
        assert validation.status == "failed"
        assert validation.reason_code == "HOSTED_RUNTIME_STOPPED"
        assert validation.finished_at is not None
        events = list(
            await session.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == row.id,
                    WorkspaceEvent.event_type == "workspace.hosted_stop_activity_settled",
                )
            )
        )
        assert len(events) == 1
        assert events[0].reason_code == "HOSTED_RUNTIME_STOPPED"
        assert events[0].payload == {
            "operation_ids": [monitor.id],
            "validation_run_ids": ["validation-hosted-stop"],
        }
        assert await leftovers(session) == []


async def test_hosted_stop_without_activity_writes_no_settlement_event(authority, stop_worker):
    runtime, payload, factory = authority
    await retained(runtime, payload, factory, "ws_cloud_stable", status="monitoring_pr")
    scope, _ = stop_worker
    await scope.stop_workspace("ws_cloud_stable")
    async with factory() as session:
        assert not await session.scalar(
            select(WorkspaceEvent.id).where(
                WorkspaceEvent.event_type == "workspace.hosted_stop_activity_settled"
            )
        )
