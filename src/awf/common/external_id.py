"""External task id helpers for API and MCP request boundaries."""

from __future__ import annotations


def validate_external_id(value: str | None) -> str | None:
    """Return *value* unchanged, or raise when it contains ASCII controls.

    PostgreSQL rejects NUL in character columns. Other C0 controls and DEL are
    rejected at the request boundary so malformed operator input becomes a 422
    instead of an untranslated database error on flush.
    """
    if value is None:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("external_id must not contain ASCII control characters (including NUL)")
    return value
