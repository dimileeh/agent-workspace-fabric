"""SQLAlchemy Declarative base and shared helpers.

The AWF control plane requires PostgreSQL via asyncpg. ORM-level code should use
PostgreSQL-compatible SQLAlchemy constructs and keep database-specific behavior
explicit.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all AWF ORM models."""


def _now() -> datetime:
    """UTC-aware now(). Used as default/on-update for every timestamp column."""
    return datetime.now(UTC)
