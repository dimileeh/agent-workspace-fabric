"""Git worktree removal helpers for filesystem GC."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from inspect import isawaitable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from awf.db.models import Workspace
from awf.service.gc_classify import PATH_ALREADY_REMOVED
from awf.service.gc_companions import companion_worktree_remove_targets

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from awf.service.gc import WorkspaceGCCandidate, WorkspaceGCPath

_GIT_BARE_PROBE_ENV_KEYS = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_PREFIX",
    "GIT_INTERNAL_SUPER_PREFIX",
    "GIT_SHALLOW_FILE",
    "GIT_COMMON_DIR",
)


@dataclass(frozen=True)
class WorkspaceGCWorktreeRemoveTargetResult:
    """Structured result for one primary or companion git worktree removal."""

    worktree_id: str
    status: Literal["succeeded", "failed", "skipped"]
    reason_code: str
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "worktree_id": self.worktree_id,
            "status": self.status,
            "reason_code": self.reason_code,
        }
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True)
class WorkspaceGCWorktreeRemoveResult:
    """Structured outcome for optional git worktree removal before filesystem deletion."""

    status: Literal["succeeded", "failed", "skipped", "partial"]
    reason_code: str
    error: str | None = None
    target_results: tuple[WorkspaceGCWorktreeRemoveTargetResult, ...] = ()

    @property
    def ok(self) -> bool:
        """Return whether worktree removal succeeded or was intentionally skipped."""
        return self.status in {"succeeded", "skipped"}

    def to_dict(self) -> dict[str, object]:
        """Serialize the aggregate worktree-removal outcome for audit payloads."""
        payload: dict[str, object] = {
            "status": self.status,
            "reason_code": self.reason_code,
        }
        if self.error:
            payload["error"] = self.error
        if self.target_results:
            payload["target_results"] = [target.to_dict() for target in self.target_results]
        return payload


@dataclass(frozen=True)
class _ExistingWorktreeGitContext:
    """Resolved Git-management context for an existing worktree path."""

    is_non_git_worktree: bool
    mirror_path: Path | None = None


def blocked_worktree_paths_after_remove(
    candidate: WorkspaceGCCandidate,
    worktree_remove: WorkspaceGCWorktreeRemoveResult,
) -> set[Path]:
    """Return worktree paths that must not be filesystem-deleted after a partial remove."""
    worktree_paths_by_id_map = worktree_paths_by_id(candidate)
    if not worktree_remove.target_results:
        return set(worktree_paths_by_id_map.values())

    reported_ids = {target.worktree_id for target in worktree_remove.target_results}
    blocked_paths = {
        worktree_paths_by_id_map[target.worktree_id]
        for target in worktree_remove.target_results
        if target.status == "failed" and target.worktree_id in worktree_paths_by_id_map
    }
    for worktree_id, path in worktree_paths_by_id_map.items():
        if worktree_id not in reported_ids:
            blocked_paths.add(path)
    return blocked_paths


def worktree_paths_by_id(candidate: WorkspaceGCCandidate) -> dict[str, Path]:
    paths = {candidate.workspace_id: candidate.worktree.path}
    paths.update({target.path.name: target.path for target in candidate.companion_worktrees})
    return paths


def worktree_id_for_gc_path(candidate: WorkspaceGCCandidate, path: WorkspaceGCPath) -> str:
    """Map a GC worktree path to its git worktree id for mirror-aware removal."""
    if path.path == candidate.worktree.path:
        return candidate.workspace_id
    return path.path.name


def _first_failed_target_reason_code(
    target_results: list[WorkspaceGCWorktreeRemoveTargetResult],
    *,
    fallback: str,
) -> str:
    """Return the first failed target reason code, or ``fallback`` when none exist."""
    return next(
        (
            target.reason_code
            for target in target_results
            if target.status == "failed" and target.reason_code
        ),
        fallback,
    )


def _existing_worktree_git_context_result(
    path: Path,
    *,
    work_dir: Path,
    worktree_id: str,
    target_results: list[WorkspaceGCWorktreeRemoveTargetResult],
    errors: list[str],
) -> _ExistingWorktreeGitContext | None:
    """Resolve mirror ownership for an existing worktree path or record probe failures."""
    from awf.node.git_manager import GitOperationError

    if not path.exists():
        return _ExistingWorktreeGitContext(is_non_git_worktree=False)

    try:
        mirror_path = git_context_mirror_path_for_worktree(path, work_dir=work_dir)
        is_non_git_worktree = mirror_path is None and is_existing_non_git_worktree(
            path,
            work_dir=work_dir,
            fail_on_metadata_probe_error=True,
        )
    except GitOperationError as exc:
        error = str(exc)
        errors.append(f"{worktree_id}: {error}")
        target_results.append(
            WorkspaceGCWorktreeRemoveTargetResult(
                worktree_id=worktree_id,
                status="failed",
                reason_code=exc.reason_code,
                error=error,
            )
        )
        return None

    return _ExistingWorktreeGitContext(
        is_non_git_worktree=is_non_git_worktree,
        mirror_path=mirror_path,
    )


async def default_worktree_remover(
    candidate: WorkspaceGCCandidate,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    work_dir: Path,
) -> WorkspaceGCWorktreeRemoveResult:
    """Remove primary and companion worktrees for a GC candidate via Git metadata.

    Loads ``repo_url`` from the workspace row, resolves each worktree's linked bare
    mirror, and delegates to ``GitManager.remove_worktree_from_mirror``. Non-Git
    worktrees are skipped; partial failures aggregate per-target results.
    """
    from awf.node.git_manager import GitManager, GitOperationError

    async with session_factory() as session:
        workspace = await session.get(Workspace, candidate.workspace_id)
    if workspace is None or not workspace.repo_url:
        return WorkspaceGCWorktreeRemoveResult(
            status="skipped",
            reason_code="NO_REPO_URL",
        )
    git_manager = GitManager(work_dir / "git")
    worktree_targets: list[tuple[str, str, Path | None, bool]] = []
    target_results: list[WorkspaceGCWorktreeRemoveTargetResult] = []
    errors: list[str] = []
    primary_path_exists = candidate.worktree.exists or candidate.worktree.path.exists()
    primary_git_context = _existing_worktree_git_context_result(
        candidate.worktree.path,
        work_dir=work_dir,
        worktree_id=candidate.workspace_id,
        target_results=target_results,
        errors=errors,
    )
    if primary_git_context is None:
        pass
    elif primary_git_context.is_non_git_worktree:
        target_results.append(
            WorkspaceGCWorktreeRemoveTargetResult(
                worktree_id=candidate.workspace_id,
                status="skipped",
                reason_code="WORKTREE_NOT_GIT_MANAGED",
            )
        )
    else:
        worktree_targets.append(
            (
                candidate.workspace_id,
                workspace.repo_url,
                primary_git_context.mirror_path,
                primary_path_exists,
            )
        )
    companion_paths = {item.path.name: item for item in candidate.companion_worktrees}
    for worktree_id, repo_url in companion_worktree_remove_targets(workspace):
        companion_path = companion_paths.get(worktree_id)
        companion_path_exists = companion_path is not None and (
            companion_path.exists or companion_path.path.exists()
        )
        companion_git_context = (
            _existing_worktree_git_context_result(
                companion_path.path,
                work_dir=work_dir,
                worktree_id=worktree_id,
                target_results=target_results,
                errors=errors,
            )
            if companion_path is not None
            else _ExistingWorktreeGitContext(is_non_git_worktree=False)
        )
        if companion_git_context is None:
            continue
        if companion_git_context.is_non_git_worktree:
            target_results.append(
                WorkspaceGCWorktreeRemoveTargetResult(
                    worktree_id=worktree_id,
                    status="skipped",
                    reason_code="WORKTREE_NOT_GIT_MANAGED",
                )
            )
            continue
        worktree_targets.append(
            (worktree_id, repo_url, companion_git_context.mirror_path, companion_path_exists)
        )
    if not worktree_targets:
        if errors:
            return WorkspaceGCWorktreeRemoveResult(
                status="failed",
                reason_code=_first_failed_target_reason_code(
                    target_results,
                    fallback="GIT_WORKTREE_REMOVE_FAILED",
                ),
                error="; ".join(errors)[:1000],
                target_results=tuple(target_results),
            )
        return WorkspaceGCWorktreeRemoveResult(
            status="skipped",
            reason_code="WORKTREE_NOT_GIT_MANAGED",
            target_results=tuple(target_results),
        )
    existing_path_successes: set[str] = set()
    for worktree_id, repo_url, mirror_path, path_existed in worktree_targets:
        try:
            if mirror_path is not None:
                await git_manager.remove_worktree_from_mirror(
                    workspace_id=worktree_id,
                    mirror_path=mirror_path,
                )
            else:
                await git_manager.remove_worktree(workspace_id=worktree_id, repo_url=repo_url)
        except GitOperationError as exc:
            error = str(exc)
            errors.append(f"{worktree_id}: {error}")
            target_results.append(
                WorkspaceGCWorktreeRemoveTargetResult(
                    worktree_id=worktree_id,
                    status="failed",
                    reason_code=exc.reason_code,
                    error=error,
                )
            )
        else:
            if path_existed:
                existing_path_successes.add(worktree_id)
            target_results.append(
                WorkspaceGCWorktreeRemoveTargetResult(
                    worktree_id=worktree_id,
                    status="succeeded",
                    reason_code="WORKTREE_REMOVE_SUCCEEDED",
                )
            )
    if errors:
        status: Literal["failed", "partial"] = "partial" if existing_path_successes else "failed"
        return WorkspaceGCWorktreeRemoveResult(
            status=status,
            reason_code=_first_failed_target_reason_code(
                target_results,
                fallback="GIT_WORKTREE_REMOVE_FAILED",
            ),
            error="; ".join(errors)[:1000],
            target_results=tuple(target_results),
        )
    return WorkspaceGCWorktreeRemoveResult(
        status="succeeded",
        reason_code="WORKTREE_REMOVE_SUCCEEDED",
        target_results=tuple(target_results),
    )


async def remove_orphan_worktree(
    *,
    workspace_id: str,
    path: Path,
    work_dir: Path,
) -> WorkspaceGCWorktreeRemoveResult:
    """Remove a row-less classified orphan worktree through Git metadata.

    Classified orphan reaping may not have a ``Workspace`` row, so it cannot use
    ``default_worktree_remover`` to fetch ``repo_url`` from the DB. Resolve the
    linked bare mirror from the worktree's ``.git`` file, then call
    ``GitManager.remove_worktree_from_mirror`` so removal serializes on the same
    mirror lock as hook repair without re-hashing the mirror's configured
    origin URL.
    """
    from awf.node.git_manager import GitManager, GitOperationError

    worktree_id = path.name or workspace_id
    if not path.exists():
        return WorkspaceGCWorktreeRemoveResult(
            status="skipped",
            reason_code=PATH_ALREADY_REMOVED,
            target_results=(
                WorkspaceGCWorktreeRemoveTargetResult(
                    worktree_id=worktree_id,
                    status="skipped",
                    reason_code=PATH_ALREADY_REMOVED,
                ),
            ),
        )
    try:
        mirror_path = git_context_mirror_path_for_worktree(path, work_dir=work_dir)
        is_non_git_worktree = mirror_path is None and is_existing_non_git_worktree(
            path,
            work_dir=work_dir,
            fail_on_metadata_probe_error=True,
        )
    except GitOperationError as exc:
        error = str(exc)
        return WorkspaceGCWorktreeRemoveResult(
            status="failed",
            reason_code=exc.reason_code,
            error=error,
            target_results=(
                WorkspaceGCWorktreeRemoveTargetResult(
                    worktree_id=worktree_id,
                    status="failed",
                    reason_code=exc.reason_code,
                    error=error,
                ),
            ),
        )

    if is_non_git_worktree:
        return WorkspaceGCWorktreeRemoveResult(
            status="skipped",
            reason_code="WORKTREE_NOT_GIT_MANAGED",
            target_results=(
                WorkspaceGCWorktreeRemoveTargetResult(
                    worktree_id=worktree_id,
                    status="skipped",
                    reason_code="WORKTREE_NOT_GIT_MANAGED",
                ),
            ),
        )

    if mirror_path is None:
        return WorkspaceGCWorktreeRemoveResult(
            status="failed",
            reason_code="ORPHAN_WORKTREE_GIT_CONTEXT_UNRESOLVED",
            error=f"could not resolve mirror for Git-managed orphan worktree {path}",
            target_results=(
                WorkspaceGCWorktreeRemoveTargetResult(
                    worktree_id=worktree_id,
                    status="failed",
                    reason_code="ORPHAN_WORKTREE_GIT_CONTEXT_UNRESOLVED",
                    error=f"could not resolve mirror for Git-managed orphan worktree {path}",
                ),
            ),
        )

    try:
        await GitManager(work_dir / "git").remove_worktree_from_mirror(
            workspace_id=worktree_id, mirror_path=mirror_path
        )
    except GitOperationError as exc:
        error = str(exc)
        return WorkspaceGCWorktreeRemoveResult(
            status="failed",
            reason_code=exc.reason_code,
            error=error,
            target_results=(
                WorkspaceGCWorktreeRemoveTargetResult(
                    worktree_id=worktree_id,
                    status="failed",
                    reason_code=exc.reason_code,
                    error=error,
                ),
            ),
        )
    return WorkspaceGCWorktreeRemoveResult(
        status="succeeded",
        reason_code="WORKTREE_REMOVE_SUCCEEDED",
        target_results=(
            WorkspaceGCWorktreeRemoveTargetResult(
                worktree_id=worktree_id,
                status="succeeded",
                reason_code="WORKTREE_REMOVE_SUCCEEDED",
            ),
        ),
    )


def git_context_mirror_path_for_worktree(path: Path, *, work_dir: Path) -> Path | None:
    """Return the managed bare mirror that owns a worktree path, if resolvable."""

    from awf.node.git_manager import (
        GitOperationError,
        mirror_path_for_registered_worktree,
        mirror_path_for_worktree,
    )

    mirrors_root = work_dir / "git" / "mirrors"
    try:
        linked_mirror_path = _managed_bare_mirror_path(
            mirror_path_for_worktree(path),
            mirrors_root,
        )
    except (OSError, RuntimeError) as exc:
        raise GitOperationError(
            operation="worktree.git_context_probe",
            returncode=1,
            stdout="",
            stderr=f"could not resolve linked git context for worktree {path}: {exc}",
            reason_code="WORKTREE_GIT_CONTEXT_RESOLUTION_FAILED",
        ) from exc
    linked_registry_matches = (
        linked_mirror_path is not None
        and _mirror_registry_points_to_worktree(linked_mirror_path, path)
    )
    registered_mirror_path = (
        None if linked_registry_matches else mirror_path_for_registered_worktree(path, mirrors_root)
    )
    if linked_mirror_path is None:
        return _managed_bare_mirror_path(registered_mirror_path, mirrors_root)
    try:
        managed_registered_mirror_path = _managed_bare_mirror_path(
            registered_mirror_path, mirrors_root
        )
    except GitOperationError:
        if linked_registry_matches:
            return linked_mirror_path
        raise
    if managed_registered_mirror_path is None:
        return linked_mirror_path
    if linked_mirror_path == managed_registered_mirror_path:
        return linked_mirror_path
    if linked_registry_matches:
        return linked_mirror_path
    return managed_registered_mirror_path


def _mirror_registry_points_to_worktree(mirror_path: Path, worktree_path: Path) -> bool:
    """Return whether ``mirror_path`` registry metadata points at ``worktree_path``."""
    from awf.node.git_manager import GitOperationError, linked_worktree_path_from_git_dir

    try:
        resolved_worktree = worktree_path.resolve()
    except (OSError, RuntimeError):
        resolved_worktree = worktree_path.absolute()
    linked_git_dir = mirror_path / "worktrees" / worktree_path.name
    if not linked_git_dir.is_dir():
        return False
    try:
        return linked_worktree_path_from_git_dir(linked_git_dir) == resolved_worktree
    except GitOperationError:
        return False


def _managed_mirror_path(
    path: Path | None,
    mirrors_root: Path,
    *,
    require_existing_dir: bool = False,
) -> Path | None:
    """Return ``path`` when it resolves under ``mirrors_root``, else ``None``."""
    if path is None:
        return None
    try:
        resolved_path = path.resolve()
        resolved_root = mirrors_root.resolve()
    except (OSError, RuntimeError):
        # ``resolve()`` already failed; ``abspath()`` still collapses ``..``.
        resolved_path = Path(os.path.abspath(path))  # noqa: PTH100
        resolved_root = Path(os.path.abspath(mirrors_root))  # noqa: PTH100
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return None
    if require_existing_dir and not resolved_path.is_dir():
        return None
    return resolved_path


def _managed_bare_mirror_path(path: Path | None, mirrors_root: Path) -> Path | None:
    """Return a managed bare mirror path under ``mirrors_root``, if resolvable."""
    mirror_path = _managed_mirror_path(path, mirrors_root, require_existing_dir=True)
    if mirror_path is None:
        return None
    if not _is_bare_git_repository(mirror_path, fail_closed=True):
        return None
    return mirror_path


def _is_bare_git_repository(path: Path, *, fail_closed: bool = False) -> bool:
    """Return whether ``path`` probes as a bare Git repository."""
    from awf.node.git_manager import _GIT_BARE_PROBE_TIMEOUT_SECONDS, GitOperationError

    operation = "worktree.git_context_probe"
    try:
        probe = subprocess.run(
            ["git", "--bare", "--git-dir", str(path), "rev-parse", "--is-bare-repository"],
            capture_output=True,
            check=False,
            env=_git_bare_probe_env(),
            text=True,
            timeout=_GIT_BARE_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        if fail_closed:
            raise GitOperationError(
                operation=operation,
                returncode=1,
                stdout="",
                stderr=(
                    f"bare mirror probe timed out after {_GIT_BARE_PROBE_TIMEOUT_SECONDS:g}s "
                    f"for {path}"
                ),
                reason_code="WORKTREE_GIT_CONTEXT_RESOLUTION_FAILED",
            ) from exc
        return False
    except OSError as exc:
        if fail_closed:
            raise GitOperationError(
                operation=operation,
                returncode=1,
                stdout="",
                stderr=f"could not probe bare mirror {path}: {exc}",
                reason_code="WORKTREE_GIT_CONTEXT_RESOLUTION_FAILED",
            ) from exc
        return False
    if fail_closed and probe.returncode != 0 and _looks_like_bare_git_repository(path):
        raise GitOperationError(
            operation=operation,
            returncode=probe.returncode,
            stdout=probe.stdout,
            stderr=probe.stderr,
            reason_code="WORKTREE_GIT_CONTEXT_RESOLUTION_FAILED",
        )
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def _git_bare_probe_env() -> dict[str, str]:
    """Build the Git environment used for bare-repository probes during GC."""
    from awf.node.git_manager import git_env_without_object_lookup_overrides

    env = git_env_without_object_lookup_overrides()
    for key in _GIT_BARE_PROBE_ENV_KEYS:
        env.pop(key, None)
    return env


def _looks_like_bare_git_repository(path: Path) -> bool:
    """Return whether ``path`` has the on-disk layout of a bare Git repository."""
    return (
        (path / "config").is_file()
        and (path / "HEAD").is_file()
        and (path / "objects").is_dir()
        and (path / "refs").is_dir()
    )


def _has_stale_managed_linked_mirror(path: Path, *, work_dir: Path) -> bool:
    """Return whether a managed linked mirror exists but no longer probes as bare."""
    from awf.node.git_manager import mirror_path_for_worktree

    mirrors_root = work_dir / "git" / "mirrors"
    try:
        linked_mirror_path = _managed_mirror_path(mirror_path_for_worktree(path), mirrors_root)
    except (OSError, RuntimeError):
        return False
    return linked_mirror_path is not None and not _is_bare_git_repository(linked_mirror_path)


def is_existing_non_git_worktree(
    path: Path,
    *,
    work_dir: Path | None = None,
    fail_on_metadata_probe_error: bool = False,
) -> bool:
    """Return whether an existing worktree path lacks usable Git management metadata."""

    if not path.exists():
        return False
    git_entry = path / ".git"
    if work_dir is None:
        return not git_entry.exists()
    if not git_entry.exists():
        from awf.node.git_manager import GitOperationError

        try:
            return git_context_mirror_path_for_worktree(path, work_dir=work_dir) is None
        except GitOperationError as exc:
            if (
                exc.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
                and not fail_on_metadata_probe_error
            ):
                return True
            raise
    if git_context_mirror_path_for_worktree(path, work_dir=work_dir):
        return False
    if git_entry.is_file() and _has_stale_managed_linked_mirror(path, work_dir=work_dir):
        return True
    return not git_entry.is_file()


async def run_worktree_remove(
    candidate: WorkspaceGCCandidate,
    worktree_remover: (
        Callable[
            [WorkspaceGCCandidate],
            WorkspaceGCWorktreeRemoveResult | Awaitable[WorkspaceGCWorktreeRemoveResult],
        ]
        | None
    ),
) -> WorkspaceGCWorktreeRemoveResult | None:
    """Invoke an optional worktree remover, awaiting async implementations when needed."""
    if worktree_remover is None:
        return None
    result = worktree_remover(candidate)
    if isawaitable(result):
        result = await result
    return result
