"""Git worktree removal helpers for filesystem GC."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from inspect import isawaitable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from awf.db.models import Workspace
from awf.service.gc_companions import companion_worktree_remove_targets

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from awf.service.gc import WorkspaceGCCandidate, WorkspaceGCPath


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
        return self.status in {"succeeded", "skipped"}

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "reason_code": self.reason_code,
        }
        if self.error:
            payload["error"] = self.error
        if self.target_results:
            payload["target_results"] = [target.to_dict() for target in self.target_results]
        return payload


def blocked_worktree_paths_after_remove(
    candidate: WorkspaceGCCandidate,
    worktree_remove: WorkspaceGCWorktreeRemoveResult,
) -> set[Path]:
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
    if path.path == candidate.worktree.path:
        return candidate.workspace_id
    return path.path.name


async def default_worktree_remover(
    candidate: WorkspaceGCCandidate,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    work_dir: Path,
) -> WorkspaceGCWorktreeRemoveResult:
    from awf.node.git_manager import GitManager

    async with session_factory() as session:
        workspace = await session.get(Workspace, candidate.workspace_id)
    if workspace is None or not workspace.repo_url:
        return WorkspaceGCWorktreeRemoveResult(
            status="skipped",
            reason_code="NO_REPO_URL",
        )
    git_manager = GitManager(work_dir / "git")
    worktree_targets: list[tuple[str, str, bool]] = []
    target_results: list[WorkspaceGCWorktreeRemoveTargetResult] = []
    primary_path_exists = candidate.worktree.exists or candidate.worktree.path.exists()
    if is_existing_non_git_worktree(candidate.worktree.path):
        target_results.append(
            WorkspaceGCWorktreeRemoveTargetResult(
                worktree_id=candidate.workspace_id,
                status="skipped",
                reason_code="WORKTREE_NOT_GIT_MANAGED",
            )
        )
    else:
        worktree_targets.append((candidate.workspace_id, workspace.repo_url, primary_path_exists))
    companion_paths = {item.path.name: item for item in candidate.companion_worktrees}
    for worktree_id, repo_url in companion_worktree_remove_targets(workspace):
        companion_path = companion_paths.get(worktree_id)
        companion_path_exists = companion_path is not None and (
            companion_path.exists or companion_path.path.exists()
        )
        if companion_path is not None and is_existing_non_git_worktree(companion_path.path):
            target_results.append(
                WorkspaceGCWorktreeRemoveTargetResult(
                    worktree_id=worktree_id,
                    status="skipped",
                    reason_code="WORKTREE_NOT_GIT_MANAGED",
                )
            )
            continue
        worktree_targets.append((worktree_id, repo_url, companion_path_exists))
    if not worktree_targets:
        return WorkspaceGCWorktreeRemoveResult(
            status="skipped",
            reason_code="WORKTREE_NOT_GIT_MANAGED",
            target_results=tuple(target_results),
        )
    errors: list[str] = []
    existing_path_successes: set[str] = set()
    for worktree_id, repo_url, path_existed in worktree_targets:
        try:
            await git_manager.remove_worktree(workspace_id=worktree_id, repo_url=repo_url)
        except Exception as exc:
            error = str(exc)
            errors.append(f"{worktree_id}: {error}")
            target_results.append(
                WorkspaceGCWorktreeRemoveTargetResult(
                    worktree_id=worktree_id,
                    status="failed",
                    reason_code="GIT_WORKTREE_REMOVE_FAILED",
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
            reason_code="GIT_WORKTREE_REMOVE_FAILED",
            error="; ".join(errors)[:1000],
            target_results=tuple(target_results),
        )
    return WorkspaceGCWorktreeRemoveResult(
        status="succeeded",
        reason_code="WORKTREE_REMOVE_SUCCEEDED",
        target_results=tuple(target_results),
    )


def is_existing_non_git_worktree(path: Path) -> bool:
    return path.exists() and not (path / ".git").exists()


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
    if worktree_remover is None:
        return None
    result = worktree_remover(candidate)
    if isawaitable(result):
        result = await result
    return result
