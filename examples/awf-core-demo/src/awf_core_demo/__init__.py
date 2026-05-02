"""Tiny AWF Core demo service."""

from __future__ import annotations


def health() -> dict[str, str]:
    """Return a stable health payload for smoke validation."""
    return {"status": "ok"}
