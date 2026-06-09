"""In-process realtime broadcast for committed workspace events."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

from awf.db.models import WorkspaceEvent

_SESSION_EVENTS_KEY = "awf_workspace_events_to_broadcast"


@dataclass(frozen=True)
class WorkspaceEventFrame:
    id: str
    workspace_id: str
    event_type: str
    old_state: str | None
    new_state: str | None
    reason_code: str | None
    payload: dict[str, Any] | None
    occurred_at: datetime

    @classmethod
    def from_model(cls, row: WorkspaceEvent) -> WorkspaceEventFrame:
        occurred_at = row.occurred_at or datetime.now(UTC)
        return cls(
            id=row.id,
            workspace_id=row.workspace_id,
            event_type=row.event_type,
            old_state=row.old_state,
            new_state=row.new_state,
            reason_code=row.reason_code,
            payload=row.payload,
            occurred_at=occurred_at,
        )

    def model_dump(self) -> dict[str, object]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "event_type": self.event_type,
            "old_state": self.old_state,
            "new_state": self.new_state,
            "reason_code": self.reason_code,
            "payload": self.payload,
            "occurred_at": self.occurred_at.isoformat(),
        }


class WorkspaceEventBroadcaster:
    """Tiny per-process pub/sub for committed workspace events."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[WorkspaceEventFrame]]] = defaultdict(set)

    @asynccontextmanager
    async def subscribe(
        self,
        workspace_id: str,
    ) -> AsyncIterator[asyncio.Queue[WorkspaceEventFrame]]:
        queue: asyncio.Queue[WorkspaceEventFrame] = asyncio.Queue(maxsize=1000)
        self._subscribers[workspace_id].add(queue)
        try:
            yield queue
        finally:
            self._subscribers[workspace_id].discard(queue)
            if not self._subscribers[workspace_id]:
                del self._subscribers[workspace_id]

    def publish(self, frame: WorkspaceEventFrame) -> None:
        for queue in tuple(self._subscribers.get(frame.workspace_id, ())):
            with suppress(asyncio.QueueFull):
                queue.put_nowait(frame)


WORKSPACE_EVENT_BROADCASTER = WorkspaceEventBroadcaster()


def ensure_workspace_event_broadcasting() -> None:
    """Import hook used by session setup so SQLAlchemy listeners are registered."""


@event.listens_for(Session, "after_flush")
def _collect_workspace_events(session: Session, _flush_context: object) -> None:
    frames = session.info.setdefault(_SESSION_EVENTS_KEY, [])
    for row in session.new:
        if isinstance(row, WorkspaceEvent):
            frames.append(WorkspaceEventFrame.from_model(row))


@event.listens_for(Session, "after_commit")
def _publish_workspace_events(session: Session) -> None:
    frames = session.info.pop(_SESSION_EVENTS_KEY, [])
    for frame in frames:
        WORKSPACE_EVENT_BROADCASTER.publish(frame)


@event.listens_for(Session, "after_rollback")
def _clear_workspace_events(session: Session) -> None:
    session.info.pop(_SESSION_EVENTS_KEY, None)
