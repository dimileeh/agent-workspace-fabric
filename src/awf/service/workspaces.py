"""Workspace service operations shared by REST routes and MCP tools."""

from __future__ import annotations

import builtins
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import (
    OperationResponse,
    OwnedPathOverlapResponse,
    RuntimeServiceResponse,
    ValidationFreshnessSummaryResponse,
    WorkspaceControlResponse,
    WorkspaceCreateRequest,
    WorkspaceCreateV2Request,
    WorkspaceEventResponse,
    WorkspaceLogStreamResponse,
    WorkspaceResponse,
    WorkspaceRetryResponse,
    WorkspaceRuntimeResponse,
    WorkspaceWarningResponse,
)
from awf.common.config import Settings, get_settings
from awf.common.logging import get_logger
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Operation, Task, TaskAttempt, Workspace
from awf.db.repositories import (
    OperationRepository,
    OwnedPathOverlap,
    QueueDecisionRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
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
from awf.service.validation_observability import (
    latest_merge_candidate,
    validation_freshness_summary,
    validation_provenance_unavailable,
)
from awf.service.workspace_observability import workspace_observability_payload


class RuntimeInspection(Protocol):
    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot: ...


OWNED_PATH_OVERLAP_RISK_CODE = "OWNED_PATH_OVERLAP_RISK"
OWNED_PATH_OVERLAP_RISK_EVENT_TYPE = "workspace.owned_path_overlap_risk"
OWNED_PATH_OVERLAP_RISK_MESSAGE = (
    "Owned paths overlap active workspaces; this may require rebase "
    "or conflict resolution."
)
OWNED_PATH_OVERLAP_PAYLOAD_FIELDS = (
    "workspace_id",
    "existing_path",
    "requested_path",
)
QUEUE_DECISION_ADMITTED = "admitted"
QUEUE_DECISION_ADMITTED_LOCAL_REASON = "ADMITTED_LOCAL"
RESOURCE_RESERVATION_PHASE_WORKSPACE = "workspace_lifecycle"
RETRYABLE_WORKSPACE_STATUSES = (
    WorkspaceStatus.failed,
    WorkspaceStatus.cancelled,
)


@dataclass(frozen=True)
class _WorkspaceResponseSource:
    workspace: Workspace
    computed_fields: Mapping[str, Any]

    def __getattr__(self, name: str) -> Any:
        try:
            return self.computed_fields[name]
        except KeyError:
            return getattr(self.workspace, name)


TASK_CLASS_PRIORITIES = {
    "migration_task": 5,
    "dependency_task": 4,
    "build_config_task": 3,
    "refactor_task": 2,
    "test_task": 1,
    "docs_task": 0,
}
TASK_CLASS_BIASES = {
    "migration_task": 15,
    "dependency_task": 12,
    "build_config_task": 10,
    "refactor_task": 4,
    "test_task": 2,
    "docs_task": 0,
}
_log = get_logger(__name__)


class WorkspaceRetryError(Exception):
    error_code = "WORKSPACE_RETRY_ERROR"
    message = "Workspace retry failed."
    detail: dict[str, Any] | None

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if message is not None:
            self.message = message
        self.detail = detail
        super().__init__(self.message)


class WorkspaceRetryNotFoundError(WorkspaceRetryError):
    error_code = "WORKSPACE_NOT_FOUND"

    def __init__(self, workspace_id: str) -> None:
        super().__init__(f"No workspace with id {workspace_id}")


class WorkspaceRetryNotAllowedError(WorkspaceRetryError):
    error_code = "WORKSPACE_NOT_RETRYABLE"

    def __init__(self, workspace: Workspace) -> None:
        super().__init__(
            "Only failed or cancelled workspaces can be retried.",
            detail={
                "status": workspace.status,
                "retryable_statuses": [
                    status.value for status in RETRYABLE_WORKSPACE_STATUSES
                ],
            },
        )


@dataclass(frozen=True)
class WorkspaceRetryResult:
    source_workspace_id: str
    new_workspace: Workspace
    operation: Operation
    attempt_number: int


@dataclass(frozen=True)
class ResourceReservationPlan:
    node_id: str
    steady_cpu: float
    steady_memory_gb: float
    peak_cpu: float
    peak_memory_gb: float
    disk_mb: int | None
    phase: str = RESOURCE_RESERVATION_PHASE_WORKSPACE

    def summary(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "steady_cpu": self.steady_cpu,
            "steady_memory_gb": self.steady_memory_gb,
            "peak_cpu": self.peak_cpu,
            "peak_memory_gb": self.peak_memory_gb,
            "disk_mb": self.disk_mb,
            "phase": self.phase,
        }


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
            return workspace_response(ws)

    async def create_v2(self, req: WorkspaceCreateV2Request) -> WorkspaceResponse:
        async with self._factory() as s:
            ws = await create_workspace_v2_row(s, req)
            await s.commit()
            return workspace_response(ws)

    async def retry_workspace(self, workspace_id: str) -> WorkspaceRetryResponse:
        async with self._factory() as s:
            result = await retry_workspace_row(s, workspace_id)
            await s.commit()
            return workspace_retry_response(result)

    async def get(self, workspace_id: str) -> WorkspaceResponse | None:
        async with self._factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            if ws is None:
                return None
            validation_runs = await ValidationRunRepository(s).list_for_workspace(workspace_id)
            validation_provenance = validation_freshness_summary(
                ws,
                validation_runs,
                candidate=latest_merge_candidate(ws),
            )
            return workspace_response(ws, validation_provenance=validation_provenance)

    async def list(self, *, limit: int = 50) -> list[WorkspaceResponse]:
        async with self._factory() as s:
            rows = await WorkspaceRepository(s).list(limit=limit)
            return [workspace_response(r) for r in rows]

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
    settings: Settings | None = None,
) -> Workspace:
    """Persist one v2 workspace request without committing the session."""
    resolved_settings = settings or get_settings()
    repo = WorkspaceRepository(session)
    overlaps = await repo.find_active_owned_path_overlaps(
        repo_url=payload.repo.url,
        branch_base=payload.repo.base_branch,
        owned_paths=payload.task.owned_paths,
    )

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
        task_policy=v2_task_policy_snapshot(payload),
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
        task_kind=payload.task.kind,
    )
    task = await TaskRepository(session).create_or_get(
        repo_url=payload.repo.url,
        base_branch=payload.repo.base_branch,
        title=payload.task.title,
        prompt=payload.task.prompt,
        external_id=payload.task.external_id,
        idempotency_key=idempotency_key,
        task_class=(
            payload.task.task_class.value if payload.task.task_class is not None else None
        ),
        owned_paths=payload.task.owned_paths,
    )
    attempt = await TaskAttemptRepository(session).create_for_workspace(task=task, workspace=ws)
    reservation_plan = resource_reservation_plan(payload, settings=resolved_settings)
    await ResourceReservationRepository(session).create(
        workspace_id=ws.id,
        attempt_id=attempt.id,
        node_id=reservation_plan.node_id,
        steady_cpu=reservation_plan.steady_cpu,
        steady_memory_gb=reservation_plan.steady_memory_gb,
        peak_cpu=reservation_plan.peak_cpu,
        peak_memory_gb=reservation_plan.peak_memory_gb,
        disk_mb=reservation_plan.disk_mb,
        phase=reservation_plan.phase,
    )
    await QueueDecisionRepository(session).create(
        workspace_id=ws.id,
        task_id=task.id,
        attempt_id=attempt.id,
        decision=QUEUE_DECISION_ADMITTED,
        reason_code=QUEUE_DECISION_ADMITTED_LOCAL_REASON,
        class_priority=task_class_priority(ws.task_class),
        computed_priority=computed_priority(
            base_priority=payload.task.priority,
            task_class=ws.task_class,
            age_boost=0,
            retry_bonus=0,
        ),
        age_boost=0,
        retry_bonus=0,
        resource_summary=reservation_plan.summary(),
        overlap_risk_summary=overlap_risk_summary(overlaps),
    )
    await _record_owned_path_overlap_risk(repo, ws, overlaps)
    await session.flush()
    return ws


