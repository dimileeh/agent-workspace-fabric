"""SQL predicate builders for terminal-workspace garbage collection.

These are pure ``ColumnElement`` builders used by ``plan_terminal_workspace_gc``
to fetch candidate / preserved workspace rows. They are factored into this
sibling module (mirroring ``gc_worktrees`` / ``gc_classify``) to keep ``gc.py``
within the first-party file-size limit; ``gc`` re-aliases them under their
historical ``_workspace_*`` names for its callers and tests.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, or_
from sqlalchemy.sql.elements import ColumnElement

from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace


def workspace_gc_candidate_predicate(
    *,
    eligible_statuses: set[str],
    cutoff_at: datetime,
    default_policy: bool,
    cleanup_enabled: bool,
) -> ColumnElement[bool] | None:
    if not cleanup_enabled:
        return None
    if default_policy:
        if WorkspaceStatus.completed.value not in eligible_statuses:
            return None
        return and_(
            Workspace.status == WorkspaceStatus.completed.value,
            Workspace.updated_at <= cutoff_at,
            workspace_has_pr_metadata_predicate(),
            workspace_has_pr_merge_predicate(),
        )
    return and_(
        Workspace.status.in_(sorted(eligible_statuses)),
        Workspace.updated_at <= cutoff_at,
    )


def workspace_gc_preserved_predicate(
    *,
    eligible_statuses: set[str],
    cutoff_at: datetime,
    default_policy: bool,
    cleanup_enabled: bool,
) -> ColumnElement[bool] | None:
    if not cleanup_enabled:
        return Workspace.status.in_(sorted(eligible_statuses))
    if default_policy:
        clauses: list[ColumnElement[bool]] = []
        if WorkspaceStatus.failed.value in eligible_statuses:
            clauses.append(Workspace.status == WorkspaceStatus.failed.value)
        if "superseded" in eligible_statuses:
            clauses.append(Workspace.status == "superseded")
        if WorkspaceStatus.completed.value in eligible_statuses:
            clauses.append(
                and_(
                    Workspace.status == WorkspaceStatus.completed.value,
                    or_(
                        workspace_lacks_pr_metadata_predicate(),
                        workspace_pr_not_merged_predicate(),
                        Workspace.updated_at > cutoff_at,
                    ),
                )
            )
        if not clauses:
            return None
        return or_(*clauses)
    return and_(
        Workspace.status.in_(sorted(eligible_statuses)),
        Workspace.updated_at > cutoff_at,
    )


def workspace_gc_age_capped_predicate(
    *,
    eligible_statuses: set[str],
    preserved_failed_cutoff_at: datetime,
    default_policy: bool,
    cleanup_enabled: bool,
) -> ColumnElement[bool] | None:
    """Failed / superseded rows aged past the preserved-failed cap.

    The preserved query applies ``limit`` (ordered oldest-first) *before* the
    age-cap classification runs, and it also matches rows that are preserved
    indefinitely with no age cap (e.g. completed-without-PR / completed-not-
    merged). A backlog of such rows that predate an aged failed/superseded row
    would fill the preserved-query limit and starve the cap — the aged row would
    never be fetched, never promoted, and its pressure dirs never reclaimed.

    Fetching the age-capped rows independently guarantees they are always
    classified regardless of the preserved-only backlog. Only meaningful under
    ``default_policy`` (the sole branch that honours ``preserved_failed_cutoff_at``).
    """
    if not cleanup_enabled or not default_policy:
        return None
    statuses = {WorkspaceStatus.failed.value, "superseded"} & eligible_statuses
    if not statuses:
        return None
    return and_(
        Workspace.status.in_(sorted(statuses)),
        Workspace.updated_at <= preserved_failed_cutoff_at,
    )


def workspace_has_pr_metadata_predicate() -> ColumnElement[bool]:
    return or_(
        Workspace.pr_number.is_not(None),
        and_(Workspace.pr_url.is_not(None), Workspace.pr_url != ""),
    )


def workspace_lacks_pr_metadata_predicate() -> ColumnElement[bool]:
    return and_(
        Workspace.pr_number.is_(None),
        or_(Workspace.pr_url.is_(None), Workspace.pr_url == ""),
    )


def workspace_has_pr_merge_predicate() -> ColumnElement[bool]:
    return Workspace.pr_merge_sha.is_not(None)


def workspace_pr_not_merged_predicate() -> ColumnElement[bool]:
    return and_(
        workspace_has_pr_metadata_predicate(),
        Workspace.pr_merge_sha.is_(None),
    )
