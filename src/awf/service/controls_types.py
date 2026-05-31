"""Internal type helpers for workspace control operations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from awf.db.models import Operation, Workspace
from awf.node.cleanup import WorkspaceCleanupResult

ProjectStopper = Callable[[str | None], Awaitable[None]]
CleanupResultLike = WorkspaceCleanupResult | Sequence[str] | Mapping[str, object]
CleanerFactory = Callable[[], "WorkspaceCleanerProtocol"]


class _PreparedOperationKind(StrEnum):
    exact_replay = "exact_replay"
    active_coalesce = "active_coalesce"


@dataclass(frozen=True)
class _PreparedOperation:
    workspace: Workspace
    replay: Operation | None = None
    kind: _PreparedOperationKind | None = None
    idempotency_key: str | None = None


class WorkspaceCleanerProtocol(Protocol):
    async def cleanup(  # pragma: no cover - Protocol method declaration only.
        self,
        *,
        workspace_id: str,
        repo_url: str,
        companion_worktrees: tuple[tuple[str, str], ...] = (),
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> CleanupResultLike: ...