async def retry_workspace_row(
    session: AsyncSession,
    workspace_id: str,
) -> WorkspaceRetryResult:
    """Create a fresh requested workspace cloned from a failed/cancelled attempt."""
    repo = WorkspaceRepository(session)
    source = await repo.get_for_update(workspace_id)
    if source is None:
        raise WorkspaceRetryNotFoundError(workspace_id)

    if WorkspaceStatus(source.status) not in RETRYABLE_WORKSPACE_STATUSES:
        raise WorkspaceRetryNotAllowedError(source)

    overlaps = await repo.find_active_owned_path_overlaps(
        repo_url=source.repo_url,
        branch_base=source.branch_base,
        owned_paths=list(source.owned_paths),
    )

    retried = await repo.create(
        repo_url=source.repo_url,
        branch_base=source.branch_base,
        task_title=source.task_title,
        task_prompt=source.task_prompt,
        task_external_id=source.task_external_id,
        task_class=source.task_class,
        owned_paths=list(source.owned_paths),
        task_policy=deepcopy(source.task_policy),
        auto_merge=source.auto_merge,
        initial_review_grace_period_seconds=(
            source.initial_review_grace_period_seconds
        ),
        agent=source.agent,
        env_profile=source.env_profile,
        profile_ref=source.profile_ref,
        requested_profile=deepcopy(source.requested_profile),
        resolved_profile=deepcopy(source.resolved_profile),
        test_commands=list(source.test_commands),
        requires_database=source.requires_database,
        idempotency_key=None,
        task_kind=source.task_kind,
        remote_push_branch=source.remote_push_branch,
    )

    attempt_repo = TaskAttemptRepository(session)
    source_attempt = await attempt_repo.get_by_workspace_id(source.id)
    task = await _retry_task_for_source(session, source, source_attempt=source_attempt)
    attempt = await attempt_repo.create_for_workspace(
        task=task,
        workspace=retried,
        parent_attempt_id=source_attempt.id if source_attempt is not None else None,
        redispatch_from_attempt_id=source_attempt.id if source_attempt is not None else None,
    )

    operation_repo = OperationRepository(session)
    operation = await operation_repo.create(
        workspace_id=retried.id,
        operation_type=OperationType.retry,
        status=OperationStatus.running,
        payload={"source_workspace_id": source.id},
    )
    event_payload = {
        "source_workspace_id": source.id,
        "new_workspace_id": retried.id,
        "attempt_number": attempt.attempt_number,
    }
    await repo.add_event(
        source,
        event_type="workspace.retry_requested",
        reason_code="RETRY_REQUESTED",
        payload=event_payload,
    )
    await repo.add_event(
        retried,
        event_type="workspace.retry_created",
        reason_code="RETRY_CREATED",
        payload=event_payload,
    )
    await _record_owned_path_overlap_risk(repo, retried, overlaps)
    await operation_repo.finish(
        operation,
        status=OperationStatus.succeeded,
        result={
            "new_workspace_id": retried.id,
            "attempt_number": attempt.attempt_number,
            "status": retried.status,
        },
    )
    await session.flush()
    return WorkspaceRetryResult(
        source_workspace_id=source.id,
        new_workspace=retried,
        operation=operation,
        attempt_number=attempt.attempt_number,
    )


