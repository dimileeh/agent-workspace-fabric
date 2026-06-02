"""Scheduler SQL expression builders for workspace ordering and claiming."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol, cast

from sqlalchemy import (
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
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from awf.db.enums import (
    FailureReason,
    WorkspaceStatus,
)
from awf.db.models import ResourceReservation, Workspace
from awf.service.config import DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID
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
    """Protocol for building dialect-specific age-boost SQL expressions."""

    def __call__(
        self,
        *,
        scoring_at: datetime,
        workspace_entity: Any,
    ) -> ColumnElement[Any]: ...


_POSTGRES_INTEGER_MIN: Final = -(2**31)
_POSTGRES_INTEGER_MAX: Final = 2**31 - 1
_LEGACY_LOCAL_RESERVATION_NODE_ID_PREFIXES: Final = (
    "container-",
    "legacy-container-",
)


@dataclass(frozen=True)
class SchedulerOrderExpressions:
    """Column expressions used to rank workspaces for scheduler ordering."""

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
) -> Any:
    """Build a SELECT for workspaces in a given status ordered for scheduler claiming."""
    order_expressions = scheduler_order_expressions(
        scoring_at=scoring_at,
        dialect_name=dialect_name,
    )
    stmt = select(Workspace).where(Workspace.status == status.value)
    if node_id is not None:
        stmt = stmt.where(_scheduler_node_scope_condition(status=status, node_id=node_id))
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


def _scheduler_node_scope_condition(
    *,
    status: WorkspaceStatus,
    node_id: str,
) -> ColumnElement[bool]:
    """Build the node-scope condition for scheduler candidate queries."""
    if status != WorkspaceStatus.requested:
        return or_(Workspace.node_id == node_id, Workspace.node_id.is_(None))

    latest_reservation_node_id = _latest_active_resource_reservation_node_id_expr()
    planned_node_id = func.coalesce(
        Workspace.node_id,
        latest_reservation_node_id,
    )
    scope_conditions = [planned_node_id == node_id, planned_node_id.is_(None)]
    if node_id == DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID:
        # Local service upgrades can leave requested reservations stamped with
        # the old container hostname; current local workers must still adopt those
        # known legacy-local rows without taking work reserved for named nodes.
        scope_conditions.append(
            and_(
                Workspace.node_id.is_(None),
                _legacy_local_reservation_node_condition(latest_reservation_node_id),
            )
        )
    return or_(*scope_conditions)


def _legacy_local_reservation_node_condition(
    reservation_node_id: ColumnElement[Any],
) -> ColumnElement[bool]:
    """Return whether a reservation node id has a legacy local container hostname shape."""
    normalized_node_id = func.lower(reservation_node_id)
    return or_(
        *(
            normalized_node_id.like(f"{prefix}%")
            for prefix in _LEGACY_LOCAL_RESERVATION_NODE_ID_PREFIXES
        )
    )


def _latest_active_resource_reservation_node_id_expr() -> ColumnElement[Any]:
    """Return the latest active resource reservation node for the workspace row."""
    return cast(
        "ColumnElement[Any]",
        select(ResourceReservation.node_id)
        .where(
            ResourceReservation.workspace_id == Workspace.id,
            ResourceReservation.released_at.is_(None),
        )
        .order_by(ResourceReservation.reserved_at.desc(), ResourceReservation.id.desc())
        .limit(1)
        .scalar_subquery(),
    )


def _scheduler_scoring_time(
    *,
    after: SchedulerOrderCursor | None,
    scoring_at: datetime | None,
) -> datetime:
    """Determine the effective scoring timestamp, enforcing pagination consistency."""
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
    """Build the class-priority and effective-score expressions for the scheduler."""

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
    """Reconstruct order expressions at the pagination cursor workspace."""
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
    """Build a SQL CASE that maps task_class values to their integer weights."""
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
    """Build a chained JSON index expression for a nested task_policy path."""
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
    """Extract a trimmed, non-empty string from a JSON path in task_policy."""
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
    """Extract an integer from a JSON path in task_policy, with dialect-specific parsing."""
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
    r"""Format a JSON path tuple as a SQLite ``$.a.b`` path string."""
    return "$." + ".".join(path)


def _bounded_scheduler_int_expr(
    value: ColumnElement[Any],
    *,
    lower: int,
    upper: int,
) -> ColumnElement[Any]:
    """Clamp a scheduler integer expression within [lower, upper]."""
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
    """Dispatch to the dialect-specific age-boost expression builder."""
    builder = _SCHEDULER_SQL_AGE_BOOST_EXPR_BUILDERS.get(dialect_name or "")
    if builder is None:
        return _scheduler_zero_age_boost_expr()
    return builder(scoring_at=scoring_at, workspace_entity=workspace_entity)


def _postgresql_scheduler_age_boost_expr(
    *,
    scoring_at: datetime,
    workspace_entity: Any,
) -> ColumnElement[Any]:
    """Build a PostgreSQL-specific step-function age-boost expression."""
    from sqlalchemy import DateTime

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
    """Build a PostgreSQL ``make_interval`` expression for *seconds*."""
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
    """Build a SQLite-compatible age-boost expression using epoch subtraction."""
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
    """Return a zero-valued age-boost expression for unsupported dialects."""
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
    """Build the keyset-pagination WHERE clause for rows after the given cursor."""
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
