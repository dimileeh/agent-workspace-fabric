"""Leaf result/data dataclasses for terminal-workspace filesystem GC.

These frozen data containers are split out of ``awf.service.gc`` to keep that
module under the maintainability line limit. They are re-imported back into
``awf.service.gc`` so every public name remains importable from there.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from awf.db.enums import WorkspaceStatus
from awf.service.gc_classify import (
    PATH_DELETE_FAILED,
    PROTECTED_WORKSPACE_GC_STATUSES,
    TERMINAL_WORKSPACE_GC_STATUSES,
    WorkspaceGCPath,
    _path_payload_for_candidate,
)
from awf.service.gc_worktrees import WorkspaceGCWorktreeRemoveResult

WorkspaceCleanupExecutionStatus = Literal["dry_run", "succeeded", "partial"]
WorkspaceCleanupPathStatus = Literal["planned", "deleted", "already_removed", "skipped", "failed"]


@dataclass(frozen=True)
class WorkspaceGCPreserved:
    """A workspace considered by policy but intentionally not cleaned."""

    workspace_id: str
    status: str
    updated_at: datetime
    age_hours: int
    reason_code: str
    compose_project_name: str | None = None
    compose_file_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "workspace_id": self.workspace_id,
            "status": self.status,
            "updated_at": self.updated_at.isoformat(),
            "age_hours": self.age_hours,
            "reason_code": self.reason_code,
        }
        if self.compose_project_name is not None:
            payload["compose_project_name"] = self.compose_project_name
        if self.compose_file_path is not None:
            payload["compose_file_path"] = self.compose_file_path
        return payload


@dataclass(frozen=True)
class WorkspaceGCComposeTeardownResult:
    """Structured outcome for optional compose teardown before filesystem deletion."""

    status: Literal["succeeded", "failed", "skipped"]
    reason_code: str
    error: str | None = None

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
        return payload


@dataclass(frozen=True)
class WorkspaceGCPathOutcome:
    """Structured execution outcome for one pressure-directory target."""

    workspace_id: str
    kind: str
    path: Path
    status: WorkspaceCleanupPathStatus
    reason_code: str
    deleted: bool = False
    error: str | None = None
    estimated_bytes: int = 0

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "workspace_id": self.workspace_id,
            "kind": self.kind,
            "path": str(self.path),
            "status": self.status,
            "reason_code": self.reason_code,
            "deleted": self.deleted,
            "estimated_bytes": self.estimated_bytes,
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True)
class WorkspaceGCDeleteError:
    """One deletion failure captured without aborting the rest of the GC run."""

    workspace_id: str
    kind: str
    path: Path
    error: str
    reason_code: str = PATH_DELETE_FAILED

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "kind": self.kind,
            "path": str(self.path),
            "reason_code": self.reason_code,
            "error": self.error,
        }


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
    claude_base_reap: dict[str, object] | None = None

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
                "compose_teardowns": {
                    ws_id: result.to_dict() for ws_id, result in self.compose_teardowns.items()
                },
                "secret_leases": self.secret_lease_revocations,
                "worktree_removes": {
                    ws_id: result.to_dict() for ws_id, result in self.worktree_removes.items()
                },
                "reservation_releases": self.reservation_releases,
            }
        )
        if self.companion_image_prune is not None:
            payload["companion_image_prune"] = self.companion_image_prune
        if self.claude_base_reap is not None:
            payload["claude_base_reap"] = self.claude_base_reap
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
