from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from awf.db.enums import OperationStatus, OperationType
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_session_factory


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = make_session_factory(engine)
    async with factory() as s:
        yield s


@pytest.fixture
async def sample_data(session: AsyncSession):
    ws_repo = WorkspaceRepository(session)
    op_repo = OperationRepository(session)

    ws1 = await ws_repo.create(
        repo_url="https://github.com/org/repo1",
        branch_base="main",
        task_title="task1",
        task_prompt="prompt1",
        agent="claude-3-sonnet",
        test_commands=[],
    )
    ws2 = await ws_repo.create(
        repo_url="https://github.com/org/repo2",
        branch_base="main",
        task_title="task2",
        task_prompt="prompt2",
        agent="claude-3-sonnet",
        test_commands=[],
    )

    # op1: ws1, create, succeeded
    await op_repo.create(
        workspace_id=ws1.id, operation_type=OperationType.create, status=OperationStatus.succeeded
    )
    # op2: ws1, validate, running
    await op_repo.create(
        workspace_id=ws1.id, operation_type=OperationType.validate, status=OperationStatus.running
    )
    # op3: ws2, create, succeeded
    await op_repo.create(
        workspace_id=ws2.id, operation_type=OperationType.create, status=OperationStatus.succeeded
    )

    await session.commit()
    return ws1, ws2


@pytest.mark.unit
async def test_list_operations_global(client: AsyncClient, sample_data):
    ws1, ws2 = sample_data

    # Test global list
    response = await client.get("/v1/operations")
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 3
    assert body["next_cursor"] is None
    assert body["has_more"] is False

    # Test filter by workspace_id
    response = await client.get(f"/v1/operations?workspace_id={ws1.id}")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 2

    # Test filter by status
    response = await client.get("/v1/operations?status=running")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["status"] == "running"

    # Test filter by type
    response = await client.get("/v1/operations?type=validate")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["type"] == "validate"


@pytest.mark.unit
async def test_list_workspace_operations_filters(client: AsyncClient, sample_data):
    ws1, ws2 = sample_data

    # Test workspace-scoped list with status filter
    response = await client.get(f"/v1/workspaces/{ws1.id}/operations?status=succeeded")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["type"] == "create"

    # Test workspace-scoped list with type filter
    response = await client.get(f"/v1/workspaces/{ws1.id}/operations?type=validate")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["status"] == "running"


@pytest.mark.unit
async def test_list_workspace_operations_not_found(client: AsyncClient):
    response = await client.get("/v1/workspaces/ws_missing/operations")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "NOT_FOUND"


@pytest.mark.unit
async def test_list_operations_limit_validation(client: AsyncClient):
    response = await client.get("/v1/operations?limit=0")
    assert response.status_code == 422

    response = await client.get("/v1/operations?limit=501")
    assert response.status_code == 422


@pytest.mark.unit
async def test_list_operations_ordering(client: AsyncClient, session: AsyncSession):
    ws_repo = WorkspaceRepository(session)
    op_repo = OperationRepository(session)

    ws = await ws_repo.create(
        repo_url="https://github.com/org/repo_order",
        branch_base="main",
        task_title="task",
        task_prompt="prompt",
        agent="claude-3-sonnet",
        test_commands=[],
    )

    op1 = await op_repo.create(workspace_id=ws.id, operation_type=OperationType.create)
    op2 = await op_repo.create(workspace_id=ws.id, operation_type=OperationType.validate)
    op3 = await op_repo.create(workspace_id=ws.id, operation_type=OperationType.start)
    base_created_at = datetime(2026, 1, 1, tzinfo=UTC)
    op1.created_at = base_created_at
    op2.created_at = base_created_at + timedelta(seconds=1)
    op3.created_at = base_created_at + timedelta(seconds=2)

    await session.commit()

    response = await client.get(f"/v1/operations?workspace_id={ws.id}")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 3
    # Newest first
    assert items[0]["id"] == op3.id
    assert items[1]["id"] == op2.id
    assert items[2]["id"] == op1.id


@pytest.mark.unit
async def test_list_operations_empty(client: AsyncClient, session: AsyncSession):
    ws_repo = WorkspaceRepository(session)
    ws = await ws_repo.create(
        repo_url="https://github.com/org/repo_empty",
        branch_base="main",
        task_title="task",
        task_prompt="prompt",
        agent="claude-3-sonnet",
        test_commands=[],
    )
    await session.commit()

    # Empty global list
    response = await client.get("/v1/operations?type=validate")
    assert response.status_code == 200
    assert response.json()["items"] == []

    # Empty workspace list
    response = await client.get(f"/v1/workspaces/{ws.id}/operations")
    assert response.status_code == 200
    assert response.json()["items"] == []
