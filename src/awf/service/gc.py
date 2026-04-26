"""Filesystem garbage collection for terminal service workspaces.

This module only relieves disk pressure from per-workspace runtime directories.
It deliberately does not delete control-plane rows, workspace events, or durable
log streams.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace

DEFAULT_MIN_AGE_HOURS = 168

TERMINAL_WORKSPACE_GC_STATUSES = frozenset(
    {
        WorkspaceStatus.completed.value,
        WorkspaceStatus.failed.value,
        WorkspaceStatus.cancelled.value,
        WorkspaceStatus.destroyed.value,
    }
)

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
class WorkspaceGCPath:
    """One filesystem target considered for workspace GC."""

    kind: str
    path: Path
    exists: bool
    estimated_bytes: int

    def to_dict(self, *, deleted: bool = False, error: str | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            "path": str(self.path),
            "exists": self.exists,
            "estimated_bytes": self.estimated_bytes,
            "deleted": deleted,
        }
        if error is not None:
            payload["error"] = error
        return payload


@dataclass(frozen=True)
class WorkspaceGCCandidate:
    """A terminal workspace whose pressure directories are eligible for GC."""

    workspace_id: str
    status: str
    updated_at: datetime
    age_hours: int
    worktree: WorkspaceGCPath
    compose: WorkspaceGCPath
    auth: WorkspaceGCPath

    @property
    def total_estimated_bytes(self) -> int:
        return (
            self.worktree.estimated_bytes + self.compose.estimated_bytes + self.auth.estimated_bytes
        )

    def paths(self) -> Iterator[WorkspaceGCPath]:
        yield self.worktree
        yield self.compose
        yield self.auth

    def to_dict(
        self,
        *,
        deleted_paths: set[Path] | None = None,
        delete_errors: dict[tuple[str, Path], str] | None = None,
    ) -> dict[str, object]:
        deleted_paths = deleted_paths or set()
        delete_errors = delete_errors or {}
        paths = {
            item.kind: item.to_dict(
                deleted=item.path in deleted_paths,
                error=delete_errors.get((item.kind, item.path)),
            )
            for item in self.paths()
        }
        return {
            "workspace_id": self.workspace_id,
            "status": self.status,
            "updated_at": self.updated_at.isoformat(),
            "age_hours": self.age_hours,
            "estimated_bytes": {
                "worktree": self.worktree.estimated_bytes,
                "compose": self.compose.estimated_bytes,
                "auth": self.auth.estimated_bytes,
                "total": self.total_estimated_bytes,
            },
            "paths": paths,
        }


@dataclass(frozen=True)
class WorkspaceGCPlan:
    """Inspectable GC plan before deletion."""

    work_dir: Path
    min_age_hours: float
    cutoff_at: datetime
    include_statuses: tuple[str, ...]
    exclude_statuses: tuple[str, ...]
    candidates: list[WorkspaceGCCandidate]

    @property
    def total_estimated_bytes(self) -> int:
        return sum(candidate.total_estimated_bytes for candidate in self.candidates)

    def to_dict(self) -> dict[str, object]:
        return {
            "work_dir": str(self.work_dir),
            "min_age_hours": self.min_age_hours,
            "cutoff_at": self.cutoff_at.isoformat(),
            "include_statuses": list(self.include_statuses),
            "exclude_statuses": list(self.exclude_statuses),
            "candidate_count": len(self.candidates),
            "total_estimated_bytes": self.total_estimated_bytes,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True)
class WorkspaceGCDeleteError:
    """One deletion failure captured without aborting the rest of the GC run."""

    workspace_id: str
    kind: str
    path: Path
    error: str

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "kind": self.kind,
            "path": str(self.path),
            "error": self.error,
        }


@dataclass(frozen=True)
class WorkspaceGCResult:
    """GC plan plus optional execution outcome."""

    plan: WorkspaceGCPlan
    dry_run: bool
    deleted_paths: list[Path]
    delete_errors: list[WorkspaceGCDeleteError]

    def to_dict(self) -> dict[str, object]:
        deleted_paths = set(self.deleted_paths)
        delete_errors = {(error.kind, error.path): error.error for error in self.delete_errors}
        payload = self.plan.to_dict()
        payload.update(
            {
                "dry_run": self.dry_run,
                "deleted_paths": [str(path) for path in self.deleted_paths],
                "deleted_path_count": len(self.deleted_paths),
                "delete_errors": [error.to_dict() for error in self.delete_errors],
            }
        )
        payload["candidates"] = [
            candidate.to_dict(
                deleted_paths=deleted_paths,
                delete_errors=delete_errors,
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
    now: datetime | None = None,
) -> WorkspaceGCPlan:
    """Build a terminal-workspace filesystem cleanup plan.

    Active and destroying workspaces are never eligible, even if explicitly
    requested through ``include_statuses``.
    """

    current_time = _to_utc(now or datetime.now(UTC))
    normalized_work_dir = Path(work_dir).expanduser()
    cutoff_at = current_time - timedelta(hours=min_age_hours)
    requested_statuses = _normalize_statuses(include_statuses)
    excluded_statuses = _normalize_statuses(exclude_statuses) or set()
    eligible_statuses = (
        set(TERMINAL_WORKSPACE_GC_STATUSES)
        if requested_statuses is None
        else requested_statuses & set(TERMINAL_WORKSPACE_GC_STATUSES)
    )
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
        )

    async with session_factory() as session:
        stmt = (
            select(Workspace)
            .where(Workspace.status.in_(sorted(eligible_statuses)))
            .where(Workspace.updated_at <= cutoff_at)
            .order_by(Workspace.updated_at.asc(), Workspace.id.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = list((await session.execute(stmt)).scalars())

    candidates = [
        _candidate_for_workspace(
            workspace,
            work_dir=normalized_work_dir,
            now=current_time,
        )
        for workspace in rows
    ]
    return WorkspaceGCPlan(
        work_dir=normalized_work_dir,
        min_age_hours=min_age_hours,
        cutoff_at=cutoff_at,
        include_statuses=tuple(sorted(plan_include_statuses)),
        exclude_statuses=tuple(sorted(excluded_statuses)),
        candidates=candidates,
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
    now: datetime | None = None,
) -> WorkspaceGCResult:
    """Plan terminal workspace GC and optionally delete selected directories."""

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=min_age_hours,
        limit=limit,
        include_statuses=include_statuses,
        exclude_statuses=exclude_statuses,
        now=now,
    )
    if not execute:
        return WorkspaceGCResult(
            plan=plan,
            dry_run=True,
            deleted_paths=[],
            delete_errors=[],
        )

    deleted_paths, delete_errors = _delete_gc_plan_paths(plan)
    return WorkspaceGCResult(
        plan=plan,
        dry_run=False,
        deleted_paths=deleted_paths,
        delete_errors=delete_errors,
    )


async def run_workspace_filesystem_gc(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    work_dir: Path | str,
    workspace_id: str,
    execute: bool = False,
    now: datetime | None = None,
) -> WorkspaceGCResult:
    """Plan or execute filesystem GC for one terminal workspace.

    This is used by the PR monitor after a successful merge. It keeps the
    durable workspace row, events, logs, and artifacts intact while removing
    the checkout/auth/compose pressure directories for the single completed
    workspace.
    """

    current_time = _to_utc(now or datetime.now(UTC))
    normalized_work_dir = Path(work_dir).expanduser()
    async with session_factory() as session:
        workspace = await session.get(Workspace, workspace_id)

    candidates: list[WorkspaceGCCandidate] = []
    include_statuses: tuple[str, ...] = ()
    if workspace is not None:
        include_statuses = (workspace.status,)
        if (
            workspace.status in TERMINAL_WORKSPACE_GC_STATUSES
            and workspace.status not in PROTECTED_WORKSPACE_GC_STATUSES
        ):
            candidates.append(
                _candidate_for_workspace(
                    workspace,
                    work_dir=normalized_work_dir,
                    now=current_time,
                )
            )

    plan = WorkspaceGCPlan(
        work_dir=normalized_work_dir,
        min_age_hours=0,
        cutoff_at=current_time,
        include_statuses=include_statuses,
        exclude_statuses=(),
        candidates=candidates,
    )
    if not execute:
        return WorkspaceGCResult(
            plan=plan,
            dry_run=True,
            deleted_paths=[],
            delete_errors=[],
        )

    deleted_paths, delete_errors = _delete_gc_plan_paths(plan)
    return WorkspaceGCResult(
        plan=plan,
        dry_run=False,
        deleted_paths=deleted_paths,
        delete_errors=delete_errors,
    )


def _delete_gc_plan_paths(
    plan: WorkspaceGCPlan,
) -> tuple[list[Path], list[WorkspaceGCDeleteError]]:
    deleted_paths: list[Path] = []
    delete_errors: list[WorkspaceGCDeleteError] = []
    for candidate in plan.candidates:
        for target in candidate.paths():
            deleted, error = _delete_gc_path(target, work_dir=plan.work_dir)
            if deleted:
                deleted_paths.append(target.path)
            if error is not None:
                delete_errors.append(
                    WorkspaceGCDeleteError(
                        workspace_id=candidate.workspace_id,
                        kind=target.kind,
                        path=target.path,
                        error=error,
                    )
                )
    return deleted_paths, delete_errors


def _candidate_for_workspace(
    workspace: Workspace,
    *,
    work_dir: Path,
    now: datetime,
) -> WorkspaceGCCandidate:
    updated_at = _to_utc(workspace.updated_at)
    age_hours = max(0, int((now - updated_at).total_seconds() // 3600))
    worktree_path = work_dir / "git" / "worktrees" / workspace.id
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
        worktree=_gc_path("worktree", worktree_path),
        compose=_gc_path("compose", compose_path),
        auth=_gc_path("auth", auth_path),
    )


def _gc_path(kind: str, path: Path) -> WorkspaceGCPath:
    return WorkspaceGCPath(
        kind=kind,
        path=path,
        exists=path.exists(),
        estimated_bytes=_estimate_bytes(path),
    )


def _estimate_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _delete_gc_path(target: WorkspaceGCPath, *, work_dir: Path) -> tuple[bool, str | None]:
    if not target.path.exists():
        return False, None
    if not _is_safe_gc_path(target, work_dir=work_dir):
        return False, "path is outside the expected service GC roots"
    if target.path.is_symlink():
        return False, "refusing to delete symlink"
    if not target.path.is_dir():
        return False, "refusing to delete non-directory path"
    try:
        shutil.rmtree(target.path)
    except OSError as exc:
        return False, str(exc)
    return True, None


def _is_safe_gc_path(target: WorkspaceGCPath, *, work_dir: Path) -> bool:
    roots = {
        "worktree": work_dir / "git" / "worktrees",
        "compose": work_dir / "compose",
        "auth": work_dir / "auth",
    }
    root = roots.get(target.kind)
    if root is None:
        return False
    try:
        target.path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _normalize_statuses(
    statuses: Iterable[WorkspaceStatus | str] | None,
) -> set[str] | None:
    if statuses is None:
        return None
    return {
        status.value if isinstance(status, WorkspaceStatus) else str(status) for status in statuses
    }


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
