"""Small metadata coercion helpers for executor modules."""

from __future__ import annotations

from collections.abc import Mapping


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int_or_none(value: object) -> int | None:
    return value if type(value) is int else None


def _metadata_str(metadata: Mapping[str, object], key: str) -> str | None:
    value = metadata.get(key)
    return value if isinstance(value, str) else None


def _metadata_int(metadata: Mapping[str, object], key: str) -> int | None:
    value = metadata.get(key)
    return value if type(value) is int else None


def _metadata_number(metadata: Mapping[str, object], key: str) -> int | float | None:
    value = metadata.get(key)
    return value if (isinstance(value, int | float) and not isinstance(value, bool)) else None
