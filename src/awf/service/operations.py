"""Shared operation-list response builders for REST and MCP."""

from __future__ import annotations

from awf.api.schemas import OperationListResponse, OperationResponse
from awf.service.bounded_list import encode_bounded_list_cursor


def build_operation_list_response(
    rows: list[OperationResponse],
    *,
    limit: int,
    cursor: str | None = None,
    offset: int = 0,
) -> OperationListResponse:
    page_rows = rows[:limit]
    has_more = len(rows) > limit
    return OperationListResponse(
        items=page_rows,
        next_cursor=encode_bounded_list_cursor(offset + limit) if has_more else None,
        has_more=has_more,
        limit=limit,
        cursor=cursor,
    )
