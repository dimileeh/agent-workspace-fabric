"""Cloud-neutral workspace runtime driver contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from awf.runtime.inspection import RuntimeInspector, RuntimeSnapshot

if TYPE_CHECKING:
    from awf.node.cleanup import WorkspaceCleanupResult
    from awf.runtime.validation_types import ValidationResult

WORKSPACE_EXECUTION_V1 = "workspace.execution.v1"
LOCAL_RUNTIME_DRIVER = "local"


@dataclass(frozen=True)
class RuntimeDriverConfig:
    """Core runtime-driver selection.

    AWF Core currently ships only the local Docker/Compose driver. Unsupported
    names fail explicitly so cloud substrates can plug in at the seam instead of
    silently changing Core behavior.
    """

    name: str = LOCAL_RUNTIME_DRIVER

    def __post_init__(self) -> None:
        if self.name != LOCAL_RUNTIME_DRIVER:
            raise ValueError(f"Unsupported AWF Core runtime driver: {self.name}")

    @property
    def capabilities(self) -> tuple[str, ...]:
        return (WORKSPACE_EXECUTION_V1,)


@dataclass(frozen=True)
class WorkspaceProvisionRequest:
    workspace_id: str
    execution_claim_epoch: int | None = None


@dataclass(frozen=True)
class WorkspaceStartRequest:
    workspace_id: str
    execution_owner_id: str | None = None
    execution_lease_expires_at: datetime | None = None


@dataclass(frozen=True)
class WorkspaceStopRequest:
    workspace_id: str
    repo_url: str
    companion_worktrees: tuple[tuple[str, str], ...] = ()
    compose_project_name: str | None = None
    compose_file_path: Path | None = None
    worktree_host_path: Path | None = None
    remove_volumes: bool = True
    remove_worktree: bool = True


@dataclass(frozen=True)
class WorkspaceValidateRequest:
    workspace_id: str
    compose_project: str
    compose_file: Path
    test_commands: tuple[str, ...] = ()
    requires_database: bool = False
    workspace_worktree: Path | None = None


@dataclass(frozen=True)
class WorkspaceStatusRequest:
    compose_project_name: str | None


@runtime_checkable
class WorkspaceRuntimeDriver(Protocol):
    @property
    def capabilities(self) -> tuple[str, ...]:  # pragma: no cover - Protocol declaration.
        ...

    async def provision(
        self,
        request: WorkspaceProvisionRequest,
    ) -> Any:  # pragma: no cover - Protocol declaration.
        ...

    async def start(
        self,
        request: WorkspaceStartRequest,
    ) -> Any:  # pragma: no cover - Protocol declaration.
        ...

    async def stop(
        self,
        request: WorkspaceStopRequest,
    ) -> WorkspaceCleanupResult:  # pragma: no cover - Protocol declaration.
        ...

    async def validate(
        self,
        request: WorkspaceValidateRequest,
    ) -> ValidationResult:  # pragma: no cover - Protocol declaration.
        ...

    async def status(
        self,
        request: WorkspaceStatusRequest,
    ) -> RuntimeSnapshot:  # pragma: no cover - Protocol declaration.
        ...


class LocalRuntimeDriver:
    """Thin adapter over the existing local Docker/Compose collaborators."""

    def __init__(
        self,
        *,
        provisioner: Any,
        executor: Any,
        cleaner: Any,
        validation_runner: Any,
        runtime_inspector: Any | None = None,
    ) -> None:
        self.provisioner = provisioner
        self.executor = executor
        self.cleaner = cleaner
        self.validation_runner = validation_runner
        self.runtime_inspector = runtime_inspector or RuntimeInspector()

    @property
    def capabilities(self) -> tuple[str, ...]:
        return (WORKSPACE_EXECUTION_V1,)

    async def provision(self, request: WorkspaceProvisionRequest) -> Any:
        if request.execution_claim_epoch is None:
            return await self.provisioner.provision(request.workspace_id)
        return await self.provisioner.provision_claimed(
            request.workspace_id,
            execution_claim_epoch=request.execution_claim_epoch,
        )

    async def start(self, request: WorkspaceStartRequest) -> Any:
        return await self.executor.execute(
            request.workspace_id,
            execution_owner_id=request.execution_owner_id,
            execution_lease_expires_at=request.execution_lease_expires_at,
        )

    async def stop(self, request: WorkspaceStopRequest) -> Any:
        return await self.cleaner.cleanup(
            workspace_id=request.workspace_id,
            repo_url=request.repo_url,
            companion_worktrees=request.companion_worktrees,
            compose_project_name=request.compose_project_name,
            compose_file_path=request.compose_file_path,
            worktree_host_path=request.worktree_host_path,
            remove_volumes=request.remove_volumes,
            remove_worktree=request.remove_worktree,
        )

    async def validate(self, request: WorkspaceValidateRequest) -> Any:
        return await self.validation_runner.run(
            workspace_id=request.workspace_id,
            compose_project=request.compose_project,
            compose_file=request.compose_file,
            test_commands=list(request.test_commands),
            requires_database=request.requires_database,
            workspace_worktree=request.workspace_worktree,
        )

    async def status(self, request: WorkspaceStatusRequest) -> Any:
        return await self.runtime_inspector.inspect(request.compose_project_name)
