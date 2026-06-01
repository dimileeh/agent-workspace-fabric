"""Common database repository helper definitions, constants, and utilities."""

from __future__ import annotations

import builtins
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import (
    and_,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from awf.common.audit import redact_audit_value
from awf.common.callback_events import (
    CALLBACK_EVENT_WILDCARDS,
    PUBLIC_CALLBACK_EVENT_TYPES,
)
from awf.common.owned_paths import normalize_owned_path
from awf.db.dialect import SESSION_DIALECT_NAME_KEY
from awf.db.enums import (
    OperationStatus,
    WorkspaceStatus,
)
from awf.db.models import (
    CallbackDelivery,
    CallbackSubscription,
    PRFeedbackResolution,
    ProviderModelCircuitBreaker,
    ResourceReservation,
    Workspace,
    WorkspaceEvent,
    WorkspaceSecretLease,
)

ACTIVE_OWNED_PATH_OVERLAP_STATUSES: Final[tuple[str, ...]] = (
    WorkspaceStatus.requested.value,
    WorkspaceStatus.provisioning.value,
    WorkspaceStatus.ready.value,
    WorkspaceStatus.running.value,
    WorkspaceStatus.validating.value,
    WorkspaceStatus.pushing.value,
    WorkspaceStatus.monitoring_pr.value,
)
HOST_PORT_CONFLICT_STATUSES: Final[tuple[str, ...]] = (
    *ACTIVE_OWNED_PATH_OVERLAP_STATUSES,
    WorkspaceStatus.destroying.value,
)
"""Workspace statuses whose host ports should be checked for collision."""
HOST_PORT_TERMINAL_RELEASE_STATUSES: Final[tuple[str, ...]] = (
    WorkspaceStatus.failed.value,
    WorkspaceStatus.cancelled.value,
    WorkspaceStatus.completed.value,
    WorkspaceStatus.destroyed.value,
)
"""Terminal statuses that indicate a workspace's host ports are released."""
ACTIVE_OWNED_PATH_CONFLICT_STATUSES: Final[tuple[str, ...]] = ACTIVE_OWNED_PATH_OVERLAP_STATUSES
ACTIVE_RESOURCE_RESERVATION_EXCLUDED_STATUSES: Final[tuple[str, ...]] = (
    WorkspaceStatus.completed.value,
    WorkspaceStatus.failed.value,
    WorkspaceStatus.cancelled.value,
    WorkspaceStatus.destroyed.value,
)
_ACTIVE_RECOVERY_OPERATION_STATUSES: Final[tuple[str, ...]] = (
    OperationStatus.pending.value,
    OperationStatus.running.value,
)
_VALIDATE_ONLY_RECOVERY_MODES: Final[tuple[str, ...]] = ("validate_only", "rebase_only")
_WORKER_RESTART_RECOVERY_EXECUTION_CLAIM_STATUSES: Final[tuple[str, ...]] = (
    WorkspaceStatus.running.value,
)
ALLOCATED_RESOURCE_RESERVATION_STATUSES: Final[tuple[str, ...]] = (
    WorkspaceStatus.provisioning.value,
    WorkspaceStatus.ready.value,
    WorkspaceStatus.running.value,
    WorkspaceStatus.validating.value,
    WorkspaceStatus.pushing.value,
    WorkspaceStatus.monitoring_pr.value,
    WorkspaceStatus.destroying.value,
)
DEFAULT_IDEMPOTENCY_REPLAY_KEY_LIMIT: Final[int] = 4096
OWNED_PATH_EXACT_MATCH_REASON: Final = "OWNED_PATH_EXACT_MATCH"
OWNED_PATH_ANCESTOR_MATCH_REASON: Final = "OWNED_PATH_ANCESTOR_MATCH"
OWNED_PATH_WILDCARD_MATCH_REASON: Final = "OWNED_PATH_WILDCARD_MATCH"
_SECRET_LEASE_DECLARATION_CONFLICT_COLUMNS: Final[tuple[str, ...]] = (
    "workspace_id",
    "secret_name",
    "kind",
    "target",
)
_CALLBACK_SUBSCRIPTION_IDEMPOTENCY_CONFLICT_COLUMNS: Final[tuple[str, ...]] = ("idempotency_key",)
_CALLBACK_DELIVERY_DEDUPE_CONFLICT_COLUMNS: Final[tuple[str, ...]] = (
    "subscription_id",
    "dedupe_key",
)
_PROVIDER_MODEL_CIRCUIT_BREAKER_CONFLICT_COLUMNS: Final[tuple[str, ...]] = (
    "provider",
    "model",
)
_PR_FEEDBACK_RESOLUTION_CONFLICT_COLUMNS: Final[tuple[str, ...]] = (
    "scm_provider",
    "repository_key",
    "pull_request_key",
    "feedback_kind",
    "feedback_id",
    "feedback_body_hash",
)


@dataclass(frozen=True)
class OwnedPathOverlap:
    """Two owned paths that conflict because one contains the other."""

    workspace_id: str
    existing_path: str
    requested_path: str


@dataclass(frozen=True)
class OwnedPathConflict:
    """Alias kept for backwards-compat; prefer ``OwnedPathOverlap``."""

    workspace_id: str
    existing_path: str
    requested_path: str


@dataclass(frozen=True)
class HostPortConflict:
    """A host-port already claimed by another active or staged workspace."""

    host_port: int
    workspace_id: str


def _extract_host_ports(port_entries: builtins.list[Any]) -> builtins.list[int]:
    """Extract host-side port numbers from a flat list of port-mapping entries.

    Each entry may be a list/tuple of form ``[container_port, host_port, ...]``.
    Invalid or malformed entries are silently skipped.
    """
    host_ports: builtins.list[int] = []
    for port_mapping in port_entries:
        if isinstance(port_mapping, (list, tuple)) and len(port_mapping) >= 2:
            try:
                host_ports.append(int(port_mapping[1]))
            except (ValueError, TypeError):
                continue
    return host_ports


def host_ports_from_resolved_profile(
    resolved_profile: Mapping[str, Any] | None,
) -> builtins.list[int]:
    """Extract host-side ports from a resolved profile's services block.

    Shared by the service layer and the repository layer so that the
    port-mapping data shape is parsed in exactly one place.
    """
    if not resolved_profile or not isinstance(resolved_profile, dict):
        return []
    services = resolved_profile.get("services")
    if not services or not isinstance(services, list):
        return []
    host_ports: builtins.list[int] = []
    for service in services:
        if not isinstance(service, dict):
            continue
        svc_ports = service.get("ports")
        if not svc_ports or not isinstance(svc_ports, list):
            continue
        host_ports.extend(_extract_host_ports(svc_ports))
    return host_ports


def host_ports_from_task_policy_companions(
    task_policy: Mapping[str, Any] | None,
) -> builtins.list[int]:
    """Extract host-side ports from companions stored inside a workspace task_policy.

    Shared by the service layer and the repository layer so that the
    companion port-mapping data shape is parsed in exactly one place.
    """
    if not task_policy or not isinstance(task_policy, dict):
        return []
    companions = task_policy.get("companions")
    if not companions or not isinstance(companions, list):
        return []
    host_ports: builtins.list[int] = []
    for companion in companions:
        if not isinstance(companion, dict):
            continue
        ports = companion.get("ports")
        if not ports or not isinstance(ports, list):
            continue
        host_ports.extend(_extract_host_ports(ports))
    return host_ports


@dataclass(frozen=True)
class OwnedPathOverlapMatch:
    """Detailed overlap result with normalization context and match reason."""

    left_path: str
    right_path: str
    normalized_left_path: str
    normalized_right_path: str
    match_reason_code: str
    explanation: str


@dataclass(frozen=True)
class WorkspaceEventCreate:
    """Payload for creating a workspace event via ``WorkspaceEventRepository``."""

    event_type: str
    reason_code: str | None = None
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class _IssuedSecretLease:
    """A secret lease that was issued and may require a workspace event."""

    lease: WorkspaceSecretLease
    issue_event_required: bool


@dataclass(frozen=True)
class QueueDecisionCreate:
    """Payload for recording a scheduler queue-decision event."""

    workspace_id: str
    task_id: str
    attempt_id: str
    decision: str
    reason_code: str
    class_priority: int
    computed_priority: int
    age_boost: int
    retry_bonus: int
    resource_summary: dict[str, Any]
    overlap_risk_summary: dict[str, Any]
    score_summary: dict[str, Any] | None = None
    decided_at: datetime | None = None


@dataclass(frozen=True)
class StaleReasonCreate:
    """Per-finding payload for ``StaleReasonRepository.replace_active_findings``."""

    reason_code: str
    trigger_type: str
    trigger_ref: str | None
    explanation: str


@dataclass(frozen=True)
class PolicyFindingCreate:
    """Per-finding payload for ``PolicyFindingRepository.replace_active_findings``."""

    reason_code: str
    severity: str
    subject_path: str | None
    explanation: str
    details: dict[str, Any]


@dataclass(frozen=True)
class SecretLeaseIssue:
    """A secret-lease that could not be issued during workspace provisioning."""

    secret_name: str
    kind: str
    target: str
    mode: str
    required: bool
    provider: str | None
    ref_digest: str | None
    expires_at: datetime | None
    issue_metadata: dict[str, Any]
    attempt_id: str | None = None


def validation_command_set_hash(commands: list[dict[str, Any]]) -> str:
    """Stable hash for the configured command metadata in a validation run."""

    payload = json.dumps(
        [_validation_command_identity(command) for command in commands],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validation_command_identity(command: Mapping[str, Any]) -> dict[str, Any]:
    """Strip evidence keys from a command dict for identity hashing."""
    return {
        key: value
        for key, value in command.items()
        if key not in {"evidence_status", "evidence_reason_code", "evidence_source_run_id"}
    }


def _coverage_metadata_has_pytest_failures(coverage: Mapping[str, Any]) -> bool:
    """Return True if coverage metadata contains pytest failure details."""
    node_ids = coverage.get("failing_test_node_ids")
    evidence = coverage.get("failing_test_evidence")
    return bool(node_ids or evidence)


def resolve_session_dialect_name(
    session: AsyncSession,
    dialect_name: str | None,
) -> str | None:
    """Return the SQLAlchemy dialect name, falling back to the session bind."""

    if dialect_name is not None:
        return dialect_name

    info_value = session.info.get(SESSION_DIALECT_NAME_KEY)
    if isinstance(info_value, str):
        return info_value

    bind = getattr(session, "bind", None)
    if bind is None:
        return None
    dialect = getattr(bind, "dialect", None)
    name = getattr(dialect, "name", None)
    return name if isinstance(name, str) else None


def _secret_lease_insert_if_absent_stmt(dialect_name: str | None) -> Any | None:
    """Build a PostgreSQL INSERT-if-absent statement for secret leases."""
    if dialect_name == "postgresql":
        return (
            postgresql_insert(WorkspaceSecretLease)
            .on_conflict_do_nothing(index_elements=_SECRET_LEASE_DECLARATION_CONFLICT_COLUMNS)
            .returning(WorkspaceSecretLease.id)
        )
    return None


def _callback_subscription_insert_if_absent_stmt(dialect_name: str | None) -> Any | None:
    """Build a PostgreSQL INSERT-if-absent statement for callback subscriptions."""
    if dialect_name == "postgresql":
        return (
            postgresql_insert(CallbackSubscription)
            .on_conflict_do_nothing(
                index_elements=_CALLBACK_SUBSCRIPTION_IDEMPOTENCY_CONFLICT_COLUMNS
            )
            .returning(CallbackSubscription.id)
        )
    return None


def _callback_delivery_insert_if_absent_stmt(dialect_name: str | None) -> Any | None:
    """Build a PostgreSQL INSERT-if-absent statement for callback deliveries."""
    if dialect_name == "postgresql":
        return (
            postgresql_insert(CallbackDelivery)
            .on_conflict_do_nothing(index_elements=_CALLBACK_DELIVERY_DEDUPE_CONFLICT_COLUMNS)
            .returning(CallbackDelivery.id)
        )
    return None


def _provider_model_circuit_breaker_insert_if_absent_stmt(
    dialect_name: str | None,
) -> Any | None:
    """Build a PostgreSQL INSERT-if-absent statement for circuit-breaker records."""
    if dialect_name == "postgresql":
        return (
            postgresql_insert(ProviderModelCircuitBreaker)
            .on_conflict_do_nothing(index_elements=_PROVIDER_MODEL_CIRCUIT_BREAKER_CONFLICT_COLUMNS)
            .returning(ProviderModelCircuitBreaker.id)
        )
    return None


def _pr_feedback_resolution_upsert_stmt(dialect_name: str | None) -> Any | None:
    """Build a PostgreSQL upsert statement for PR feedback resolutions."""
    if dialect_name != "postgresql":
        return None
    inserted = postgresql_insert(PRFeedbackResolution)
    return inserted.on_conflict_do_update(
        index_elements=_PR_FEEDBACK_RESOLUTION_CONFLICT_COLUMNS,
        set_={
            "pull_request_url": inserted.excluded.pull_request_url,
            "head_sha": inserted.excluded.head_sha,
            "feedback_url": inserted.excluded.feedback_url,
            "feedback_author": inserted.excluded.feedback_author,
            "verdict": inserted.excluded.verdict,
            "reason": inserted.excluded.reason,
            "source_workspace_id": inserted.excluded.source_workspace_id,
            "source_operation_id": inserted.excluded.source_operation_id,
            "resolved_at": inserted.excluded.resolved_at,
            "updated_at": func.now(),
        },
    ).returning(PRFeedbackResolution)


def _callback_subscription_event_type_candidates(event_type: str) -> tuple[str, ...]:
    """Return the event type and its wildcard candidate for subscription matching."""
    if event_type not in PUBLIC_CALLBACK_EVENT_TYPES:
        return ()

    candidates = [event_type]
    namespace, separator, _suffix = event_type.partition(".")
    wildcard = f"{namespace}.*"
    if separator and wildcard in CALLBACK_EVENT_WILDCARDS:
        candidates.append(wildcard)
    return tuple(candidates)


def _callback_subscription_event_type_filter(
    event_type_candidates: tuple[str, ...],
    dialect_name: str | None,
) -> ColumnElement[bool]:
    """Build an EXISTS filter for subscriptions that declare any of the candidate event types."""
    del dialect_name
    event_type_values = (
        func.jsonb_array_elements_text(CallbackSubscription.event_types.cast(JSONB))
        .table_valued("value")
        .render_derived(name="callback_event_type")
    )

    return (
        select(1)
        .select_from(event_type_values)
        .where(event_type_values.c.value.in_(event_type_candidates))
        .exists()
    )


def pr_feedback_body_hash(body: str | None) -> str:
    """Stable hash for matching the same feedback text across workspaces."""
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()


def _normalize_provider_key(value: str) -> str:
    """Normalize a provider identifier for case-insensitive comparison."""
    return value.strip().lower()


def _normalize_repository_key(value: str) -> str:
    """Normalize a repository identifier for case-insensitive comparison."""
    return value.strip().lower()


def _circuit_breaker_expired(
    breaker: ProviderModelCircuitBreaker,
    now: datetime,
) -> bool:
    """Return True if an open circuit breaker's cooldown has elapsed."""
    cooldown_until = breaker.cooldown_until
    if breaker.state != "open" or cooldown_until is None:
        return False
    return _as_utc_naive(cooldown_until) <= _as_utc_naive(now)


def _as_utc_naive(value: datetime) -> datetime:
    """Convert a datetime to a naive UTC datetime."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def empty_resource_reservation_totals() -> dict[str, float | int]:
    """Return a zeroed-out resource-reservation summary dict."""

    return {
        "workspace_count": 0,
        "steady_cpu": 0.0,
        "steady_memory_gb": 0.0,
        "peak_cpu": 0.0,
        "peak_memory_gb": 0.0,
        "disk_mb": 0,
        "dind_slots": 0,
    }


def _active_resource_reservation_status_filter(
    statuses: Iterable[WorkspaceStatus | str] | None,
) -> ColumnElement[Any] | None:
    """Build a WHERE-clause filter for resource-reservation-active workspace statuses."""
    if statuses is None:
        return ~Workspace.status.in_(ACTIVE_RESOURCE_RESERVATION_EXCLUDED_STATUSES)
    status_values = tuple(
        status.value if isinstance(status, WorkspaceStatus) else str(status) for status in statuses
    )
    if not status_values:
        return None
    return Workspace.status.in_(status_values)


def _active_latest_resource_reservation_totals_stmt(
    *,
    statuses: Iterable[WorkspaceStatus | str] | None = None,
    reservation_node_id: str | None = None,
    workspace_node_id: str | None = None,
    scheduler_allocation_node_id: str | None = None,
    metrics_allocation_node_id: str | None = None,
) -> Select[tuple[Any, ...]] | None:
    """Build a SELECT that sums the latest active resource-reservation totals."""
    status_filter = _active_resource_reservation_status_filter(statuses)
    if status_filter is None:
        return None
    latest_active_reservations_query = (
        select(
            ResourceReservation.workspace_id.label("workspace_id"),
            ResourceReservation.node_id.label("node_id"),
            Workspace.node_id.label("workspace_node_id"),
            ResourceReservation.steady_cpu.label("steady_cpu"),
            ResourceReservation.steady_memory_gb.label("steady_memory_gb"),
            ResourceReservation.peak_cpu.label("peak_cpu"),
            ResourceReservation.peak_memory_gb.label("peak_memory_gb"),
            ResourceReservation.disk_mb.label("disk_mb"),
            ResourceReservation.dind_slots.label("dind_slots"),
            func.row_number()
            .over(
                partition_by=ResourceReservation.workspace_id,
                order_by=(
                    ResourceReservation.reserved_at.desc(),
                    ResourceReservation.id.desc(),
                ),
            )
            .label("reservation_rank"),
        )
        .join(Workspace, ResourceReservation.workspace_id == Workspace.id)
        .where(
            ResourceReservation.released_at.is_(None),
            status_filter,
        )
    )
    if workspace_node_id is not None:
        latest_active_reservations_query = latest_active_reservations_query.where(
            or_(Workspace.node_id == workspace_node_id, Workspace.node_id.is_(None))
        )
    latest_active_reservations = latest_active_reservations_query.subquery()
    stmt = (
        select(
            func.count(latest_active_reservations.c.workspace_id),
            func.coalesce(func.sum(latest_active_reservations.c.steady_cpu), 0.0),
            func.coalesce(
                func.sum(latest_active_reservations.c.steady_memory_gb),
                0.0,
            ),
            func.coalesce(func.sum(latest_active_reservations.c.peak_cpu), 0.0),
            func.coalesce(
                func.sum(latest_active_reservations.c.peak_memory_gb),
                0.0,
            ),
            func.coalesce(func.sum(latest_active_reservations.c.disk_mb), 0),
            func.coalesce(func.sum(latest_active_reservations.c.dind_slots), 0),
        )
        .select_from(latest_active_reservations)
        .where(latest_active_reservations.c.reservation_rank == 1)
    )
    if reservation_node_id is not None:
        stmt = stmt.where(latest_active_reservations.c.node_id == reservation_node_id)
    if scheduler_allocation_node_id is not None:
        stmt = stmt.where(
            or_(
                latest_active_reservations.c.node_id == scheduler_allocation_node_id,
                latest_active_reservations.c.workspace_node_id == scheduler_allocation_node_id,
                and_(
                    latest_active_reservations.c.node_id.is_(None),
                    latest_active_reservations.c.workspace_node_id.is_(None),
                ),
            )
        )
    if metrics_allocation_node_id is not None:
        stmt = stmt.where(
            or_(
                latest_active_reservations.c.workspace_node_id == metrics_allocation_node_id,
                and_(
                    latest_active_reservations.c.workspace_node_id.is_(None),
                    latest_active_reservations.c.node_id == metrics_allocation_node_id,
                ),
                and_(
                    latest_active_reservations.c.workspace_node_id.is_(None),
                    latest_active_reservations.c.node_id.is_(None),
                ),
            )
        )
    return stmt


async def _fetch_resource_reservation_totals(
    session: AsyncSession,
    stmt: Select[tuple[Any, ...]],
) -> dict[str, float | int]:
    """Execute a resource-reservation totals statement and return the summary dict."""
    row = (await session.execute(stmt)).one()
    return {
        "workspace_count": int(row[0] or 0),
        "steady_cpu": float(row[1] or 0.0),
        "steady_memory_gb": float(row[2] or 0.0),
        "peak_cpu": float(row[3] or 0.0),
        "peak_memory_gb": float(row[4] or 0.0),
        "disk_mb": int(row[5] or 0),
        "dind_slots": int(row[6] or 0),
    }


PROVISIONING_LAUNCHING_EVENT_TYPE: Final = "workspace.provisioning_launching"
"""Event type recorded when a workspace enters the launching phase of provisioning."""

PROVISIONING_LAUNCHING_REASON_CODE: Final = "PROVISIONING_LAUNCHING"
"""Reason code accompanying the ``workspace.provisioning_launching`` event."""

TERMINAL_RUNTIME_RELEASE_EVENT_TYPE: Final = "workspace.terminal_runtime_released"
"""Event type recorded when a terminal-status workspace releases its runtime resources (containers, host ports)."""

TERMINAL_RUNTIME_RELEASE_REASON_CODE: Final = "TERMINAL_RUNTIME_RELEASED"
"""Reason code accompanying the ``workspace.terminal_runtime_released`` event."""

TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE: Final = "workspace.terminal_runtime_release_revoked"
"""Event type recorded when a terminal-runtime release is revoked because orphan containers could not be stopped."""

TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE: Final = (
    "TERMINAL_RUNTIME_RELEASE_REVOKED_ORPHAN_STOP_FAILED"
)
"""Reason code accompanying the ``workspace.terminal_runtime_release_revoked`` event."""


def terminal_runtime_effectively_released_expr(
    correlated_to: type[Workspace] | None = None,
    workspace_id: str | None = None,
) -> ColumnElement[bool]:
    """Build a boolean SQL expression for "terminal runtime was effectively released".

    A release is "effective" when the latest ``terminal_runtime_released``
    event has not been superseded by a later ``terminal_runtime_release_revoked``
    event.  The latest event is determined by ordering all release and revoke
    events by ``(occurred_at DESC, event_order DESC)`` and picking the first
    row's ``event_type``.  This avoids comparing two independent ``MAX``
    projections that can diverge when ``event_order`` is not globally
    monotonic across event types.

    Must pass exactly one of *correlated_to* or *workspace_id*:

    - *correlated_to* (typically the ``Workspace`` ORM class): builds
      subqueries that correlate to the outer ``Workspace`` row — for use
      inside larger ``SELECT`` statements that iterate over ``Workspace``.
    - *workspace_id*: builds subqueries that filter to a single workspace
      — for use when checking one workspace at a time.
    """
    if (correlated_to is None) == (workspace_id is None):
        raise ValueError("Pass exactly one of correlated_to or workspace_id")

    both_types = [
        TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
        TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
    ]
    both_reasons = [
        TERMINAL_RUNTIME_RELEASE_REASON_CODE,
        TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
    ]

    stmt = (
        select(WorkspaceEvent.event_type)
        .where(WorkspaceEvent.event_type.in_(both_types))
        .where(WorkspaceEvent.reason_code.in_(both_reasons))
        .order_by(WorkspaceEvent.occurred_at.desc(), WorkspaceEvent.event_order.desc().nullslast())
        .limit(1)
    )
    if correlated_to is not None:
        stmt = stmt.where(WorkspaceEvent.workspace_id == correlated_to.id).correlate(correlated_to)
    else:
        stmt = stmt.where(WorkspaceEvent.workspace_id == workspace_id)

    latest_type = stmt.scalar_subquery()

    return and_(
        latest_type.isnot(None),
        latest_type == literal(TERMINAL_RUNTIME_RELEASE_EVENT_TYPE),
    )


async def has_terminal_runtime_released_event(
    session: AsyncSession,
    workspace_id: str,
) -> bool:
    """Return True if a ``terminal_runtime_released`` event exists for *workspace_id* and has not been revoked.

    The latest event among all release and revoke events is determined by
    ordering on ``(occurred_at DESC, event_order DESC)``; if the latest
    event's type is ``terminal_runtime_released``, the runtime is effectively
    released.
    """
    expr = terminal_runtime_effectively_released_expr(workspace_id=workspace_id)
    stmt = select(expr)
    row = (await session.execute(stmt)).one()
    return bool(row[0])


SECRET_LEASE_AUDIT_EVENT_TYPE: Final = "workspace.secret_lease"
SECRET_LEASE_AUDIT_SCHEMA: Final = "secret_lease_audit.v1"
SECRET_LEASE_STATUS_ISSUED: Final = "issued"
SECRET_LEASE_STATUS_MOUNTED: Final = "mounted"
SECRET_LEASE_STATUS_EXPIRED: Final = "expired"
SECRET_LEASE_STATUS_REVOKED: Final = "revoked"
_SECRET_LEASE_ACTIVE_STATUSES: Final = (
    SECRET_LEASE_STATUS_ISSUED,
    SECRET_LEASE_STATUS_MOUNTED,
)
_SECRET_LEASE_REVOCABLE_STATUSES: Final = (
    SECRET_LEASE_STATUS_ISSUED,
    SECRET_LEASE_STATUS_MOUNTED,
    SECRET_LEASE_STATUS_EXPIRED,
)


def _sanitize_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Redact sensitive values from a metadata mapping."""
    redacted = redact_audit_value(dict(metadata))
    return redacted if isinstance(redacted, dict) else {}


def _secret_lease_declaration_key(
    secret_name: str,
    kind: str,
    target: str,
) -> tuple[str, str, str]:
    """Return the composite key tuple for a secret-lease declaration."""
    return (secret_name, kind, target)


def _declared_lease_requires_reissue(
    lease: WorkspaceSecretLease,
    issue: SecretLeaseIssue,
) -> bool:
    """Return True if an existing lease must be re-issued to match the declaration."""
    if lease.status not in _SECRET_LEASE_ACTIVE_STATUSES:
        return True
    return (
        lease.attempt_id != issue.attempt_id
        or lease.mode != issue.mode
        or lease.required != issue.required
        or lease.provider != issue.provider
        or lease.ref_digest != issue.ref_digest
        or lease.issue_metadata != _sanitize_metadata(issue.issue_metadata)
    )


def _reissue_declared_lease(
    lease: WorkspaceSecretLease,
    *,
    issue: SecretLeaseIssue,
    now: datetime,
) -> None:
    """Mutate an existing lease in-place so it reflects a fresh issue."""
    lease.attempt_id = issue.attempt_id
    lease.mode = issue.mode
    lease.required = issue.required
    lease.provider = issue.provider
    lease.ref_digest = issue.ref_digest
    lease.status = SECRET_LEASE_STATUS_ISSUED
    lease.issued_at = now
    lease.expires_at = issue.expires_at
    lease.mounted_at = None
    lease.revoked_at = None
    lease.revoke_reason_code = None
    lease.issue_metadata = _sanitize_metadata(issue.issue_metadata)
    lease.mount_metadata = {}


def _lease_audit_payload(
    lease: WorkspaceSecretLease,
    *,
    action: str,
    reason_code: str,
    occurred_at: datetime,
) -> dict[str, Any]:
    """Build the JSON payload for a secret-lease audit event."""
    payload: dict[str, Any] = {
        "schema": SECRET_LEASE_AUDIT_SCHEMA,
        "lease_id": lease.id,
        "action": action,
        "reason_code": reason_code,
        "workspace_id": lease.workspace_id,
        "attempt_id": lease.attempt_id,
        "secret_name": lease.secret_name,
        "kind": lease.kind,
        "target": lease.target,
        "mode": lease.mode,
        "required": lease.required,
        "provider": lease.provider,
        "ref_digest": lease.ref_digest,
        "status": lease.status,
        "issued_at": lease.issued_at.isoformat(),
        "mounted_at": lease.mounted_at.isoformat() if lease.mounted_at else None,
        "expires_at": lease.expires_at.isoformat() if lease.expires_at else None,
        "revoked_at": lease.revoked_at.isoformat() if lease.revoked_at else None,
        "revoke_reason_code": lease.revoke_reason_code,
        "occurred_at": occurred_at.isoformat(),
    }
    if lease.issue_metadata:
        payload["issue_metadata"] = _sanitize_metadata(lease.issue_metadata)
    if lease.mount_metadata:
        payload["mount_metadata"] = _sanitize_metadata(lease.mount_metadata)
    return {key: value for key, value in payload.items() if value is not None}


def _group_leases_by_workspace(
    leases: Iterable[WorkspaceSecretLease],
) -> dict[str, list[WorkspaceSecretLease]]:
    """Group secret leases into a dict keyed by workspace ID."""
    grouped: dict[str, list[WorkspaceSecretLease]] = {}
    for lease in leases:
        grouped.setdefault(lease.workspace_id, []).append(lease)
    return grouped


def _workspace_status_value(status: WorkspaceStatus | str) -> str:
    """Coerce a WorkspaceStatus enum or raw string into its string value."""
    return status.value if isinstance(status, WorkspaceStatus) else status


def _matches_pr_adoption_identity(
    workspace: Workspace,
    *,
    task_external_id: str,
    idempotency_key: str,
    task_kind: str,
    repo_slug: str,
    pr_number: int,
) -> bool:
    """Return True if the workspace matches a PR adoption identity."""
    if workspace.task_kind == task_kind and workspace.task_external_id == task_external_id:
        return True

    adoption = _workspace_pr_adoption_policy(workspace)
    if not adoption:
        return False
    if workspace.task_kind != task_kind and workspace.idempotency_key != idempotency_key:
        return False

    adoption_repo = adoption.get("repo_slug")
    adoption_pr_number = adoption.get("pr_number")
    if not isinstance(adoption_pr_number, str | int):
        return False
    try:
        normalized_pr_number = int(adoption_pr_number)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(adoption_repo, str)
        and adoption_repo.lower() == repo_slug.lower()
        and normalized_pr_number == pr_number
    )


def _workspace_pr_adoption_policy(workspace: Workspace) -> Mapping[str, Any]:
    """Extract the pr_adoption dict from a workspace's task_policy, if present."""
    policy = workspace.task_policy
    adoption = policy.get("pr_adoption") if isinstance(policy, dict) else None
    return adoption if isinstance(adoption, Mapping) else {}


def _candidate_terminal_close_reason(status: WorkspaceStatus) -> str:
    """Return a close-reason string corresponding to *status* for terminal cleanup."""
    if status == WorkspaceStatus.failed:
        return "WORKSPACE_FAILED"
    if status == WorkspaceStatus.cancelled:
        return "WORKSPACE_CANCELLED"
    return f"WORKSPACE_{status.value.upper()}"


def _releases_resource_reservation(status: WorkspaceStatus) -> bool:
    """Return True if a workspace in *status* releases its resource reservation."""
    return status in {
        WorkspaceStatus.completed,
        WorkspaceStatus.failed,
        WorkspaceStatus.cancelled,
        WorkspaceStatus.destroyed,
    }


def _owned_paths_overlap(left: str, right: str) -> bool:
    """Return True when two owned paths overlap (exact, ancestor, or wildcard)."""
    return _owned_path_overlap_match(left, right) is not None


def _owned_path_overlap_match(left: str, right: str) -> OwnedPathOverlapMatch | None:
    """Return a detailed overlap match between two owned paths, or None."""
    left_path = _normalize_owned_path(left)
    right_path = _normalize_owned_path(right)
    if left_path == "" or right_path == "":
        return None
    if left_path == right_path:
        return _owned_path_match(
            left,
            right,
            normalized_left_path=left_path,
            normalized_right_path=right_path,
            match_reason_code=OWNED_PATH_EXACT_MATCH_REASON,
            explanation=f"Owned paths normalize to the same path: {left_path}.",
        )
    if _literal_paths_overlap(left_path, right_path):
        ancestor, descendant = (
            (left_path, right_path)
            if _is_descendant(left_path, right_path)
            else (right_path, left_path)
        )
        return _owned_path_match(
            left,
            right,
            normalized_left_path=left_path,
            normalized_right_path=right_path,
            match_reason_code=OWNED_PATH_ANCESTOR_MATCH_REASON,
            explanation=f"One owned path contains the other: {ancestor} -> {descendant}.",
        )

    left_prefix = _wildcard_prefix(left_path)
    right_prefix = _wildcard_prefix(right_path)
    if left_prefix is not None and _wildcard_prefix_overlaps(left_prefix, right_path):
        return _wildcard_owned_path_match(
            left,
            right,
            normalized_left_path=left_path,
            normalized_right_path=right_path,
        )
    if right_prefix is not None and _wildcard_prefix_overlaps(right_prefix, left_path):
        return _wildcard_owned_path_match(
            left,
            right,
            normalized_left_path=left_path,
            normalized_right_path=right_path,
        )
    if (
        left_prefix is not None
        and right_prefix is not None
        and _wildcard_prefixes_overlap(left_prefix, right_prefix)
    ):
        return _wildcard_owned_path_match(
            left,
            right,
            normalized_left_path=left_path,
            normalized_right_path=right_path,
        )
    return None


def _owned_path_match(
    left: str,
    right: str,
    *,
    normalized_left_path: str,
    normalized_right_path: str,
    match_reason_code: str,
    explanation: str,
) -> OwnedPathOverlapMatch:
    """Construct an OwnedPathOverlapMatch from raw and normalized path data."""
    return OwnedPathOverlapMatch(
        left_path=left,
        right_path=right,
        normalized_left_path=normalized_left_path,
        normalized_right_path=normalized_right_path,
        match_reason_code=match_reason_code,
        explanation=explanation,
    )


def _wildcard_owned_path_match(
    left: str,
    right: str,
    *,
    normalized_left_path: str,
    normalized_right_path: str,
) -> OwnedPathOverlapMatch:
    """Construct a wildcard-specific owned-path overlap match."""
    return _owned_path_match(
        left,
        right,
        normalized_left_path=normalized_left_path,
        normalized_right_path=normalized_right_path,
        match_reason_code=OWNED_PATH_WILDCARD_MATCH_REASON,
        explanation=f"Wildcard owned-path prefixes overlap: {left} <-> {right}.",
    )


def _owned_path_conflict_advisory_lock_key(*, repo_url: str, branch_base: str) -> int:
    """Return a stable int64 key for a PostgreSQL advisory lock scoped to an owned-path conflict."""
    digest = hashlib.sha256(
        f"awf:owned-path-conflicts\x00{repo_url}\x00{branch_base}".encode()
    ).digest()
    unsigned = int.from_bytes(digest[:8], byteorder="big", signed=False)
    if unsigned >= 1 << 63:
        return unsigned - (1 << 64)
    return unsigned


def _operation_idempotency_advisory_lock_key(key: str) -> int:
    """Return a stable int64 key for a PostgreSQL advisory lock scoped to operation idempotency."""
    digest = hashlib.sha256(f"awf:operation-idempotency\x00{key}".encode()).digest()
    unsigned = int.from_bytes(digest[:8], byteorder="big", signed=False)
    if unsigned >= 1 << 63:
        return unsigned - (1 << 64)
    return unsigned


def _callback_subscription_idempotency_advisory_lock_key(key: str) -> int:
    """Return a stable int64 key for a PostgreSQL advisory lock scoped to callback-subscription idempotency."""
    digest = hashlib.sha256(f"awf:callback-subscription-idempotency\x00{key}".encode()).digest()
    unsigned = int.from_bytes(digest[:8], byteorder="big", signed=False)
    if unsigned >= 1 << 63:
        return unsigned - (1 << 64)
    return unsigned


def _workspace_idempotency_advisory_lock_key(key: str) -> int:
    """Return a stable int64 key for a PostgreSQL advisory lock scoped to workspace idempotency."""
    digest = hashlib.sha256(f"awf:workspace-idempotency\x00{key}".encode()).digest()
    unsigned = int.from_bytes(digest[:8], byteorder="big", signed=False)
    if unsigned >= 1 << 63:
        return unsigned - (1 << 64)
    return unsigned


def _host_port_admission_advisory_lock_key(host_port: int) -> int:
    """Return a stable int64 key for a PostgreSQL advisory lock scoped to *host_port*."""
    digest = hashlib.sha256(f"awf:host-port-admission\x00{host_port}".encode()).digest()
    unsigned = int.from_bytes(digest[:8], byteorder="big", signed=False)
    if unsigned >= 1 << 63:
        return unsigned - (1 << 64)
    return unsigned


def owned_paths_overlap(left: str, right: str) -> bool:
    """Return ``True`` when two repository owned paths overlap."""

    return _owned_paths_overlap(left, right)


def owned_path_overlap_match(left: str, right: str) -> OwnedPathOverlapMatch | None:
    """Return a detailed overlap match, or ``None`` if the paths do not overlap."""

    return _owned_path_overlap_match(left, right)


def _normalize_owned_path(path: str) -> str:
    """Normalize repository owned-path entries through the shared classifier."""
    return normalize_owned_path(path)


def _literal_paths_overlap(left: str, right: str) -> bool:
    """Return True if two literal (non-wildcard) owned paths overlap."""
    return left == right or _is_descendant(left, right) or _is_descendant(right, left)


def _is_descendant(parent: str, child: str) -> bool:
    """Return True if *child* is a subdirectory of *parent*."""
    return child.startswith(f"{parent.rstrip('/')}/")


def _wildcard_prefix(path: str) -> str | None:
    """Return the literal prefix before the first wildcard character, or None."""
    wildcard_indexes = [
        index for index in (path.find("*"), path.find("?"), path.find("[")) if index >= 0
    ]
    if not wildcard_indexes:
        return None
    return path[: min(wildcard_indexes)]


def _wildcard_prefix_overlaps(prefix: str, path: str) -> bool:
    """Return True if a wildcard prefix could match a given literal path."""
    if prefix == "":
        return True
    if path.startswith(prefix):
        return True
    return _literal_paths_overlap(prefix.rstrip("/"), path.rstrip("/"))


def _wildcard_prefixes_overlap(left: str, right: str) -> bool:
    """Return True if two wildcard prefixes could match a common path."""
    if left == "" or right == "":
        return True
    if left.startswith(right) or right.startswith(left):
        return True
    return _literal_paths_overlap(left.rstrip("/"), right.rstrip("/"))


def _operation_result_with_log_stream_refs(
    result: dict[str, Any] | None,
    *,
    log_stream_refs: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Merge log_stream_refs into an operation result dict."""
    if log_stream_refs is None:
        return result
    merged_result = dict(result or {})
    existing_refs = merged_result.get("log_stream_refs")
    merged_refs: dict[str, Any] = {}
    if isinstance(existing_refs, Mapping):
        merged_refs.update(existing_refs)
    merged_refs.update(dict(log_stream_refs))
    merged_result["log_stream_refs"] = merged_refs
    return merged_result
