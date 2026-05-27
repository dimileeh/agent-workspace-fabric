"""Time and status normalization helpers for workspace GC."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from awf.db.enums import WorkspaceStatus


def normalize_statuses(statuses: Iterable[WorkspaceStatus | str] | None) -> set[str] | None:
    if statuses is None:
        return None
    return {
        status.value if isinstance(status, WorkspaceStatus) else str(status) for status in statuses
    }


def to_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
