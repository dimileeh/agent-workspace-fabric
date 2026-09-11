"""Value normalization helpers for workspace observability projections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

_MAX_PAYLOAD_KEYS = 32
_MAX_PAYLOAD_DEPTH = 4
_MAX_PAYLOAD_SEQUENCE_ITEMS = 20


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def bounded_payload(payload: Mapping[str, object] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    bounded: dict[str, Any] = {}
    for index, (key, value) in enumerate(payload.items()):
        if index >= _MAX_PAYLOAD_KEYS:
            bounded["__truncated__"] = True
            break
        bounded[str(key)] = json_safe_value(value)
    return bounded


def json_safe_value(value: object, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat()
    if depth >= _MAX_PAYLOAD_DEPTH:
        return str(value)
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for index, (key, nested_value) in enumerate(value.items()):
            if index >= _MAX_PAYLOAD_KEYS:
                safe["__truncated__"] = True
                break
            safe[str(key)] = json_safe_value(nested_value, depth=depth + 1)
        return safe
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        items = [
            json_safe_value(item, depth=depth + 1)
            for item in list(value)[:_MAX_PAYLOAD_SEQUENCE_ITEMS]
        ]
        if len(value) > _MAX_PAYLOAD_SEQUENCE_ITEMS:
            items.append("__truncated__")
        return items
    return str(value)
