"""Backfill Claude auth-overlay umount retry markers.

Revision ID: 0f1e2d3c4b5a
Revises: f9a0b1c2d3e4
Create Date: 2026-06-05
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final, cast

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection
from sqlalchemy.sql.elements import ColumnElement

revision: str = "0f1e2d3c4b5a"
down_revision: str | Sequence[str] | None = "f9a0b1c2d3e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TERMINAL_RUNTIME_RELEASE_EVENT_TYPE: Final = "workspace.terminal_runtime_released"
_TERMINAL_RUNTIME_RELEASE_REASON_CODE: Final = "TERMINAL_RUNTIME_RELEASED"
_TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE: Final = (
    "workspace.terminal_runtime_release_revoked"
)
_TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE: Final = (
    "TERMINAL_RUNTIME_RELEASE_REVOKED_ORPHAN_STOP_FAILED"
)
_TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE: Final = (
    "workspace.terminal_auth_overlay_unmount_pending"
)
_TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE: Final = (
    "workspace.terminal_auth_overlay_unmount_resolved"
)
_TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_EVENT_TYPE: Final = (
    "workspace.terminal_auth_overlay_unmount_exhausted"
)
_TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE: Final = (
    "TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING"
)
_PENDING_EVENT_UUID_NAMESPACE: Final = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "awf:terminal-auth-overlay-unmount-backfill",
)

_WORKSPACES = sa.table(
    "workspaces",
    sa.column("id", sa.String()),
    sa.column("status", sa.String()),
    sa.column("event_sequence", sa.Integer()),
    sa.column("compose_project_name", sa.String()),
)
_WORKSPACE_EVENTS = sa.table(
    "workspace_events",
    sa.column("id", sa.String()),
    sa.column("workspace_id", sa.String()),
    sa.column("event_type", sa.String()),
    sa.column("old_state", sa.String()),
    sa.column("new_state", sa.String()),
    sa.column("reason_code", sa.String()),
    sa.column("payload", sa.JSON()),
    sa.column("event_order", sa.Integer()),
    sa.column("occurred_at", sa.DateTime(timezone=True)),
)
_AUTH_OVERLAY_MARKER_EVENT_TYPES: Final = (
    _TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
    _TERMINAL_AUTH_OVERLAY_UNMOUNT_RESOLVED_EVENT_TYPE,
    _TERMINAL_AUTH_OVERLAY_UNMOUNT_EXHAUSTED_EVENT_TYPE,
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("SET LOCAL statement_timeout = '10min'"))
        # Per-workspace event-order reservations may wait behind ordinary
        # writers; leave total runtime bounded by statement_timeout instead.
        bind.execute(sa.text("SET LOCAL lock_timeout = '0'"))
    backfill_auth_overlay_unmount_pending(bind)


def downgrade() -> None:
    # Append-only workspace_events history is not deleted on downgrade.
    return None


def backfill_auth_overlay_unmount_pending(connection: Connection) -> int:
    """Seed pending retry markers for historical failed auth-overlay umount releases.

    The runtime scan intentionally remains event-type based for Postgres/SQLite
    portability. This one-time data migration reads the candidate release payloads
    and interprets JSON booleans in Python instead.
    """
    inserted = 0
    for row in connection.execute(_latest_effective_release_stmt()).mappings():
        if not _payload_records_failed_auth_overlay_unmount(row["payload"]):
            continue

        workspace_id = cast(str, row["workspace_id"])
        release_event_id = cast(str, row["release_event_id"])
        status = cast(str, row["status"])
        compose_project_name = cast(str | None, row["compose_project_name"])
        cycle_floor = _event_order_floor(row["release_event_order"])

        if _has_current_cycle_auth_overlay_marker(
            connection,
            workspace_id=workspace_id,
            cycle_floor=cycle_floor,
        ):
            continue

        event_order = _reserve_workspace_event_order(
            connection,
            workspace_id=workspace_id,
            release_event_id=release_event_id,
            release_event_order=row["release_event_order"],
            cycle_floor=cycle_floor,
        )
        if event_order is None:
            continue

        connection.execute(
            _WORKSPACE_EVENTS.insert().values(
                id=_pending_event_id(
                    workspace_id=workspace_id,
                    release_event_id=release_event_id,
                ),
                workspace_id=workspace_id,
                event_type=_TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_EVENT_TYPE,
                old_state=status,
                new_state=status,
                reason_code=_TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING_REASON_CODE,
                payload={
                    "compose_project_name": compose_project_name,
                    "workspace_status": status,
                    "attempt": 1,
                },
                event_order=event_order,
                occurred_at=datetime.now(UTC),
            )
        )
        inserted += 1
    return inserted


def _latest_effective_release_stmt() -> sa.Select[tuple[Any, ...]]:
    latest_release_or_revoke = _latest_release_or_revoke_subquery()
    return (
        sa.select(
            _WORKSPACES.c.id.label("workspace_id"),
            _WORKSPACES.c.status.label("status"),
            _WORKSPACES.c.compose_project_name.label("compose_project_name"),
            latest_release_or_revoke.c.release_event_id,
            latest_release_or_revoke.c.payload,
            latest_release_or_revoke.c.release_event_order,
        )
        .join(
            latest_release_or_revoke,
            latest_release_or_revoke.c.workspace_id == _WORKSPACES.c.id,
        )
        .where(latest_release_or_revoke.c.row_number == 1)
        .where(latest_release_or_revoke.c.event_type == _TERMINAL_RUNTIME_RELEASE_EVENT_TYPE)
        .where(latest_release_or_revoke.c.reason_code == _TERMINAL_RUNTIME_RELEASE_REASON_CODE)
        .order_by(_WORKSPACES.c.id.asc())
    )


def _latest_release_or_revoke_subquery() -> Any:
    event_order_nulls_last = sa.case(
        (_WORKSPACE_EVENTS.c.event_order.is_(None), 1),
        else_=0,
    )
    return (
        sa.select(
            _WORKSPACE_EVENTS.c.id.label("release_event_id"),
            _WORKSPACE_EVENTS.c.workspace_id.label("workspace_id"),
            _WORKSPACE_EVENTS.c.event_type.label("event_type"),
            _WORKSPACE_EVENTS.c.reason_code.label("reason_code"),
            sa.cast(_WORKSPACE_EVENTS.c.payload, sa.Text).label("payload"),
            _WORKSPACE_EVENTS.c.event_order.label("release_event_order"),
            sa.func.row_number()
            .over(
                partition_by=_WORKSPACE_EVENTS.c.workspace_id,
                order_by=(
                    _WORKSPACE_EVENTS.c.occurred_at.desc(),
                    event_order_nulls_last.asc(),
                    _WORKSPACE_EVENTS.c.event_order.desc(),
                    _WORKSPACE_EVENTS.c.id.desc(),
                ),
            )
            .label("row_number"),
        )
        .where(
            _WORKSPACE_EVENTS.c.event_type.in_(
                (
                    _TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                    _TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
                )
            )
        )
        .where(
            _WORKSPACE_EVENTS.c.reason_code.in_(
                (
                    _TERMINAL_RUNTIME_RELEASE_REASON_CODE,
                    _TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
                )
            )
        )
        .subquery()
    )


def _payload_records_failed_auth_overlay_unmount(payload: object) -> bool:
    parsed = _payload_mapping(payload)
    return parsed is not None and parsed.get("auth_overlay_unmounted") is False


def _payload_mapping(payload: object) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    if isinstance(payload, bytes | bytearray):
        try:
            decoded = bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            return None
        try:
            parsed = json.loads(decoded)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _event_order_floor(value: int | str | bytes | bytearray | None) -> int:
    if value is None:
        return -1
    return int(value)


def _has_current_cycle_auth_overlay_marker(
    connection: Connection,
    *,
    workspace_id: str,
    cycle_floor: int,
) -> bool:
    marker = connection.execute(
        sa.select(sa.literal(1))
        .where(
            _current_cycle_auth_overlay_marker_exists(
                workspace_id=workspace_id,
                cycle_floor=cycle_floor,
            )
        )
        .limit(1)
    ).scalar_one_or_none()
    return marker is not None


def _current_cycle_auth_overlay_marker_exists(
    *,
    workspace_id: str,
    cycle_floor: int,
) -> ColumnElement[bool]:
    event_order_matches_cycle = _WORKSPACE_EVENTS.c.event_order >= cycle_floor
    if cycle_floor == -1:
        event_order_matches_cycle = sa.or_(
            event_order_matches_cycle,
            _WORKSPACE_EVENTS.c.event_order.is_(None),
        )
    return (
        sa.select(sa.literal(1))
        .select_from(_WORKSPACE_EVENTS)
        .where(_WORKSPACE_EVENTS.c.workspace_id == workspace_id)
        .where(_WORKSPACE_EVENTS.c.event_type.in_(_AUTH_OVERLAY_MARKER_EVENT_TYPES))
        .where(event_order_matches_cycle)
        .exists()
    )


def _reserve_workspace_event_order(
    connection: Connection,
    *,
    workspace_id: str,
    release_event_id: str,
    release_event_order: int | str | bytes | bytearray | None,
    cycle_floor: int,
) -> int | None:
    current_sequence = sa.func.coalesce(_WORKSPACES.c.event_sequence, 0)
    if connection.dialect.name == "postgresql":
        sequence_floor = sa.func.greatest(current_sequence, cycle_floor)
    else:
        sequence_floor = sa.func.max(current_sequence, cycle_floor)
    event_order = connection.execute(
        _WORKSPACES.update()
        .where(_WORKSPACES.c.id == workspace_id)
        .where(
            _latest_effective_release_matches(
                workspace_id=workspace_id,
                release_event_id=release_event_id,
                release_event_order=release_event_order,
            )
        )
        .where(
            sa.not_(
                _current_cycle_auth_overlay_marker_exists(
                    workspace_id=workspace_id,
                    cycle_floor=cycle_floor,
                )
            )
        )
        .values(event_sequence=sequence_floor + 1)
        .returning(_WORKSPACES.c.event_sequence)
    ).scalar_one_or_none()
    return int(event_order) if event_order is not None else None


def _latest_effective_release_matches(
    *,
    workspace_id: str,
    release_event_id: str,
    release_event_order: int | str | bytes | bytearray | None,
) -> ColumnElement[bool]:
    latest_release_or_revoke = _latest_release_or_revoke_subquery()
    release_order = _event_order_value(release_event_order)
    if release_order is None:
        release_order_matches = latest_release_or_revoke.c.release_event_order.is_(None)
    else:
        release_order_matches = latest_release_or_revoke.c.release_event_order == release_order
    return (
        sa.select(sa.literal(1))
        .select_from(latest_release_or_revoke)
        .where(latest_release_or_revoke.c.workspace_id == workspace_id)
        .where(latest_release_or_revoke.c.row_number == 1)
        .where(latest_release_or_revoke.c.event_type == _TERMINAL_RUNTIME_RELEASE_EVENT_TYPE)
        .where(latest_release_or_revoke.c.reason_code == _TERMINAL_RUNTIME_RELEASE_REASON_CODE)
        .where(latest_release_or_revoke.c.release_event_id == release_event_id)
        .where(release_order_matches)
        .exists()
    )


def _event_order_value(value: int | str | bytes | bytearray | None) -> int | None:
    if value is None:
        return None
    return int(value)


def _pending_event_id(*, workspace_id: str, release_event_id: str) -> str:
    return str(
        uuid.uuid5(
            _PENDING_EVENT_UUID_NAMESPACE,
            f"{revision}:{workspace_id}:{release_event_id}",
        )
    )
