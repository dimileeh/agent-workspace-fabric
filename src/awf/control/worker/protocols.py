"""Worker dependency protocols."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from awf.common.github_client import BranchOpenPullRequest
from awf.node.cleanup import WorkspaceCleanupResult
from awf.runtime.inspection import RuntimeSnapshot


class WorkspaceExecutorProtocol(Protocol):
    async def execute(  # pragma: no cover - Protocol method declaration only.
        self: Any,
        workspace_id: str,
        *,
        execution_owner_id: str | None = None,
        execution_lease_expires_at: datetime | None = None,
    ) -> None: ...

    async def resume_pr_monitor(  # pragma: no cover - Protocol method declaration only.
        self: Any,
        workspace_id: str,
    ) -> None: ...


class ProvisionerProtocol(Protocol):
    async def provision(  # pragma: no cover - Protocol method declaration only.
        self: Any,
        workspace_id: str,
    ) -> None: ...

    async def provision_claimed(  # pragma: no cover - Protocol method declaration only.
        self: Any,
        workspace_id: str,
        execution_claim_epoch: int | None = None,
    ) -> None: ...

    def get_worktree_path(  # pragma: no cover - Protocol method declaration only.
        self: Any,
        workspace_id: str,
    ) -> Path | None: ...


class BranchOpenPullRequestResolverProtocol(Protocol):
    async def resolve(  # pragma: no cover - Protocol method declaration only.
        self: Any,
        *,
        repo_url: str,
        branch_name: str,
        base_branch: str | None,
    ) -> Sequence[BranchOpenPullRequest]: ...


class RuntimeInspectorProtocol(Protocol):
    async def inspect(  # pragma: no cover - Protocol method declaration only.
        self: Any,
        compose_project_name: str | None,
    ) -> RuntimeSnapshot: ...


class RuntimeCleanerProtocol(Protocol):
    async def cleanup(  # pragma: no cover - Protocol method declaration only.
        self: Any,
        *,
        workspace_id: str,
        repo_url: str,
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> WorkspaceCleanupResult: ...
