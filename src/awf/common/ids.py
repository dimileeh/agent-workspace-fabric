"""ID generation helpers.

We use string UUIDs everywhere so the same ID format works across Postgres
(uuid), SQLite (varchar), API payloads, and log lines. Generated with uuid4 for
unguessability; callers that need sortable IDs should use ULIDs in a later phase.
"""

from __future__ import annotations

from uuid import uuid4


def new_workspace_id() -> str:
    return f"ws_{uuid4().hex[:24]}"


def new_operation_id() -> str:
    return f"op_{uuid4().hex[:24]}"


def new_event_id() -> str:
    return f"evt_{uuid4().hex[:24]}"
