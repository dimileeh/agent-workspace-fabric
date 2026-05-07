"""Workspace runtime health service tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from awf.service.workspace_runtime_health import RUNTIME_STRANDED_EVENT_TYPE
from awf.service.workspaces import WorkspaceService

PRESERVED_EXECUTION_EVENT_TYPE = "workspace.active_execution_preserved_after_restart"
PRESERVED_EXECUTION_REASON_CODE = "ACTIVE_EXECUTION_PRESERVED_AFTER_RESTART"
REFRESH_REQUESTED_EVENT_TYPE = "workspace.refresh_requested"
REFRESH_REQUESTED_REASON_CODE = "OPERATOR_REFRESH"


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


class _RuntimeInspector:
    def __init__(self, snapshots: dict[str | None, RuntimeSnapshot]) -> None:
        self.snapshots = snapshots
        self.calls: list[str | None] = []

    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
        self.calls.append(compose_project_name)
        return self.snapshots[compose_project_name]


async def _workspace(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: WorkspaceStatus,
    compose_project_name: str | None = "awf_ws_runtime",
    pr_url: str | None = None,
) -> str:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/runtime.git",
            branch_base="main",
            task_title=f"{status.value} runtime",
            task_prompt="inspect runtime",
            agent="codex",
            test_commands=[],
        )
        workspace.status = status.value
        workspace.compose_project_name = compose_project_name
        if compose_project_name is not None:
            workspace.compose_file_path = f"/tmp/{workspace.id}/compose.yml"
        workspace.pr_url = pr_url
        workspace.updated_at = datetime.now(UTC)
        await session.commit()
        return workspace.id


async def _workspace_with_runtime_health_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    payload: dict[str, object],
    event_type: str = RUNTIME_STRANDED_EVENT_TYPE,
    reason_code: str = "STRANDED_WORKSPACE",
    status: WorkspaceStatus = WorkspaceStatus.failed,
) -> str:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/runtime.git",
            branch_base="main",
            task_title="persisted runtime edge",
            task_prompt="show persisted runtime finding edge",
            agent="codex",
            test_commands=[],
        )
        workspace.status = status.value
        if status == WorkspaceStatus.failed:
            workspace.failure_reason = "infrastructure_failure"
        await repo.add_event(
            workspace,
            event_type=event_type,
            reason_code=reason_code,
            payload=payload,
        )
        await session.commit()
        return workspace.id


@pytest.mark.unit
async def test_runtime_health_flags_missing_compose_project_as_stranded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.running,
        compose_project_name="awf_ws_runtime",
    )
    inspector = _RuntimeInspector({"awf_ws_runtime": RuntimeSnapshot(stack_state="stopped")})

    runtime = await WorkspaceService(
        session_factory,
        runtime_inspector=inspector,
    ).get_runtime(workspace_id)

    assert runtime is not None
    assert runtime.runtime_health is not None
    assert runtime.runtime_health.status == "stranded"
    assert runtime.runtime_health.reason_code == "STRANDED_WORKSPACE"
    assert runtime.runtime_health.decision == "fail_workspace"


@pytest.mark.unit
async def test_runtime_health_flags_missing_agent_container(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _workspace(session_factory, status=WorkspaceStatus.running)
    inspector = _RuntimeInspector(
        {
            "awf_ws_runtime": RuntimeSnapshot(
                stack_state="running",
                services=[
                    RuntimeService(
                        name="postgres",
                        container_id="pg",
                        image="postgres:16",
                        state="running",
                    )
                ],
            )
        }
    )

    runtime = await WorkspaceService(
        session_factory,
        runtime_inspector=inspector,
    ).get_runtime(workspace_id)

    assert runtime is not None
    assert runtime.runtime_health is not None
    assert runtime.runtime_health.reason_code == "AGENT_CONTAINER_MISSING"
    assert runtime.runtime_health.decision == "fail_workspace"
    assert runtime.runtime_health.services == [
        {"name": "postgres", "state": "running", "container_id": "pg"}
    ]


@pytest.mark.unit
async def test_runtime_health_flags_exited_agent_container(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _workspace(session_factory, status=WorkspaceStatus.running)
    inspector = _RuntimeInspector(
        {
            "awf_ws_runtime": RuntimeSnapshot(
                stack_state="stopped",
                services=[
                    RuntimeService(
                        name="agent",
                        container_id="agent",
                        image="awf-agent:latest",
                        state="exited",
                        status="Exited (1) 2 minutes ago",
                    )
                ],
            )
        }
    )

    runtime = await WorkspaceService(
        session_factory,
        runtime_inspector=inspector,
    ).get_runtime(workspace_id)

    assert runtime is not None
    assert runtime.runtime_health is not None
    assert runtime.runtime_health.reason_code == "AGENT_CONTAINER_EXITED"
    assert runtime.runtime_health.decision == "fail_workspace"


@pytest.mark.unit
async def test_monitoring_pr_with_pr_url_is_recoverable_instead_of_failed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.monitoring_pr,
        pr_url="https://github.com/example/runtime/pull/42",
    )
    inspector = _RuntimeInspector({"awf_ws_runtime": RuntimeSnapshot(stack_state="stopped")})

    runtime = await WorkspaceService(
        session_factory,
        runtime_inspector=inspector,
    ).get_runtime(workspace_id)

    assert runtime is not None
    assert runtime.runtime_health is not None
    assert runtime.runtime_health.reason_code == "STRANDED_WORKSPACE"
    assert runtime.runtime_health.decision == "remonitor_workspace"


@pytest.mark.unit
@pytest.mark.parametrize(
    "status",
    [WorkspaceStatus.running, WorkspaceStatus.monitoring_pr],
)
async def test_pre_pr_or_missing_pr_url_runtime_health_fails_workspace(
    status: WorkspaceStatus,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _workspace(
        session_factory,
        status=status,
        pr_url=None,
    )
    inspector = _RuntimeInspector({"awf_ws_runtime": RuntimeSnapshot(stack_state="stopped")})

    runtime = await WorkspaceService(
        session_factory,
        runtime_inspector=inspector,
    ).get_runtime(workspace_id)

    assert runtime is not None
    assert runtime.runtime_health is not None
    assert runtime.runtime_health.reason_code == "STRANDED_WORKSPACE"
    assert runtime.runtime_health.decision == "fail_workspace"


@pytest.mark.unit
async def test_requested_without_compose_metadata_is_not_stranded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        compose_project_name=None,
    )
    inspector = _RuntimeInspector(
        {None: RuntimeSnapshot(stack_state="unknown", reason="workspace has no compose project")}
    )

    runtime = await WorkspaceService(
        session_factory,
        runtime_inspector=inspector,
    ).get_runtime(workspace_id)

    assert runtime is not None
    assert runtime.runtime_health is None


@pytest.mark.unit
async def test_workspace_detail_exposes_persisted_runtime_health(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/runtime.git",
            branch_base="main",
            task_title="persisted runtime",
            task_prompt="show persisted runtime finding",
            agent="codex",
            test_commands=[],
        )
        workspace.status = WorkspaceStatus.failed.value
        workspace.failure_reason = "infrastructure_failure"
        await repo.add_event(
            workspace,
            event_type=RUNTIME_STRANDED_EVENT_TYPE,
            reason_code="AGENT_CONTAINER_EXITED",
            payload={
                "reason_code": "AGENT_CONTAINER_EXITED",
                "decision": "fail_workspace",
                "message": "Workspace agent container is not running.",
                "runtime": {
                    "services": [
                        {
                            "name": "agent",
                            "state": "exited",
                            "container_id": "agent",
                        }
                    ]
                },
            },
        )
        await session.commit()
        workspace_id = workspace.id

    detail = await WorkspaceService(session_factory).get(workspace_id)

    assert detail is not None
    assert detail.runtime_health is not None
    assert detail.runtime_health.reason_code == "AGENT_CONTAINER_EXITED"
    assert detail.runtime_health.decision == "fail_workspace"
    assert detail.runtime_health.services == [
        {"name": "agent", "state": "exited", "container_id": "agent"}
    ]


@pytest.mark.unit
async def test_workspace_detail_exposes_persisted_preserved_runtime_health(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/runtime.git",
            branch_base="main",
            task_title="persisted preserved runtime",
            task_prompt="show preserved runtime finding",
            agent="codex",
            test_commands=[],
        )
        workspace.status = WorkspaceStatus.running.value
        await repo.add_event(
            workspace,
            event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            reason_code=PRESERVED_EXECUTION_REASON_CODE,
            payload={
                "reason_code": PRESERVED_EXECUTION_REASON_CODE,
                "decision": "preserve_runtime",
                "workspace_status": WorkspaceStatus.running.value,
                "message": "Live agent runtime was preserved after worker restart.",
                "runtime": {
                    "services": [
                        {
                            "name": "agent",
                            "state": "running",
                            "container_id": "agent",
                        }
                    ]
                },
            },
        )
        await session.commit()
        workspace_id = workspace.id

    detail = await WorkspaceService(session_factory).get(workspace_id)

    assert detail is not None
    assert detail.runtime_health is not None
    assert detail.runtime_health.status == "ok"
    assert detail.runtime_health.reason_code == PRESERVED_EXECUTION_REASON_CODE
    assert detail.runtime_health.decision == "preserve_runtime"
    assert detail.runtime_health.services == [
        {"name": "agent", "state": "running", "container_id": "agent"}
    ]


@pytest.mark.unit
async def test_workspace_detail_ignores_preserved_health_from_prior_active_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _workspace_with_runtime_health_event(
        session_factory,
        status=WorkspaceStatus.validating,
        event_type=PRESERVED_EXECUTION_EVENT_TYPE,
        reason_code=PRESERVED_EXECUTION_REASON_CODE,
        payload={
            "reason_code": PRESERVED_EXECUTION_REASON_CODE,
            "decision": "preserve_runtime",
            "workspace_status": WorkspaceStatus.running.value,
            "message": "Live agent runtime was preserved after worker restart.",
            "runtime": {
                "services": [
                    {
                        "name": "agent",
                        "state": "running",
                        "container_id": "agent",
                    }
                ]
            },
        },
    )

    detail = await WorkspaceService(session_factory).get(workspace_id)

    assert detail is not None
    assert detail.runtime_health is None


@pytest.mark.unit
async def test_workspace_detail_falls_back_to_stranded_health_after_mismatched_preserved_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/runtime.git",
            branch_base="main",
            task_title="persisted mismatched preserved runtime",
            task_prompt="show earlier stranded runtime finding",
            agent="codex",
            test_commands=[],
        )
        workspace.status = WorkspaceStatus.validating.value
        base = datetime.now(UTC)
        stranded = await repo.add_event(
            workspace,
            event_type=RUNTIME_STRANDED_EVENT_TYPE,
            reason_code="AGENT_CONTAINER_EXITED",
            payload={
                "reason_code": "AGENT_CONTAINER_EXITED",
                "decision": "fail_workspace",
                "message": "Workspace agent container is not running.",
            },
        )
        stranded.occurred_at = base
        preserved = await repo.add_event(
            workspace,
            event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            reason_code=PRESERVED_EXECUTION_REASON_CODE,
            payload={
                "reason_code": PRESERVED_EXECUTION_REASON_CODE,
                "decision": "preserve_runtime",
                "workspace_status": WorkspaceStatus.running.value,
                "message": "Live agent runtime was preserved after worker restart.",
            },
        )
        preserved.occurred_at = base + timedelta(seconds=1)
        await session.commit()
        workspace_id = workspace.id

    detail = await WorkspaceService(session_factory).get(workspace_id)

    assert detail is not None
    assert detail.runtime_health is not None
    assert detail.runtime_health.status == "stranded"
    assert detail.runtime_health.reason_code == "AGENT_CONTAINER_EXITED"
    assert detail.runtime_health.decision == "fail_workspace"


@pytest.mark.unit
async def test_preserved_runtime_health_is_scoped_to_current_status_cycle(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/runtime.git",
            branch_base="main",
            task_title="preserved runtime cycle floor",
            task_prompt="do not show previous-cycle preserved runtime",
            agent="codex",
            test_commands=[],
        )
        workspace.status = WorkspaceStatus.running.value
        workspace.compose_project_name = "awf_preserved_runtime_cycle_floor"
        workspace.compose_file_path = f"/tmp/{workspace.id}/compose.yml"
        base = datetime.now(UTC)
        prior_running = await repo.add_event(
            workspace,
            event_type="workspace.state_changed",
            reason_code="WORKSPACE_RUNNING",
        )
        prior_running.old_state = WorkspaceStatus.ready.value
        prior_running.new_state = WorkspaceStatus.running.value
        prior_running.occurred_at = base
        preserved = await repo.add_event(
            workspace,
            event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            reason_code=PRESERVED_EXECUTION_REASON_CODE,
            payload={
                "reason_code": PRESERVED_EXECUTION_REASON_CODE,
                "decision": "preserve_runtime",
                "workspace_status": WorkspaceStatus.running.value,
                "message": "Live agent runtime was preserved after worker restart.",
                "runtime": {
                    "services": [
                        {
                            "name": "agent",
                            "state": "running",
                            "container_id": "agent-old",
                        }
                    ]
                },
            },
        )
        preserved.occurred_at = base + timedelta(seconds=1)
        completed = await repo.add_event(
            workspace,
            event_type="workspace.state_changed",
            reason_code="WORKSPACE_COMPLETED",
        )
        completed.old_state = WorkspaceStatus.running.value
        completed.new_state = WorkspaceStatus.completed.value
        completed.occurred_at = base + timedelta(seconds=2)
        current_running = await repo.add_event(
            workspace,
            event_type="workspace.state_changed",
            reason_code="WORKSPACE_RUNNING",
        )
        current_running.old_state = WorkspaceStatus.ready.value
        current_running.new_state = WorkspaceStatus.running.value
        current_running.occurred_at = base + timedelta(seconds=3)
        await session.commit()
        workspace_id = workspace.id

    detail = await WorkspaceService(session_factory).get(workspace_id)
    inspector = _RuntimeInspector(
        {
            "awf_preserved_runtime_cycle_floor": RuntimeSnapshot(
                stack_state="running",
                services=[
                    RuntimeService(
                        name="agent",
                        container_id="agent-current",
                        image="awf-agent:latest",
                        state="running",
                    )
                ],
            )
        }
    )
    runtime = await WorkspaceService(
        session_factory,
        runtime_inspector=inspector,
    ).get_runtime(workspace_id)

    assert detail is not None
    assert detail.runtime_health is None
    assert runtime is not None
    assert runtime.runtime_health is None


@pytest.mark.unit
async def test_preserved_runtime_health_is_floored_by_operator_refresh(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/runtime.git",
            branch_base="main",
            task_title="preserved runtime refresh floor",
            task_prompt="do not show preservation superseded by refresh",
            agent="codex",
            test_commands=[],
        )
        workspace.status = WorkspaceStatus.pushing.value
        workspace.compose_project_name = "awf_preserved_runtime_refresh_floor"
        workspace.compose_file_path = f"/tmp/{workspace.id}/compose.yml"
        base = datetime.now(UTC)
        preserved = await repo.add_event(
            workspace,
            event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            reason_code=PRESERVED_EXECUTION_REASON_CODE,
            payload={
                "reason_code": PRESERVED_EXECUTION_REASON_CODE,
                "decision": "preserve_runtime",
                "workspace_status": WorkspaceStatus.pushing.value,
                "message": "Live agent runtime was preserved after worker restart.",
                "runtime": {
                    "services": [
                        {
                            "name": "agent",
                            "state": "running",
                            "container_id": "agent-preserved",
                        }
                    ]
                },
            },
        )
        preserved.occurred_at = base
        refresh = await repo.add_event(
            workspace,
            event_type=REFRESH_REQUESTED_EVENT_TYPE,
            reason_code=REFRESH_REQUESTED_REASON_CODE,
        )
        refresh.occurred_at = base + timedelta(seconds=1)
        refresh.new_state = None
        await session.commit()
        workspace_id = workspace.id

    inspector = _RuntimeInspector(
        {
            "awf_preserved_runtime_refresh_floor": RuntimeSnapshot(
                stack_state="running",
                services=[
                    RuntimeService(
                        name="agent",
                        container_id="agent-current",
                        image="awf-agent:latest",
                        state="running",
                    )
                ],
            )
        }
    )

    detail = await WorkspaceService(session_factory).get(workspace_id)
    runtime = await WorkspaceService(
        session_factory,
        runtime_inspector=inspector,
    ).get_runtime(workspace_id)

    assert detail is not None
    assert detail.runtime_health is None
    assert runtime is not None
    assert runtime.runtime_health is None


@pytest.mark.unit
async def test_preserved_runtime_health_is_floored_by_past_execution_claim_expiry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/runtime.git",
            branch_base="main",
            task_title="preserved runtime claim floor",
            task_prompt="do not show preservation from replaced execution claim",
            agent="codex",
            test_commands=[],
        )
        workspace.status = WorkspaceStatus.running.value
        workspace.compose_project_name = "awf_preserved_runtime_claim_floor"
        workspace.compose_file_path = f"/tmp/{workspace.id}/compose.yml"
        base = datetime.now(UTC) - timedelta(minutes=10)
        preserved = await repo.add_event(
            workspace,
            event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            reason_code=PRESERVED_EXECUTION_REASON_CODE,
            payload={
                "reason_code": PRESERVED_EXECUTION_REASON_CODE,
                "decision": "preserve_runtime",
                "workspace_status": WorkspaceStatus.running.value,
                "message": "Live agent runtime was preserved after worker restart.",
                "runtime": {
                    "services": [
                        {
                            "name": "agent",
                            "state": "running",
                            "container_id": "agent-preserved",
                        }
                    ]
                },
            },
        )
        preserved.occurred_at = base
        workspace.execution_claim_expires_at = base + timedelta(seconds=1)
        await session.commit()
        workspace_id = workspace.id

    inspector = _RuntimeInspector(
        {
            "awf_preserved_runtime_claim_floor": RuntimeSnapshot(
                stack_state="running",
                services=[
                    RuntimeService(
                        name="agent",
                        container_id="agent-current",
                        image="awf-agent:latest",
                        state="running",
                    )
                ],
            )
        }
    )

    detail = await WorkspaceService(session_factory).get(workspace_id)
    runtime = await WorkspaceService(
        session_factory,
        runtime_inspector=inspector,
    ).get_runtime(workspace_id)

    assert detail is not None
    assert detail.runtime_health is None
    assert runtime is not None
    assert runtime.runtime_health is None


@pytest.mark.unit
async def test_preserved_runtime_health_keeps_current_event_with_unexpired_execution_claim(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/runtime.git",
            branch_base="main",
            task_title="preserved runtime fresh claim",
            task_prompt="show preservation owned by active execution claim",
            agent="codex",
            test_commands=[],
        )
        workspace.status = WorkspaceStatus.validating.value
        base = datetime.now(UTC)
        preserved = await repo.add_event(
            workspace,
            event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            reason_code=PRESERVED_EXECUTION_REASON_CODE,
            payload={
                "reason_code": PRESERVED_EXECUTION_REASON_CODE,
                "decision": "preserve_runtime",
                "workspace_status": WorkspaceStatus.validating.value,
                "message": "Live agent runtime was preserved after worker restart.",
            },
        )
        preserved.occurred_at = base
        workspace.execution_claim_expires_at = base + timedelta(minutes=5)
        await session.commit()
        workspace_id = workspace.id

    detail = await WorkspaceService(session_factory).get(workspace_id)

    assert detail is not None
    assert detail.runtime_health is not None
    assert detail.runtime_health.status == "ok"
    assert detail.runtime_health.reason_code == PRESERVED_EXECUTION_REASON_CODE
    assert detail.runtime_health.decision == "preserve_runtime"


@pytest.mark.unit
async def test_preserved_runtime_health_ignores_operator_refresh_from_prior_status(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/runtime.git",
            branch_base="main",
            task_title="preserved runtime status-scoped refresh",
            task_prompt="do not let another status refresh hide preservation",
            agent="codex",
            test_commands=[],
        )
        workspace.status = WorkspaceStatus.ready.value
        base = datetime.now(UTC)
        refresh = await repo.add_event(
            workspace,
            event_type=REFRESH_REQUESTED_EVENT_TYPE,
            reason_code=REFRESH_REQUESTED_REASON_CODE,
        )
        refresh.occurred_at = base - timedelta(seconds=1)
        await repo.transition(
            workspace,
            to=WorkspaceStatus.running,
            reason_code="WORKSPACE_RUNNING",
        )
        running = workspace.events[-1]
        running.occurred_at = base
        preserved = await repo.add_event(
            workspace,
            event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            reason_code=PRESERVED_EXECUTION_REASON_CODE,
            payload={
                "reason_code": PRESERVED_EXECUTION_REASON_CODE,
                "decision": "preserve_runtime",
                "workspace_status": WorkspaceStatus.running.value,
                "message": "Live agent runtime was preserved after worker restart.",
            },
        )
        preserved.occurred_at = base + timedelta(seconds=1)
        await session.commit()
        workspace_id = workspace.id

    detail = await WorkspaceService(session_factory).get(workspace_id)

    assert detail is not None
    assert detail.runtime_health is not None
    assert detail.runtime_health.status == "ok"
    assert detail.runtime_health.reason_code == PRESERVED_EXECUTION_REASON_CODE
    assert detail.runtime_health.decision == "preserve_runtime"


@pytest.mark.unit
async def test_workspace_detail_ignores_persisted_preserved_runtime_health_after_cancel(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _workspace_with_runtime_health_event(
        session_factory,
        status=WorkspaceStatus.cancelled,
        event_type=PRESERVED_EXECUTION_EVENT_TYPE,
        reason_code=PRESERVED_EXECUTION_REASON_CODE,
        payload={
            "reason_code": PRESERVED_EXECUTION_REASON_CODE,
            "decision": "preserve_runtime",
            "workspace_status": WorkspaceStatus.running.value,
            "message": "Live agent runtime was preserved after worker restart.",
            "runtime": {
                "services": [
                    {
                        "name": "agent",
                        "state": "running",
                        "container_id": "agent",
                    }
                ]
            },
        },
    )

    detail = await WorkspaceService(session_factory).get(workspace_id)

    assert detail is not None
    assert detail.runtime_health is None


@pytest.mark.unit
async def test_runtime_detail_uses_preserved_health_when_live_snapshot_is_healthy(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/runtime.git",
            branch_base="main",
            task_title="preserved runtime endpoint",
            task_prompt="show preserved runtime in runtime endpoint",
            agent="codex",
            test_commands=[],
        )
        workspace.status = WorkspaceStatus.pushing.value
        workspace.compose_project_name = "awf_preserved_runtime_endpoint"
        workspace.compose_file_path = f"/tmp/{workspace.id}/compose.yml"
        await repo.add_event(
            workspace,
            event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            reason_code=PRESERVED_EXECUTION_REASON_CODE,
            payload={
                "reason_code": PRESERVED_EXECUTION_REASON_CODE,
                "decision": "preserve_runtime",
                "workspace_status": WorkspaceStatus.pushing.value,
                "message": "Live agent runtime was preserved after worker restart.",
                "runtime": {
                    "services": [
                        {
                            "name": "agent",
                            "state": "running",
                            "container_id": "agent",
                        }
                    ]
                },
            },
        )
        await session.commit()
        workspace_id = workspace.id

    inspector = _RuntimeInspector(
        {
            "awf_preserved_runtime_endpoint": RuntimeSnapshot(
                stack_state="running",
                services=[
                    RuntimeService(
                        name="agent",
                        container_id="agent",
                        image="awf-agent:latest",
                        state="running",
                    )
                ],
            )
        }
    )

    runtime = await WorkspaceService(
        session_factory,
        runtime_inspector=inspector,
    ).get_runtime(workspace_id)

    assert runtime is not None
    assert runtime.runtime_health is not None
    assert runtime.runtime_health.status == "ok"
    assert runtime.runtime_health.reason_code == PRESERVED_EXECUTION_REASON_CODE
    assert runtime.runtime_health.decision == "preserve_runtime"


@pytest.mark.unit
async def test_runtime_detail_ignores_stale_stranded_health_when_live_snapshot_is_healthy(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/runtime.git",
            branch_base="main",
            task_title="recovered runtime endpoint",
            task_prompt="do not show stale stranded findings",
            agent="codex",
            test_commands=[],
        )
        workspace.status = WorkspaceStatus.running.value
        workspace.compose_project_name = "awf_recovered_runtime_endpoint"
        workspace.compose_file_path = f"/tmp/{workspace.id}/compose.yml"
        await repo.add_event(
            workspace,
            event_type=RUNTIME_STRANDED_EVENT_TYPE,
            reason_code="AGENT_CONTAINER_EXITED",
            payload={
                "reason_code": "AGENT_CONTAINER_EXITED",
                "decision": "fail_workspace",
                "message": "Workspace agent container is not running.",
                "runtime": {
                    "services": [
                        {
                            "name": "agent",
                            "state": "exited",
                            "container_id": "agent-old",
                        }
                    ]
                },
            },
        )
        await session.commit()
        workspace_id = workspace.id

    inspector = _RuntimeInspector(
        {
            "awf_recovered_runtime_endpoint": RuntimeSnapshot(
                stack_state="running",
                services=[
                    RuntimeService(
                        name="agent",
                        container_id="agent-running",
                        image="awf-agent:latest",
                        state="running",
                    )
                ],
            )
        }
    )

    runtime = await WorkspaceService(
        session_factory,
        runtime_inspector=inspector,
    ).get_runtime(workspace_id)

    assert runtime is not None
    assert runtime.runtime_health is None


@pytest.mark.unit
async def test_runtime_detail_ignores_preserved_health_for_stopped_terminal_workspace(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.cancelled,
        compose_project_name="awf_cancelled_preserved_runtime",
    )
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.add_event(
            workspace,
            event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            reason_code=PRESERVED_EXECUTION_REASON_CODE,
            payload={
                "reason_code": PRESERVED_EXECUTION_REASON_CODE,
                "decision": "preserve_runtime",
                "workspace_status": WorkspaceStatus.running.value,
                "message": "Live agent runtime was preserved after worker restart.",
                "runtime": {
                    "services": [
                        {
                            "name": "agent",
                            "state": "running",
                            "container_id": "agent",
                        }
                    ]
                },
            },
        )
        await session.commit()

    inspector = _RuntimeInspector(
        {"awf_cancelled_preserved_runtime": RuntimeSnapshot(stack_state="stopped")}
    )

    runtime = await WorkspaceService(
        session_factory,
        runtime_inspector=inspector,
    ).get_runtime(workspace_id)

    assert runtime is not None
    assert runtime.stack_state == "stopped"
    assert runtime.runtime_health is None


@pytest.mark.unit
async def test_workspace_detail_ignores_persisted_runtime_health_without_decision(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _workspace_with_runtime_health_event(
        session_factory,
        payload={"reason_code": "STRANDED_WORKSPACE"},
    )

    detail = await WorkspaceService(session_factory).get(workspace_id)

    assert detail is not None
    assert detail.runtime_health is None


@pytest.mark.unit
async def test_workspace_detail_defaults_runtime_health_message_and_ignores_bad_services(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    no_runtime_id = await _workspace_with_runtime_health_event(
        session_factory,
        payload={
            "reason_code": "STRANDED_WORKSPACE",
            "decision": "fail_workspace",
            "runtime": "not parsed",
        },
    )
    bad_services_id = await _workspace_with_runtime_health_event(
        session_factory,
        payload={
            "reason_code": "AGENT_CONTAINER_MISSING",
            "decision": "fail_workspace",
            "message": "Workspace runtime is present but the agent container is missing.",
            "runtime": {"services": "not a list"},
        },
        reason_code="AGENT_CONTAINER_MISSING",
    )

    no_runtime = await WorkspaceService(session_factory).get(no_runtime_id)
    bad_services = await WorkspaceService(session_factory).get(bad_services_id)

    assert no_runtime is not None
    assert no_runtime.runtime_health is not None
    assert no_runtime.runtime_health.message == "STRANDED_WORKSPACE"
    assert no_runtime.runtime_health.services == []
    assert bad_services is not None
    assert bad_services.runtime_health is not None
    assert bad_services.runtime_health.reason_code == "AGENT_CONTAINER_MISSING"
    assert bad_services.runtime_health.services == []
