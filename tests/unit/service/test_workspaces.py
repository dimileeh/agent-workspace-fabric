"""Workspace runtime health service tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

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
    reason_code: str = "STRANDED_WORKSPACE",
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
        workspace.status = WorkspaceStatus.failed.value
        workspace.failure_reason = "infrastructure_failure"
        await repo.add_event(
            workspace,
            event_type=RUNTIME_STRANDED_EVENT_TYPE,
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
