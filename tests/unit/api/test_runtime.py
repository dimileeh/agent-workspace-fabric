"""Workspace runtime API contract tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.api.app import configure_database, create_app
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.inspection import RuntimeSnapshot


class _RuntimeInspector:
    def __init__(self, snapshot: RuntimeSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[str | None] = []

    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
        self.calls.append(compose_project_name)
        return self.snapshot


@pytest.fixture
async def runtime_app_and_client(engine: AsyncEngine) -> AsyncIterator[tuple[object, AsyncClient]]:
    app = create_app(use_lifespan=False)
    configure_database(app, make_session_factory(engine))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield app, client


def _runtime_endpoint_profile() -> dict[str, object]:
    return {
        "name": "runtime-endpoints",
        "services": [{"name": "app", "image": "example/app:latest"}],
        "app_endpoints": [
            {
                "name": "app",
                "service": "app",
                "port": 3000,
                "path": "/",
                "health": {"path": "/healthz"},
                "visibility": "agent",
            }
        ],
    }


async def _running_workspace(
    engine: AsyncEngine,
    *,
    resolved_profile: dict[str, object] | None = None,
) -> str:
    factory = make_session_factory(engine)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/runtime-api.git",
            branch_base="main",
            task_title="runtime api",
            task_prompt="inspect runtime api",
            agent="codex",
            test_commands=[],
            resolved_profile=resolved_profile,
        )
        workspace.status = WorkspaceStatus.running.value
        workspace.compose_project_name = "awf_ws_runtime_api"
        workspace.compose_file_path = f"/tmp/{workspace.id}/compose.yml"
        await session.commit()
        return workspace.id


@pytest.mark.unit
async def test_runtime_endpoint_serializes_structured_runtime_health(
    runtime_app_and_client: tuple[object, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = runtime_app_and_client
    workspace_id = await _running_workspace(engine)
    inspector = _RuntimeInspector(RuntimeSnapshot(stack_state="stopped"))
    app.state.workspace_runtime_inspector = inspector

    response = await client.get(f"/v1/workspaces/{workspace_id}/runtime")

    assert response.status_code == 200
    body = response.json()
    assert body["workspace_id"] == workspace_id
    assert body["stack_state"] == "stopped"
    assert body["runtime_health"] == {
        "status": "stranded",
        "reason_code": "STRANDED_WORKSPACE",
        "decision": "fail_workspace",
        "message": "Workspace has compose metadata but no managed runtime containers were found.",
        "services": [],
    }
    assert inspector.calls == ["awf_ws_runtime_api"]


@pytest.mark.unit
async def test_runtime_endpoint_serializes_app_endpoint_metadata(
    runtime_app_and_client: tuple[object, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = runtime_app_and_client
    workspace_id = await _running_workspace(
        engine,
        resolved_profile=_runtime_endpoint_profile(),
    )
    app.state.workspace_runtime_inspector = _RuntimeInspector(
        RuntimeSnapshot(stack_state="running")
    )

    response = await client.get(f"/v1/workspaces/{workspace_id}/runtime")

    assert response.status_code == 200
    assert response.json()["app_endpoints"] == [
        {
            "name": "app",
            "service": "app",
            "scheme": "http",
            "port": 3000,
            "path": "/",
            "internal_url": "http://app:3000/",
            "visibility": "agent",
            "health": {
                "path": "/healthz",
                "method": "GET",
                "expected_status": 200,
                "internal_url": "http://app:3000/healthz",
            },
        }
    ]


@pytest.mark.unit
async def test_runtime_endpoint_missing_workspace_keeps_existing_404_shape(
    runtime_app_and_client: tuple[object, AsyncClient],
) -> None:
    _app, client = runtime_app_and_client

    response = await client.get("/v1/workspaces/ws_missing/runtime")

    assert response.status_code == 404
    assert response.json() == {
        "detail": {
            "error_code": "NOT_FOUND",
            "message": "No workspace with id ws_missing",
        }
    }