async def _retry_task_for_source(
    session: AsyncSession,
    source: Workspace,
    *,
    source_attempt: TaskAttempt | None = None,
) -> Task:
    if source_attempt is None:
        source_attempt = await TaskAttemptRepository(session).get_by_workspace_id(source.id)
    if source_attempt is not None:
        task = await TaskRepository(session).get(source_attempt.task_id)
        if task is not None:
            return task

    fallback_idempotency_key = f"retry-source-workspace:{source.id}"
    return await TaskRepository(session).create_or_get(
        repo_url=source.repo_url,
        base_branch=source.branch_base,
        title=source.task_title,
        prompt=source.task_prompt,
        external_id=source.task_external_id,
        idempotency_key=fallback_idempotency_key,
        task_class=source.task_class,
        owned_paths=list(source.owned_paths),
    )


def workspace_retry_response(result: WorkspaceRetryResult) -> WorkspaceRetryResponse:
    new_workspace_id = result.new_workspace.id
    return WorkspaceRetryResponse(
        source_workspace_id=result.source_workspace_id,
        new_workspace_id=new_workspace_id,
        operation_id=result.operation.id,
        status=WorkspaceStatus(result.new_workspace.status),
        attempt_number=result.attempt_number,
        status_url=f"/v1/workspaces/{new_workspace_id}",
        events_url=f"/v1/workspaces/{new_workspace_id}/events",
    )


