"""ID generation helpers.

We use string UUIDs everywhere so the same ID format works across PostgreSQL,
API payloads, and log lines. Generated with uuid4 for unguessability; callers
that need sortable IDs should use ULIDs in a later phase.
"""

from __future__ import annotations

import re
from typing import Final
from uuid import uuid4

WORKSPACE_ID_PREFIX: Final = "ws_"
WORKSPACE_ID_HEX_LENGTH: Final = 24
WORKSPACE_ID_SUFFIX_PATTERN: Final = rf"[0-9a-f]{{{WORKSPACE_ID_HEX_LENGTH}}}"
WORKSPACE_ID_PATTERN: Final = rf"{re.escape(WORKSPACE_ID_PREFIX)}{WORKSPACE_ID_SUFFIX_PATTERN}"


def new_workspace_id() -> str:
    return f"{WORKSPACE_ID_PREFIX}{uuid4().hex[:WORKSPACE_ID_HEX_LENGTH]}"


def new_task_id() -> str:
    return f"task_{uuid4().hex[:24]}"


def new_task_attempt_id() -> str:
    return f"att_{uuid4().hex[:24]}"


def new_provider_model_circuit_breaker_id() -> str:
    return f"pcb_{uuid4().hex[:24]}"


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


def new_secret_lease_id() -> str:
    return f"sl_{uuid4().hex[:24]}"


def new_callback_subscription_id() -> str:
    return f"cb_{uuid4().hex[:24]}"


def new_callback_delivery_id() -> str:
    return f"cbd_{uuid4().hex[:24]}"


def new_stale_reason_id() -> str:
    return f"sr_{uuid4().hex[:24]}"


def new_policy_finding_id() -> str:
    return f"pf_{uuid4().hex[:24]}"


def new_egress_audit_record_id() -> str:
    return f"ear_{uuid4().hex[:24]}"
