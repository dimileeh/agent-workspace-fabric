"""Bounded list pagination cursor tests."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from awf.service.bounded_list import (
    InvalidBoundedListCursorError,
    bounded_list_limit,
    decode_bounded_list_cursor,
    encode_bounded_list_cursor,
    paginate_bounded_iterable,
    paginate_bounded_list,
)


@pytest.mark.unit
def test_paginate_bounded_list_clamps_limit_and_returns_cursor() -> None:
    page = paginate_bounded_list(["a", "b", "c"], limit=10, max_limit=2)

    assert page.items == ["a", "b"]
    assert page.limit == 2
    assert page.has_more is True
    assert page.next_cursor == encode_bounded_list_cursor(2)


@pytest.mark.unit
def test_paginate_bounded_iterable_uses_offset_cursor_without_over_reading() -> None:
    cursor = encode_bounded_list_cursor(1)
    page = paginate_bounded_iterable(
        iter(["a", "b", "c", "d"]),
        limit=2,
        max_limit=10,
        cursor=cursor,
    )

    assert page.items == ["b", "c"]
    assert page.cursor == cursor
    assert page.next_cursor == encode_bounded_list_cursor(3)
    assert page.has_more is True


@pytest.mark.unit
@pytest.mark.parametrize("limit", [-10, 0, 1, 50])
def test_bounded_list_limit_clamps_to_valid_range(limit: int) -> None:
    assert 1 <= bounded_list_limit(limit, 5) <= 5


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        b"not-base64!",
        base64.urlsafe_b64encode(b"{not-json"),
        base64.urlsafe_b64encode(json.dumps({}).encode("utf-8")),
        base64.urlsafe_b64encode(json.dumps({"o": "1"}).encode("utf-8")),
        base64.urlsafe_b64encode(json.dumps({"o": True}).encode("utf-8")),
        base64.urlsafe_b64encode(json.dumps({"o": -1}).encode("utf-8")),
    ],
)
def test_decode_bounded_list_cursor_rejects_malformed_payloads(payload: bytes) -> None:
    cursor = payload.decode("ascii").rstrip("=")

    with pytest.raises(InvalidBoundedListCursorError):
        decode_bounded_list_cursor(cursor)


@pytest.mark.unit
def test_decode_bounded_list_cursor_round_trips_unpadded_payload() -> None:
    cursor = encode_bounded_list_cursor(42)

    assert "=" not in cursor
    assert decode_bounded_list_cursor(cursor) == 42


def _cursor(payload: dict[str, Any]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return encoded.decode("ascii").rstrip("=")


@pytest.mark.unit
@pytest.mark.parametrize("offset", [-1, True, "2"])
def test_decode_bounded_list_cursor_rejects_non_integer_offsets(offset: object) -> None:
    with pytest.raises(InvalidBoundedListCursorError):
        decode_bounded_list_cursor(_cursor({"o": offset}))