def workspace_response(
    workspace: Workspace,
    *,
    validation_provenance: ValidationFreshnessSummaryResponse | None = None,
) -> WorkspaceResponse:
    computed_fields = dict(workspace_observability_payload(workspace))
    computed_fields["validation_provenance"] = (
        validation_provenance
        if validation_provenance is not None
        else validation_provenance_unavailable(workspace)
    )
    return WorkspaceResponse.model_validate(
        _WorkspaceResponseSource(workspace, computed_fields)
    )


async def _record_owned_path_overlap_risk(
    repo: WorkspaceRepository,
    workspace: Workspace,
    overlaps: list[OwnedPathOverlap],
) -> None:
    if not overlaps:
        return
    await repo.add_event(
        workspace,
        event_type=OWNED_PATH_OVERLAP_RISK_EVENT_TYPE,
        reason_code=OWNED_PATH_OVERLAP_RISK_CODE,
        payload=owned_path_overlap_warning_payload(overlaps),
    )


def owned_path_overlap_warning_payload(overlaps: list[OwnedPathOverlap]) -> dict[str, Any]:
    workspace_ids: dict[str, None] = {}
    overlap_items: list[dict[str, str]] = []
    for overlap in overlaps:
        if overlap.workspace_id not in workspace_ids:
            workspace_ids[overlap.workspace_id] = None
        overlap_items.append(
            {
                "workspace_id": overlap.workspace_id,
                "existing_path": overlap.existing_path,
                "requested_path": overlap.requested_path,
            }
        )
    return {
        "warning_code": OWNED_PATH_OVERLAP_RISK_CODE,
        "message": OWNED_PATH_OVERLAP_RISK_MESSAGE,
        "workspace_ids": list(workspace_ids),
        "overlaps": overlap_items,
    }


def overlap_risk_summary(overlaps: list[OwnedPathOverlap]) -> dict[str, Any]:
    if not overlaps:
        return {
            "warning_code": None,
            "overlap_count": 0,
            "workspace_ids": [],
            "overlaps": [],
        }
    payload = owned_path_overlap_warning_payload(overlaps)
    return {
        "warning_code": OWNED_PATH_OVERLAP_RISK_CODE,
        "overlap_count": len(overlaps),
        "workspace_ids": payload["workspace_ids"],
        "overlaps": payload["overlaps"],
    }


def task_class_priority(task_class: str | None) -> int:
    return TASK_CLASS_PRIORITIES.get(task_class or "", 0)


def computed_priority(
    *,
    base_priority: int,
    task_class: str | None,
    age_boost: int,
    retry_bonus: int,
) -> int:
    return (
        base_priority
        + TASK_CLASS_BIASES.get(task_class or "", 0)
        + age_boost
        + retry_bonus
    )


