"""ID generation helpers.

We use string UUIDs everywhere so the same ID format works across Postgres
(uuid), SQLite (varchar), API payloads, and log lines. Generated with uuid4 for
unguessability; callers that need sortable IDs should use ULIDs in a later phase.
"""

from __future__ import annotations

from uuid import uuid4


def new_workspace_id() -> str:
    return f"ws_{uuid4().hex[:24]}"


def new_task_id() -> str:
    return f"task_{uuid4().hex[:24]}"


def new_task_attempt_id() -> str:
    return f"att_{uuid4().hex[:24]}"


def new_queue_decision_id() -> str:
    return f"qd_{uuid4().hex[:24]}"


def new_resource_reservation_id() -> str:
    return f"rr_{uuid4().hex[:24]}"


def new_merge_candidate_id() -> str:
    return f"mc_{uuid4().hex[:24]}"


def new_validation_run_id() -> str:
    return f"vr_{uuid4().hex[:24]}"


def new_operation_id() -> str:
    return f"op_{uuid4().hex[:24]}"


def new_event_id() -> str:
    return f"evt_{uuid4().hex[:24]}"


def new_log_stream_id() -> str:
    return f"log_{uuid4().hex[:24]}"


def new_stale_reason_id() -> str:
    return f"sr_{uuid4().hex[:24]}"
