"""Workspace service operations shared by REST routes and MCP tools."""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import (
    OperationResponse,
    RuntimeServiceResponse,
    WorkspaceControlResponse,
    WorkspaceCreateRequest,
    WorkspaceCreateV2Request,
    WorkspaceEventResponse,
    WorkspaceLogStreamResponse,
    WorkspaceResponse,
    WorkspaceRuntimeResponse,
)
from awf.db.enums import OperationStatus, OperationType
from awf.db.models import Workspace
from awf.db.repositories import (
    OperationRepository,
    OwnedPathConflict,
    WorkspaceEventRepository,
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import resolve_workspace_profile
from awf.runtime.inspection import RuntimeInspector, RuntimeSnapshot
from awf.runtime.logs import read_log_chunk
from awf.service.controls import (
    CleanerFactory,
    ProjectStopper,
    WorkspaceControlService,
    default_cleaner,
    stop_project_containers,
)


class RuntimeInspection(Protocol):
    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot: ...


OWNED_PATH_CONFLICT_CODE = "WORKSPACE_OWNED_PATH_CONFLICT"
OWNED_PATH_CONFLICT_MESSAGE = "Requested owned paths overlap an active workspace."


class WorkspaceOwnedPathConflictError(Exception):
    def __init__(self, conflicts: list[OwnedPathConflict]) -> None:
        self.error_code = OWNED_PATH_CONFLICT_CODE
        self.message = OWNED_PATH_CONFLICT_MESSAGE
        self.detail = owned_path_conflict_detail(conflicts)
        super().__init__(self.message)


class WorkspaceService:
    """Domain operations shared across REST routes and MCP tools."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        log_root: Path | str | None = None,
        runtime_inspector: RuntimeInspection | None = None,
        project_stopper: ProjectStopper | None = None,
        cleaner_factory: CleanerFactory | None = None,
    ) -> None:
        self._factory = session_factory
        self._log_root = Path(log_root).resolve() if log_root is not None else None
        self._runtime_inspector = runtime_inspector or RuntimeInspector()
        self._project_stopper = project_stopper or stop_project_containers
        self._cleaner_factory = cleaner_factory or default_cleaner

    async def create(self, req: WorkspaceCreateRequest) -> WorkspaceResponse:
        async with self._factory() as s:
            ws = await WorkspaceRepository(s).create(
                repo_url=req.repo_url,
                branch_base=req.branch_base,
                task_title=req.task_title,
                task_prompt=req.task_prompt,
                task_external_id=req.task_external_id,
                task_class=None,
                owned_paths=[],
                agent=req.agent.value,
                env_profile=req.env_profile,
                test_commands=req.test_commands,
                requires_database=req.requires_database,
            )
            await s.commit()
            return WorkspaceResponse.model_validate(ws)

    async def create_v2(self, req: WorkspaceCreateV2Request) -> WorkspaceResponse:
        async with self._factory() as s:
            ws = await create_workspace_v2_row(s, req)
            await s.commit()
            return WorkspaceResponse.model_validate(ws)

    async def get(self, workspace_id: str) -> WorkspaceResponse | None:
        async with self._factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            return WorkspaceResponse.model_validate(ws) if ws is not None else None

    async def list(self, *, limit: int = 50) -> list[WorkspaceResponse]:
        async with self._factory() as s:
            rows = await WorkspaceRepository(s).list(limit=limit)
            return [WorkspaceResponse.model_validate(r) for r in rows]

    async def cancel_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        stop_stack: bool = True,
    ) -> WorkspaceControlResponse:
        async with self._factory() as s:
            result = await self._controls(s).cancel_workspace(
                workspace_id,
                reason=reason,
                stop_stack=stop_stack,
            )
            await s.commit()
            return result

    async def stop_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
    ) -> WorkspaceControlResponse:
        async with self._factory() as s:
            result = await self._controls(s).stop_workspace(
                workspace_id,
                reason=reason,
            )
            await s.commit()
            return result

    async def destroy_workspace(
        self,
        workspace_id: str,
        *,
        force: bool = False,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> WorkspaceControlResponse:
        async with self._factory() as s:
            result = await self._controls(s).destroy_workspace(
                workspace_id,
                force=force,
                remove_volumes=remove_volumes,
                remove_worktree=remove_worktree,
            )
            await s.commit()
            return result

    async def get_runtime(self, workspace_id: str) -> WorkspaceRuntimeResponse | None:
        async with self._factory() as s:
            workspace = await WorkspaceRepository(s).get(workspace_id)
            if workspace is None:
                return None
            compose_project_name = workspace.compose_project_name

        snapshot = await self._runtime_inspector.inspect(compose_project_name)
        return WorkspaceRuntimeResponse(
            workspace_id=workspace_id,
            compose_project_name=compose_project_name,
            stack_state=snapshot.stack_state,
            services=[
                RuntimeServiceResponse(
                    name=s.name,
                    container_id=s.container_id,
                    image=s.image,
                    state=s.state,
                    status=s.status,
                    health=s.health,
                    ports=s.ports,
                    started_at=s.started_at,
                )
                for s in snapshot.services
            ],
            logs_available=True,
            control_available=True,
            reason=snapshot.reason,
        )

    async def list_operations(
        self,
        workspace_id: str,
        *,
        status: OperationStatus | str | None = None,
        operation_type: OperationType | str | None = None,
        limit: int = 50,
    ) -> builtins.list[OperationResponse] | None:
        async with self._factory() as s:
            workspace_repo = WorkspaceRepository(s)
            if not await workspace_repo.exists(workspace_id):
                return None
            rows = await OperationRepository(s).list_for_workspace(
                workspace_id,
                status=status,
                operation_type=operation_type,
                limit=limit,
            )
            return [OperationResponse.model_validate(row) for row in rows]

    async def list_all_operations(
        self,
        *,
        workspace_id: str | None = None,
        status: OperationStatus | str | None = None,
        operation_type: OperationType | str | None = None,
        limit: int = 50,
    ) -> builtins.list[OperationResponse]:
        async with self._factory() as s:
            rows = await OperationRepository(s).list_all(
                workspace_id=workspace_id,
                status=status,
                operation_type=operation_type,
                limit=limit,
            )
            return [OperationResponse.model_validate(row) for row in rows]

    async def get_operation(self, operation_id: str) -> OperationResponse | None:
        async with self._factory() as s:
            operation = await OperationRepository(s).get(operation_id)
            return (
                OperationResponse.model_validate(operation)
                if operation is not None
                else None
            )

    async def list_events(
        self,
        workspace_id: str,
        *,
        limit: int = 50,
        event_type: str | None = None,
    ) -> builtins.list[WorkspaceEventResponse] | None:
        async with self._factory() as s:
            workspace_repo = WorkspaceRepository(s)
            if not await workspace_repo.exists(workspace_id):
                return None
            rows = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=event_type,
                limit=limit,
            )
            return [WorkspaceEventResponse.model_validate(row) for row in rows]

    async def list_logs(
        self, workspace_id: str
    ) -> builtins.list[WorkspaceLogStreamResponse] | None:
        async with self._factory() as s:
            workspace_repo = WorkspaceRepository(s)
            if not await workspace_repo.exists(workspace_id):
                return None
            rows = await WorkspaceLogStreamRepository(s).list_for_workspace(workspace_id)
            return [WorkspaceLogStreamResponse.model_validate(row) for row in rows]

    async def read_log(
        self,
        workspace_id: str,
        stream_id: str,
        *,
        offset: int = 0,
        limit_bytes: int = 65_536,
    ) -> dict[str, Any] | None:
        async with self._factory() as s:
            workspace_repo = WorkspaceRepository(s)
            if not await workspace_repo.exists(workspace_id):
                return None
            stream = await WorkspaceLogStreamRepository(s).get(
                workspace_id=workspace_id,
                stream_id=stream_id,
            )
            if stream is None:
                return None
            path = Path(stream.path)

        if (
            self._log_root is not None
            and not path.resolve().is_relative_to(self._log_root)
        ):
            return None
        if not path.is_file():
            return None
        data, next_offset, eof = await read_log_chunk(
            path=path,
            offset=offset,
            limit_bytes=limit_bytes,
        )
        return {
            "stream_id": stream_id,
            "offset": offset,
            "next_offset": next_offset,
            "eof": eof,
            "text": data,
        }

    def _controls(self, session: AsyncSession) -> WorkspaceControlService:
        return WorkspaceControlService(
            session,
            project_stopper=self._project_stopper,
            cleaner_factory=self._cleaner_factory,
        )


async def create_workspace_v2_row(
    session: AsyncSession,
    payload: WorkspaceCreateV2Request,
    *,
    idempotency_key: str | None = None,
) -> Workspace:
    """Persist one v2 workspace request without committing the session."""
    repo = WorkspaceRepository(session)
    await repo.acquire_owned_path_conflict_lock(
        repo_url=payload.repo.url,
        branch_base=payload.repo.base_branch,
        owned_paths=payload.task.owned_paths,
    )
    conflicts = await repo.find_active_owned_path_conflicts(
        repo_url=payload.repo.url,
        branch_base=payload.repo.base_branch,
        owned_paths=payload.task.owned_paths,
    )
    if conflicts:
        raise WorkspaceOwnedPathConflictError(conflicts)

    requested_profile, resolved_profile = v2_profile_snapshots(payload)
    ws = await repo.create(
        repo_url=payload.repo.url,
        branch_base=payload.repo.base_branch,
        task_title=payload.task.title,
        task_prompt=payload.task.prompt,
        task_external_id=payload.task.external_id,
        task_class=(
            payload.task.task_class.value if payload.task.task_class is not None else None
        ),
        owned_paths=payload.task.owned_paths,
        auto_merge=payload.task.auto_merge,
        initial_review_grace_period_seconds=(
            payload.task.initial_review_grace_period_seconds
        ),
        agent=payload.task.agent.value,
        env_profile=None,
        profile_ref=payload.workspace.profile_ref,
        requested_profile=requested_profile,
        resolved_profile=resolved_profile,
        test_commands=payload.validation.commands,
        requires_database=False,
        idempotency_key=idempotency_key,
    )
    ws.task_kind = payload.task.kind
    await session.flush()
    return ws


def owned_path_conflict_detail(conflicts: list[OwnedPathConflict]) -> dict[str, Any]:
    workspace_ids: list[str] = []
    conflict_items: list[dict[str, str]] = []
    for conflict in conflicts:
        if conflict.workspace_id not in workspace_ids:
            workspace_ids.append(conflict.workspace_id)
        conflict_items.append(
            {
                "workspace_id": conflict.workspace_id,
                "existing_path": conflict.existing_path,
                "requested_path": conflict.requested_path,
            }
        )
    return {
        "workspace_ids": workspace_ids,
        "conflicts": conflict_items,
    }


def v2_profile_snapshots(
    payload: WorkspaceCreateV2Request,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    requested_profile = (
        payload.workspace.profile.model_dump(mode="json", by_alias=True)
        if payload.workspace.profile is not None
        else None
    )
    resolved_profile = None
    if payload.workspace.profile is not None or (
        payload.workspace.profile_ref and payload.workspace.profile_ref != "auto"
    ):
        resolved = resolve_workspace_profile(
            worktree_path=None,
            inline_profile=payload.workspace.profile,
            profile_ref=payload.workspace.profile_ref,
            validation_commands=payload.validation.commands,
        )
        profile = profile_with_requested_tier(
            resolved.profile,
            payload.validation.requested_tier,
        )
        resolved_profile = profile.model_dump(mode="json", by_alias=True)
    return requested_profile, resolved_profile


def profile_with_requested_tier(
    profile: WorkspaceProfile,
    requested_tier: int,
) -> WorkspaceProfile:
    if profile.validation.requested_tier == requested_tier:
        return profile
    return profile.model_copy(
        update={
            "validation": profile.validation.model_copy(
                update={"requested_tier": requested_tier}
            )
        }
    )
