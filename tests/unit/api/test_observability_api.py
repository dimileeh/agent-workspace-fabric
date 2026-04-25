"""Console-ready observability and workspace-control API tests."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.testclient import WebSocketDenialResponse

import awf.api.routes.controls as controls_route
import awf.api.routes.runtime as runtime_route
import awf.api.routes.ws as ws_route
from awf.api.app import configure_database, create_app
from awf.common.config import get_settings
from awf.db.base import Base
from awf.db.enums import OperationStatus, OperationType
from awf.db.repositories import (
    OperationRepository,
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from awf.db.session import make_engine, make_session_factory
from awf.runtime.events import WorkspaceEventFrame
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from awf.runtime.logs import LOG_BROADCASTER

_BODY = {
    "repo_url": "git@github.com:example/console.git",
    "branch_base": "main",
    "task_title": "Add console data",
    "task_prompt": "Expose useful workspace observability.",
    "task_external_id": "TICKET-123",
    "agent": "codex",
    "test_commands": ["pytest -q"],
}


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _create_workspace(client: AsyncClient, **overrides: object) -> str:
    response = await client.post("/v1/workspaces", json={**_BODY, **overrides})
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


async def _mutate_workspace(engine: AsyncEngine, workspace_id: str) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        workspace.branch_name = "codex/observe"
        workspace.compose_project_name = "awf_ws_observe"
        workspace.pr_url = "https://github.com/example/console/pull/1"
        await repo.add_event(
            workspace,
            event_type="workspace.phase_started",
            reason_code="RUNNING_TEST",
            payload={"phase": "validation"},
        )
        await OperationRepository(session).create(
            workspace_id=workspace_id,
            operation_type=OperationType.validate,
            status=OperationStatus.running,
        )
        await session.commit()


def _auth(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("AWF_API_TOKEN", "secret")
    get_settings.cache_clear()
    return {"Authorization": "Bearer secret"}


class TestConsoleViews:
    @pytest.mark.unit
    async def test_task_list_maps_workspace_rows(self, client: AsyncClient) -> None:
        workspace_id = await _create_workspace(client)

        response = await client.get("/v1/tasks")

        assert response.status_code == 200
        body = response.json()
        assert body["next_cursor"] is None
        assert body["has_more"] is False
        assert body["items"][0]["task_id"] == "TICKET-123"
        assert body["items"][0]["workspace_id"] == workspace_id
        assert body["items"][0]["repo_url"] == _BODY["repo_url"]
        assert body["items"][0]["agent"] == "codex"

    @pytest.mark.unit
    async def test_workspace_overview_exposes_last_event_and_active_operation(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        workspace_id = await _create_workspace(client)
        await _mutate_workspace(engine, workspace_id)

        response = await client.get("/v1/workspaces/overview")

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["workspace_id"] == workspace_id
        assert item["task_id"] == "TICKET-123"
        assert item["branch_name"] == "codex/observe"
        assert item["pr_url"] == "https://github.com/example/console/pull/1"
        assert item["current_phase"] == "requested"
        assert item["active_operation"] == "validate"
        assert item["last_event"]["event_type"] == "workspace.phase_started"

    @pytest.mark.unit
    async def test_runtime_endpoint_returns_mocked_container_snapshot(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_id = await _create_workspace(client)
        await _mutate_workspace(engine, workspace_id)

        class FakeRuntimeInspector:
            async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
                assert compose_project_name == "awf_ws_observe"
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

        monkeypatch.setattr(runtime_route, "RuntimeInspector", FakeRuntimeInspector)

        response = await client.get(f"/v1/workspaces/{workspace_id}/runtime")

        assert response.status_code == 200
        body = response.json()
        assert body["stack_state"] == "running"
        assert body["compose_project_name"] == "awf_ws_observe"
        assert body["services"][0]["name"] == "agent"
        assert body["services"][0]["health"] == "healthy"


class TestLogs:
    @pytest.mark.unit
    async def test_log_endpoints_require_token_when_configured(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_id = await _create_workspace(client)
        _auth(monkeypatch)

        response = await client.get(f"/v1/workspaces/{workspace_id}/logs")

        assert response.status_code == 401
        assert response.json()["detail"]["error_code"] == "UNAUTHORIZED"

    @pytest.mark.unit
    async def test_list_and_read_log_streams_with_offsets(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace_id = await _create_workspace(client)
        log_path = tmp_path / "agent.log"
        log_path.write_text("alpha\nbeta\n", encoding="utf-8")
        factory = make_session_factory(engine)
        async with factory() as session:
            repo = WorkspaceLogStreamRepository(session)
            await repo.create_or_get(
                workspace_id=workspace_id,
                stream_id="agent.stdout",
                source="agent",
                name="Agent stdout",
                kind="stdout",
                path=str(log_path),
            )
            await repo.append_metadata(
                workspace_id=workspace_id,
                stream_id="agent.stdout",
                byte_delta=log_path.stat().st_size,
                line_delta=2,
            )
            await session.commit()

        headers = _auth(monkeypatch)

        listed = await client.get(f"/v1/workspaces/{workspace_id}/logs", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["items"][0]["stream_id"] == "agent.stdout"
        assert listed.json()["items"][0]["byte_count"] == len("alpha\nbeta\n")

        read = await client.get(
            f"/v1/workspaces/{workspace_id}/logs/agent.stdout",
            params={"offset": 6, "limit_bytes": 4},
            headers=headers,
        )
        assert read.status_code == 200
        assert read.json() == {
            "stream_id": "agent.stdout",
            "offset": 6,
            "next_offset": 10,
            "eof": False,
            "data": "beta",
        }

    @pytest.mark.unit
    async def test_missing_log_stream_returns_404(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_id = await _create_workspace(client)
        headers = _auth(monkeypatch)

        response = await client.get(
            f"/v1/workspaces/{workspace_id}/logs/nope",
            headers=headers,
        )

        assert response.status_code == 404
        assert response.json()["detail"]["error_code"] == "NOT_FOUND"


class TestWorkspaceWebSocket:
    @pytest.mark.unit
    def test_websocket_requires_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace_id, client, engine = _make_sync_test_client(monkeypatch, tmp_path)
        try:
            with (
                pytest.raises(WebSocketDenialResponse) as exc_info,
                client.websocket_connect(f"/v1/workspaces/{workspace_id}/ws"),
            ):
                pass
            assert exc_info.value.status_code == 401
            assert exc_info.value.json()["detail"]["error_code"] == "UNAUTHORIZED"
        finally:
            asyncio.run(engine.dispose())

    @pytest.mark.unit
    def test_websocket_reports_missing_token_configuration(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace_id, client, engine = _make_sync_test_client(
            monkeypatch,
            tmp_path,
            api_token=None,
        )
        try:
            with (
                pytest.raises(WebSocketDenialResponse) as exc_info,
                client.websocket_connect(f"/v1/workspaces/{workspace_id}/ws"),
            ):
                pass
            assert exc_info.value.status_code == 503
            assert (
                exc_info.value.json()["detail"]["error_code"] == "API_TOKEN_NOT_CONFIGURED"
            )
        finally:
            asyncio.run(engine.dispose())

    @pytest.mark.unit
    def test_websocket_sends_snapshot_events_and_log_tail(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace_id, client, engine = _make_sync_test_client(monkeypatch, tmp_path)
        try:
            with client.websocket_connect(
                f"/v1/workspaces/{workspace_id}/ws?channels=events,agent&tail_bytes=20",
                headers={"Authorization": "Bearer secret"},
            ) as websocket:
                snapshot = websocket.receive_json()
                event = websocket.receive_json()
                tail = websocket.receive_json()
        finally:
            asyncio.run(engine.dispose())

        assert snapshot["type"] == "snapshot"
        assert snapshot["workspace"]["id"] == workspace_id
        assert event["type"] == "event"
        assert event["event"]["event_type"] == "workspace.created"
        assert tail["type"] == "log"
        assert tail["stream_id"] == "agent.stdout"
        assert tail["data"] == "hello websocket\n"

    @pytest.mark.unit
    async def test_live_events_are_not_starved_by_steady_logs(self) -> None:
        workspace_id = "ws_live_events"
        event_queue: asyncio.Queue[WorkspaceEventFrame] = asyncio.Queue()
        websocket = _RecordingWebSocket()
        stream_task = asyncio.create_task(
            ws_route._stream_live_frames(
                websocket,
                workspace_id=workspace_id,
                selected={"events", "agent"},
                seen_event_ids=set(),
                event_queue=event_queue,
                heartbeat_interval=60,
            )
        )

        async def publish_logs_until_event() -> None:
            seq = 0
            while not websocket.event_sent.is_set():
                seq += 1
                await LOG_BROADCASTER.publish(
                    workspace_id=workspace_id,
                    stream_id="agent.stdout",
                    source="agent",
                    fd="stdout",
                    offset=seq,
                    data=f"log {seq}\n",
                )
                await asyncio.sleep(0)

        log_task = asyncio.create_task(publish_logs_until_event())
        await event_queue.put(
            WorkspaceEventFrame(
                id="evt_live",
                workspace_id=workspace_id,
                event_type="workspace.state_changed",
                old_state="requested",
                new_state="running",
                reason_code="TEST",
                payload=None,
                occurred_at=datetime.now(UTC),
            )
        )

        try:
            await asyncio.wait_for(websocket.event_sent.wait(), timeout=1)
        finally:
            log_task.cancel()
            stream_task.cancel()
            with suppress(asyncio.CancelledError):
                await log_task
            with suppress(asyncio.CancelledError):
                await stream_task

        assert websocket.events == ["workspace.state_changed"]


class TestOperationsAndControls:
    @pytest.mark.unit
    async def test_stop_records_operation_and_cancels_active_workspace(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_id = await _create_workspace(client)
        await _mutate_workspace(engine, workspace_id)
        stopped: list[str | None] = []

        async def fake_stop(compose_project_name: str | None) -> None:
            stopped.append(compose_project_name)

        monkeypatch.setattr(controls_route, "_stop_project", fake_stop)
        headers = _auth(monkeypatch)

        response = await client.post(
            f"/v1/workspaces/{workspace_id}/stop",
            json={"reason": "operator requested", "stop_stack": True},
            headers=headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "cancelled"
        assert stopped == ["awf_ws_observe"]

        operation = await client.get(f"/v1/operations/{body['operation_id']}")
        assert operation.status_code == 200
        assert operation.json()["type"] == "stop"
        assert operation.json()["status"] == "succeeded"

        operations = await client.get(f"/v1/workspaces/{workspace_id}/operations")
        assert operations.status_code == 200
        assert [item["type"] for item in operations.json()["items"]] == ["stop", "validate"]

    @pytest.mark.unit
    async def test_destroy_rejects_active_workspace_without_force(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_id = await _create_workspace(client)
        headers = _auth(monkeypatch)

        response = await client.delete(f"/v1/workspaces/{workspace_id}", headers=headers)

        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "WORKSPACE_ACTIVE"

    @pytest.mark.unit
    async def test_force_destroy_runs_cleanup_and_marks_destroyed(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_id = await _create_workspace(client)
        calls: list[dict[str, object]] = []

        class FakeCleaner:
            async def cleanup(
                self,
                *,
                workspace_id: str,
                repo_url: str,
                worktree_host_path: Path | None = None,
            ) -> list[str]:
                calls.append(
                    {
                        "workspace_id": workspace_id,
                        "repo_url": repo_url,
                        "worktree_host_path": worktree_host_path,
                    }
                )
                return []

        monkeypatch.setattr(controls_route, "_cleaner", FakeCleaner)
        headers = _auth(monkeypatch)

        response = await client.delete(
            f"/v1/workspaces/{workspace_id}",
            params={"force": True},
            headers=headers,
        )

        assert response.status_code == 200
        assert response.json()["status"] == "destroyed"
        assert calls == [
            {
                "workspace_id": workspace_id,
                "repo_url": _BODY["repo_url"],
                "worktree_host_path": None,
            }
        ]


def _make_sync_test_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    api_token: str | None = "secret",
) -> tuple[str, TestClient, AsyncEngine]:
    if api_token is None:
        monkeypatch.delenv("AWF_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("AWF_API_TOKEN", api_token)
    get_settings.cache_clear()
    db_path = tmp_path / "ws.db"
    engine = make_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = make_session_factory(engine)
    log_path = tmp_path / "agent.log"
    log_path.write_text("hello websocket\n", encoding="utf-8")

    async def seed() -> str:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url=str(_BODY["repo_url"]),
                branch_base=str(_BODY["branch_base"]),
                task_title=str(_BODY["task_title"]),
                task_prompt=str(_BODY["task_prompt"]),
                task_external_id=str(_BODY["task_external_id"]),
                agent=str(_BODY["agent"]),
                test_commands=[],
            )
            repo = WorkspaceLogStreamRepository(session)
            await repo.create_or_get(
                workspace_id=workspace.id,
                stream_id="agent.stdout",
                source="agent",
                name="Agent stdout",
                kind="stdout",
                path=str(log_path),
            )
            await repo.append_metadata(
                workspace_id=workspace.id,
                stream_id="agent.stdout",
                byte_delta=log_path.stat().st_size,
                line_delta=1,
            )
            await session.commit()
            return workspace.id

    workspace_id = asyncio.run(seed())
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    return workspace_id, TestClient(app), engine


class _RecordingWebSocket:
    def __init__(self) -> None:
        self.event_sent = asyncio.Event()
        self.events: list[str] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        if payload.get("type") != "event":
            return
        event = payload["event"]
        assert isinstance(event, dict)
        event_type = event["event_type"]
        assert isinstance(event_type, str)
        self.events.append(event_type)
        self.event_sent.set()
