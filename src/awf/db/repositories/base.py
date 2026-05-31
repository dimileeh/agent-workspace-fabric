"""Common database repository helper definitions, constants, and utilities."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol, cast

from sqlalchemy import (
    DateTime,
    Float,
    Integer,
    Numeric,
    and_,
    bindparam,
    case,
    func,
    literal,
    or_,
    select,
    text,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
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
    FailureReason,
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
    WorkspaceSecretLease,
)
from awf.service.scheduler import (
    AGE_BOOST_INTERVAL_SECONDS,
    AGE_BOOST_MAX,
    HUMAN_BOOST_MAX,
    POLICY_INT_TEXT_PATTERN,
    RETRY_BONUS_INFRASTRUCTURE_FAILURE,
    SCHEDULER_POLICY_KEY,
    TASK_CLASS_BIASES,
    TASK_CLASS_PRIORITIES,
    SchedulerOrderCursor,
)


class _SchedulerAgeBoostExprBuilder(Protocol):
    def __call__(
        self,
        *,
        scoring_at: datetime,
        workspace_entity: Any,
    ) -> ColumnElement[Any]: ...


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
HOST_PORT_TERMINAL_RELEASE_STATUSES: Final[tuple[str, ...]] = (
    WorkspaceStatus.failed.value,
    WorkspaceStatus.cancelled.value,
    WorkspaceStatus.completed.value,
    WorkspaceStatus.destroyed.value,
)
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
_POSTGRES_INTEGER_MIN: Final = -(2**31)
_POSTGRES_INTEGER_MAX: Final = 2**31 - 1


@dataclass(frozen=True)
class OwnedPathOverlap:
    workspace_id: str
    existing_path: str
    requested_path: str


@dataclass(frozen=True)
class OwnedPathConflict:
    workspace_id: str
    existing_path: str
    requested_path: str


@dataclass(frozen=True)
class HostPortConflict:
    host_port: int
    workspace_id: str


@dataclass(frozen=True)
class OwnedPathOverlapMatch:
    left_path: str
    right_path: str
    normalized_left_path: str
    normalized_right_path: str
    match_reason_code: str
    explanation: str


@dataclass(frozen=True)
class WorkspaceEventCreate:
    event_type: str
    reason_code: str | None = None
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class _IssuedSecretLease:
    lease: WorkspaceSecretLease
    issue_event_required: bool


@dataclass(frozen=True)
class QueueDecisionCreate:
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
    return {
        key: value
        for key, value in command.items()
        if key not in {"evidence_status", "evidence_reason_code", "evidence_source_run_id"}
    }


def _coverage_metadata_has_pytest_failures(coverage: Mapping[str, Any]) -> bool:
    node_ids = coverage.get("failing_test_node_ids")
    evidence = coverage.get("failing_test_evidence")
    return bool(node_ids or evidence)


def resolve_session_dialect_name(
    session: AsyncSession,
    dialect_name: str | None,
) -> str | None:
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
    if dialect_name == "postgresql":
        return (
            postgresql_insert(WorkspaceSecretLease)
            .on_conflict_do_nothing(index_elements=_SECRET_LEASE_DECLARATION_CONFLICT_COLUMNS)
            .returning(WorkspaceSecretLease.id)
        )
    return None


def _callback_subscription_insert_if_absent_stmt(dialect_name: str | None) -> Any | None:
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
    if dialect_name == "postgresql":
        return (
            postgresql_insert(ProviderModelCircuitBreaker)
            .on_conflict_do_nothing(index_elements=_PROVIDER_MODEL_CIRCUIT_BREAKER_CONFLICT_COLUMNS)
            .returning(ProviderModelCircuitBreaker.id)
        )
    return None


def _pr_feedback_resolution_upsert_stmt(dialect_name: str | None) -> Any | None:
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
    return value.strip().lower()


def _normalize_repository_key(value: str) -> str:
    return value.strip().lower()


def _circuit_breaker_expired(
    breaker: ProviderModelCircuitBreaker,
    now: datetime,
) -> bool:
    cooldown_until = breaker.cooldown_until
    if breaker.state != "open" or cooldown_until is None:
        return False
    return _as_utc_naive(cooldown_until) <= _as_utc_naive(now)


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def empty_resource_reservation_totals() -> dict[str, float | int]:
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


TERMINAL_RUNTIME_RELEASE_EVENT_TYPE: Final = "workspace.terminal_runtime_released"
TERMINAL_RUNTIME_RELEASE_REASON_CODE: Final = "TERMINAL_RUNTIME_RELEASED"
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
    redacted = redact_audit_value(dict(metadata))
    return redacted if isinstance(redacted, dict) else {}


def _secret_lease_declaration_key(
    secret_name: str,
    kind: str,
    target: str,
) -> tuple[str, str, str]:
    return (secret_name, kind, target)


def _declared_lease_requires_reissue(
    lease: WorkspaceSecretLease,
    issue: SecretLeaseIssue,
) -> bool:
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
    grouped: dict[str, list[WorkspaceSecretLease]] = {}
    for lease in leases:
        grouped.setdefault(lease.workspace_id, []).append(lease)
    return grouped


def _workspace_status_value(status: WorkspaceStatus | str) -> str:
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
    policy = workspace.task_policy
    adoption = policy.get("pr_adoption") if isinstance(policy, dict) else None
    return adoption if isinstance(adoption, Mapping) else {}


def _candidate_terminal_close_reason(status: WorkspaceStatus) -> str:
    if status == WorkspaceStatus.failed:
        return "WORKSPACE_FAILED"
    if status == WorkspaceStatus.cancelled:
        return "WORKSPACE_CANCELLED"
    return f"WORKSPACE_{status.value.upper()}"


def _releases_resource_reservation(status: WorkspaceStatus) -> bool:
    return status in {
        WorkspaceStatus.completed,
        WorkspaceStatus.failed,
        WorkspaceStatus.cancelled,
        WorkspaceStatus.destroyed,
    }


@dataclass(frozen=True)
class SchedulerOrderExpressions:
    class_priority: ColumnElement[Any]
    effective_score: ColumnElement[Any]


def _schedulable_workspace_ids_stmt(
    *,
    status: WorkspaceStatus,
    limit: int | None,
    exclude_ids: set[str] | None = None,
    node_id: str | None = None,
    after: SchedulerOrderCursor | None = None,
    scoring_at: datetime,
    dialect_name: str | None,
    skip_locked: bool,
    claim_cutoff: datetime | None = None,
) -> Select[tuple[Workspace]]:
    order_expressions = scheduler_order_expressions(
        scoring_at=scoring_at,
        dialect_name=dialect_name,
    )
    stmt = select(Workspace).where(Workspace.status == status.value)
    if node_id is not None:
        stmt = stmt.where(or_(Workspace.node_id == node_id, Workspace.node_id.is_(None)))
    if status == WorkspaceStatus.monitoring_pr and claim_cutoff is not None:
        stmt = stmt.where(
            or_(
                Workspace.monitor_claim_expires_at.is_(None),
                Workspace.monitor_claim_expires_at <= claim_cutoff,
            )
        )
    if exclude_ids:
        stmt = stmt.where(~Workspace.id.in_(sorted(exclude_ids)))
    if after is not None:
        stmt = stmt.where(
            _scheduler_after_cursor_condition(
                order_expressions,
                after,
                dialect_name=dialect_name,
            )
        )
    stmt = stmt.order_by(
        order_expressions.class_priority.desc(),
        order_expressions.effective_score.desc(),
        Workspace.created_at.asc(),
        Workspace.id.asc(),
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    if skip_locked:
        stmt = stmt.with_for_update(skip_locked=True, of=Workspace)
    return stmt


def _scheduler_scoring_time(
    *,
    after: SchedulerOrderCursor | None,
    scoring_at: datetime | None,
) -> datetime:
    if after is None:
        return scoring_at or datetime.now(UTC)
    if scoring_at is not None and scoring_at != after.scoring_at:
        raise ValueError("scoring_at must match after.scoring_at for scheduler pagination")
    return after.scoring_at


def scheduler_order_expressions(
    *,
    scoring_at: datetime,
    dialect_name: str | None,
    workspace_entity: Any = Workspace,
) -> SchedulerOrderExpressions:
    class_priority = _task_class_case(TASK_CLASS_PRIORITIES, workspace_entity=workspace_entity)
    class_bias = _task_class_case(TASK_CLASS_BIASES, workspace_entity=workspace_entity)
    base_priority = _bounded_scheduler_int_expr(
        func.coalesce(
            _scheduler_json_int_expr(
                (SCHEDULER_POLICY_KEY, "base_priority"),
                dialect_name,
                workspace_entity=workspace_entity,
            ),
            _scheduler_json_int_expr(
                ("priority",),
                dialect_name,
                workspace_entity=workspace_entity,
            ),
            0,
        ),
        lower=0,
        upper=100,
    )
    human_boost = _bounded_scheduler_int_expr(
        func.coalesce(
            _scheduler_json_int_expr(
                (SCHEDULER_POLICY_KEY, "human_boost"),
                dialect_name,
                workspace_entity=workspace_entity,
            ),
            _scheduler_json_int_expr(
                (SCHEDULER_POLICY_KEY, "human_escalation_boost"),
                dialect_name,
                workspace_entity=workspace_entity,
            ),
            _scheduler_json_int_expr(
                ("human_boost",),
                dialect_name,
                workspace_entity=workspace_entity,
            ),
            0,
        ),
        lower=0,
        upper=HUMAN_BOOST_MAX,
    )
    parent_failure_reason = func.coalesce(
        _scheduler_json_string_expr(
            (SCHEDULER_POLICY_KEY, "parent_failure_reason"),
            dialect_name,
            workspace_entity=workspace_entity,
        ),
        _scheduler_json_string_expr(
            ("provider_recovery_state", "parent_failure_reason"),
            dialect_name,
            workspace_entity=workspace_entity,
        ),
    )
    retry_bonus = case(
        (
            parent_failure_reason == FailureReason.infrastructure_failure.value,
            RETRY_BONUS_INFRASTRUCTURE_FAILURE,
        ),
        else_=0,
    )
    age_boost = _scheduler_age_boost_expr(
        scoring_at=scoring_at,
        dialect_name=dialect_name,
        workspace_entity=workspace_entity,
    )
    return SchedulerOrderExpressions(
        class_priority=class_priority,
        effective_score=base_priority + class_bias + age_boost + retry_bonus + human_boost,
    )


def _scheduler_cursor_order_expressions(
    *,
    after: SchedulerOrderCursor,
    dialect_name: str | None,
) -> SchedulerOrderExpressions:
    cursor_workspace = aliased(Workspace, name="scheduler_cursor_workspace")
    cursor_order = scheduler_order_expressions(
        scoring_at=after.scoring_at,
        dialect_name=dialect_name,
        workspace_entity=cursor_workspace,
    )
    cursor_class_priority = func.coalesce(
        select(cursor_order.class_priority)
        .where(cursor_workspace.id == after.workspace_id)
        .scalar_subquery(),
        literal(after.class_priority),
    ).label("class_priority")
    cursor_effective_score = func.coalesce(
        select(cursor_order.effective_score)
        .where(cursor_workspace.id == after.workspace_id)
        .scalar_subquery(),
        literal(after.effective_score),
    ).label("effective_score")
    cursor_order_cte = select(
        cursor_class_priority,
        cursor_effective_score,
    ).cte("scheduler_cursor_order")
    return SchedulerOrderExpressions(
        class_priority=cursor_order_cte.c.class_priority,
        effective_score=cursor_order_cte.c.effective_score,
    )


def _task_class_case(
    values: Mapping[str, int],
    *,
    workspace_entity: Any = Workspace,
) -> ColumnElement[Any]:
    return case(
        *(
            (workspace_entity.task_class == task_class, value)
            for task_class, value in values.items()
        ),
        else_=0,
    )


def _scheduler_json_path_expr(
    path: tuple[str, ...],
    *,
    workspace_entity: Any = Workspace,
) -> ColumnElement[Any]:
    expr: Any = workspace_entity.task_policy
    for key in path:
        expr = expr[key]
    return cast("ColumnElement[Any]", expr)


def _scheduler_json_string_expr(
    path: tuple[str, ...],
    dialect_name: str | None,
    *,
    workspace_entity: Any = Workspace,
) -> ColumnElement[Any]:
    del dialect_name
    return func.nullif(
        func.trim(_scheduler_json_path_expr(path, workspace_entity=workspace_entity).as_string()),
        "",
    )


def _scheduler_json_int_expr(
    path: tuple[str, ...],
    dialect_name: str | None,
    *,
    workspace_entity: Any = Workspace,
) -> ColumnElement[Any]:
    if dialect_name == "postgresql":
        text_value = func.nullif(
            func.trim(
                _scheduler_json_path_expr(path, workspace_entity=workspace_entity).as_string()
            ),
            "",
        )
        postgres_whole_text = func.replace(func.split_part(text_value, ".", 1), "-", "")
        integer_text: ColumnElement[Any] = text_value.op("~")(POLICY_INT_TEXT_PATTERN)
        max_str_digits = sys.get_int_max_str_digits()
        if max_str_digits > 0:
            integer_text = and_(integer_text, func.length(postgres_whole_text) <= max_str_digits)
        numeric_value = case(
            (
                integer_text,
                sql_cast(text_value, Numeric),
            ),
            else_=None,
        )
        clamped_value = _bounded_scheduler_int_expr(
            numeric_value,
            lower=_POSTGRES_INTEGER_MIN,
            upper=_POSTGRES_INTEGER_MAX,
        )
        return sql_cast(clamped_value, Integer)
    if dialect_name == "sqlite":
        json_path = _sqlite_json_path(path)
        json_type = func.json_type(workspace_entity.task_policy, json_path)
        json_value = func.json_extract(workspace_entity.task_policy, json_path)
        text_value = func.trim(json_value)
        decimal_pos = func.instr(text_value, ".")
        whole_text = case(
            (decimal_pos > 0, func.substr(text_value, 1, decimal_pos - 1)),
            else_=text_value,
        )
        fraction_text = func.substr(text_value, decimal_pos + 1)
        unsigned_text_int = and_(
            whole_text != "",
            whole_text.op("GLOB")("[0-9]*"),
            ~whole_text.op("GLOB")("*[^0-9]*"),
        )
        signed_text_int = and_(
            whole_text.op("GLOB")("-[0-9]*"),
            ~func.substr(whole_text, 2).op("GLOB")("*[^0-9]*"),
        )
        integer_valued_text = and_(
            or_(unsigned_text_int, signed_text_int),
            or_(
                decimal_pos == 0,
                and_(
                    fraction_text != "",
                    ~fraction_text.op("GLOB")("*[^0]*"),
                ),
            ),
        )
        max_str_digits = sys.get_int_max_str_digits()
        if max_str_digits > 0:
            sqlite_whole_digits = func.replace(whole_text, "-", "")
            integer_valued_text = and_(
                integer_valued_text,
                func.length(sqlite_whole_digits) <= max_str_digits,
            )
        return case(
            (json_type == "integer", sql_cast(json_value, Integer)),
            (
                and_(
                    json_type == "real",
                    json_value == sql_cast(sql_cast(json_value, Integer), Numeric),
                ),
                sql_cast(json_value, Integer),
            ),
            (
                and_(json_type == "text", integer_valued_text),
                sql_cast(json_value, Integer),
            ),
            else_=None,
        )
    return cast(
        "ColumnElement[Any]",
        _scheduler_json_path_expr(path, workspace_entity=workspace_entity).as_integer(),
    )


def _sqlite_json_path(path: tuple[str, ...]) -> str:
    return "$." + ".".join(path)


def _bounded_scheduler_int_expr(
    value: ColumnElement[Any],
    *,
    lower: int,
    upper: int,
) -> ColumnElement[Any]:
    return case(
        (value < lower, lower),
        (value > upper, upper),
        else_=value,
    )


def _scheduler_age_boost_expr(
    *,
    scoring_at: datetime,
    dialect_name: str | None,
    workspace_entity: Any = Workspace,
) -> ColumnElement[Any]:
    builder = _SCHEDULER_SQL_AGE_BOOST_EXPR_BUILDERS.get(dialect_name or "")
    if builder is None:
        return _scheduler_zero_age_boost_expr()
    return builder(scoring_at=scoring_at, workspace_entity=workspace_entity)


def _postgresql_scheduler_age_boost_expr(
    *,
    scoring_at: datetime,
    workspace_entity: Any,
) -> ColumnElement[Any]:
    scoring_time = sql_cast(literal(scoring_at), DateTime(timezone=True))
    return case(
        *(
            (
                workspace_entity.created_at
                <= scoring_time
                - _postgresql_interval_seconds_expr(boost * AGE_BOOST_INTERVAL_SECONDS),
                boost,
            )
            for boost in range(AGE_BOOST_MAX, 0, -1)
        ),
        else_=0,
    )


def _postgresql_interval_seconds_expr(seconds: int) -> ColumnElement[Any]:
    return cast(
        "ColumnElement[Any]",
        text("make_interval(secs => :seconds)").bindparams(
            bindparam("seconds", float(seconds), type_=Float, unique=True),
        ),
    )


def _sqlite_scheduler_age_boost_expr(
    *,
    scoring_at: datetime,
    workspace_entity: Any,
) -> ColumnElement[Any]:
    wait_seconds = sql_cast(func.strftime("%s", literal(scoring_at)), Integer) - sql_cast(
        func.strftime("%s", workspace_entity.created_at),
        Integer,
    )
    intervals = sql_cast(wait_seconds / AGE_BOOST_INTERVAL_SECONDS, Integer)
    return _bounded_scheduler_int_expr(
        func.coalesce(intervals, 0),
        lower=0,
        upper=AGE_BOOST_MAX,
    )


def _scheduler_zero_age_boost_expr() -> ColumnElement[Any]:
    return _bounded_scheduler_int_expr(
        func.coalesce(cast("ColumnElement[Any]", literal(0)), 0),
        lower=0,
        upper=AGE_BOOST_MAX,
    )


_SCHEDULER_SQL_AGE_BOOST_EXPR_BUILDERS: Final[Mapping[str, _SchedulerAgeBoostExprBuilder]] = {
    "postgresql": _postgresql_scheduler_age_boost_expr,
    "sqlite": _sqlite_scheduler_age_boost_expr,
}
SCHEDULER_SQL_AGE_BOOST_DIALECTS: Final[frozenset[str]] = frozenset(
    _SCHEDULER_SQL_AGE_BOOST_EXPR_BUILDERS
)


def _scheduler_after_cursor_condition(
    order_expressions: SchedulerOrderExpressions,
    after: SchedulerOrderCursor,
    *,
    dialect_name: str | None,
) -> ColumnElement[bool]:
    cursor_order = _scheduler_cursor_order_expressions(
        after=after,
        dialect_name=dialect_name,
    )
    return or_(
        order_expressions.class_priority < cursor_order.class_priority,
        and_(
            order_expressions.class_priority == cursor_order.class_priority,
            order_expressions.effective_score < cursor_order.effective_score,
        ),
        and_(
            order_expressions.class_priority == cursor_order.class_priority,
            order_expressions.effective_score == cursor_order.effective_score,
            Workspace.created_at > after.queued_at,
        ),
        and_(
            order_expressions.class_priority == cursor_order.class_priority,
            order_expressions.effective_score == cursor_order.effective_score,
            Workspace.created_at == after.queued_at,
            Workspace.id > after.workspace_id,
        ),
    )


def _owned_paths_overlap(left: str, right: str) -> bool:
    return _owned_path_overlap_match(left, right) is not None


def _owned_path_overlap_match(left: str, right: str) -> OwnedPathOverlapMatch | None:
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
    return _owned_path_match(
        left,
        right,
        normalized_left_path=normalized_left_path,
        normalized_right_path=normalized_right_path,
        match_reason_code=OWNED_PATH_WILDCARD_MATCH_REASON,
        explanation=f"Wildcard owned-path prefixes overlap: {left} <-> {right}.",
    )


def _owned_path_conflict_advisory_lock_key(*, repo_url: str, branch_base: str) -> int:
    digest = hashlib.sha256(
        f"awf:owned-path-conflicts\x00{repo_url}\x00{branch_base}".encode()
    ).digest()
    unsigned = int.from_bytes(digest[:8], byteorder="big", signed=False)
    if unsigned >= 1 << 63:
        return unsigned - (1 << 64)
    return unsigned


def _operation_idempotency_advisory_lock_key(key: str) -> int:
    digest = hashlib.sha256(f"awf:operation-idempotency\x00{key}".encode()).digest()
    unsigned = int.from_bytes(digest[:8], byteorder="big", signed=False)
    if unsigned >= 1 << 63:
        return unsigned - (1 << 64)
    return unsigned


def _callback_subscription_idempotency_advisory_lock_key(key: str) -> int:
    digest = hashlib.sha256(f"awf:callback-subscription-idempotency\x00{key}".encode()).digest()
    unsigned = int.from_bytes(digest[:8], byteorder="big", signed=False)
    if unsigned >= 1 << 63:
        return unsigned - (1 << 64)
    return unsigned


def _workspace_idempotency_advisory_lock_key(key: str) -> int:
    digest = hashlib.sha256(f"awf:workspace-idempotency\x00{key}".encode()).digest()
    unsigned = int.from_bytes(digest[:8], byteorder="big", signed=False)
    if unsigned >= 1 << 63:
        return unsigned - (1 << 64)
    return unsigned


def owned_paths_overlap(left: str, right: str) -> bool:
    return _owned_paths_overlap(left, right)


def owned_path_overlap_match(left: str, right: str) -> OwnedPathOverlapMatch | None:
    return _owned_path_overlap_match(left, right)


def _normalize_owned_path(path: str) -> str:
    """Normalize repository owned-path entries through the shared classifier."""
    return normalize_owned_path(path)


def _literal_paths_overlap(left: str, right: str) -> bool:
    return left == right or _is_descendant(left, right) or _is_descendant(right, left)


def _is_descendant(parent: str, child: str) -> bool:
    return child.startswith(f"{parent.rstrip('/')}/")


def _wildcard_prefix(path: str) -> str | None:
    wildcard_indexes = [
        index for index in (path.find("*"), path.find("?"), path.find("[")) if index >= 0
    ]
    if not wildcard_indexes:
        return None
    return path[: min(wildcard_indexes)]


def _wildcard_prefix_overlaps(prefix: str, path: str) -> bool:
    if prefix == "":
        return True
    if path.startswith(prefix):
        return True
    return _literal_paths_overlap(prefix.rstrip("/"), path.rstrip("/"))


def _wildcard_prefixes_overlap(left: str, right: str) -> bool:
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
