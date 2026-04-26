"""Workspace service observability helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceCreateV2Request
from awf.db.base import Base
from awf.db.enums import OperationStatus, OperationType
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from awf.service.workspaces import WorkspaceService


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.mark.unit
async def test_workspace_service_round_trips_policy_metadata(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    request = WorkspaceCreateV2Request(
        repo={"url": "git@github.com:example/service.git", "base_branch": "main"},
        task={
            "title": "Update dependency",
            "prompt": "Bump the dependency and adjust tests.",
            "agent": "codex",
            "kind": "feature_branch_pr",
            "task_class": "dependency_task",
            "owned_paths": ["pyproject.toml", "uv.lock"],
        },
        workspace={"profile_ref": "auto", "profile": None},
        validation={"commands": ["uv run pytest -q"], "requested_tier": 1},
        resources={},
    )

    created = await service.create_v2(request)
    fetched = await service.get(created.id)
    listed = await service.list(limit=10)

    assert created.task_class == "dependency_task"
    assert created.owned_paths == ["pyproject.toml", "uv.lock"]
    assert fetched is not None
    assert fetched.task_class == "dependency_task"
    assert fetched.owned_paths == ["pyproject.toml", "uv.lock"]
    assert listed[0].task_class == "dependency_task"
    assert listed[0].owned_paths == ["pyproject.toml", "uv.lock"]


@pytest.mark.unit
async def test_get_runtime_returns_snapshot_and_none_for_missing_workspace(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    class FakeRuntimeInspector:
        async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
            assert compose_project_name == "awf_ws_service_runtime"
            return RuntimeSnapshot(
                stack_state="running",
                services=[
                    RuntimeService(
                        name="agent",
                        container_id="abc123",
                        image="awf-agent-runtime:latest",
                        state="running",
                        status="Up 1 minute",
                        health="healthy",
                        ports=["127.0.0.1:8000->8000/tcp"],
                        started_at="2026-04-25T10:00:00Z",
                    )
                ],
            )

    service = WorkspaceService(factory, runtime_inspector=FakeRuntimeInspector())
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Observe runtime",
            task_prompt="Inspect runtime.",
            agent="codex",
            test_commands=[],
        )
        workspace.compose_project_name = "awf_ws_service_runtime"
        await session.commit()

    snapshot = await service.get_runtime(workspace.id)
    missing = await service.get_runtime("ws_missing")

    assert snapshot is not None
    assert snapshot.workspace_id == workspace.id
    assert snapshot.compose_project_name == "awf_ws_service_runtime"
    assert snapshot.stack_state == "running"
    assert snapshot.logs_available is True
    assert snapshot.control_available is True
    assert snapshot.reason is None
    assert len(snapshot.services) == 1
    assert snapshot.services[0].name == "agent"
    assert snapshot.services[0].health == "healthy"
    assert missing is None


@pytest.mark.unit
async def test_list_operations_respects_limit_and_none_for_missing_workspace(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    base = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/app.git",
            branch_base="main",
            task_title="Observe operations",
            task_prompt="List operations.",
            agent="codex",
            test_commands=[],
        )
        repo = OperationRepository(session)
        create = await repo.create(
            workspace_id=workspace.id,
            operation_type=OperationType.create,
            status=OperationStatus.succeeded,
        )
        validate = await repo.create(
            workspace_id=workspace.id,
            operation_type=OperationType.validate,
            status=OperationStatus.running,
        )
        stop = await repo.create(
            workspace_id=workspace.id,
            operation_type=OperationType.stop,
            status=OperationStatus.pending,
        )
        create.created_at = base
        validate.created_at = base + timedelta(seconds=1)
        stop.created_at = base + timedelta(seconds=2)
        await session.commit()

    rows = await service.list_operations(workspace.id, limit=2)
    missing = await service.list_operations("ws_missing")

    assert rows is not None
    assert [row.id for row in rows] == [stop.id, validate.id]
    assert [row.type for row in rows] == ["stop", "validate"]
    assert [row.status for row in rows] == ["pending", "running"]
    assert missing is None


@pytest.mark.unit
async def test_global_operation_helpers_filter_and_get(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    base = datetime(2026, 4, 25, 13, 0, tzinfo=UTC)
    async with factory() as session:
        ws_repo = WorkspaceRepository(session)
        first_workspace = await ws_repo.create(
            repo_url="git@github.com:example/first.git",
            branch_base="main",
            task_title="First operations",
            task_prompt="List first operations.",
            agent="codex",
            test_commands=[],
        )
        second_workspace = await ws_repo.create(
            repo_url="git@github.com:example/second.git",
            branch_base="main",
            task_title="Second operations",
            task_prompt="List second operations.",
            agent="codex",
            test_commands=[],
        )
        repo = OperationRepository(session)
        first_operation = await repo.create(
            workspace_id=first_workspace.id,
            operation_type=OperationType.create,
            status=OperationStatus.succeeded,
        )
        second_operation = await repo.create(
            workspace_id=second_workspace.id,
            operation_type=OperationType.validate,
            status=OperationStatus.running,
        )
        first_operation.created_at = base
        second_operation.created_at = base + timedelta(seconds=1)
        await session.commit()

    rows = await service.list_all_operations(status=OperationStatus.running)
    operation = await service.get_operation(first_operation.id)
    missing = await service.get_operation("op_missing")

    assert [row.id for row in rows] == [second_operation.id]
    assert rows[0].workspace_id == second_workspace.id
    assert operation is not None
    assert operation.id == first_operation.id
    assert operation.type == "create"
    assert missing is None
