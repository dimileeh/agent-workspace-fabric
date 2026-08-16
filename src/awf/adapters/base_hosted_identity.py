"""Output and hosted-identity normalization helpers for agent adapters."""

from __future__ import annotations

from typing import Any


def _prepend_missing_streamed_output(*, chunks: list[str], buffered: str) -> str:
    streamed = "".join(chunks)
    if not streamed or buffered.startswith(streamed):
        return buffered
    if streamed.startswith(buffered):
        return streamed
    return streamed + buffered


def _buffered_output_not_streamed(*, chunks: list[str], buffered: str) -> str:
    if not buffered:
        return ""
    streamed = "".join(chunks)
    if not streamed:
        return buffered
    if buffered.startswith(streamed):
        return buffered[len(streamed) :]
    if streamed.startswith(buffered):
        return ""
    return buffered


def _hosted_identity_str(identity: dict[str, Any] | None, key: str) -> str | None:
    if identity is None:
        return None
    value = identity.get(key)
    return value if isinstance(value, str) and value else None


def _hosted_identity_int(identity: dict[str, Any] | None, key: str) -> int | None:
    if identity is None:
        return None
    value = identity.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _hosted_identity_str_tuple(identity: dict[str, Any] | None, key: str) -> tuple[str, ...]:
    if identity is None:
        return ()
    value = identity.get(key)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)
