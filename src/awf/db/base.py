"""SQLAlchemy Declarative base and shared helpers.

The DB layer targets both Postgres (production, via asyncpg) and SQLite (unit
tests, via aiosqlite). Keep ORM-level code portable across both by using
``JSON`` (generic — Postgres still uses JSONB), ``String(36)`` for UUID-shaped
IDs, and ``DateTime(timezone=True)`` everywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all AWF ORM models."""


def _now() -> datetime:
    """UTC-aware now(). Used as default/on-update for every timestamp column."""
    return datetime.now(UTC)
