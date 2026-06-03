"""Filesystem-vs-DB reconciler for orphaned per-workspace directories.

WS-B1 (PR #372) gave AWF a reclaim *engine* that deletes per-workspace pressure
directories as **root** through the control-plane and reaps the per-workspace
Docker volumes via a ``compose_teardown`` callback. That engine, however,
selects candidates **by DB row**: a per-workspace directory whose ``workspaces``
row no longer exists (``auth/<id>``, ``git/worktrees/<id>``, ``compose/<id>``) is
unreachable and orphans forever.

This module closes that gap. It scans the three per-workspace roots, cross-
references the live workspace-id set, and reaps directories whose row is gone --
reusing WS-B1's deletion primitive (:func:`awf.service.gc_classify._delete_gc_path`,
permission-aware and safe-root-checked) and its volume-removing compose teardown
(:meth:`awf.node.compose_manager.ComposeManager.teardown_project`) rather than
re-implementing either.

Safety properties (see ``docs/awf-plans/ws_e3ba9b641bb5453bbaf76e38.md``):

- **Row-less only.** Any existing row (across *all* statuses) protects its dirs,
  so a transitional or still-provisioning workspace is never reaped.
- **Minimum-age grace.** A row-less dir younger than ``min_age_hours`` is left
  alone, so a dir created just before its row commits is never reaped mid-flight.
- **Bounded + logged.** The sweep caps how many dirs it reaps per run and logs
  the cap and dropped count; it never silently truncates.
- **Loud on refusal.** A permission-denied reap surfaces
  ``PATH_DELETE_PERMISSION_DENIED`` and makes the run ``partial`` -- never a
  silent success.
- **Idempotent.** An already-absent dir is a no-op (``PATH_ALREADY_REMOVED``);
  re-running after a full reap finds zero orphans.

Phase 1 is single-node: the scan operates on the local ``work_dir``. A
multi-node sweep (mirroring the terminal-runtime release sweep) is future work.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.companions import parent_workspace_id_from_companion_worktree_id
from awf.common.logging import get_logger
from awf.db.models import Workspace
from awf.db.session import session_scope
from awf.service.gc import DEFAULT_MIN_AGE_HOURS
from awf.service.gc_classify import (
    PATH_ALREADY_REMOVED,
    PATH_DELETED,
    _delete_gc_path,
    _gc_path,
)

if TYPE_CHECKING:
    from awf.node.compose_manager import ComposeManager

_log = get_logger(__name__)

OrphanDirKind = Literal["auth", "worktree", "compose"]

# Status/reason codes for the reconcile run as a whole. Per-path outcomes reuse
# WS-B1's ``PATH_*`` codes so the loud-refusal / idempotent-already-removed
# semantics stay identical to terminal GC.
ORPHAN_DIR_REAP_OK = "ORPHAN_DIR_REAP_OK"
ORPHAN_DIR_REAP_PARTIAL = "ORPHAN_DIR_REAP_PARTIAL"
ORPHAN_DIR_REAP_DRY_RUN = "ORPHAN_DIR_REAP_DRY_RUN"
ORPHAN_DIR_REAP_CAP_HIT = "ORPHAN_DIR_REAP_CAP_HIT"

DEFAULT_ORPHAN_RECONCILE_LIMIT = 50

# (kind, relative path parts under work_dir). The kind doubles as the
# ``_is_safe_gc_path`` root key, so it must stay one of the safe-root kinds.
_ORPHAN_DIR_ROOTS: tuple[tuple[OrphanDirKind, tuple[str, ...]], ...] = (
    ("auth", ("auth",)),
    ("worktree", ("git", "worktrees")),
    ("compose", ("compose",)),
)


class _ComposeTeardownOutcome(Protocol):
    """Structural type for a compose teardown result (e.g. ``ComposeTeardownResult``).

    All members are read-only properties so a frozen dataclass with narrower
    (``Literal``) field types still satisfies the protocol structurally.
    """

    @property
    def status(self) -> str: ...  # pragma: no cover - Protocol declaration only.

    @property
    def reason_code(self) -> str: ...  # pragma: no cover - Protocol declaration only.

    @property
    def error(self) -> str | None: ...  # pragma: no cover - Protocol declaration only.

    @property
    def ok(self) -> bool: ...  # pragma: no cover - Protocol declaration only.


OrphanComposeTeardown = Callable[["OrphanDirTarget"], Awaitable[_ComposeTeardownOutcome]]


@dataclass(frozen=True)
class OrphanDirTarget:
    """One per-workspace directory whose workspace row no longer exists."""

    kind: OrphanDirKind
    workspace_id: str
    path: Path
    age_seconds: float

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "workspace_id": self.workspace_id,
            "path": str(self.path),
            "age_seconds": round(self.age_seconds, 3),
        }


@dataclass(frozen=True)
class OrphanDirReapOutcome:
    """Structured execution outcome for one orphan directory."""

    target: OrphanDirTarget
    status: Literal["deleted", "already_removed", "skipped", "failed"]
    reason_code: str
    deleted: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"deleted", "already_removed"}

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            **self.target.to_dict(),
            "status": self.status,
            "reason_code": self.reason_code,
            "deleted": self.deleted,
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True)
class OrphanDirReconcileResult:
    """Inspectable result of one reconcile sweep."""

    status: Literal["ok", "partial", "dry_run"]
    reason_code: str
    scanned_count: int
    orphan_count: int
    reaped_count: int
    cap_limit: int
    capped: bool
    dropped_count: int
    reaped: tuple[OrphanDirReapOutcome, ...] = ()
    errors: tuple[OrphanDirReapOutcome, ...] = ()
    compose_teardowns: dict[str, dict[str, object]] = field(default_factory=dict)
    planned: tuple[OrphanDirTarget, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "scanned_count": self.scanned_count,
            "orphan_count": self.orphan_count,
            "reaped_count": self.reaped_count,
            "cap_limit": self.cap_limit,
            "capped": self.capped,
            "dropped_count": self.dropped_count,
            "reaped": [outcome.to_dict() for outcome in self.reaped],
            "errors": [outcome.to_dict() for outcome in self.errors],
            "compose_teardowns": self.compose_teardowns,
            "planned": [target.to_dict() for target in self.planned],
        }


def scan_orphan_workspace_dirs(
    work_dir: Path,
    known_workspace_ids: frozenset[str] | set[str],
    *,
    now: float,
    min_age_hours: float,
    limit: int,
) -> tuple[tuple[OrphanDirTarget, ...], int, int]:
    """Return ``(targets, scanned_count, dropped_count)`` of orphan candidates.

    Pure: lists ``ws_*`` directory entries under each per-workspace root, maps a
    companion-worktree dir name back to its parent id (so a companion of a *live*
    parent is not mis-classified as orphaned), and keeps only entries whose
    (parent) id is row-less **and** whose mtime is older than the grace window.
    Candidates are sorted oldest-first (deterministic), then capped to ``limit``.
    """
    candidates: list[OrphanDirTarget] = []
    scanned = 0
    grace_seconds = max(0.0, min_age_hours) * 3600.0
    for kind, parts in _ORPHAN_DIR_ROOTS:
        root = work_dir.joinpath(*parts)
        if not root.exists():
            continue
        for entry in sorted(root.iterdir(), key=lambda item: item.name):
            if not entry.name.startswith("ws_"):
                continue
            if entry.is_symlink() or not entry.is_dir():
                continue
            scanned += 1
            owner_id = (
                parent_workspace_id_from_companion_worktree_id(entry.name)
                if kind == "worktree"
                else None
            ) or entry.name
            if owner_id in known_workspace_ids:
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:  # pragma: no cover - root worker can always stat its own dirs.
                continue
            age_seconds = now - mtime
            if age_seconds < grace_seconds:
                continue
            candidates.append(
                OrphanDirTarget(
                    kind=kind,
                    workspace_id=owner_id,
                    path=entry,
                    age_seconds=age_seconds,
                )
            )

    candidates.sort(key=lambda target: (-target.age_seconds, str(target.path)))
    dropped = 0
    if limit >= 0 and len(candidates) > limit:
        dropped = len(candidates) - limit
        candidates = candidates[:limit]
    return tuple(candidates), scanned, dropped


def build_default_compose_teardown(manager: ComposeManager) -> OrphanComposeTeardown:
    """Build a volume-removing compose-teardown closure over a ``ComposeManager``.

    Mirrors WS-B1's ``_service_gc_compose_teardown``: project ``awf_<id>`` and
    compose file ``<compose dir>/compose.yml`` with ``remove_volumes=True``. A
    missing compose file falls back to a label-scoped reap inside
    ``teardown_project`` (idempotent), so an already-removed stack is a safe skip.
    """

    async def _teardown(target: OrphanDirTarget) -> _ComposeTeardownOutcome:
        return await manager.teardown_project(
            project_name=f"awf_{target.workspace_id}",
            compose_file=target.path / "compose.yml",
            workspace_id=target.workspace_id,
            remove_volumes=True,
        )

    return _teardown


async def reconcile_orphaned_workspace_dirs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    work_dir: Path | str,
    compose_teardown: OrphanComposeTeardown | None = None,
    now: float | None = None,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
    limit: int = DEFAULT_ORPHAN_RECONCILE_LIMIT,
    execute: bool = False,
) -> OrphanDirReconcileResult:
    """Reconcile per-workspace dirs against the ``workspaces`` table.

    Loads the full id set across **all** statuses (any row protects its dirs),
    scans the three roots, and -- when ``execute`` -- tears down each orphaned
    compose stack (volumes included) before deleting its dir, then deletes the
    remaining orphan dirs as root via WS-B1's :func:`_delete_gc_path`. With
    ``execute=False`` it plans/reports only (the path used when the
    ``auto_cleanup_orphans`` kill-switch is off) so operators still see the leak.
    """
    normalized_work_dir = Path(work_dir).expanduser().resolve()
    resolved_now = time.time() if now is None else now
    known_ids = await _load_known_workspace_ids(session_factory)

    targets, scanned, dropped = scan_orphan_workspace_dirs(
        normalized_work_dir,
        known_ids,
        now=resolved_now,
        min_age_hours=min_age_hours,
        limit=limit,
    )
    capped = dropped > 0
    orphan_count = len(targets) + dropped

    if capped:
        _log.warning(
            "gc.orphan_dir_reconcile.capped",
            reason_code=ORPHAN_DIR_REAP_CAP_HIT,
            cap_limit=limit,
            orphan_count=orphan_count,
            dropped_count=dropped,
        )

    if not execute:
        result = OrphanDirReconcileResult(
            status="dry_run",
            reason_code=ORPHAN_DIR_REAP_DRY_RUN,
            scanned_count=scanned,
            orphan_count=orphan_count,
            reaped_count=0,
            cap_limit=limit,
            capped=capped,
            dropped_count=dropped,
            planned=targets,
        )
        _log_summary(result, executed=False)
        return result

    resolved_teardown = compose_teardown or build_default_compose_teardown(
        _default_compose_manager(normalized_work_dir)
    )

    reaped: list[OrphanDirReapOutcome] = []
    errors: list[OrphanDirReapOutcome] = []
    compose_teardowns: dict[str, dict[str, object]] = {}
    for target in targets:
        if target.kind == "compose":
            teardown = await resolved_teardown(target)
            compose_teardowns[target.workspace_id] = {
                "status": teardown.status,
                "reason_code": teardown.reason_code,
                **({"error": teardown.error} if teardown.error else {}),
            }
            if not teardown.ok:
                skipped = OrphanDirReapOutcome(
                    target=target,
                    status="skipped",
                    reason_code=teardown.reason_code,
                    error=(
                        teardown.error or "compose teardown failed; skipping filesystem removal"
                    ),
                )
                errors.append(skipped)
                _log.warning(
                    "gc.orphan_dir_reconcile.compose_teardown_failed",
                    **skipped.to_dict(),
                )
                continue

        outcome = await _reap_target(target, work_dir=normalized_work_dir)
        if outcome.ok:
            reaped.append(outcome)
            _log.info("gc.orphan_dir_reconcile.reaped", **outcome.to_dict())
        else:
            errors.append(outcome)
            _log.error("gc.orphan_dir_reconcile.reap_failed", **outcome.to_dict())

    status: Literal["ok", "partial"] = "partial" if errors else "ok"
    reason_code = (
        ORPHAN_DIR_REAP_PARTIAL
        if errors
        else (ORPHAN_DIR_REAP_CAP_HIT if capped else ORPHAN_DIR_REAP_OK)
    )
    result = OrphanDirReconcileResult(
        status=status,
        reason_code=reason_code,
        scanned_count=scanned,
        orphan_count=orphan_count,
        reaped_count=len(reaped),
        cap_limit=limit,
        capped=capped,
        dropped_count=dropped,
        reaped=tuple(reaped),
        errors=tuple(errors),
        compose_teardowns=compose_teardowns,
    )
    _log_summary(result, executed=True)
    return result


async def _reap_target(target: OrphanDirTarget, *, work_dir: Path) -> OrphanDirReapOutcome:
    """Delete one orphan dir via WS-B1's ``_delete_gc_path`` (off the event loop)."""
    gc_path = _gc_path(target.kind, target.path)
    deleted, error, reason_code = await asyncio.to_thread(
        _delete_gc_path, gc_path, work_dir=work_dir
    )
    if deleted:
        return OrphanDirReapOutcome(
            target=target,
            status="deleted",
            reason_code=PATH_DELETED,
            deleted=True,
        )
    if reason_code is None or reason_code == PATH_ALREADY_REMOVED:
        # ``None`` is the genuine not-exists case; both are idempotent no-ops.
        return OrphanDirReapOutcome(
            target=target,
            status="already_removed",
            reason_code=PATH_ALREADY_REMOVED,
        )
    return OrphanDirReapOutcome(
        target=target,
        status="failed",
        reason_code=reason_code,
        error=error,
    )


async def _load_known_workspace_ids(
    session_factory: async_sessionmaker[AsyncSession],
) -> frozenset[str]:
    """Load every workspace id across all statuses; any row protects its dirs."""
    async with session_scope(session_factory) as session:
        rows = (await session.execute(select(Workspace.id))).all()
    return frozenset(str(row[0]) for row in rows)


def _default_compose_manager(work_dir: Path) -> ComposeManager:
    from awf.node.compose_manager import ComposeManager as _ComposeManager
    from awf.service.gc import _default_workspace_compose_template

    return _ComposeManager(
        work_dir=work_dir,
        template_path=_default_workspace_compose_template(),
    )


def _log_summary(result: OrphanDirReconcileResult, *, executed: bool) -> None:
    _log.info(
        "gc.orphan_dir_reconcile.summary",
        reason_code=result.reason_code,
        status=result.status,
        executed=executed,
        scanned_count=result.scanned_count,
        orphan_count=result.orphan_count,
        reaped_count=result.reaped_count,
        error_count=len(result.errors),
        cap_limit=result.cap_limit,
        capped=result.capped,
        dropped_count=result.dropped_count,
    )
