"""Shared operation-list response builders for REST and MCP."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime

from awf.api.schemas import OperationListResponse, OperationResponse
from awf.service.bounded_list import InvalidBoundedListCursorError


@dataclass(frozen=True, slots=True)
class OperationListCursor:
    created_at: datetime
    operation_id: str


def build_operation_list_response(
    rows: list[OperationResponse],
    *,
    limit: int,
    cursor: str | None = None,
) -> OperationListResponse:
    page_rows = rows[:limit]
    has_more = len(rows) > limit
    return OperationListResponse(
        items=page_rows,
        next_cursor=_encode_operation_cursor(page_rows[-1]) if has_more and page_rows else None,
        has_more=has_more,
        limit=limit,
        cursor=cursor,
    )


def decode_operation_list_cursor(cursor: str | None) -> OperationListCursor | None:
    if cursor is None:
        return None
    try:
        padded_cursor = cursor + ("=" * (-len(cursor) % 4))
        decoded = base64.urlsafe_b64decode(padded_cursor.encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
        created_at = datetime.fromisoformat(payload["c"])
        operation_id = payload["i"]
    except (
        binascii.Error,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise InvalidBoundedListCursorError("Invalid operation list cursor") from exc
    if not isinstance(operation_id, str) or operation_id == "":
        raise InvalidBoundedListCursorError("Invalid operation list cursor")
    return OperationListCursor(created_at=created_at, operation_id=operation_id)


def _encode_operation_cursor(operation: OperationResponse) -> str:
    payload = {"c": operation.created_at.isoformat(), "i": operation.id}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return encoded.decode("ascii").rstrip("=")
