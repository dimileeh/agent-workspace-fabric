"""Small SQL helper utilities shared by database-facing code."""

from __future__ import annotations


def escape_like_pattern(value: str) -> str:
    """Escape wildcard characters for SQL LIKE patterns using a backslash escape."""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
