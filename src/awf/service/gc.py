"""Filesystem garbage collection for terminal service workspaces.

This module only relieves disk pressure from per-workspace runtime directories.
It deliberately does not delete control-plane rows, workspace events, or durable
log streams.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from inspect import isawaitable
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.logging import get_logger
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.runtime.inspection import RuntimeInspector
from awf.service import gc_predicates as _gc_predicates
from awf.service import gc_results as _gc_results
from awf.service import gc_worktrees as _gc_worktrees
from awf.service.gc_classify import (
    PATH_ALREADY_REMOVED,
    PATH_DELETE_FAILED,
    PATH_DELETED,
    _compose_project_name_for_workspace,
    _delete_gc_path,
    _gc_path,
    _has_pr_metadata,
    _path_payload_for_candidate,
    _pr_has_merged,
    _snapshot_has_no_work,
)

# Re-exported for backward-compatible import surface (referenced via
# ``awf.service.gc.<name>`` by callers/tests, not used inside this module).
from awf.service.gc_classify import PATH_DELETE_PERMISSION_DENIED as PATH_DELETE_PERMISSION_DENIED
from awf.service.gc_classify import WorkspaceGCPath as WorkspaceGCPath
from awf.service.gc_classify import _agent_service_has_no_work as _agent_service_has_no_work
from awf.service.gc_classify import _container_command_is_idle as _container_command_is_idle
from awf.service.gc_classify import _estimate_bytes as _estimate_bytes
from awf.service.gc_classify import _is_safe_gc_path as _is_safe_gc_path
from awf.service.gc_companions import companion_worktree_paths_for_gc
from awf.service.gc_time import normalize_statuses as _normalize_statuses
from awf.service.gc_time import to_utc as _to_utc
from awf.service.secret_leases import (
    TERMINAL_GC_REVOKE_REASON,
    SecretLeaseService,
    secret_lease_revocation_summary,
)

if TYPE_CHECKING:
    from awf.node.compose_manager import ComposeManager

_log = get_logger(__name__)

WorkspaceGCWorktreeRemoveResult = _gc_worktrees.WorkspaceGCWorktreeRemoveResult
WorkspaceGCWorktreeRemoveTargetResult = _gc_worktrees.WorkspaceGCWorktreeRemoveTargetResult
_blocked_worktree_paths_after_remove = _gc_worktrees.blocked_worktree_paths_after_remove
_default_worktree_remover = _gc_worktrees.default_worktree_remover
_run_worktree_remove = _gc_worktrees.run_worktree_remove
_worktree_id_for_gc_path = _gc_worktrees.worktree_id_for_gc_path
_worktree_paths_by_id = _gc_worktrees.worktree_paths_by_id

# Leaf result/data dataclasses live in ``gc_results`` (file-size budget);
# re-exported so the historical ``awf.service.gc.<name>`` surface is unchanged.
WorkspaceCleanupExecutionStatus = _gc_results.WorkspaceCleanupExecutionStatus
WorkspaceCleanupPathStatus = _gc_results.WorkspaceCleanupPathStatus
WorkspaceGCComposeTeardownResult = _gc_results.WorkspaceGCComposeTeardownResult
WorkspaceGCDeleteError = _gc_results.WorkspaceGCDeleteError
WorkspaceGCPathOutcome = _gc_results.WorkspaceGCPathOutcome
WorkspaceGCPreserved = _gc_results.WorkspaceGCPreserved

# SQL predicate builders live in ``gc_predicates`` (file-size budget); re-aliased
# under their historical ``_workspace_*`` names for callers/tests.
_workspace_gc_candidate_predicate = _gc_predicates.workspace_gc_candidate_predicate
_workspace_gc_preserved_predicate = _gc_predicates.workspace_gc_preserved_predicate
_workspace_gc_age_capped_predicate = _gc_predicates.workspace_gc_age_capped_predicate
_workspace_has_pr_metadata_predicate = _gc_predicates.workspace_has_pr_metadata_predicate
_workspace_lacks_pr_metadata_predicate = _gc_predicates.workspace_lacks_pr_metadata_predicate
_workspace_has_pr_merge_predicate = _gc_predicates.workspace_has_pr_merge_predicate
_workspace_pr_not_merged_predicate = _gc_predicates.workspace_pr_not_merged_predicate

DEFAULT_MIN_AGE_HOURS = 168
# Preserved-failed workspaces (work was kept for triage) are otherwise retained
# indefinitely. Once they age past this cap their pressure dirs are reclaimed
# while the durable record (DB row, events, logs) is kept. Far above the 168 h
# idle window so the two paths stay distinct and separately auditable.
DEFAULT_MAX_PRESERVED_FAILED_HOURS = 720

COMPLETED_PR_RETENTION_EXPIRED = "COMPLETED_PR_RETENTION_EXPIRED"
COMPLETED_PR_IMMEDIATE_RECLAIM = "COMPLETED_PR_IMMEDIATE_RECLAIM"
TERMINAL_WORKSPACE_RETENTION_EXPIRED = "TERMINAL_WORKSPACE_RETENTION_EXPIRED"
WORKSPACE_WITHIN_RETENTION = "WORKSPACE_WITHIN_RETENTION"
FAILED_WORKSPACE_TRIAGE_PRESERVED = "FAILED_WORKSPACE_TRIAGE_PRESERVED"
FAILED_WORKSPACE_NO_WORK = "FAILED_WORKSPACE_NO_WORK"
PRESERVED_FAILED_AGE_CAP_RECLAIMED = "PRESERVED_FAILED_AGE_CAP_RECLAIMED"
COMPLETED_WORKSPACE_WITHOUT_PR = "COMPLETED_WORKSPACE_WITHOUT_PR"
COMPLETED_PR_NOT_MERGED = "COMPLETED_PR_NOT_MERGED"
WORKSPACE_CLEANUP_DISABLED = "WORKSPACE_CLEANUP_DISABLED"

CLEANUP_DRY_RUN = "CLEANUP_DRY_RUN"
CLEANUP_EXECUTION_SUCCEEDED = "CLEANUP_EXECUTION_SUCCEEDED"
CLEANUP_EXECUTION_PARTIAL = "CLEANUP_EXECUTION_PARTIAL"

TERMINAL_WORKSPACE_GC_STATUSES = frozenset(
    {
        WorkspaceStatus.completed.value,
        WorkspaceStatus.failed.value,
        "superseded",
        WorkspaceStatus.cancelled.value,
        WorkspaceStatus.destroyed.value,
    }
)

_FAILED_NO_WORK_TERMINAL_STATUSES = frozenset({WorkspaceStatus.failed.value, "superseded"})

_RUNTIME_INSPECTOR = RuntimeInspector()

PROTECTED_WORKSPACE_GC_STATUSES = frozenset(
    {
        WorkspaceStatus.requested.value,
        WorkspaceStatus.provisioning.value,
        WorkspaceStatus.ready.value,
        WorkspaceStatus.running.value,
        WorkspaceStatus.validating.value,
        WorkspaceStatus.pushing.value,
        WorkspaceStatus.monitoring_pr.value,
        WorkspaceStatus.destroying.value,
    }
)


@dataclass(frozen=True)
class WorkspaceGCCandidate:
    """A terminal workspace whose pressure directories are eligible for GC."""

    workspace_id: str
    status: str
    updated_at: datetime
    age_hours: int
    reason_code: str
    worktree: WorkspaceGCPath
    compose: WorkspaceGCPath
    auth: WorkspaceGCPath
    companion_worktrees: tuple[WorkspaceGCPath, ...] = ()
    compose_project_name: str | None = None
    compose_file_path: str | None = None

    @property
    def total_estimated_bytes(self) -> int:
        return (
            self.worktree.estimated_bytes
            + self.compose.estimated_bytes
            + self.auth.estimated_bytes
            + sum(item.estimated_bytes for item in self.companion_worktrees)
        )

    def paths(self) -> Iterator[WorkspaceGCPath]:
        yield self.worktree
        yield from self.companion_worktrees
        yield self.compose
        yield self.auth

    def to_dict(
        self,
        *,
        deleted_paths: set[Path] | None = None,
        delete_errors: dict[tuple[str, Path], str] | None = None,
        path_outcomes: dict[tuple[str, Path], WorkspaceGCPathOutcome] | None = None,
        compose_teardown: WorkspaceGCComposeTeardownResult | None = None,
        worktree_remove: WorkspaceGCWorktreeRemoveResult | None = None,
    ) -> dict[str, object]:
        deleted_paths = deleted_paths or set()
        delete_errors = delete_errors or {}
        path_outcomes = path_outcomes or {}
        paths = {
            item.kind: _path_payload_for_candidate(
                item,
                deleted_paths=deleted_paths,
                delete_errors=delete_errors,
                path_outcomes=path_outcomes,
            )
            for item in self.paths()
        }
        payload: dict[str, object] = {
            "workspace_id": self.workspace_id,
            "status": self.status,
            "reason_code": self.reason_code,
            "updated_at": self.updated_at.isoformat(),
            "age_hours": self.age_hours,
            "estimated_bytes": {
                "worktree": self.worktree.estimated_bytes,
                "companion_worktrees": sum(
                    item.estimated_bytes for item in self.companion_worktrees
                ),
                "compose": self.compose.estimated_bytes,
                "auth": self.auth.estimated_bytes,
                "total": self.total_estimated_bytes,
            },
            "paths": paths,
        }
        if compose_teardown is not None:
            payload["compose_teardown"] = compose_teardown.to_dict()
        if worktree_remove is not None:
            payload["worktree_remove"] = worktree_remove.to_dict()
        return payload


WorkspaceGCComposeTeardown = Callable[
    [WorkspaceGCCandidate],
    WorkspaceGCComposeTeardownResult | Awaitable[WorkspaceGCComposeTeardownResult],
]

WorkspaceGCWorktreeRemove = Callable[
    [WorkspaceGCCandidate],
    WorkspaceGCWorktreeRemoveResult | Awaitable[WorkspaceGCWorktreeRemoveResult],
]

# Prunes stale cached companion images once per GC run (independent of the
# per-workspace candidates). Returns a small report dict for the GC payload.
CompanionImagePrune = Callable[[], Awaitable[dict[str, object]]]


@dataclass(frozen=True)
class WorkspaceGCPlan:
    """Inspectable GC plan before deletion."""

    work_dir: Path
    min_age_hours: float
    cutoff_at: datetime
    include_statuses: tuple[str, ...]
    exclude_statuses: tuple[str, ...]
    candidates: list[WorkspaceGCCandidate]
    preserved: list[WorkspaceGCPreserved]
    cleanup_enabled: bool = True
    default_policy: bool = True

    @property
    def total_estimated_bytes(self) -> int:
        return sum(candidate.total_estimated_bytes for candidate in self.candidates)

    @property
    def preserved_count(self) -> int:
        return len(self.preserved)

    @property
    def policy_eligible_statuses(self) -> tuple[str, ...]:
        if self.default_policy:
            if WorkspaceStatus.completed.value in self.include_statuses:
                return (WorkspaceStatus.completed.value,)
            return ()
        eligible_statuses = set(self.include_statuses)
        eligible_statuses &= set(TERMINAL_WORKSPACE_GC_STATUSES)
        eligible_statuses -= set(PROTECTED_WORKSPACE_GC_STATUSES)
        eligible_statuses -= set(self.exclude_statuses)
        return tuple(sorted(eligible_statuses))

    @property
    def requires_pr_metadata(self) -> bool:
        return self.default_policy and WorkspaceStatus.completed.value in self.include_statuses

    @property
    def requires_pr_merge(self) -> bool:
        return self.default_policy and WorkspaceStatus.completed.value in self.include_statuses

    @property
    def preserves_failed_workspaces(self) -> bool:
        return self.default_policy and WorkspaceStatus.failed.value in self.include_statuses

    def to_dict(self) -> dict[str, object]:
        return {
            "work_dir": str(self.work_dir),
            "min_age_hours": self.min_age_hours,
            "cutoff_at": self.cutoff_at.isoformat(),
            "policy": {
                "cleanup_enabled": self.cleanup_enabled,
                "retention_hours": self.min_age_hours,
                "eligible_statuses": list(self.policy_eligible_statuses),
                "requires_pr_metadata": self.requires_pr_metadata,
                "requires_pr_merge": self.requires_pr_merge,
                "preserves_failed_workspaces": self.preserves_failed_workspaces,
            },
            "include_statuses": list(self.include_statuses),
            "exclude_statuses": list(self.exclude_statuses),
            "candidate_count": len(self.candidates),
            "preserved_count": self.preserved_count,
            "total_estimated_bytes": self.total_estimated_bytes,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "preserved": [preserved.to_dict() for preserved in self.preserved],
        }


@dataclass(frozen=True)
class WorkspaceGCResult:
    """GC plan plus optional execution outcome."""

    plan: WorkspaceGCPlan
    dry_run: bool
    deleted_paths: list[Path]
    delete_errors: list[WorkspaceGCDeleteError]
    path_outcomes: list[WorkspaceGCPathOutcome]
    compose_teardowns: dict[str, WorkspaceGCComposeTeardownResult]
    secret_lease_revocations: dict[str, dict[str, object]]
    worktree_removes: dict[str, WorkspaceGCWorktreeRemoveResult]
    reservation_releases: dict[str, dict[str, object]]
    status: WorkspaceCleanupExecutionStatus
    reason_code: str
    companion_image_prune: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        deleted_paths = set(self.deleted_paths)
        delete_errors = {(error.kind, error.path): error.error for error in self.delete_errors}
        path_outcomes = {(outcome.kind, outcome.path): outcome for outcome in self.path_outcomes}
        payload = self.plan.to_dict()
        payload.update(
            {
                "dry_run": self.dry_run,
                "status": self.status,
                "reason_code": self.reason_code,
                "deleted_paths": [str(path) for path in self.deleted_paths],
                "deleted_path_count": len(self.deleted_paths),
                "delete_errors": [error.to_dict() for error in self.delete_errors],
                "secret_leases": self.secret_lease_revocations,
                "worktree_removes": {
                    ws_id: result.to_dict() for ws_id, result in self.worktree_removes.items()
                },
                "reservation_releases": self.reservation_releases,
            }
        )
        if self.companion_image_prune is not None:
            payload["companion_image_prune"] = self.companion_image_prune
        payload["candidates"] = [
            candidate.to_dict(
                deleted_paths=deleted_paths,
                delete_errors=delete_errors,
                path_outcomes=path_outcomes,
                compose_teardown=self.compose_teardowns.get(candidate.workspace_id),
                worktree_remove=self.worktree_removes.get(candidate.workspace_id),
            )
            for candidate in self.plan.candidates
        ]
        return payload


async def plan_terminal_workspace_gc(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    work_dir: Path | str,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
    limit: int | None = None,
    include_statuses: Iterable[WorkspaceStatus | str] | None = None,
    exclude_statuses: Iterable[WorkspaceStatus | str] | None = None,
    cleanup_enabled: bool = True,
    max_preserved_failed_hours: float = DEFAULT_MAX_PRESERVED_FAILED_HOURS,
    now: datetime | None = None,
) -> WorkspaceGCPlan:
    """Build a terminal-workspace filesystem cleanup plan.

    Active and destroying workspaces are never eligible, even if explicitly
    requested through ``include_statuses``.
    """

    current_time = _to_utc(now or datetime.now(UTC))
    normalized_work_dir = Path(work_dir).expanduser()
    cutoff_at = current_time - timedelta(hours=min_age_hours)
    preserved_failed_cutoff_at = current_time - timedelta(hours=max_preserved_failed_hours)
    requested_statuses = _normalize_statuses(include_statuses)
    excluded_statuses = _normalize_statuses(exclude_statuses) or set()
    default_policy = requested_statuses is None
    if requested_statuses is None:
        eligible_statuses = {
            WorkspaceStatus.completed.value,
            WorkspaceStatus.failed.value,
            "superseded",
        }
    else:
        eligible_statuses = requested_statuses & set(TERMINAL_WORKSPACE_GC_STATUSES)
    eligible_statuses -= excluded_statuses
    eligible_statuses -= set(PROTECTED_WORKSPACE_GC_STATUSES)
    plan_include_statuses = (
        requested_statuses if requested_statuses is not None else eligible_statuses
    )

    if not eligible_statuses:
        return WorkspaceGCPlan(
            work_dir=normalized_work_dir,
            min_age_hours=min_age_hours,
            cutoff_at=cutoff_at,
            include_statuses=tuple(sorted(plan_include_statuses)),
            exclude_statuses=tuple(sorted(excluded_statuses)),
            candidates=[],
            preserved=[],
            cleanup_enabled=cleanup_enabled,
            default_policy=default_policy,
        )

    row_limit = None if limit is None else max(limit, 0)
    candidate_predicate = _workspace_gc_candidate_predicate(
        eligible_statuses=eligible_statuses,
        cutoff_at=cutoff_at,
        default_policy=default_policy,
        cleanup_enabled=cleanup_enabled,
    )
    preserved_predicate = _workspace_gc_preserved_predicate(
        eligible_statuses=eligible_statuses,
        cutoff_at=cutoff_at,
        default_policy=default_policy,
        cleanup_enabled=cleanup_enabled,
    )
    age_capped_predicate = _workspace_gc_age_capped_predicate(
        eligible_statuses=eligible_statuses,
        preserved_failed_cutoff_at=preserved_failed_cutoff_at,
        default_policy=default_policy,
        cleanup_enabled=cleanup_enabled,
    )

    candidate_rows: list[Workspace] = []
    preserved_rows: list[Workspace] = []
    age_capped_rows: list[Workspace] = []
    async with session_factory() as session:
        if candidate_predicate is not None:
            candidate_stmt = (
                select(Workspace)
                .where(Workspace.status.in_(sorted(eligible_statuses)))
                .where(candidate_predicate)
                .order_by(Workspace.updated_at.asc(), Workspace.id.asc())
            )
            if row_limit is not None:
                candidate_stmt = candidate_stmt.limit(row_limit)
            candidate_rows = list((await session.execute(candidate_stmt)).scalars())

        if preserved_predicate is not None:
            preserved_stmt = (
                select(Workspace)
                .where(Workspace.status.in_(sorted(eligible_statuses)))
                .where(preserved_predicate)
                .order_by(Workspace.updated_at.asc(), Workspace.id.asc())
            )
            if row_limit is not None:
                preserved_stmt = preserved_stmt.limit(row_limit)
            preserved_rows = list((await session.execute(preserved_stmt)).scalars())

        # Age-capped failed/superseded rows are fetched independently so a
        # backlog of older indefinitely-preserved rows (e.g. completed-without-PR)
        # cannot fill the preserved-query limit and starve the cap, leaving aged
        # pressure dirs unreclaimed.
        if age_capped_predicate is not None:
            age_capped_stmt = (
                select(Workspace)
                .where(age_capped_predicate)
                .order_by(Workspace.updated_at.asc(), Workspace.id.asc())
            )
            if row_limit is not None:
                age_capped_stmt = age_capped_stmt.limit(row_limit)
            age_capped_rows = list((await session.execute(age_capped_stmt)).scalars())

    candidates: list[WorkspaceGCCandidate] = []
    preserved: list[WorkspaceGCPreserved] = []
    candidate_ids: set[str] = set()
    for workspace in candidate_rows:
        classification = await asyncio.to_thread(
            _classify_workspace_for_gc,
            workspace,
            work_dir=normalized_work_dir,
            now=current_time,
            cutoff_at=cutoff_at,
            default_policy=default_policy,
            cleanup_enabled=cleanup_enabled,
            preserved_failed_cutoff_at=preserved_failed_cutoff_at,
        )
        if isinstance(classification, WorkspaceGCCandidate):
            candidates.append(classification)
            candidate_ids.add(workspace.id)
        elif classification is not None:
            preserved.append(classification)
    classified_ids: set[str] = set()
    # Age-capped rows may also surface in the preserved query; dedup so a row
    # matched by both is classified exactly once.
    for workspace in (*preserved_rows, *age_capped_rows):
        if workspace.id in candidate_ids or workspace.id in classified_ids:
            continue
        classified_ids.add(workspace.id)
        classification = await asyncio.to_thread(
            _classify_workspace_for_gc,
            workspace,
            work_dir=normalized_work_dir,
            now=current_time,
            cutoff_at=cutoff_at,
            default_policy=default_policy,
            cleanup_enabled=cleanup_enabled,
            preserved_failed_cutoff_at=preserved_failed_cutoff_at,
        )
        if isinstance(classification, WorkspaceGCCandidate):
            candidates.append(classification)
            candidate_ids.add(workspace.id)
        elif isinstance(classification, WorkspaceGCPreserved):
            preserved.append(classification)
    # ``limit`` caps the candidate and preserved SQL queries independently, but
    # the preserved loop promotes age-capped / no-work rows into candidates. Left
    # unchecked a single batch could reclaim up to ~2x ``limit`` rows, breaking
    # the "maximum cleanup candidates per batch" contract. Enforce the budget on
    # the combined set, keeping the oldest candidates so cleanup stays FIFO.
    if row_limit is not None and len(candidates) > row_limit:
        candidates.sort(key=lambda candidate: (candidate.updated_at, candidate.workspace_id))
        candidates = candidates[:row_limit]
    return WorkspaceGCPlan(
        work_dir=normalized_work_dir,
        min_age_hours=min_age_hours,
        cutoff_at=cutoff_at,
        include_statuses=tuple(sorted(plan_include_statuses)),
        exclude_statuses=tuple(sorted(excluded_statuses)),
        candidates=candidates,
        preserved=preserved,
        cleanup_enabled=cleanup_enabled,
        default_policy=default_policy,
    )


async def run_terminal_workspace_gc(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    work_dir: Path | str,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
    limit: int | None = None,
    include_statuses: Iterable[WorkspaceStatus | str] | None = None,
    exclude_statuses: Iterable[WorkspaceStatus | str] | None = None,
    execute: bool = False,
    cleanup_enabled: bool = True,
    max_preserved_failed_hours: float = DEFAULT_MAX_PRESERVED_FAILED_HOURS,
    compose_teardown: WorkspaceGCComposeTeardown | None = None,
    worktree_remover: WorkspaceGCWorktreeRemove | None = None,
    companion_image_prune: CompanionImagePrune | None = None,
    now: datetime | None = None,
) -> WorkspaceGCResult:
    """Plan terminal workspace GC and optionally delete selected directories."""

    current_time = _to_utc(now or datetime.now(UTC))
    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=min_age_hours,
        limit=limit,
        include_statuses=include_statuses,
        exclude_statuses=exclude_statuses,
        cleanup_enabled=cleanup_enabled,
        max_preserved_failed_hours=max_preserved_failed_hours,
        now=current_time,
    )
    if not execute:
        return _gc_result(
            plan=plan,
            dry_run=True,
            deleted_paths=[],
            delete_errors=[],
            path_outcomes=[],
            compose_teardowns={},
            worktree_removes={},
            reservation_releases={},
        )

    resolved_worktree_remover = _resolve_worktree_remover(
        worktree_remover, session_factory, work_dir
    )
    secret_lease_revocations = await _revoke_gc_secret_leases(
        session_factory,
        workspace_ids=[candidate.workspace_id for candidate in plan.candidates],
        now=current_time,
    )
    (
        deleted_paths,
        delete_errors,
        path_outcomes,
        compose_teardowns,
        worktree_removes,
    ) = await _delete_gc_plan_paths(
        plan,
        compose_teardown=compose_teardown,
        worktree_remover=resolved_worktree_remover,
    )
    reservation_releases = await _release_gc_reservations(
        session_factory,
        workspace_ids=[candidate.workspace_id for candidate in plan.candidates],
    )
    companion_image_prune_result = (
        await companion_image_prune() if companion_image_prune is not None else None
    )
    return _gc_result(
        plan=plan,
        dry_run=False,
        deleted_paths=deleted_paths,
        delete_errors=delete_errors,
        path_outcomes=path_outcomes,
        compose_teardowns=compose_teardowns,
        secret_lease_revocations=secret_lease_revocations,
        worktree_removes=worktree_removes,
        reservation_releases=reservation_releases,
        companion_image_prune=companion_image_prune_result,
    )


def _candidate_compose_file(candidate: WorkspaceGCCandidate) -> Path:
    """Resolve the per-workspace ``compose.yml`` for a GC candidate.

    Prefers the persisted compose-file path; otherwise falls back to the
    standard ``<work_dir>/compose/<workspace_id>/compose.yml`` location (the
    candidate's ``compose`` directory already encodes the work dir).
    """
    if candidate.compose_file_path:
        return Path(candidate.compose_file_path).expanduser()
    return candidate.compose.path / "compose.yml"


def _service_gc_compose_teardown(
    manager: ComposeManager,
) -> WorkspaceGCComposeTeardown:
    """Build a volume-removing compose-teardown callback for terminal GC.

    Reaps the per-workspace Docker volumes (``awf-<project>-dind_data`` /
    ``-postgres_data``) that otherwise leak because GC never tore the stack
    down. Idempotent: an already-down stack yields an ``ok`` skip, not a
    ``partial``.
    """

    async def _teardown(candidate: WorkspaceGCCandidate) -> WorkspaceGCComposeTeardownResult:
        project_name = candidate.compose_project_name or f"awf_{candidate.workspace_id}"
        result = await manager.teardown_project(
            project_name=project_name,
            compose_file=_candidate_compose_file(candidate),
            workspace_id=candidate.workspace_id,
            remove_volumes=True,
        )
        return WorkspaceGCComposeTeardownResult(
            status=result.status,
            reason_code=result.reason_code,
            error=result.error,
        )

    return _teardown


def _default_workspace_compose_template() -> Path:
    """Resolve the repo's workspace compose template (mirrors ``worker.py``)."""
    return Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


async def run_service_workspace_gc(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    work_dir: Path | str,
    template_path: Path | str | None = None,
    execute: bool = False,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
    limit: int | None = None,
    include_statuses: Iterable[WorkspaceStatus | str] | None = None,
    exclude_statuses: Iterable[WorkspaceStatus | str] | None = None,
    cleanup_enabled: bool = True,
    max_preserved_failed_hours: float = DEFAULT_MAX_PRESERVED_FAILED_HOURS,
    companion_image_cache_enabled: bool = False,
    companion_image_retention_hours: int = DEFAULT_MIN_AGE_HOURS,
    compose_manager: ComposeManager | None = None,
    now: datetime | None = None,
) -> WorkspaceGCResult:
    """Run terminal-workspace GC inside the root control-plane.

    This is the entrypoint the ``POST /v1/service/gc`` route delegates to. The
    api/worker containers run as **root** and own the per-workspace state, so
    deletion here actually removes root-owned auth dirs (the host CLI, running as
    uid 1000, silently could not) and a volume-removing compose teardown reaps
    the per-workspace Docker volumes that GC previously leaked.
    """
    normalized_work_dir = Path(work_dir).expanduser().resolve()
    manager = compose_manager
    if manager is None:
        from awf.node.compose_manager import ComposeManager as _ComposeManager

        resolved_template = (
            _default_workspace_compose_template()
            if template_path is None
            else Path(template_path).expanduser()
        )
        manager = _ComposeManager(
            work_dir=normalized_work_dir,
            template_path=resolved_template,
        )
    compose_teardown = _service_gc_compose_teardown(manager)
    companion_image_prune: CompanionImagePrune | None = None
    if companion_image_cache_enabled:
        from awf.node.companion_images import run_companion_image_prune

        async def _prune() -> dict[str, object]:
            return await run_companion_image_prune(companion_image_retention_hours)

        companion_image_prune = _prune
    return await run_terminal_workspace_gc(
        session_factory,
        work_dir=normalized_work_dir,
        min_age_hours=min_age_hours,
        limit=limit,
        include_statuses=include_statuses,
        exclude_statuses=exclude_statuses,
        execute=execute,
        cleanup_enabled=cleanup_enabled,
        max_preserved_failed_hours=max_preserved_failed_hours,
        compose_teardown=compose_teardown,
        companion_image_prune=companion_image_prune,
        now=now,
    )


def _resolve_worktree_remover(
    worktree_remover: WorkspaceGCWorktreeRemove | None,
    session_factory: async_sessionmaker[AsyncSession],
    work_dir: Path | str,
) -> WorkspaceGCWorktreeRemove:
    if worktree_remover is not None:
        return worktree_remover
    normalized = Path(work_dir).expanduser()

    async def _default_remover(candidate: WorkspaceGCCandidate) -> WorkspaceGCWorktreeRemoveResult:
        return await _default_worktree_remover(
            candidate, session_factory=session_factory, work_dir=normalized
        )

    return _default_remover


async def run_workspace_filesystem_gc(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    work_dir: Path | str,
    workspace_id: str,
    execute: bool = False,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
    cleanup_enabled: bool = True,
    ignore_retention: bool = False,
    max_preserved_failed_hours: float = DEFAULT_MAX_PRESERVED_FAILED_HOURS,
    compose_teardown: WorkspaceGCComposeTeardown | None = None,
    worktree_remover: WorkspaceGCWorktreeRemove | None = None,
    now: datetime | None = None,
) -> WorkspaceGCResult:
    """Plan or execute filesystem GC for one terminal workspace.

    This is used by the PR monitor after a successful merge. It keeps the
    durable workspace row, events, logs, and artifacts intact while removing
    the checkout/auth/compose pressure directories for the single completed
    workspace.

    With ``ignore_retention=True`` a completed, PR-merged workspace is reclaimed
    immediately regardless of the retention window -- its pressure dirs are
    disposable once the PR has merged. Failed / superseded / without-PR /
    not-merged workspaces are still preserved by the retention policy.
    """

    current_time = _to_utc(now or datetime.now(UTC))
    normalized_work_dir = Path(work_dir).expanduser()
    cutoff_at = current_time - timedelta(hours=min_age_hours)
    preserved_failed_cutoff_at = current_time - timedelta(hours=max_preserved_failed_hours)
    resolved_worktree_remover = _resolve_worktree_remover(
        worktree_remover, session_factory, work_dir
    )
    async with session_factory() as session:
        workspace = await session.get(Workspace, workspace_id)

    candidates: list[WorkspaceGCCandidate] = []
    preserved: list[WorkspaceGCPreserved] = []
    include_statuses: tuple[str, ...] = ()
    if workspace is not None:
        include_statuses = (workspace.status,)
        classification = await asyncio.to_thread(
            _classify_workspace_for_gc,
            workspace,
            work_dir=normalized_work_dir,
            now=current_time,
            cutoff_at=cutoff_at,
            default_policy=True,
            cleanup_enabled=cleanup_enabled,
            ignore_retention=ignore_retention,
            preserved_failed_cutoff_at=preserved_failed_cutoff_at,
        )
        if isinstance(classification, WorkspaceGCCandidate):
            candidates.append(classification)
        elif classification is not None:
            preserved.append(classification)

    plan = WorkspaceGCPlan(
        work_dir=normalized_work_dir,
        min_age_hours=min_age_hours,
        cutoff_at=cutoff_at,
        include_statuses=include_statuses,
        exclude_statuses=(),
        candidates=candidates,
        preserved=preserved,
        cleanup_enabled=cleanup_enabled,
        default_policy=True,
    )
    if not execute:
        return _gc_result(
            plan=plan,
            dry_run=True,
            deleted_paths=[],
            delete_errors=[],
            path_outcomes=[],
            compose_teardowns={},
            worktree_removes={},
            reservation_releases={},
        )

    secret_lease_revocations = await _revoke_gc_secret_leases(
        session_factory,
        workspace_ids=[candidate.workspace_id for candidate in plan.candidates],
        now=current_time,
    )
    (
        deleted_paths,
        delete_errors,
        path_outcomes,
        compose_teardowns,
        worktree_removes,
    ) = await _delete_gc_plan_paths(
        plan,
        compose_teardown=compose_teardown,
        worktree_remover=resolved_worktree_remover,
    )
    reservation_releases = await _release_gc_reservations(
        session_factory,
        workspace_ids=[candidate.workspace_id for candidate in plan.candidates],
    )
    return _gc_result(
        plan=plan,
        dry_run=False,
        deleted_paths=deleted_paths,
        delete_errors=delete_errors,
        path_outcomes=path_outcomes,
        compose_teardowns=compose_teardowns,
        secret_lease_revocations=secret_lease_revocations,
        worktree_removes=worktree_removes,
        reservation_releases=reservation_releases,
    )


async def _revoke_gc_secret_leases(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    workspace_ids: list[str],
    now: datetime,
) -> dict[str, dict[str, object]]:
    summaries: dict[str, dict[str, object]] = {}
    if not workspace_ids:
        return summaries
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        service = SecretLeaseService(session)
        for workspace_id in workspace_ids:
            workspace = await repo.get(workspace_id)
            if workspace is None:
                continue
            revoked = await service.revoke_workspace_secret_leases(
                workspace,
                now=now,
                reason_code=TERMINAL_GC_REVOKE_REASON,
            )
            summaries[workspace_id] = secret_lease_revocation_summary(
                revoked,
                reason_code=TERMINAL_GC_REVOKE_REASON,
            )
        await session.commit()
    return summaries


def _unmount_candidate_auth_overlay(
    candidate: WorkspaceGCCandidate,
    *,
    work_dir: Path,
) -> None:
    """Unmount a GC candidate's Claude auth overlay before its dir is removed.

    Unmount-before-remove: a live overlay mount at ``auth/<id>/claude/merged``
    makes the subsequent ``rmtree`` of ``auth/<id>`` fail with ``EBUSY`` and
    strands both the mount and the auth dir. The PR monitor unmounts before its
    own filesystem GC, but ``POST /v1/service/gc`` funnels through here too, so
    doing it centrally covers both entrypoints. Best-effort and idempotent: a
    no-op when nothing is mounted, and a genuine umount failure is logged and
    swallowed so the per-path delete still records any residual ``EBUSY`` rather
    than aborting the whole run.
    """

    from awf.node.auth_mounts import teardown_workspace_auth_overlay

    try:
        teardown_workspace_auth_overlay(work_dir=work_dir, workspace_id=candidate.workspace_id)
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning(
            "gc.auth_overlay_unmount_failed",
            reason_code="CLAUDE_AUTH_OVERLAY_UNMOUNT_FAILED",
            workspace_id=candidate.workspace_id,
            error=repr(exc)[:400],
        )


async def _delete_gc_plan_paths(
    plan: WorkspaceGCPlan,
    *,
    compose_teardown: WorkspaceGCComposeTeardown | None,
    worktree_remover: WorkspaceGCWorktreeRemove | None,
) -> tuple[
    list[Path],
    list[WorkspaceGCDeleteError],
    list[WorkspaceGCPathOutcome],
    dict[str, WorkspaceGCComposeTeardownResult],
    dict[str, WorkspaceGCWorktreeRemoveResult],
]:
    deleted_paths: list[Path] = []
    delete_errors: list[WorkspaceGCDeleteError] = []
    path_outcomes: list[WorkspaceGCPathOutcome] = []
    compose_teardowns: dict[str, WorkspaceGCComposeTeardownResult] = {}
    worktree_removes: dict[str, WorkspaceGCWorktreeRemoveResult] = {}
    for candidate in plan.candidates:
        teardown = await _run_compose_teardown(candidate, compose_teardown)
        if teardown is not None:
            compose_teardowns[candidate.workspace_id] = teardown
            if not teardown.ok:
                delete_errors.append(
                    WorkspaceGCDeleteError(
                        workspace_id=candidate.workspace_id,
                        kind="compose_teardown",
                        path=candidate.compose.path,
                        error=teardown.error or teardown.reason_code,
                        reason_code=teardown.reason_code,
                    )
                )
                for target in candidate.paths():
                    path_outcomes.append(
                        WorkspaceGCPathOutcome(
                            workspace_id=candidate.workspace_id,
                            kind=target.kind,
                            path=target.path,
                            status="skipped",
                            reason_code=teardown.reason_code,
                            error=teardown.error,
                            estimated_bytes=target.estimated_bytes,
                        )
                    )
                continue
        # Unmount the Claude auth overlay only *after* the compose stack is torn
        # down. While the agent container is up it bind-mounts the overlay
        # ``merged`` dir, so a pre-teardown umount fails ``EBUSY`` (and is swallowed
        # by ``_unmount_candidate_auth_overlay``); nothing would retry it before the
        # auth-dir ``rmtree`` below, stranding the still-mounted overlay. Teardown
        # stops the container first, so the umount here releases the mount and the
        # auth dir can be removed. When teardown failed above we ``continue`` without
        # reaching here — the stack is still up and no paths are deleted, so there is
        # nothing to strand.
        await asyncio.to_thread(_unmount_candidate_auth_overlay, candidate, work_dir=plan.work_dir)
        wt_remove = await _run_worktree_remove(candidate, worktree_remover)
        if wt_remove is not None:
            worktree_removes[candidate.workspace_id] = wt_remove
            if not wt_remove.ok:
                delete_errors.extend(_worktree_remove_delete_errors(candidate, wt_remove))
                blocked_worktree_paths = _blocked_worktree_paths_after_remove(candidate, wt_remove)
                target_results_by_id = {
                    target.worktree_id: target for target in wt_remove.target_results
                }
                for skipped_target in (candidate.worktree, *candidate.companion_worktrees):
                    if skipped_target.path not in blocked_worktree_paths:
                        continue
                    worktree_id = _worktree_id_for_gc_path(candidate, skipped_target)
                    target_result = target_results_by_id.get(worktree_id)
                    path_outcomes.append(
                        WorkspaceGCPathOutcome(
                            workspace_id=candidate.workspace_id,
                            kind=skipped_target.kind,
                            path=skipped_target.path,
                            status="skipped",
                            reason_code=(
                                target_result.reason_code
                                if target_result is not None
                                else wt_remove.reason_code
                            ),
                            error=(
                                target_result.error
                                if target_result is not None
                                else wt_remove.error
                            ),
                            estimated_bytes=skipped_target.estimated_bytes,
                        )
                    )
                for target in candidate.paths():
                    if target.path in blocked_worktree_paths:
                        continue
                    outcome = await asyncio.to_thread(
                        _delete_gc_path_outcome,
                        candidate,
                        target,
                        work_dir=plan.work_dir,
                    )
                    path_outcomes.append(outcome)
                    if outcome.deleted:
                        deleted_paths.append(target.path)
                    if outcome.error is not None:
                        delete_errors.append(
                            WorkspaceGCDeleteError(
                                workspace_id=candidate.workspace_id,
                                kind=target.kind,
                                path=target.path,
                                error=outcome.error,
                                reason_code=outcome.reason_code,
                            )
                        )
                continue
        for target in candidate.paths():
            outcome = await asyncio.to_thread(
                _delete_gc_path_outcome,
                candidate,
                target,
                work_dir=plan.work_dir,
            )
            path_outcomes.append(outcome)
            if outcome.deleted:
                deleted_paths.append(target.path)
            if outcome.error is not None:
                delete_errors.append(
                    WorkspaceGCDeleteError(
                        workspace_id=candidate.workspace_id,
                        kind=target.kind,
                        path=target.path,
                        error=outcome.error,
                        reason_code=outcome.reason_code,
                    )
                )
    return deleted_paths, delete_errors, path_outcomes, compose_teardowns, worktree_removes


def _worktree_remove_delete_errors(
    candidate: WorkspaceGCCandidate,
    worktree_remove: WorkspaceGCWorktreeRemoveResult,
) -> list[WorkspaceGCDeleteError]:
    worktree_paths_by_id = _worktree_paths_by_id(candidate)
    delete_errors: list[WorkspaceGCDeleteError] = []
    for target in worktree_remove.target_results:
        if target.status != "failed":
            continue
        target_path = worktree_paths_by_id.get(target.worktree_id)
        if target_path is None:
            continue
        delete_errors.append(
            WorkspaceGCDeleteError(
                workspace_id=candidate.workspace_id,
                kind="worktree_remove",
                path=target_path,
                error=target.error or worktree_remove.error or target.reason_code,
                reason_code=target.reason_code,
            )
        )
    if delete_errors:
        return delete_errors
    return [
        WorkspaceGCDeleteError(
            workspace_id=candidate.workspace_id,
            kind="worktree_remove",
            path=candidate.worktree.path,
            error=worktree_remove.error or worktree_remove.reason_code,
            reason_code=worktree_remove.reason_code,
        )
    ]


async def _run_compose_teardown(
    candidate: WorkspaceGCCandidate,
    compose_teardown: WorkspaceGCComposeTeardown | None,
) -> WorkspaceGCComposeTeardownResult | None:
    if compose_teardown is None:
        return None
    result = compose_teardown(candidate)
    if isawaitable(result):
        result = await result
    return result


async def _release_gc_reservations(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    workspace_ids: list[str],
) -> dict[str, dict[str, object]]:
    from awf.db.repositories import ResourceReservationRepository

    summaries: dict[str, dict[str, object]] = {}
    if not workspace_ids:
        return summaries
    for workspace_id in workspace_ids:
        async with session_factory() as session:
            repo = ResourceReservationRepository(session)
            try:
                released = await repo.release_active_for_workspace(workspace_id)
                await session.commit()
                summaries[workspace_id] = {
                    "released_count": len(released),
                    "reason_code": "TERMINAL_GC",
                }
            except Exception as exc:
                await session.rollback()
                summaries[workspace_id] = {
                    "released_count": 0,
                    "reason_code": "TERMINAL_GC",
                    "error": str(exc),
                }
    return summaries


def _delete_gc_path_outcome(
    candidate: WorkspaceGCCandidate,
    target: WorkspaceGCPath,
    *,
    work_dir: Path,
) -> WorkspaceGCPathOutcome:
    # Route the not-exists preflight through ``_delete_gc_path`` rather than a
    # bare ``target.path.exists()`` here: an unguarded probe would raise (and
    # abort the whole execute run) when a root-owned ``0700`` parent cannot be
    # stat'ed, instead of being recorded as ``PATH_DELETE_PERMISSION_DENIED``.
    # ``_delete_gc_path`` already wraps the same probe in permission-aware
    # handling, so let it own that path. It returns ``(False, None, None)`` for a
    # genuinely absent path.
    deleted, error, failure_reason_code = _delete_gc_path(target, work_dir=work_dir)
    if deleted:
        return WorkspaceGCPathOutcome(
            workspace_id=candidate.workspace_id,
            kind=target.kind,
            path=target.path,
            status="deleted",
            reason_code=PATH_DELETED,
            deleted=True,
            estimated_bytes=target.estimated_bytes,
        )
    if error is None and failure_reason_code in (None, PATH_ALREADY_REMOVED):
        # The path was already absent (``None``) or vanished mid-delete during a
        # concurrent GC run (``PATH_ALREADY_REMOVED``). Both are idempotent
        # no-ops, not partial-run failures.
        return WorkspaceGCPathOutcome(
            workspace_id=candidate.workspace_id,
            kind=target.kind,
            path=target.path,
            status="already_removed",
            reason_code=PATH_ALREADY_REMOVED,
            estimated_bytes=target.estimated_bytes,
        )
    return WorkspaceGCPathOutcome(
        workspace_id=candidate.workspace_id,
        kind=target.kind,
        path=target.path,
        status="failed",
        reason_code=failure_reason_code or PATH_DELETE_FAILED,
        error=error or "path was not deleted",
        estimated_bytes=target.estimated_bytes,
    )


def _gc_result(
    *,
    plan: WorkspaceGCPlan,
    dry_run: bool,
    deleted_paths: list[Path],
    delete_errors: list[WorkspaceGCDeleteError],
    path_outcomes: list[WorkspaceGCPathOutcome],
    compose_teardowns: dict[str, WorkspaceGCComposeTeardownResult],
    secret_lease_revocations: dict[str, dict[str, object]] | None = None,
    worktree_removes: dict[str, WorkspaceGCWorktreeRemoveResult] | None = None,
    reservation_releases: dict[str, dict[str, object]] | None = None,
    companion_image_prune: dict[str, object] | None = None,
) -> WorkspaceGCResult:
    lease_revocations = secret_lease_revocations or {}
    wt_removes = worktree_removes or {}
    res_releases = reservation_releases or {}
    if dry_run:
        return WorkspaceGCResult(
            plan=plan,
            dry_run=True,
            deleted_paths=deleted_paths,
            delete_errors=delete_errors,
            path_outcomes=path_outcomes,
            compose_teardowns=compose_teardowns,
            secret_lease_revocations=lease_revocations,
            worktree_removes=wt_removes,
            reservation_releases=res_releases,
            status="dry_run",
            reason_code=CLEANUP_DRY_RUN,
        )
    companion_prune_failed = (
        companion_image_prune is not None and companion_image_prune.get("status") == "failed"
    )
    has_errors = (
        bool(delete_errors)
        or any(v.get("error") is not None for v in res_releases.values())
        or companion_prune_failed
    )
    status: WorkspaceCleanupExecutionStatus = "partial" if has_errors else "succeeded"
    return WorkspaceGCResult(
        plan=plan,
        dry_run=False,
        deleted_paths=deleted_paths,
        delete_errors=delete_errors,
        path_outcomes=path_outcomes,
        compose_teardowns=compose_teardowns,
        secret_lease_revocations=lease_revocations,
        worktree_removes=wt_removes,
        reservation_releases=res_releases,
        status=status,
        reason_code=(CLEANUP_EXECUTION_PARTIAL if has_errors else CLEANUP_EXECUTION_SUCCEEDED),
        companion_image_prune=companion_image_prune,
    )


def _candidate_for_workspace(
    workspace: Workspace,
    *,
    work_dir: Path,
    now: datetime,
    reason_code: str,
) -> WorkspaceGCCandidate:
    updated_at = _to_utc(workspace.updated_at)
    age_hours = max(0, int((now - updated_at).total_seconds() // 3600))
    worktree_path = work_dir / "git" / "worktrees" / workspace.id
    companion_worktrees = tuple(
        _gc_path(f"companion_worktree:{path.name}", path)
        for path in companion_worktree_paths_for_gc(workspace, work_dir=work_dir)
    )
    compose_path = (
        Path(workspace.compose_file_path).expanduser().parent
        if workspace.compose_file_path
        else work_dir / "compose" / workspace.id
    )
    auth_path = work_dir / "auth" / workspace.id
    return WorkspaceGCCandidate(
        workspace_id=workspace.id,
        status=workspace.status,
        updated_at=updated_at,
        age_hours=age_hours,
        reason_code=reason_code,
        worktree=_gc_path("worktree", worktree_path),
        companion_worktrees=companion_worktrees,
        compose=_gc_path("compose", compose_path),
        auth=_gc_path("auth", auth_path),
        compose_project_name=workspace.compose_project_name,
        compose_file_path=workspace.compose_file_path,
    )


def _classify_workspace_for_gc(
    workspace: Workspace,
    *,
    work_dir: Path,
    now: datetime,
    cutoff_at: datetime,
    default_policy: bool,
    cleanup_enabled: bool,
    ignore_retention: bool = False,
    preserved_failed_cutoff_at: datetime | None = None,
) -> WorkspaceGCCandidate | WorkspaceGCPreserved | None:
    """Classify one workspace for GC into candidate / preserved / skip.

    ``preserved_failed_cutoff_at`` caps how long a failed/superseded workspace
    whose work was preserved for triage is retained. When set and the workspace
    last changed at or before it, the pressure dirs are reclaimed under
    ``PRESERVED_FAILED_AGE_CAP_RECLAIMED`` (the durable record — DB row, events,
    logs — is kept, since GC never deletes it). ``None`` (the default) preserves
    indefinitely, matching the prior behavior for callers that don't set the cap.

    ``ignore_retention`` is only consulted on the ``default_policy=True`` →
    ``completed`` + merged-PR branch, where it bypasses the retention window for
    a workspace whose pressure dirs are already disposable. It is a no-op on
    every other branch, so passing ``ignore_retention=True`` with
    ``default_policy=False`` would silently apply normal retention. That is a
    programming error rather than a supported mode, so it is rejected loudly
    instead of being ignored.
    """
    if ignore_retention and not default_policy:
        raise ValueError("ignore_retention=True requires default_policy=True")
    if workspace.status in PROTECTED_WORKSPACE_GC_STATUSES:
        return None
    if workspace.status not in TERMINAL_WORKSPACE_GC_STATUSES:
        return None

    updated_at = _to_utc(workspace.updated_at)
    age_hours = max(0, int((now - updated_at).total_seconds() // 3600))
    if not cleanup_enabled:
        return WorkspaceGCPreserved(
            workspace_id=workspace.id,
            status=workspace.status,
            updated_at=updated_at,
            age_hours=age_hours,
            reason_code=WORKSPACE_CLEANUP_DISABLED,
        )

    def _preserved_failed_or_age_capped() -> WorkspaceGCCandidate | WorkspaceGCPreserved:
        # Work was preserved for triage. Reap pressure dirs once past the cap;
        # otherwise keep the record (and its disk) for inspection.
        if preserved_failed_cutoff_at is not None and updated_at <= preserved_failed_cutoff_at:
            return _candidate_for_workspace(
                workspace,
                work_dir=work_dir,
                now=now,
                reason_code=PRESERVED_FAILED_AGE_CAP_RECLAIMED,
            )
        return WorkspaceGCPreserved(
            workspace_id=workspace.id,
            status=workspace.status,
            updated_at=updated_at,
            age_hours=age_hours,
            reason_code=FAILED_WORKSPACE_TRIAGE_PRESERVED,
        )

    if default_policy:
        if workspace.status == WorkspaceStatus.failed.value:
            if _failed_terminal_workspace_has_no_work(workspace):
                if updated_at <= cutoff_at:
                    return _candidate_for_workspace(
                        workspace,
                        work_dir=work_dir,
                        now=now,
                        reason_code=FAILED_WORKSPACE_NO_WORK,
                    )
                return WorkspaceGCPreserved(
                    workspace_id=workspace.id,
                    status=workspace.status,
                    updated_at=updated_at,
                    age_hours=age_hours,
                    reason_code=WORKSPACE_WITHIN_RETENTION,
                )
            return _preserved_failed_or_age_capped()
        if workspace.status == "superseded":
            if _failed_terminal_workspace_has_no_work(workspace):
                if updated_at <= cutoff_at:
                    return _candidate_for_workspace(
                        workspace,
                        work_dir=work_dir,
                        now=now,
                        reason_code=FAILED_WORKSPACE_NO_WORK,
                    )
                return WorkspaceGCPreserved(
                    workspace_id=workspace.id,
                    status=workspace.status,
                    updated_at=updated_at,
                    age_hours=age_hours,
                    reason_code=WORKSPACE_WITHIN_RETENTION,
                )
            return _preserved_failed_or_age_capped()
        if workspace.status != WorkspaceStatus.completed.value:
            return None
        if not _has_pr_metadata(workspace):
            return WorkspaceGCPreserved(
                workspace_id=workspace.id,
                status=workspace.status,
                updated_at=updated_at,
                age_hours=age_hours,
                reason_code=COMPLETED_WORKSPACE_WITHOUT_PR,
            )
        if not _pr_has_merged(workspace):
            return WorkspaceGCPreserved(
                workspace_id=workspace.id,
                status=workspace.status,
                updated_at=updated_at,
                age_hours=age_hours,
                reason_code=COMPLETED_PR_NOT_MERGED,
            )
        # A merged PR's pressure dirs are disposable the moment it lands. With
        # ``ignore_retention`` the post-merge caller reclaims them immediately
        # instead of waiting out the retention window; the durable record (DB
        # row, events, logs) is preserved either way (GC never deletes it).
        if updated_at > cutoff_at and not ignore_retention:
            return WorkspaceGCPreserved(
                workspace_id=workspace.id,
                status=workspace.status,
                updated_at=updated_at,
                age_hours=age_hours,
                reason_code=WORKSPACE_WITHIN_RETENTION,
            )
        # Distinguish an immediate post-merge reclaim (``ignore_retention``
        # bypassed the window) from one that naturally aged out, so audit logs
        # and ``WorkspaceEvent`` trails do not mislabel a minutes-old workspace
        # as "retention expired".
        return _candidate_for_workspace(
            workspace,
            work_dir=work_dir,
            now=now,
            reason_code=(
                COMPLETED_PR_IMMEDIATE_RECLAIM
                if ignore_retention
                else COMPLETED_PR_RETENTION_EXPIRED
            ),
        )

    if updated_at > cutoff_at:
        return WorkspaceGCPreserved(
            workspace_id=workspace.id,
            status=workspace.status,
            updated_at=updated_at,
            age_hours=age_hours,
            reason_code=WORKSPACE_WITHIN_RETENTION,
        )
    if (
        workspace.status in _FAILED_NO_WORK_TERMINAL_STATUSES
        and workspace.compose_project_name is not None
        and not _failed_terminal_workspace_has_no_work(workspace)
    ):
        return WorkspaceGCPreserved(
            workspace_id=workspace.id,
            status=workspace.status,
            updated_at=updated_at,
            age_hours=age_hours,
            reason_code=TERMINAL_WORKSPACE_RETENTION_EXPIRED,
        )

    if workspace.status in _FAILED_NO_WORK_TERMINAL_STATUSES:
        return _candidate_for_workspace(
            workspace,
            work_dir=work_dir,
            now=now,
            reason_code=FAILED_WORKSPACE_NO_WORK,
        )

    reason_code = (
        COMPLETED_PR_RETENTION_EXPIRED
        if workspace.status == WorkspaceStatus.completed.value and _has_pr_metadata(workspace)
        else TERMINAL_WORKSPACE_RETENTION_EXPIRED
    )
    return _candidate_for_workspace(
        workspace,
        work_dir=work_dir,
        now=now,
        reason_code=reason_code,
    )


def _failed_terminal_workspace_has_no_work(workspace: Workspace) -> bool:
    """Return True when a failed terminal workspace has no active agent work."""

    compose_project_name = _compose_project_name_for_workspace(workspace)
    if compose_project_name is None:
        return False
    try:
        snapshot = asyncio.run(_RUNTIME_INSPECTOR.inspect(compose_project_name))
    except Exception:
        return False
    return _snapshot_has_no_work(snapshot)
