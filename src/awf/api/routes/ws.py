"""Read-only realtime workspace WebSocket stream."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceEventResponse, WorkspaceResponse
from awf.common.config import get_settings
from awf.db.repositories import (
    WorkspaceEventRepository,
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from awf.runtime.logs import LOG_BROADCASTER, LogStore

router = APIRouter(tags=["workspace-streams"])


@router.websocket("/v1/workspaces/{workspace_id}/ws")
async def workspace_socket(
    websocket: WebSocket,
    workspace_id: str,
    channels: str = "events,agent,validation,services",
    tail_bytes: int = 65_536,
) -> None:
    settings = get_settings()
    if not settings.api_token or websocket.headers.get("authorization") != (
        f"Bearer {settings.api_token}"
    ):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    factory: async_sessionmaker[AsyncSession] | None = getattr(
        websocket.app.state,
        "db_session_factory",
        None,
    )
    if factory is None:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return

    await websocket.accept()
    selected = {c.strip() for c in channels.split(",") if c.strip()}
    seen_event_ids: set[str] = set()

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None:
            await websocket.send_json(
                {"type": "error", "error_code": "NOT_FOUND", "message": "workspace not found"}
            )
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await websocket.send_json(
            {
                "type": "snapshot",
                "workspace": WorkspaceResponse.model_validate(workspace).model_dump(mode="json"),
            }
        )
        if "events" in selected:
            events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                limit=50,
            )
            for event in reversed(events):
                seen_event_ids.add(event.id)
                await websocket.send_json(
                    {
                        "type": "event",
                        "event": WorkspaceEventResponse.model_validate(event).model_dump(
                            mode="json"
                        ),
                    }
                )
        if {"agent", "validation", "services"} & selected:
            streams = await WorkspaceLogStreamRepository(session).list_for_workspace(workspace_id)
            for stream in streams:
                if not _stream_selected(stream.source, selected):
                    continue
                path = Path(stream.path)
                if not path.is_file():
                    continue
                offset = max(stream.byte_count - tail_bytes, 0)
                data, next_offset, _eof = await LogStore(root=path.parent).read(
                    path=path,
                    offset=offset,
                    limit_bytes=tail_bytes,
                )
                if data:
                    await websocket.send_json(
                        {
                            "type": "log",
                            "seq": 0,
                            "workspace_id": workspace_id,
                            "stream_id": stream.stream_id,
                            "source": stream.source,
                            "fd": stream.kind,
                            "offset": offset,
                            "next_offset": next_offset,
                            "data": data,
                        }
                    )

    try:
        async with LOG_BROADCASTER.subscribe(workspace_id) as queue:
            while True:
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=5.0)
                except TimeoutError:
                    await _send_new_events(websocket, factory, workspace_id, seen_event_ids)
                    await websocket.send_json({"type": "heartbeat", "workspace_id": workspace_id})
                    continue
                if _stream_selected(frame.source, selected):
                    await websocket.send_json(frame.model_dump())
    except WebSocketDisconnect:
        return


async def _send_new_events(
    websocket: WebSocket,
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    seen_event_ids: set[str],
) -> None:
    async with factory() as session:
        events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id, limit=50)
        for event in reversed(events):
            if event.id in seen_event_ids:
                continue
            seen_event_ids.add(event.id)
            await websocket.send_json(
                {
                    "type": "event",
                    "event": WorkspaceEventResponse.model_validate(event).model_dump(mode="json"),
                }
            )


def _stream_selected(source: str, selected: set[str]) -> bool:
    if source == "agent":
        return "agent" in selected
    if source == "validation":
        return "validation" in selected
    if source == "service":
        return "services" in selected
    return source in selected
