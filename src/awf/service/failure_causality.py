"""Helpers for preserving primary workspace failure evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import and_, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from awf.db.dialect import SESSION_DIALECT_NAME_KEY
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import ValidationRun, Workspace, WorkspaceEvent

PRIMARY_FAILURE_KEY = "primary_failure"
SECONDARY_FAILURE_KEY = "secondary_failure"
SECONDARY_FAILURES_KEY = "secondary_failures"
SECONDARY_FAILURE_RECORDED_EVENT_TYPE = "workspace.secondary_failure_recorded"
_SECONDARY_FAILURE_HISTORY_LIMIT: Final = 20
_PRIMARY_FAILURE_MESSAGE_MAX_LENGTH: Final = 2048
_IGNORED_PRIMARY_VALIDATION_REASON_CODES = frozenset({"STALE_CALLBACK_IGNORED"})
_FAILURE_CAUSALITY_EVENT_TYPES: Final[tuple[str, ...]] = (
    "workspace.state_changed",
    SECONDARY_FAILURE_RECORDED_EVENT_TYPE,
)
_FAILURE_EPOCH_RESET_STATES = frozenset(
    {
        WorkspaceStatus.provisioning.value,
        WorkspaceStatus.ready.value,
        WorkspaceStatus.running.value,
        WorkspaceStatus.validating.value,
        WorkspaceStatus.pushing.value,
        WorkspaceStatus.monitoring_pr.value,
    }
)


@dataclass(frozen=True)
class FailureCausalitySnapshot:
    """Current-epoch failure evidence that should survive secondary faults."""

    primary_failure: dict[str, Any]
    secondary_failures: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class _FailureCausalityEvents:
    """Failure events selected for primary and secondary evidence reads.

    ``primary_event`` may be a ``workspace.secondary_failure_recorded`` event.
    Those synthetic events embed the original primary failure snapshot, and the
    newest embedded copy is the most durable source after later secondary faults.
    """

    primary_event: WorkspaceEvent
    latest_failed_event: WorkspaceEvent


async def load_primary_failure_snapshot(
    session: AsyncSession,
    workspace: Workspace,
) -> dict[str, Any] | None:
    """Return durable primary failure evidence for ``workspace`` when present."""

    snapshot = await load_failure_causality_snapshot(session, workspace)
    return snapshot.primary_failure if snapshot is not None else None


async def load_failure_causality_snapshot(
    session: AsyncSession,
    workspace: Workspace,
) -> FailureCausalitySnapshot | None:
    """Return current-epoch primary failure and accumulated secondary evidence."""

    events = await _failure_causality_events_for_current_epoch(session, workspace.id)
    if events is None:
        primary_failure = _primary_failure_snapshot(
            workspace,
            latest_failed_event=None,
            latest_validation_run=None,
            event_payload=None,
        )
        return (
            FailureCausalitySnapshot(primary_failure=primary_failure)
            if primary_failure is not None
            else None
        )

    epoch_reset_event = await _latest_failure_epoch_reset_before(
        session,
        workspace.id,
        events.primary_event,
    )
    latest_validation_run = await _latest_failed_validation_run(
        session,
        workspace.id,
        epoch_started_at=(epoch_reset_event.occurred_at if epoch_reset_event is not None else None),
    )
    event_payload = _mapping(events.primary_event.payload)
    primary_failure = _primary_failure_snapshot(
        workspace,
        latest_failed_event=events.primary_event,
        latest_validation_run=latest_validation_run,
        event_payload=event_payload,
    )
    if primary_failure is None:
        return None
    secondary_failures = await _secondary_failure_history_for_current_epoch(
        session,
        workspace.id,
        primary_event=events.primary_event,
        latest_failed_event=events.latest_failed_event,
    )
    return FailureCausalitySnapshot(
        primary_failure=primary_failure,
        secondary_failures=secondary_failures,
    )


def _primary_failure_snapshot(
    workspace: Workspace,
    *,
    latest_failed_event: WorkspaceEvent | None,
    latest_validation_run: ValidationRun | None,
    event_payload: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    embedded_primary = _mapping(event_payload.get(PRIMARY_FAILURE_KEY) if event_payload else None)

    # Embedded primary payloads can outlive a resumed workspace after its live
    # failure fields are cleared, so they may enrich but not bootstrap evidence.
    has_workspace_evidence = bool(workspace.failure_reason or workspace.failure_message)
    if not has_workspace_evidence:
        return None

    snapshot: dict[str, Any] = {}
    if embedded_primary:
        snapshot.update(_jsonable_mapping(embedded_primary))

    if workspace.failure_reason and "failure_reason" not in snapshot:
        snapshot["failure_reason"] = workspace.failure_reason
    primary_failure_reason = _string(snapshot.get("failure_reason"))
    primary_is_validation_failure = primary_failure_reason == FailureReason.validation_failure.value
    has_embedded_validation_run = _mapping(snapshot.get("validation_run")) is not None
    validation_run_to_attach = (
        latest_validation_run
        if primary_is_validation_failure and not has_embedded_validation_run
        else None
    )

    message = (
        _string(snapshot.get("message"))
        or workspace.failure_message
        or _payload_str(event_payload, "message")
    )
    if message:
        snapshot["message"] = message

    event_reason_code = _payload_str(event_payload, "reason_code") or (
        latest_failed_event.reason_code if latest_failed_event else None
    )
    validation_reason_code = (
        validation_run_to_attach.reason_code if validation_run_to_attach is not None else None
    )
    reason_code = (
        validation_reason_code
        if primary_is_validation_failure and validation_reason_code
        else _string(snapshot.get("reason_code")) or event_reason_code or validation_reason_code
    )
    if reason_code:
        snapshot["reason_code"] = reason_code

    details = _mapping(snapshot.get("details")) or _mapping(
        event_payload.get("details") if event_payload else None
    )
    if details:
        snapshot["details"] = _jsonable_mapping(details)

    if validation_run_to_attach is not None:
        validation_snapshot = _validation_run_snapshot(validation_run_to_attach)
        snapshot["validation_run"] = validation_snapshot
        coverage = _mapping(validation_snapshot.get("coverage"))
        if coverage:
            snapshot["coverage"] = _jsonable_mapping(coverage)
            _copy_if_present(snapshot, coverage, "percent", "coverage_percent")
            _copy_if_present(snapshot, coverage, "minimum_percent", "coverage_minimum_percent")
            _copy_if_present(snapshot, coverage, "threshold", "coverage_threshold")
            _copy_if_present(snapshot, coverage, "failing_test_node_ids")
            _copy_if_present(snapshot, coverage, "failing_test_evidence")

    return snapshot or None


def primary_failure_reason_code(
    primary_failure: Mapping[str, Any] | None,
    *,
    fallback: str,
) -> str:
    if primary_failure is not None:
        reason_code = _string(primary_failure.get("reason_code"))
        if reason_code:
            return reason_code
    return fallback


def build_preserved_failure_payload(
    primary_failure: Mapping[str, Any],
    *,
    secondary_failure: Mapping[str, Any],
    extra: Mapping[str, Any] | None = None,
    previous_secondary_failures: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    primary = _jsonable_mapping(primary_failure)
    primary.pop(SECONDARY_FAILURE_KEY, None)
    primary.pop(SECONDARY_FAILURES_KEY, None)
    secondary = _jsonable_mapping(secondary_failure)
    payload: dict[str, Any] = dict(_jsonable_mapping(extra or {}))
    secondary_failures = _bounded_secondary_failure_history(
        [
            *[_jsonable_mapping(item) for item in previous_secondary_failures],
            secondary,
        ]
    )
    reason_code = _string(primary.get("reason_code"))
    message = _string(primary.get("message"))
    if reason_code:
        payload["reason_code"] = reason_code
    if message:
        payload["message"] = message
    details = _mapping(primary.get("details"))
    if details:
        payload["details"] = _jsonable_mapping(details)
    payload[PRIMARY_FAILURE_KEY] = primary
    payload[SECONDARY_FAILURE_KEY] = secondary
    payload[SECONDARY_FAILURES_KEY] = secondary_failures
    return payload


def attach_primary_failure(
    payload: Mapping[str, Any],
    primary_failure: Mapping[str, Any] | None,
) -> dict[str, Any]:
    updated = _jsonable_mapping(payload)
    if primary_failure is not None and PRIMARY_FAILURE_KEY not in updated:
        updated[PRIMARY_FAILURE_KEY] = _jsonable_mapping(primary_failure)
    return updated


def restore_primary_failure_row_fields(
    workspace: Workspace,
    primary_failure: Mapping[str, Any],
) -> None:
    """Restore live workspace row fields from preserved primary failure evidence."""

    failure_reason = _string(primary_failure.get("failure_reason"))
    workspace.failure_reason = failure_reason
    failure_message = _string(primary_failure.get("message"))
    workspace.failure_message = (
        failure_message[:_PRIMARY_FAILURE_MESSAGE_MAX_LENGTH] if failure_message else None
    )


async def _failure_causality_events_for_current_epoch(
    session: AsyncSession,
    workspace_id: str,
) -> _FailureCausalityEvents | None:
    latest_failed_event = await _latest_failed_state_event(session, workspace_id)
    if latest_failed_event is None:
        return None
    if await _has_failure_epoch_reset_after(session, workspace_id, latest_failed_event):
        return None

    latest_failed_payload = _mapping(latest_failed_event.payload)
    if (
        latest_failed_payload is not None
        and _mapping(latest_failed_payload.get(PRIMARY_FAILURE_KEY)) is not None
    ):
        return _FailureCausalityEvents(
            primary_event=latest_failed_event,
            latest_failed_event=latest_failed_event,
        )

    latest_primary_event = await _latest_failed_state_event(
        session,
        workspace_id,
        require_primary_failure=True,
    )
    if latest_primary_event is None:
        return _FailureCausalityEvents(
            primary_event=latest_failed_event,
            latest_failed_event=latest_failed_event,
        )
    if await _has_failure_epoch_reset_after(session, workspace_id, latest_primary_event):
        return _FailureCausalityEvents(
            primary_event=latest_failed_event,
            latest_failed_event=latest_failed_event,
        )
    return _FailureCausalityEvents(
        primary_event=latest_primary_event,
        latest_failed_event=latest_failed_event,
    )


async def _primary_failure_event_for_current_epoch(
    session: AsyncSession,
    workspace_id: str,
) -> WorkspaceEvent | None:
    events = await _failure_causality_events_for_current_epoch(session, workspace_id)
    return events.primary_event if events is not None else None


async def _latest_failed_state_event(
    session: AsyncSession,
    workspace_id: str,
    *,
    require_primary_failure: bool = False,
) -> WorkspaceEvent | None:
    stmt = select(WorkspaceEvent).where(
        WorkspaceEvent.workspace_id == workspace_id,
        WorkspaceEvent.event_type.in_(_FAILURE_CAUSALITY_EVENT_TYPES),
        WorkspaceEvent.new_state == WorkspaceStatus.failed.value,
    )
    if require_primary_failure:
        stmt = stmt.where(_json_payload_object_predicate(session, PRIMARY_FAILURE_KEY))
    stmt = stmt.order_by(
        WorkspaceEvent.occurred_at.desc(),
        WorkspaceEvent.event_order.desc().nullslast(),
    ).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _has_failure_epoch_reset_after(
    session: AsyncSession,
    workspace_id: str,
    event: WorkspaceEvent,
) -> bool:
    stmt = (
        select(WorkspaceEvent.id)
        .where(
            *_failure_epoch_reset_conditions(session, workspace_id),
            _event_occurs_after_or_at_same_tick(event),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _latest_failure_epoch_reset_before(
    session: AsyncSession,
    workspace_id: str,
    event: WorkspaceEvent,
) -> WorkspaceEvent | None:
    stmt = (
        select(WorkspaceEvent)
        .where(
            *_failure_epoch_reset_conditions(session, workspace_id),
            _event_occurs_before_or_at_same_tick(event),
        )
        .order_by(
            WorkspaceEvent.occurred_at.desc(),
            WorkspaceEvent.event_order.desc().nullslast(),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _latest_failed_validation_run(
    session: AsyncSession,
    workspace_id: str,
    *,
    epoch_started_at: datetime | None = None,
) -> ValidationRun | None:
    stmt = select(ValidationRun).where(
        ValidationRun.workspace_id == workspace_id,
        ValidationRun.status == "failed",
        or_(
            ValidationRun.reason_code.is_(None),
            ~ValidationRun.reason_code.in_(_IGNORED_PRIMARY_VALIDATION_REASON_CODES),
        ),
    )
    if epoch_started_at is not None:
        stmt = stmt.where(ValidationRun.started_at >= epoch_started_at)
    stmt = stmt.order_by(
        ValidationRun.finished_at.desc().nullslast(),
        ValidationRun.started_at.desc(),
        # Validation run IDs are uuid4-derived. When recorded run timestamps
        # tie, use persisted write chronology instead of random ID order.
        ValidationRun.created_at.desc(),
        ValidationRun.updated_at.desc(),
    ).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _secondary_failure_history_for_current_epoch(
    session: AsyncSession,
    workspace_id: str,
    *,
    primary_event: WorkspaceEvent,
    latest_failed_event: WorkspaceEvent,
) -> tuple[dict[str, Any], ...]:
    stmt = (
        select(WorkspaceEvent)
        .where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.event_type.in_(_FAILURE_CAUSALITY_EVENT_TYPES),
            WorkspaceEvent.new_state == WorkspaceStatus.failed.value,
            _event_occurs_after_or_at_same_tick(primary_event),
            _event_occurs_before_or_at_same_tick(latest_failed_event),
        )
        .order_by(
            WorkspaceEvent.occurred_at.asc(),
            WorkspaceEvent.event_order.asc().nullslast(),
        )
    )
    events = (await session.execute(stmt)).scalars().all()
    failures: list[dict[str, Any]] = []
    for event in events:
        _append_secondary_failure_history(
            failures,
            _secondary_failure_history(_mapping(event.payload)),
        )
    return tuple(_bounded_secondary_failure_history(failures))


def _failure_epoch_reset_conditions(
    session: AsyncSession,
    workspace_id: str,
) -> tuple[ColumnElement[bool], ...]:
    return (
        WorkspaceEvent.workspace_id == workspace_id,
        or_(
            and_(
                WorkspaceEvent.event_type == "workspace.state_changed",
                WorkspaceEvent.new_state.in_(_FAILURE_EPOCH_RESET_STATES),
            ),
            and_(
                WorkspaceEvent.event_type == "workspace.remonitor_requested",
                or_(
                    WorkspaceEvent.new_state.in_(_FAILURE_EPOCH_RESET_STATES),
                    and_(
                        _json_payload_object_predicate(session, "state_reset"),
                        WorkspaceEvent.payload["state_reset"]["to"]
                        .as_string()
                        .in_(_FAILURE_EPOCH_RESET_STATES),
                    ),
                ),
            ),
        ),
    )


def _json_payload_object_predicate(
    session: AsyncSession,
    key: str,
) -> ColumnElement[bool]:
    dialect_name = _session_dialect_name(session)
    if dialect_name == "postgresql":
        return func.json_typeof(WorkspaceEvent.payload[key]) == "object"
    if dialect_name == "sqlite":
        return func.json_type(WorkspaceEvent.payload, f"$.{key}") == "object"
    return false()


def _session_dialect_name(session: AsyncSession) -> str | None:
    info_value = session.info.get(SESSION_DIALECT_NAME_KEY)
    if isinstance(info_value, str):
        return info_value

    bind = getattr(session, "bind", None)
    if bind is None:
        return None
    dialect = getattr(bind, "dialect", None)
    name = getattr(dialect, "name", None)
    return name if isinstance(name, str) else None


def _event_occurs_after_or_at_same_tick(event: WorkspaceEvent) -> ColumnElement[bool]:
    # Event IDs are uuid4-derived, so equal timestamps must use the persisted
    # workspace-local event order. If the reference event is ordered, unordered
    # same-tick rows are ignored at the boundary because their chronological
    # position is ambiguous.
    event_order = _event_order(event)
    if event_order is None:
        return or_(
            WorkspaceEvent.occurred_at > event.occurred_at,
            WorkspaceEvent.id == event.id,
        )
    return or_(
        WorkspaceEvent.occurred_at > event.occurred_at,
        and_(
            WorkspaceEvent.occurred_at == event.occurred_at,
            WorkspaceEvent.event_order >= event_order,
        ),
    )


def _event_occurs_before_or_at_same_tick(event: WorkspaceEvent) -> ColumnElement[bool]:
    event_order = _event_order(event)
    if event_order is None:
        return or_(
            WorkspaceEvent.occurred_at < event.occurred_at,
            WorkspaceEvent.id == event.id,
        )
    return or_(
        WorkspaceEvent.occurred_at < event.occurred_at,
        and_(
            WorkspaceEvent.occurred_at == event.occurred_at,
            WorkspaceEvent.event_order <= event_order,
        ),
    )


def _event_order(event: WorkspaceEvent) -> int | None:
    event_order = event.event_order
    return event_order if isinstance(event_order, int) else None


def _validation_run_snapshot(run: ValidationRun) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "id": run.id,
        "status": run.status,
        "reason_code": run.reason_code,
        "tier": run.tier,
        "attempt_id": run.attempt_id,
        "command_set_hash": run.command_set_hash,
        "commands": _jsonable(run.commands),
        "base_commit": run.base_commit,
        "base_sha": run.base_sha,
        "workspace_head_sha": run.workspace_head_sha,
        "target_branch": run.target_branch,
        "target_head_sha": run.target_head_sha,
        "profile_name": run.profile_name,
        "profile_version": run.profile_version,
        "profile_source": run.profile_source,
        "resolved_profile_digest": run.resolved_profile_digest,
        "environment_identity_digest": run.environment_identity_digest,
        "environment_identity_inputs": _jsonable(run.environment_identity_inputs),
        "started_at": _jsonable(run.started_at),
        "finished_at": _jsonable(run.finished_at),
        "log_stream_refs": _jsonable(run.log_stream_refs),
        "coverage": _jsonable(run.coverage),
    }
    return {key: value for key, value in snapshot.items() if value is not None}


def _copy_if_present(
    target: dict[str, Any],
    source: Mapping[str, Any],
    source_key: str,
    target_key: str | None = None,
) -> None:
    if source_key in source:
        target[target_key or source_key] = _jsonable(source[source_key])


def _payload_str(payload: Mapping[str, Any] | None, key: str) -> str | None:
    if payload is None:
        return None
    return _string(payload.get(key))


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _jsonable_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _jsonable(item) for key, item in value.items()}


def _secondary_failure_history(payload: Mapping[str, Any] | None) -> tuple[dict[str, Any], ...]:
    if payload is None:
        return ()

    failures: list[dict[str, Any]] = []
    secondary_failures = payload.get(SECONDARY_FAILURES_KEY)
    if isinstance(secondary_failures, Sequence) and not isinstance(
        secondary_failures, str | bytes | bytearray
    ):
        failures.extend(
            _jsonable_mapping(item) for item in secondary_failures if isinstance(item, Mapping)
        )

    secondary_failure = _mapping(payload.get(SECONDARY_FAILURE_KEY))
    if secondary_failure is not None:
        legacy_secondary = _jsonable_mapping(secondary_failure)
        if legacy_secondary not in failures:
            failures.append(legacy_secondary)

    return tuple(_bounded_secondary_failure_history(failures))


def _append_secondary_failure_history(
    failures: list[dict[str, Any]],
    history: Sequence[dict[str, Any]],
) -> None:
    history_items = list(history)
    if not history_items:
        return

    overlap = _secondary_failure_history_overlap(failures, history_items)
    failures.extend(history_items[overlap:])


def _secondary_failure_history_overlap(
    failures: Sequence[dict[str, Any]],
    history_items: Sequence[dict[str, Any]],
) -> int:
    max_overlap = min(len(failures), len(history_items))
    for candidate in range(max_overlap, 0, -1):
        prefix = history_items[:candidate]
        # Persisted payloads can carry bounded windows of earlier history, so
        # the overlap may appear before the current accumulated suffix.
        if _secondary_failure_history_contains(failures, prefix):
            return candidate
    return 0


def _secondary_failure_history_contains(
    failures: Sequence[dict[str, Any]],
    prefix: Sequence[dict[str, Any]],
) -> bool:
    if not prefix:
        return True
    prefix_length = len(prefix)
    for start in range(0, len(failures) - prefix_length + 1):
        if list(failures[start : start + prefix_length]) == list(prefix):
            return True
    return False


def _bounded_secondary_failure_history(
    failures: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    return list(failures[-_SECONDARY_FAILURE_HISTORY_LIMIT:])


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return _jsonable_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_jsonable(item) for item in value]
    return value
