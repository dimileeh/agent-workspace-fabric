"""Bounded-list cursor helper tests."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from awf.service.bounded_list import (
    InvalidBoundedListCursorError,
    decode_bounded_list_cursor,
)


def _cursor(payload: dict[str, Any]) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    return encoded.decode("ascii").rstrip("=")


@pytest.mark.unit
@pytest.mark.parametrize("offset", [-1, True, "2"])
def test_decode_bounded_list_cursor_rejects_non_integer_offsets(offset: object) -> None:
    with pytest.raises(InvalidBoundedListCursorError):
        decode_bounded_list_cursor(_cursor({"o": offset}))
