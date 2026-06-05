"""Filesystem garbage collection for terminal service workspaces.

This module only relieves disk pressure from per-workspace runtime directories.
It deliberately does not delete control-plane rows, workspace events, or durable
log streams.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextlib import suppress
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
from awf.service import gc_classify as _gc_classify
from awf.service import gc_predicates as _gc_predicates
from awf.service import gc_results as _gc_results
from awf.service import gc_worktrees as _gc_worktrees
from awf.service.gc_auth_overlay import (
    _auth_overlay_unmount_skips_target,
    _auth_unmount_skipped_outcome,
    _unmount_candidate_auth_overlay,
)
from awf.service.gc_classify import (
    PATH_ALREADY_REMOVED,
    PATH_DELETE_FAILED,
    PATH_DELETED,
    _compose_project_name_for_workspace,
    _delete_gc_path,
    _gc_path,
    _has_pr_metadata,
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
from awf.service.gc_claude_base import reap_superseded_claude_bases as reap_superseded_claude_bases
from awf.service.gc_companions import companion_worktree_paths_for_gc
from awf.service.gc_models import PROTECTED_WORKSPACE_GC_STATUSES as PROTECTED_WORKSPACE_GC_STATUSES
from awf.service.gc_models import TERMINAL_WORKSPACE_GC_STATUSES as TERMINAL_WORKSPACE_GC_STATUSES
from awf.service.gc_models import ClaudeBaseReap as ClaudeBaseReap
from awf.service.gc_models import CompanionImagePrune as CompanionImagePrune
from awf.service.gc_models import WorkspaceGCCandidate as WorkspaceGCCandidate
from awf.service.gc_models import WorkspaceGCComposeTeardown as WorkspaceGCComposeTeardown
from awf.service.gc_models import WorkspaceGCPlan as WorkspaceGCPlan
from awf.service.gc_models import WorkspaceGCResult as WorkspaceGCResult
from awf.service.gc_models import WorkspaceGCWorktreeRemove as WorkspaceGCWorktreeRemove
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

_FAILED_NO_WORK_TERMINAL_STATUSES = _gc_classify.FAILED_NO_WORK_TERMINAL_STATUSES

# Result/data dataclasses live in ``gc_results`` (file-size budget);
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
# Bound Docker compose teardown fan-out during service GC batches. The work is
# slow enough to benefit from limited overlap, but unbounded bursts can saturate
# small Docker daemons.
_COMPOSE_TEARDOWN_CONCURRENCY_LIMIT = 4
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
WORKSPACE_GC_EMPTY_PLAN_COMPOSE_TEARDOWN = "WORKSPACE_GC_EMPTY_PLAN_COMPOSE_TEARDOWN"
COMPOSE_TEARDOWN_CALLBACK_RAISED = "COMPOSE_TEARDOWN_CALLBACK_RAISED"

# Extension point: add preserved-workspace reason/status pairs here only when
# future states should allow compose teardown and the follow-on runtime side
# effects (secret lease revocation and reservation release) before filesystem
# retention expires.
_PRESERVED_COMPOSE_TEARDOWN_FALLBACK_STATES: frozenset[tuple[str, str]] = frozenset(
    {
        (WORKSPACE_WITHIN_RETENTION, WorkspaceStatus.completed.value),
    }
)

CLEANUP_DRY_RUN = "CLEANUP_DRY_RUN"
CLEANUP_EXECUTION_SUCCEEDED = "CLEANUP_EXECUTION_SUCCEEDED"
CLEANUP_EXECUTION_PARTIAL = "CLEANUP_EXECUTION_PARTIAL"
_COMPOSE_TEARDOWN_EXCEPTION_RESULT_ATTR = "_awf_compose_teardown_result"

_RUNTIME_INSPECTOR = RuntimeInspector()


def compose_teardown_result_for_exception(exc: Exception) -> WorkspaceGCComposeTeardownResult:
    """Return the stable failed compose teardown result for a callback exception.

    Completed-monitor lifecycle tracking may record this before re-raising the
    exception; GC later catches the same exception and uses the cached result so
    both logs and the returned GC payload describe the same teardown failure.
    """

    cached = getattr(exc, _COMPOSE_TEARDOWN_EXCEPTION_RESULT_ATTR, None)
    if isinstance(cached, WorkspaceGCComposeTeardownResult):
        return cached
    error = str(exc)
    error_message = f"{type(exc).__name__}: {error}" if error else type(exc).__name__
    result = WorkspaceGCComposeTeardownResult(
        status="failed",
        reason_code=COMPOSE_TEARDOWN_CALLBACK_RAISED,
        error=error_message[:400],
    )
    with suppress(Exception):
        setattr(exc, _COMPOSE_TEARDOWN_EXCEPTION_RESULT_ATTR, result)
    return result


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
    claude_base_reap: ClaudeBaseReap | None = None,
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
    # The shared-base reaper is a host-wide step independent of the per-workspace
    # candidates. On execute the candidate auth dirs (and their ``base.signature``
    # pins) are deleted *before* the reaper runs (see below), so a base pinned only by
    # a candidate is reaped in the same pass. A dry run deletes nothing, so the pins
    # are still on disk; tell the reaper to treat those candidate auth dirs as already
    # pruned so a base the matching execute pass would free is previewed as ``planned``
    # rather than mislabeled ``protected`` (PRRT_kwDOSJAM6s6HIepf).
    if not execute:
        pruned_auth_dirs = frozenset(candidate.auth.path for candidate in plan.candidates)
        claude_base_reap_result = (
            await claude_base_reap(pruned_auth_dirs) if claude_base_reap is not None else None
        )
        return _gc_result(
            plan=plan,
            dry_run=True,
            deleted_paths=[],
            delete_errors=[],
            path_outcomes=[],
            compose_teardowns={},
            worktree_removes={},
            reservation_releases={},
            claude_base_reap=claude_base_reap_result,
        )

    resolved_worktree_remover = _resolve_worktree_remover(
        worktree_remover, session_factory, work_dir
    )
    compose_teardowns = await _run_gc_compose_teardowns(plan, compose_teardown)
    side_effect_workspace_ids = _workspace_ids_after_compose_teardown(
        plan,
        compose_teardowns,
    )
    secret_lease_revocations = await _revoke_gc_secret_leases(
        session_factory,
        workspace_ids=side_effect_workspace_ids,
        now=current_time,
    )
    (
        deleted_paths,
        delete_errors,
        path_outcomes,
        worktree_removes,
    ) = await _delete_gc_plan_paths(
        plan,
        compose_teardowns=compose_teardowns,
        worktree_remover=resolved_worktree_remover,
    )
    reservation_releases = await _release_gc_reservations(
        session_factory,
        workspace_ids=side_effect_workspace_ids,
    )
    companion_image_prune_result = (
        await companion_image_prune() if companion_image_prune is not None else None
    )
    # Reap superseded shared bases *after* the candidate auth dirs (and their
    # ``base.signature`` pins) are deleted above, so a base pinned only by a workspace
    # just reclaimed in this pass is reaped now instead of leaking until the next GC
    # (PRRT_kwDOSJAM6s6HIHN6). The pins are already gone from disk here, so no auth dirs
    # are pruned explicitly: relying on the actual on-disk state means a base whose
    # candidate delete failed stays protected (its ``upper`` must not be stranded).
    claude_base_reap_result = (
        await claude_base_reap(frozenset()) if claude_base_reap is not None else None
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
        claude_base_reap=claude_base_reap_result,
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
    host_home: Path | str | None = None,
    reap_claude_bases: bool = False,
    compose_manager: ComposeManager | None = None,
    now: datetime | None = None,
) -> WorkspaceGCResult:
    """Run terminal-workspace GC inside the root control-plane.

    This is the entrypoint the ``POST /v1/service/gc`` route delegates to. The
    api/worker containers run as **root** and own the per-workspace state, so
    deletion here actually removes root-owned auth dirs (the host CLI, running as
    uid 1000, silently could not) and a volume-removing compose teardown reaps
    the per-workspace Docker volumes that GC previously leaked.

    With ``reap_claude_bases`` enabled (and ``host_home`` provided) a host-wide
    GC-B step (#389) also reaps superseded shared ``~/.claude`` overlay bases,
    preserving the current signature and any live-mounted or pinned base.
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
    claude_base_reap: ClaudeBaseReap | None = None
    if reap_claude_bases and host_home is not None:
        resolved_host_home = Path(host_home).expanduser()

        async def _reap_bases(pruned_auth_dirs: frozenset[Path]) -> dict[str, object]:
            return await asyncio.to_thread(
                reap_superseded_claude_bases,
                work_dir=normalized_work_dir,
                host_home=resolved_host_home,
                execute=execute,
                pruned_auth_dirs=pruned_auth_dirs,
            )

        claude_base_reap = _reap_bases
    elif reap_claude_bases:
        # ``reap_claude_bases`` is on but no ``host_home`` was supplied, so GC-B has no
        # host ``~/.claude`` signature to protect the current base with — running it
        # blind could reap a live base, so it is skipped. The production route always
        # threads ``settings.host_home`` (default ``"~"``), so this only fires for a
        # programmatic caller that set the flag without a home; log it so the otherwise
        # silent no-op is diagnosable.
        _log.warning("service_gc_claude_base_reap_skipped_no_host_home")
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
        claude_base_reap=claude_base_reap,
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

    fallback_compose_teardown_candidate: WorkspaceGCCandidate | None = None
    if not candidates:
        if workspace is None:
            fallback_compose_teardown_candidate = _missing_workspace_compose_teardown_candidate(
                workspace_id=workspace_id,
                work_dir=normalized_work_dir,
                now=current_time,
            )
        elif preserved and _preserved_workspace_allows_compose_teardown_fallback(preserved[0]):
            # Extension point for callers that honor retention while still
            # wanting early runtime teardown. The production post-merge monitor
            # passes ``ignore_retention=True``, so merged completed workspaces
            # bypass this preserved branch and are reclaimed as candidates.
            fallback_compose_teardown_candidate = _candidate_for_workspace(
                workspace,
                work_dir=normalized_work_dir,
                now=current_time,
                reason_code=preserved[0].reason_code,
            )
    compose_teardowns = await _run_gc_compose_teardowns(
        plan,
        compose_teardown,
        fallback_candidate=fallback_compose_teardown_candidate,
    )
    side_effect_workspace_ids = _workspace_ids_after_compose_teardown(
        plan,
        compose_teardowns,
    )
    secret_lease_revocations = await _revoke_gc_secret_leases(
        session_factory,
        workspace_ids=side_effect_workspace_ids,
        now=current_time,
    )
    (
        deleted_paths,
        delete_errors,
        path_outcomes,
        worktree_removes,
    ) = await _delete_gc_plan_paths(
        plan,
        compose_teardowns=compose_teardowns,
        worktree_remover=resolved_worktree_remover,
    )
    reservation_releases = await _release_gc_reservations(
        session_factory,
        workspace_ids=side_effect_workspace_ids,
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


async def _delete_gc_plan_paths(
    plan: WorkspaceGCPlan,
    *,
    compose_teardowns: dict[str, WorkspaceGCComposeTeardownResult],
    worktree_remover: WorkspaceGCWorktreeRemove | None,
) -> tuple[
    list[Path],
    list[WorkspaceGCDeleteError],
    list[WorkspaceGCPathOutcome],
    dict[str, WorkspaceGCWorktreeRemoveResult],
]:
    deleted_paths: list[Path] = []
    delete_errors: list[WorkspaceGCDeleteError] = []
    path_outcomes: list[WorkspaceGCPathOutcome] = []
    worktree_removes: dict[str, WorkspaceGCWorktreeRemoveResult] = {}
    for candidate in plan.candidates:
        teardown = compose_teardowns.get(candidate.workspace_id)
        if teardown is not None and not teardown.ok:
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
        auth_unmount_failure = await asyncio.to_thread(
            _unmount_candidate_auth_overlay, candidate, work_dir=plan.work_dir
        )
        if auth_unmount_failure is not None:
            # The overlay could not be unmounted/verified in this process (no
            # CAP_SYS_ADMIN, or a genuine umount failure). Record a loud delete
            # error and skip removing the auth dir below so we never ``rmtree``
            # over a possibly-live mount and its ``upper`` inodes. Worktree and
            # compose paths still proceed — only the auth dir is at risk.
            auth_reason_code, auth_message = auth_unmount_failure
            delete_errors.append(
                WorkspaceGCDeleteError(
                    workspace_id=candidate.workspace_id,
                    kind="auth_overlay_unmount",
                    path=candidate.auth.path,
                    error=auth_message,
                    reason_code=auth_reason_code,
                )
            )
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
                    if _auth_overlay_unmount_skips_target(auth_unmount_failure, target):
                        path_outcomes.append(
                            _auth_unmount_skipped_outcome(candidate, target, auth_unmount_failure)
                        )
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
            if _auth_overlay_unmount_skips_target(auth_unmount_failure, target):
                path_outcomes.append(
                    _auth_unmount_skipped_outcome(candidate, target, auth_unmount_failure)
                )
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
    return deleted_paths, delete_errors, path_outcomes, worktree_removes


async def _run_gc_compose_teardowns(
    plan: WorkspaceGCPlan,
    compose_teardown: WorkspaceGCComposeTeardown | None,
    *,
    fallback_candidate: WorkspaceGCCandidate | None = None,
) -> dict[str, WorkspaceGCComposeTeardownResult]:
    compose_teardowns: dict[str, WorkspaceGCComposeTeardownResult] = {}
    if compose_teardown is None:
        return compose_teardowns
    candidates = list(plan.candidates)
    if not candidates and fallback_candidate is not None:
        candidates = [fallback_candidate]
    if not candidates:
        return compose_teardowns

    semaphore = asyncio.Semaphore(_COMPOSE_TEARDOWN_CONCURRENCY_LIMIT)

    async def _teardown_candidate(
        candidate: WorkspaceGCCandidate,
    ) -> tuple[str, WorkspaceGCComposeTeardownResult | None]:
        async with semaphore:
            try:
                teardown = await _run_compose_teardown(candidate, compose_teardown)
            except Exception as exc:
                teardown = compose_teardown_result_for_exception(exc)
        return candidate.workspace_id, teardown

    results = await asyncio.gather(*(_teardown_candidate(candidate) for candidate in candidates))
    for workspace_id, teardown in results:
        if teardown is not None:
            compose_teardowns[workspace_id] = teardown
    return compose_teardowns


def _workspace_ids_after_compose_teardown(
    plan: WorkspaceGCPlan,
    compose_teardowns: dict[str, WorkspaceGCComposeTeardownResult],
) -> list[str]:
    workspace_ids: list[str] = []
    candidate_ids: set[str] = set()
    for candidate in plan.candidates:
        candidate_ids.add(candidate.workspace_id)
        teardown = compose_teardowns.get(candidate.workspace_id)
        # No teardown entry means no compose callback was supplied; preserve the
        # legacy GC semantics where candidate side effects proceed unconditionally.
        if teardown is None or teardown.ok:
            workspace_ids.append(candidate.workspace_id)
    # Non-candidate compose teardowns come from the single-workspace fallback
    # path only: missing rows or explicitly allowed preserved terminal-status
    # workspaces. Runtime side effects are released only after successful
    # compose teardown; failed teardown keeps leases/reservations in place so
    # running containers do not lose credentials while the result records the
    # failed compose outcome for monitoring.
    for workspace_id, teardown in compose_teardowns.items():
        if workspace_id not in candidate_ids and teardown.ok:
            workspace_ids.append(workspace_id)
    return workspace_ids


def _missing_workspace_compose_teardown_candidate(
    *,
    workspace_id: str,
    work_dir: Path,
    now: datetime,
) -> WorkspaceGCCandidate:
    return WorkspaceGCCandidate(
        workspace_id=workspace_id,
        status=WorkspaceStatus.destroyed.value,
        updated_at=now,
        age_hours=0,
        reason_code=WORKSPACE_GC_EMPTY_PLAN_COMPOSE_TEARDOWN,
        worktree=_gc_path("worktree", work_dir / "git" / "worktrees" / workspace_id),
        compose=_gc_path("compose", work_dir / "compose" / workspace_id),
        auth=_gc_path("auth", work_dir / "auth" / workspace_id),
    )


def _preserved_workspace_allows_compose_teardown_fallback(
    preserved: WorkspaceGCPreserved,
) -> bool:
    return (
        preserved.reason_code,
        preserved.status,
    ) in _PRESERVED_COMPOSE_TEARDOWN_FALLBACK_STATES


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
    claude_base_reap: dict[str, object] | None = None,
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
            claude_base_reap=claude_base_reap,
        )
    companion_prune_failed = (
        companion_image_prune is not None and companion_image_prune.get("status") == "failed"
    )
    # A ``partial`` shared-base reap (a permission-denied removal) drives the whole
    # run partial too — it leaked disk it could not reclaim, so the run must not
    # report a clean success.
    claude_base_reap_partial = (
        claude_base_reap is not None and claude_base_reap.get("status") == "partial"
    )
    # Candidate teardown failures are also reflected in delete_errors by the
    # path loop, but fallback compose teardowns never enter that loop.
    compose_teardown_failed = any(not teardown.ok for teardown in compose_teardowns.values())
    has_errors = (
        bool(delete_errors)
        or compose_teardown_failed
        or any(v.get("error") is not None for v in res_releases.values())
        or companion_prune_failed
        or claude_base_reap_partial
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
        claude_base_reap=claude_base_reap,
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

    def _preserved(reason_code: str) -> WorkspaceGCPreserved:
        return WorkspaceGCPreserved(
            workspace_id=workspace.id,
            status=workspace.status,
            updated_at=updated_at,
            age_hours=age_hours,
            reason_code=reason_code,
            compose_project_name=workspace.compose_project_name,
            compose_file_path=workspace.compose_file_path,
        )

    if not cleanup_enabled:
        return _preserved(WORKSPACE_CLEANUP_DISABLED)

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
        return _preserved(FAILED_WORKSPACE_TRIAGE_PRESERVED)

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
                return _preserved(WORKSPACE_WITHIN_RETENTION)
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
                return _preserved(WORKSPACE_WITHIN_RETENTION)
            return _preserved_failed_or_age_capped()
        if workspace.status != WorkspaceStatus.completed.value:
            return None
        if not _has_pr_metadata(workspace):
            return _preserved(COMPLETED_WORKSPACE_WITHOUT_PR)
        if not _pr_has_merged(workspace):
            return _preserved(COMPLETED_PR_NOT_MERGED)
        # A merged PR's pressure dirs are disposable the moment it lands. With
        # ``ignore_retention`` the post-merge caller reclaims them immediately
        # instead of waiting out the retention window; the durable record (DB
        # row, events, logs) is preserved either way (GC never deletes it).
        if updated_at > cutoff_at and not ignore_retention:
            return _preserved(WORKSPACE_WITHIN_RETENTION)
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
        return _preserved(WORKSPACE_WITHIN_RETENTION)
    if (
        workspace.status in _FAILED_NO_WORK_TERMINAL_STATUSES
        and workspace.compose_project_name is not None
        and not _failed_terminal_workspace_has_no_work(workspace)
    ):
        return _preserved(TERMINAL_WORKSPACE_RETENTION_EXPIRED)

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