def resource_reservation_plan(
    payload: WorkspaceCreateV2Request,
    *,
    settings: Settings,
) -> ResourceReservationPlan:
    resources = payload.resources
    legacy_memory_gb = _parse_memory_gb(resources.memory)
    return ResourceReservationPlan(
        node_id=settings.worker_node_id or "local",
        steady_cpu=(
            resources.steady_state_cpu_cores
            if resources.steady_state_cpu_cores is not None
            else resources.cpu
            if resources.cpu is not None
            else settings.workspace_steady_cpu
        ),
        steady_memory_gb=(
            resources.steady_state_memory_gb
            if resources.steady_state_memory_gb is not None
            else legacy_memory_gb
            if legacy_memory_gb is not None
            else settings.workspace_steady_memory_gb
        ),
        peak_cpu=(
            resources.peak_cpu_cores
            if resources.peak_cpu_cores is not None
            else resources.cpu
            if resources.cpu is not None
            else settings.workspace_peak_cpu
        ),
        peak_memory_gb=(
            resources.peak_memory_gb
            if resources.peak_memory_gb is not None
            else legacy_memory_gb
            if legacy_memory_gb is not None
            else settings.workspace_peak_memory_gb
        ),
        disk_mb=resources.disk_mb,
    )


def _parse_memory_gb(value: str | None) -> float | None:
    if value is None:
        return None
    normalized = value.strip().lower().replace(" ", "")
    if normalized == "":
        return None
    multipliers = {
        "gb": 1.0,
        "g": 1.0,
        "mb": 1.0 / 1024.0,
        "m": 1.0 / 1024.0,
    }
    for suffix, multiplier in multipliers.items():
        if normalized.endswith(suffix):
            number = normalized[: -len(suffix)]
            try:
                return float(number) * multiplier
            except ValueError:
                return None
    try:
        memory_gb = float(normalized)
    except ValueError:
        return None
    _log.warning(
        "workspace.resources.memory_unit_missing",
        raw_value=normalized,
        interpreted_unit="gb",
    )
    return memory_gb


def owned_path_overlap_warnings(workspace: Workspace) -> list[WorkspaceWarningResponse]:
    warnings: list[WorkspaceWarningResponse] = []
    for event in workspace.events:
        if event.event_type != OWNED_PATH_OVERLAP_RISK_EVENT_TYPE:
            continue
        payload = event.payload
        if payload is None:
            continue
        warnings.append(_owned_path_overlap_warning_response(payload))
    return warnings


def _owned_path_overlap_warning_response(
    payload: dict[str, Any],
) -> WorkspaceWarningResponse:
    return WorkspaceWarningResponse(
        warning_code=str(payload.get("warning_code", OWNED_PATH_OVERLAP_RISK_CODE)),
        message=str(payload.get("message", OWNED_PATH_OVERLAP_RISK_MESSAGE)),
        workspace_ids=_string_payload_list(payload, "workspace_ids"),
        overlaps=_owned_path_overlap_payload_responses(payload),
    )


def _string_payload_list(payload: dict[str, Any], field: str) -> list[str]:
    value = payload.get(field)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _owned_path_overlap_payload_responses(
    payload: dict[str, Any],
) -> list[OwnedPathOverlapResponse]:
    overlaps = payload.get("overlaps")
    if not isinstance(overlaps, list):
        return []
    return [
        OwnedPathOverlapResponse(
            workspace_id=str(item["workspace_id"]),
            existing_path=str(item["existing_path"]),
            requested_path=str(item["requested_path"]),
        )
        for item in overlaps
        if _has_owned_path_overlap_payload_fields(item)
    ]


def _has_owned_path_overlap_payload_fields(item: Any) -> bool:
    return isinstance(item, dict) and all(
        field in item for field in OWNED_PATH_OVERLAP_PAYLOAD_FIELDS
    )


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


def v2_task_policy_snapshot(payload: WorkspaceCreateV2Request) -> dict[str, Any]:
    policy: dict[str, Any] = {}
    if payload.task.model is not None:
        policy["agent_model"] = payload.task.model
    if payload.task.out_of_scope_changes is not None:
        policy["out_of_scope_changes"] = payload.task.out_of_scope_changes.model_dump(
            mode="json"
        )
    return policy


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
