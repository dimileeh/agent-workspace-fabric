"""Scheduling eligibility for durably contained hosted recovery outcomes."""

from sqlalchemy import func, or_
from sqlalchemy.sql.elements import ColumnElement

from awf.db.models import Workspace


def recovery_allows_work() -> ColumnElement[bool]:
    return or_(
        Workspace.task_policy["hosted_node"].as_string().is_(None),
        func.coalesce(Workspace.task_policy["hosted_recovery"]["disposition"].as_string(), "")
        != "attention",
    )
