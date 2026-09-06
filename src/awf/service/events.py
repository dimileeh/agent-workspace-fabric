"""Shared workspace-event list response builders for REST."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime

from awf.api.schemas import WorkspaceEventListResponse, WorkspaceEventResponse
from awf.service.bounded_list import InvalidBoundedListCursorError

_INVALID_CURSOR_MESSAGE = "Invalid workspace event list cursor"


@dataclass(frozen=True, slots=True)
class WorkspaceEventListCursor:
    occurred_at: datetime
    event_id: str


def build_workspace_event_list_response(
    rows: list[WorkspaceEventResponse],
    *,
    limit: int,
    workspace_id: str,
    event_type: str | None = None,
    cursor: str | None = None,
) -> WorkspaceEventListResponse:
    page_rows = rows[:limit]
    has_more = len(rows) > limit
    return WorkspaceEventListResponse(
        items=page_rows,
        next_cursor=(
            encode_workspace_event_cursor(
                page_rows[-1],
                workspace_id=workspace_id,
                event_type=event_type,
            )
            if has_more and page_rows
            else None
        ),
        has_more=has_more,
        limit=limit,
        cursor=cursor,
    )


def decode_workspace_event_list_cursor(
    cursor: str | None,
    *,
    workspace_id: str,
    event_type: str | None,
) -> WorkspaceEventListCursor | None:
    if cursor is None:
        return None
    try:
        if cursor == "" or len(cursor) > 512:
            raise InvalidBoundedListCursorError(_INVALID_CURSOR_MESSAGE)
        padded_cursor = cursor + ("=" * (-len(cursor) % 4))
        decoded = base64.urlsafe_b64decode(padded_cursor.encode("ascii"))
        # Encoded cursor is capped at 512 chars above ⇒ decoded ≤ 384 bytes.
        if len(decoded) > 512:  # pragma: no cover - unreachable under encoded-length gate
            raise InvalidBoundedListCursorError(_INVALID_CURSOR_MESSAGE)
        payload = json.loads(decoded.decode("utf-8"))
        if not isinstance(payload, dict):
            raise InvalidBoundedListCursorError(_INVALID_CURSOR_MESSAGE)
        occurred_at = datetime.fromisoformat(payload["o"])
        event_id = payload["i"]
        cursor_workspace_id = payload["w"]
        cursor_event_type = payload.get("e", None)
    except (
        binascii.Error,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise InvalidBoundedListCursorError(_INVALID_CURSOR_MESSAGE) from exc
    if occurred_at.utcoffset() is None:
        raise InvalidBoundedListCursorError(_INVALID_CURSOR_MESSAGE)
    if not isinstance(event_id, str) or event_id == "":
        raise InvalidBoundedListCursorError(_INVALID_CURSOR_MESSAGE)
    if not isinstance(cursor_workspace_id, str) or cursor_workspace_id == "":
        raise InvalidBoundedListCursorError(_INVALID_CURSOR_MESSAGE)
    if cursor_event_type is not None and not isinstance(cursor_event_type, str):
        raise InvalidBoundedListCursorError(_INVALID_CURSOR_MESSAGE)
    if cursor_event_type == "":
        raise InvalidBoundedListCursorError(_INVALID_CURSOR_MESSAGE)
    if cursor_workspace_id != workspace_id or cursor_event_type != event_type:
        raise InvalidBoundedListCursorError(_INVALID_CURSOR_MESSAGE)
    return WorkspaceEventListCursor(occurred_at=occurred_at, event_id=event_id)


def encode_workspace_event_cursor(
    event: WorkspaceEventResponse,
    *,
    workspace_id: str,
    event_type: str | None,
) -> str:
    payload = {
        "o": event.occurred_at.isoformat(),
        "i": event.id,
        "w": workspace_id,
        "e": event_type,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    return encoded.decode("ascii").rstrip("=")
