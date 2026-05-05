"""Cursor helpers for bounded list responses."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import islice


class InvalidBoundedListCursorError(ValueError):
    """Raised when an offset cursor cannot be decoded."""


@dataclass(frozen=True, slots=True)
class BoundedListPage[T]:
    items: list[T]
    next_cursor: str | None
    has_more: bool
    limit: int
    cursor: str | None


def paginate_bounded_list[T](
    items: Sequence[T],
    *,
    limit: int,
    max_limit: int,
    cursor: str | None = None,
) -> BoundedListPage[T]:
    bounded_limit = bounded_list_limit(limit, max_limit)
    offset = decode_bounded_list_cursor(cursor)
    next_offset = offset + bounded_limit
    page_items = list(items[offset:next_offset])
    has_more = len(items) > next_offset
    return BoundedListPage(
        items=page_items,
        next_cursor=encode_bounded_list_cursor(next_offset) if has_more else None,
        has_more=has_more,
        limit=bounded_limit,
        cursor=cursor,
    )


def paginate_bounded_iterable[T](
    items: Iterable[T],
    *,
    limit: int,
    max_limit: int,
    cursor: str | None = None,
) -> BoundedListPage[T]:
    bounded_limit = bounded_list_limit(limit, max_limit)
    offset = decode_bounded_list_cursor(cursor)
    next_offset = offset + bounded_limit
    window = list(islice(items, offset, next_offset + 1))
    has_more = len(window) > bounded_limit
    return BoundedListPage(
        items=window[:bounded_limit],
        next_cursor=encode_bounded_list_cursor(next_offset) if has_more else None,
        has_more=has_more,
        limit=bounded_limit,
        cursor=cursor,
    )


def bounded_list_limit(limit: int, max_limit: int) -> int:
    return max(1, min(limit, max_limit))


def encode_bounded_list_cursor(offset: int) -> str:
    payload = {"o": offset}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return encoded.decode("ascii").rstrip("=")


def decode_bounded_list_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        padded_cursor = cursor + ("=" * (-len(cursor) % 4))
        decoded = base64.urlsafe_b64decode(padded_cursor.encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
        offset = payload["o"]
    except (
        binascii.Error,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise InvalidBoundedListCursorError("Invalid bounded list cursor") from exc
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise InvalidBoundedListCursorError("Invalid bounded list cursor")
    return offset
