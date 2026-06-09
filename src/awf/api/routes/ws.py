"""Read-only realtime workspace WebSocket stream."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.deps import require_websocket_api_token
from awf.api.schemas import WorkspaceEventResponse
from awf.db.repositories import (
    WorkspaceEventRepository,
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from awf.runtime.events import WORKSPACE_EVENT_BROADCASTER, WorkspaceEventFrame
from awf.runtime.logs import LOG_BROADCASTER, LogFrame, read_log_chunk
from awf.service.workspaces import workspace_response

router = APIRouter(tags=["workspace-streams"])

# Upper bound for the initial log-tail snapshot, mirroring the REST log
# endpoint's ``limit_bytes`` cap (see ``routes/logs.py``). Authenticated
# callers connecting directly to the WebSocket bypass the console proxy
# sanitizer, so the server clamps ``tail_bytes`` itself to avoid reading and
# serializing an unbounded log tail into the initial snapshot.
MAX_TAIL_BYTES = 1_048_576


@router.websocket(
    "/v1/workspaces/{workspace_id}/ws",
    dependencies=[Depends(require_websocket_api_token)],
)
async def workspace_socket(
    websocket: WebSocket,
    workspace_id: str,
    channels: str = "events,agent,validation,services",
    tail_bytes: int = 65_536,
) -> None:
    tail_bytes = min(max(tail_bytes, 0), MAX_TAIL_BYTES)
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

    try:
        if "events" in selected:
            async with WORKSPACE_EVENT_BROADCASTER.subscribe(workspace_id) as event_queue:
                if not await _send_initial_state(
                    websocket,
                    factory,
                    workspace_id,
                    selected,
                    seen_event_ids,
                    tail_bytes,
                ):
                    return
                await _stream_live_frames(
                    websocket,
                    workspace_id=workspace_id,
                    selected=selected,
                    seen_event_ids=seen_event_ids,
                    event_queue=event_queue,
                )
        else:
            if not await _send_initial_state(
                websocket,
                factory,
                workspace_id,
                selected,
                seen_event_ids,
                tail_bytes,
            ):
                return
            await _stream_live_frames(
                websocket,
                workspace_id=workspace_id,
                selected=selected,
                seen_event_ids=seen_event_ids,
                event_queue=None,
            )
    except WebSocketDisconnect:
        return


async def _send_initial_state(
    websocket: WebSocket,
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    selected: set[str],
    seen_event_ids: set[str],
    tail_bytes: int,
) -> bool:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get_with_operations(workspace_id)
        if workspace is None:
            await websocket.send_json(
                {"type": "error", "error_code": "NOT_FOUND", "message": "workspace not found"}
            )
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return False
        await websocket.send_json(
            {
                "type": "snapshot",
                "workspace": workspace_response(workspace).model_dump(mode="json"),
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
        if selected - {"events"}:
            streams = await WorkspaceLogStreamRepository(session).list_for_workspace(workspace_id)
            for stream in streams:
                if not _stream_selected(stream.source, selected):
                    continue
                path = Path(stream.path)
                if not path.is_file():
                    continue
                offset = max(stream.byte_count - tail_bytes, 0)
                data, next_offset, _eof = await read_log_chunk(
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
    return True


async def _stream_live_frames(
    websocket: WebSocket,
    *,
    workspace_id: str,
    selected: set[str],
    seen_event_ids: set[str],
    event_queue: asyncio.Queue[WorkspaceEventFrame] | None,
    heartbeat_interval: float = 5.0,
) -> None:
    async with LOG_BROADCASTER.subscribe(workspace_id) as log_queue:
        while True:
            waiters: dict[asyncio.Task[Any], str] = {
                asyncio.create_task(log_queue.get()): "log",
                asyncio.create_task(asyncio.sleep(heartbeat_interval)): "heartbeat",
            }
            if event_queue is not None:
                waiters[asyncio.create_task(event_queue.get())] = "event"

            done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in pending:
                with suppress(asyncio.CancelledError):
                    await task

            for task in done:
                kind = waiters[task]
                if kind == "heartbeat":
                    await websocket.send_json({"type": "heartbeat", "workspace_id": workspace_id})
                    continue
                if kind == "event":
                    event = cast(WorkspaceEventFrame, task.result())
                    if event.id in seen_event_ids:
                        continue
                    seen_event_ids.add(event.id)
                    await websocket.send_json({"type": "event", "event": event.model_dump()})
                    continue

                frame = cast(LogFrame, task.result())
                if _stream_selected(frame.source, selected):
                    await websocket.send_json(frame.model_dump())


def _stream_selected(source: str, selected: set[str]) -> bool:
    if source == "agent":
        return "agent" in selected
    if source == "validation":
        return "validation" in selected
    if source == "service":
        return "services" in selected
    return source in selected
