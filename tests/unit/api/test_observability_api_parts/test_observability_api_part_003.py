"""Workspace websocket streaming API tests (relocated from part 001)."""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

import awf.api.routes.ws as ws_route
from awf.api.app import configure_database, create_app
from awf.common.config import get_settings
from awf.db.repositories import (
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.events import WorkspaceEventFrame
from awf.runtime.logs import LOG_BROADCASTER
from tests.postgres import create_postgres_test_engine

_BODY = {
    "repo_url": "git@github.com:example/console.git",
    "branch_base": "main",
    "task_title": "Add console data",
    "task_prompt": "Expose useful workspace observability.",
    "task_external_id": "TICKET-123",
    "agent": "codex",
    "test_commands": ["pytest -q"],
    "preflight": {
        "provider_readiness_override": True,
        "provider_readiness_override_reason": "observability API fixture",
    },
}
_REPOSITORY_BODY = {key: value for key, value in _BODY.items() if key != "preflight"}


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _auth(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("AWF_API_TOKEN", "secret")
    get_settings.cache_clear()
    return {"Authorization": "Bearer secret"}


async def _create_workspace(client: AsyncClient, **overrides: object) -> str:
    response = await client.post("/v1/workspaces", json={**_BODY, **overrides})
    assert response.status_code == 202
    return str(response.json()["workspace_id"])


def _make_sync_test_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    api_token: str | None = "secret",
) -> tuple[str, TestClient, AsyncEngine]:
    if api_token is None:
        monkeypatch.setenv("AWF_API_TOKEN", "")
    else:
        monkeypatch.setenv("AWF_API_TOKEN", api_token)
    get_settings.cache_clear()
    engine = asyncio.run(create_postgres_test_engine())
    factory = make_session_factory(engine)
    log_path = tmp_path / "agent.log"
    log_path.write_text("hello websocket\n", encoding="utf-8")

    async def seed() -> str:
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


def _make_empty_sync_test_client(tmp_path: Path) -> tuple[TestClient, AsyncEngine]:
    del tmp_path
    engine = asyncio.run(create_postgres_test_engine())
    factory = make_session_factory(engine)
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    return TestClient(app), engine


async def _seed_websocket_workspace(engine: AsyncEngine, tmp_path: Path) -> str:
    factory = make_session_factory(engine)
    log_path = tmp_path / "agent.log"
    log_path.write_text("hello websocket\n", encoding="utf-8")
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


@contextmanager
def _temporary_api_token(token: str | None) -> object:
    previous = os.environ.get("AWF_API_TOKEN")
    try:
        if token is None:
            os.environ["AWF_API_TOKEN"] = ""
        else:
            os.environ["AWF_API_TOKEN"] = token
        get_settings.cache_clear()
        yield
    finally:
        if previous is None:
            os.environ.pop("AWF_API_TOKEN", None)
        else:
            os.environ["AWF_API_TOKEN"] = previous
        get_settings.cache_clear()


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


class _FrameRecordingWebSocket:
    def __init__(self) -> None:
        self.frames: list[dict[str, object]] = []
        self.closed_codes: list[int] = []
        self._frame_event = asyncio.Event()

    async def send_json(self, payload: dict[str, object]) -> None:
        self.frames.append(payload)
        self._frame_event.set()

    async def close(self, *, code: int) -> None:
        self.closed_codes.append(code)

    async def wait_for_type(self, frame_type: str) -> None:
        while not any(frame.get("type") == frame_type for frame in self.frames):
            self._frame_event.clear()
            await self._frame_event.wait()

    async def wait_for_source(self, source: str) -> None:
        while not any(frame.get("source") == source for frame in self.frames):
            self._frame_event.clear()
            await self._frame_event.wait()


class TestWorkspaceWebSocket:
    @pytest.mark.unit
    def test_websocket_closes_when_database_factory_is_missing(self) -> None:
        app = create_app(use_lifespan=False)

        with _temporary_api_token("secret"):
            client = TestClient(app)
            with (
                client,
                pytest.raises(WebSocketDisconnect) as exc_info,
                client.websocket_connect(
                    "/v1/workspaces/ws_missing/ws",
                    headers={"Authorization": "Bearer secret"},
                ),
            ):
                pass

        assert exc_info.value.code == 1011

    @pytest.mark.unit
    def test_websocket_sends_error_frame_for_missing_workspace(self, tmp_path: Path) -> None:
        with _temporary_api_token("secret"):
            client, engine = _make_empty_sync_test_client(tmp_path)
            try:
                with (
                    client,
                    client.websocket_connect(
                        "/v1/workspaces/ws_missing/ws",
                        headers={"Authorization": "Bearer secret"},
                    ) as websocket,
                ):
                    assert websocket.receive_json() == {
                        "type": "error",
                        "error_code": "NOT_FOUND",
                        "message": "workspace not found",
                    }
                    with pytest.raises(WebSocketDisconnect) as exc_info:
                        websocket.receive_json()
            finally:
                asyncio.run(engine.dispose())

        assert exc_info.value.code == 1008

    @pytest.mark.unit
    def test_websocket_no_events_path_sends_error_frame_for_missing_workspace(
        self,
        tmp_path: Path,
    ) -> None:
        with _temporary_api_token("secret"):
            client, engine = _make_empty_sync_test_client(tmp_path)
            try:
                with (
                    client,
                    client.websocket_connect(
                        "/v1/workspaces/ws_missing/ws?channels=agent",
                        headers={"Authorization": "Bearer secret"},
                    ) as websocket,
                ):
                    assert websocket.receive_json() == {
                        "type": "error",
                        "error_code": "NOT_FOUND",
                        "message": "workspace not found",
                    }
                    with pytest.raises(WebSocketDisconnect) as exc_info:
                        websocket.receive_json()
            finally:
                asyncio.run(engine.dispose())

        assert exc_info.value.code == 1008

    @pytest.mark.unit
    def test_websocket_requires_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace_id, client, engine = _make_sync_test_client(monkeypatch, tmp_path)
        try:
            with (
                client,
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
                client,
                pytest.raises(WebSocketDenialResponse) as exc_info,
                client.websocket_connect(f"/v1/workspaces/{workspace_id}/ws"),
            ):
                pass
            assert exc_info.value.status_code == 503
            assert exc_info.value.json()["detail"]["error_code"] == "API_TOKEN_NOT_CONFIGURED"
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
            with (
                client,
                client.websocket_connect(
                    f"/v1/workspaces/{workspace_id}/ws?channels=events,agent&tail_bytes=20",
                    headers={"Authorization": "Bearer secret"},
                ) as websocket,
            ):
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
    def test_websocket_public_route_tails_logs_without_events_channel(
        self,
        tmp_path: Path,
    ) -> None:
        with _temporary_api_token("secret"):
            client, engine = _make_empty_sync_test_client(tmp_path)
            try:
                workspace_id = asyncio.run(_seed_websocket_workspace(engine, tmp_path))
                with (
                    client,
                    client.websocket_connect(
                        f"/v1/workspaces/{workspace_id}/ws?channels=agent&tail_bytes=100",
                        headers={"Authorization": "Bearer secret"},
                    ) as websocket,
                ):
                    snapshot = websocket.receive_json()
                    tail = websocket.receive_json()
            finally:
                asyncio.run(engine.dispose())

        assert snapshot["type"] == "snapshot"
        assert snapshot["workspace"]["id"] == workspace_id
        assert tail["type"] == "log"
        assert tail["source"] == "agent"
        assert tail["data"] == "hello websocket\n"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("channels", "expected_selected", "expects_event_queue"),
        [
            ("events", {"events"}, True),
            ("agent", {"agent"}, False),
        ],
    )
    async def test_workspace_socket_handles_disconnect_from_live_stream(
        self,
        channels: str,
        expected_selected: set[str],
        expects_event_queue: bool,
    ) -> None:
        class DisconnectingWebSocket:
            def __init__(self) -> None:
                self.app = type(
                    "App",
                    (),
                    {"state": type("State", (), {"db_session_factory": object()})()},
                )()
                self.accepted = False

            async def accept(self) -> None:
                self.accepted = True

        async def fake_send_initial_state(
            websocket: object,
            factory: object,
            workspace_id: str,
            selected: set[str],
            seen_event_ids: set[str],
            tail_bytes: int,
        ) -> bool:
            assert workspace_id == "ws_disconnect"
            assert selected == expected_selected
            assert seen_event_ids == set()
            assert tail_bytes == 65_536
            assert factory is not None
            assert websocket is fake_websocket
            return True

        async def fake_stream_live_frames(websocket: object, **_kwargs: object) -> None:
            assert websocket is fake_websocket
            assert (_kwargs["event_queue"] is not None) is expects_event_queue
            raise WebSocketDisconnect(code=1000)

        fake_websocket = DisconnectingWebSocket()
        original_send_initial_state = ws_route._send_initial_state
        original_stream_live_frames = ws_route._stream_live_frames
        try:
            ws_route._send_initial_state = fake_send_initial_state  # type: ignore[assignment]
            ws_route._stream_live_frames = fake_stream_live_frames  # type: ignore[assignment]
            await ws_route.workspace_socket(
                fake_websocket,  # type: ignore[arg-type]
                "ws_disconnect",
                channels=channels,
            )
        finally:
            ws_route._send_initial_state = original_send_initial_state
            ws_route._stream_live_frames = original_stream_live_frames

        assert fake_websocket.accepted is True

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("requested_tail_bytes", "expected_tail_bytes"),
        [
            (ws_route.MAX_TAIL_BYTES * 4, ws_route.MAX_TAIL_BYTES),
            (-1, 0),
            (4_096, 4_096),
        ],
    )
    async def test_workspace_socket_clamps_tail_bytes(
        self,
        requested_tail_bytes: int,
        expected_tail_bytes: int,
    ) -> None:
        class CapturingWebSocket:
            def __init__(self) -> None:
                self.app = type(
                    "App",
                    (),
                    {"state": type("State", (), {"db_session_factory": object()})()},
                )()

            async def accept(self) -> None:
                return None

        captured: dict[str, int] = {}

        async def fake_send_initial_state(
            websocket: object,
            factory: object,
            workspace_id: str,
            selected: set[str],
            seen_event_ids: set[str],
            tail_bytes: int,
        ) -> bool:
            captured["tail_bytes"] = tail_bytes
            return True

        async def fake_stream_live_frames(websocket: object, **_kwargs: object) -> None:
            raise WebSocketDisconnect(code=1000)

        fake_websocket = CapturingWebSocket()
        original_send_initial_state = ws_route._send_initial_state
        original_stream_live_frames = ws_route._stream_live_frames
        try:
            ws_route._send_initial_state = fake_send_initial_state  # type: ignore[assignment]
            ws_route._stream_live_frames = fake_stream_live_frames  # type: ignore[assignment]
            await ws_route.workspace_socket(
                fake_websocket,  # type: ignore[arg-type]
                "ws_clamp",
                channels="agent",
                tail_bytes=requested_tail_bytes,
            )
        finally:
            ws_route._send_initial_state = original_send_initial_state
            ws_route._stream_live_frames = original_stream_live_frames

        assert captured["tail_bytes"] == expected_tail_bytes

    @pytest.mark.unit
    async def test_workspace_socket_returns_when_events_initial_state_fails(self) -> None:
        class ClosingWebSocket:
            def __init__(self) -> None:
                self.app = type(
                    "App",
                    (),
                    {"state": type("State", (), {"db_session_factory": object()})()},
                )()
                self.accepted = False

            async def accept(self) -> None:
                self.accepted = True

        async def fake_send_initial_state(
            websocket: object,
            factory: object,
            workspace_id: str,
            selected: set[str],
            seen_event_ids: set[str],
            tail_bytes: int,
        ) -> bool:
            assert websocket is fake_websocket
            assert factory is not None
            assert workspace_id == "ws_missing"
            assert selected == {"events"}
            assert seen_event_ids == set()
            assert tail_bytes == 65_536
            return False

        async def fail_stream_live_frames(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("live streaming should not start after initial-state failure")

        fake_websocket = ClosingWebSocket()
        original_send_initial_state = ws_route._send_initial_state
        original_stream_live_frames = ws_route._stream_live_frames
        try:
            ws_route._send_initial_state = fake_send_initial_state  # type: ignore[assignment]
            ws_route._stream_live_frames = fail_stream_live_frames  # type: ignore[assignment]
            await ws_route.workspace_socket(
                fake_websocket,  # type: ignore[arg-type]
                "ws_missing",
                channels="events",
            )
        finally:
            ws_route._send_initial_state = original_send_initial_state
            ws_route._stream_live_frames = original_stream_live_frames

        assert fake_websocket.accepted is True

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
            await asyncio.wait_for(websocket.event_sent.wait(), timeout=15)
        finally:
            log_task.cancel()
            stream_task.cancel()
            with suppress(asyncio.CancelledError):
                await log_task
            with suppress(asyncio.CancelledError):
                await stream_task

        assert websocket.events == ["workspace.state_changed"]

    @pytest.mark.unit
    async def test_initial_state_tails_only_selected_log_sources(
        self,
        engine: AsyncEngine,
        tmp_path: Path,
    ) -> None:
        factory = make_session_factory(engine)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/ws.git",
                branch_base="main",
                task_title="Stream selected logs",
                task_prompt="Check websocket backlog filtering.",
                agent="codex",
                test_commands=[],
            )
            log_repo = WorkspaceLogStreamRepository(session)
            for source, stream_id, contents in (
                ("agent", "agent.stdout", "agent backlog\n"),
                ("validation", "validation.stdout", "validation backlog\n"),
                ("service", "service.stdout", "service backlog\n"),
                ("custom", "custom.stdout", "custom backlog\n"),
            ):
                path = tmp_path / f"{stream_id}.log"
                path.write_text(contents, encoding="utf-8")
                await log_repo.create_or_get(
                    workspace_id=workspace.id,
                    stream_id=stream_id,
                    source=source,
                    name=f"{source} stdout",
                    kind="stdout",
                    path=str(path),
                )
                await log_repo.append_metadata(
                    workspace_id=workspace.id,
                    stream_id=stream_id,
                    byte_delta=path.stat().st_size,
                    line_delta=1,
                )
            await log_repo.create_or_get(
                workspace_id=workspace.id,
                stream_id="validation.missing",
                source="validation",
                name="missing validation stdout",
                kind="stdout",
                path=str(tmp_path / "missing.log"),
            )
            empty_path = tmp_path / "custom.empty.log"
            empty_path.write_text("", encoding="utf-8")
            await log_repo.create_or_get(
                workspace_id=workspace.id,
                stream_id="custom.empty",
                source="custom",
                name="empty custom stdout",
                kind="stdout",
                path=str(empty_path),
            )
            await session.commit()
            workspace_id = workspace.id

        websocket = _FrameRecordingWebSocket()

        sent = await ws_route._send_initial_state(
            websocket,
            factory,
            workspace_id,
            selected={"validation", "services", "custom"},
            seen_event_ids=set(),
            tail_bytes=10,
        )

        assert sent is True
        assert [frame["type"] for frame in websocket.frames] == ["snapshot", "log", "log", "log"]
        assert [frame["source"] for frame in websocket.frames[1:]] == [
            "validation",
            "service",
            "custom",
        ]
        assert [frame["data"] for frame in websocket.frames[1:]] == [
            "n backlog\n",
            "e backlog\n",
            "m backlog\n",
        ]
        assert websocket.closed_codes == []

    @pytest.mark.unit
    async def test_initial_state_reports_missing_workspace(self, engine: AsyncEngine) -> None:
        websocket = _FrameRecordingWebSocket()

        sent = await ws_route._send_initial_state(
            websocket,
            make_session_factory(engine),
            "ws_missing",
            selected={"events"},
            seen_event_ids=set(),
            tail_bytes=100,
        )

        assert sent is False
        assert websocket.frames == [
            {
                "type": "error",
                "error_code": "NOT_FOUND",
                "message": "workspace not found",
            }
        ]
        assert websocket.closed_codes == [1008]

    @pytest.mark.unit
    async def test_initial_state_can_send_event_backlog_without_logs(
        self,
        engine: AsyncEngine,
    ) -> None:
        factory = make_session_factory(engine)
        async with factory() as session:
            repo = WorkspaceRepository(session)
            workspace = await repo.create(
                repo_url="git@github.com:example/ws.git",
                branch_base="main",
                task_title="Stream event backlog",
                task_prompt="Check websocket event backlog.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
            workspace_id = workspace.id

        websocket = _FrameRecordingWebSocket()
        seen_event_ids: set[str] = set()

        sent = await ws_route._send_initial_state(
            websocket,
            factory,
            workspace_id,
            selected={"events"},
            seen_event_ids=seen_event_ids,
            tail_bytes=100,
        )

        assert sent is True
        assert [frame["type"] for frame in websocket.frames] == ["snapshot", "event"]
        assert websocket.frames[1]["event"]["event_type"] == "workspace.created"
        assert seen_event_ids == {websocket.frames[1]["event"]["id"]}

    @pytest.mark.unit
    async def test_initial_state_can_send_snapshot_without_backlog_channels(
        self,
        engine: AsyncEngine,
    ) -> None:
        factory = make_session_factory(engine)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).create(
                repo_url="git@github.com:example/ws.git",
                branch_base="main",
                task_title="Snapshot only",
                task_prompt="Check websocket snapshot-only channel selection.",
                agent="codex",
                test_commands=[],
            )
            await session.commit()
            workspace_id = workspace.id

        websocket = _FrameRecordingWebSocket()

        sent = await ws_route._send_initial_state(
            websocket,
            factory,
            workspace_id,
            selected={"custom"},
            seen_event_ids=set(),
            tail_bytes=100,
        )

        assert sent is True
        assert [frame["type"] for frame in websocket.frames] == ["snapshot"]
        assert websocket.frames[0]["workspace"]["id"] == workspace_id

    @pytest.mark.unit
    async def test_live_stream_emits_heartbeats_and_selected_logs(self) -> None:
        workspace_id = "ws_live_heartbeat"
        websocket = _FrameRecordingWebSocket()
        stream_task = asyncio.create_task(
            ws_route._stream_live_frames(
                websocket,
                workspace_id=workspace_id,
                selected={"agent"},
                seen_event_ids=set(),
                event_queue=None,
                heartbeat_interval=0.01,
            )
        )

        try:
            await asyncio.wait_for(websocket.wait_for_type("heartbeat"), timeout=1)
            await LOG_BROADCASTER.publish(
                workspace_id=workspace_id,
                stream_id="validation.stdout",
                source="validation",
                fd="stdout",
                offset=0,
                data="ignored\n",
            )
            await LOG_BROADCASTER.publish(
                workspace_id=workspace_id,
                stream_id="agent.stdout",
                source="agent",
                fd="stdout",
                offset=0,
                data="selected\n",
            )
            await asyncio.wait_for(websocket.wait_for_source("agent"), timeout=1)
        finally:
            stream_task.cancel()
            with suppress(asyncio.CancelledError):
                await stream_task

        assert {"type": "heartbeat", "workspace_id": workspace_id} in websocket.frames
        log_frames = [frame for frame in websocket.frames if frame.get("type") == "log"]
        assert [frame["source"] for frame in log_frames] == ["agent"]
        assert log_frames[0]["data"] == "selected\n"

    @pytest.mark.unit
    async def test_live_stream_suppresses_duplicate_event_ids(self) -> None:
        workspace_id = "ws_live_duplicate_events"
        event_queue: asyncio.Queue[WorkspaceEventFrame] = asyncio.Queue()
        websocket = _FrameRecordingWebSocket()
        duplicate_event = WorkspaceEventFrame(
            id="evt_duplicate",
            workspace_id=workspace_id,
            event_type="workspace.state_changed",
            old_state="requested",
            new_state="running",
            reason_code="TEST",
            payload=None,
            occurred_at=datetime.now(UTC),
        )
        stream_task = asyncio.create_task(
            ws_route._stream_live_frames(
                websocket,
                workspace_id=workspace_id,
                selected={"events"},
                seen_event_ids={"evt_duplicate"},
                event_queue=event_queue,
                heartbeat_interval=60,
            )
        )

        try:
            await event_queue.put(duplicate_event)
            await event_queue.put(
                WorkspaceEventFrame(
                    id="evt_unique",
                    workspace_id=workspace_id,
                    event_type="workspace.finished",
                    old_state="running",
                    new_state="completed",
                    reason_code="TEST_DONE",
                    payload=None,
                    occurred_at=datetime.now(UTC),
                )
            )
            await asyncio.wait_for(websocket.wait_for_type("event"), timeout=1)
            await asyncio.sleep(0)
        finally:
            stream_task.cancel()
            with suppress(asyncio.CancelledError):
                await stream_task

        event_frames = [frame for frame in websocket.frames if frame.get("type") == "event"]
        assert [frame["event"]["id"] for frame in event_frames] == ["evt_unique"]

    @pytest.mark.unit
    def test_stream_selected_maps_builtin_and_custom_sources(self) -> None:
        selected = {"agent", "validation", "services", "custom"}

        assert ws_route._stream_selected("agent", selected) is True
        assert ws_route._stream_selected("validation", selected) is True
        assert ws_route._stream_selected("service", selected) is True
        assert ws_route._stream_selected("custom", selected) is True
        assert ws_route._stream_selected("other", selected) is False
        assert ws_route._stream_selected("monitor", {"monitor"}) is True
        assert ws_route._stream_selected("recovery", {"recovery"}) is True

    @pytest.mark.unit
    async def test_log_api_includes_monitor_and_recovery_streams(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws_id = await _create_workspace(client)
        headers = _auth(monkeypatch)

        factory = make_session_factory(engine)
        async with factory() as session:
            repo = WorkspaceLogStreamRepository(session)
            await repo.create_or_get(
                workspace_id=ws_id,
                stream_id="monitor.log",
                source="monitor",
                name="monitor",
                kind="stdout",
                path="/path/monitor",
            )
            await repo.create_or_get(
                workspace_id=ws_id,
                stream_id="recovery.stdout",
                source="recovery",
                name="recovery",
                kind="stdout",
                path="/path/recovery",
            )
            await session.commit()

        response = await client.get(f"/v1/workspaces/{ws_id}/logs", headers=headers)
        assert response.status_code == 200
        data = response.json()
        sources = {item["source"] for item in data["items"]}
        assert "monitor" in sources
        assert "recovery" in sources

    @pytest.mark.unit
    def test_websocket_stream_includes_monitor_and_recovery_logs(
        self,
        tmp_path: Path,
    ) -> None:
        with _temporary_api_token("secret"):
            sync_client, engine = _make_empty_sync_test_client(tmp_path)
            try:
                factory = make_session_factory(engine)
                mon_log = tmp_path / "monitor.log"
                rec_log = tmp_path / "recovery.log"
                mon_log.write_text("monitor data\n", encoding="utf-8")
                rec_log.write_text("recovery data\n", encoding="utf-8")

                async def setup():
                    async with factory() as session:
                        ws_repo = WorkspaceRepository(session)
                        ws = await ws_repo.create(**_REPOSITORY_BODY)
                        await session.commit()
                        repo = WorkspaceLogStreamRepository(session)
                        await repo.create_or_get(
                            workspace_id=ws.id,
                            stream_id="monitor.log",
                            source="monitor",
                            name="monitor",
                            kind="stdout",
                            path=str(mon_log),
                        )
                        await repo.create_or_get(
                            workspace_id=ws.id,
                            stream_id="recovery.stdout",
                            source="recovery",
                            name="recovery",
                            kind="stdout",
                            path=str(rec_log),
                        )
                        await repo.append_metadata(
                            workspace_id=ws.id,
                            stream_id="monitor.log",
                            byte_delta=mon_log.stat().st_size,
                            line_delta=1,
                        )
                        await repo.append_metadata(
                            workspace_id=ws.id,
                            stream_id="recovery.stdout",
                            byte_delta=rec_log.stat().st_size,
                            line_delta=1,
                        )
                        await session.commit()
                        return ws.id

                ws_id = asyncio.run(setup())

                with (
                    sync_client,
                    sync_client.websocket_connect(
                        f"/v1/workspaces/{ws_id}/ws?channels=monitor,recovery&tail_bytes=100",
                        headers={"Authorization": "Bearer secret"},
                    ) as websocket,
                ):
                    snapshot = websocket.receive_json()
                    first_tail = websocket.receive_json()
                    second_tail = websocket.receive_json()
                    websocket.close()
            finally:
                asyncio.run(engine.dispose())

        assert snapshot["type"] == "snapshot"
        assert snapshot["workspace"]["id"] == ws_id
        tails = {first_tail["stream_id"]: first_tail, second_tail["stream_id"]: second_tail}
        assert tails["monitor.log"]["source"] == "monitor"
        assert tails["monitor.log"]["data"] == "monitor data\n"
        assert tails["recovery.stdout"]["source"] == "recovery"
        assert tails["recovery.stdout"]["data"] == "recovery data\n"
