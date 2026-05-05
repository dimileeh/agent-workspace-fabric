"""Workspace service operations shared by REST routes and MCP tools."""

from __future__ import annotations

import asyncio
import builtins
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import SplitResult, urlsplit, urlunsplit

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import NoInspectionAvailable
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import (
    EgressAuditRecordResponse,
    FallbackTargetResponse,
    OperationResponse,
    OwnedPathOverlapResponse,
    ProviderRecoveryStateResponse,
    PullRequestMonitorAdoptionRequest,
    PullRequestMonitorAdoptionResponse,
    RuntimeServiceResponse,
    ValidationFreshnessSummaryResponse,
    WorkspaceAppEndpointResponse,
    WorkspaceControlResponse,
    WorkspaceCreateRequest,
    WorkspaceCreateV2Request,
    WorkspaceEventResponse,
    WorkspaceLogStreamResponse,
    WorkspaceResponse,
    WorkspaceRetryResponse,
    WorkspaceRuntimeHealthResponse,
    WorkspaceRuntimeResponse,
    WorkspaceWarningResponse,
)
from awf.common.config import Settings, get_settings
from awf.common.logging import get_logger
from awf.common.redaction import redact_secrets
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import EgressAuditRecord, Operation, Task, TaskAttempt, Workspace, WorkspaceSecretLease
from awf.db.repositories import (
    EgressAuditRepository,
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
from awf.profiles.compose import resolve_profile_app_endpoints
from awf.profiles.models import ProfileAppEndpoint, WorkspaceProfile
from awf.profiles.resolver import resolve_workspace_profile
from awf.runtime.inspection import RuntimeInspector, RuntimeSnapshot
from awf.runtime.logs import read_log_chunk
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    PLAN_CONFORMANCE_UNSATISFIED,
    build_planning_scope_retry_prompt,
)
from awf.service.config import resolve_service_settings
from awf.service.conformance_salvage import (
    CONFORMANCE_SALVAGE_POLICY_KEY,
    ConformanceSalvageError,
    build_conformance_salvage_retry_prompt,
    capture_conformance_salvage,
)
from awf.service.controls import (
    CleanerFactory,
    ProjectStopper,
    WorkspaceControlService,
    default_cleaner,
    stop_project_containers,
)
from awf.service.coordination import (
    coordination_warnings_from_task_policy,
    owned_path_overlap_coordination_warnings,
    task_policy_with_coordination_warnings,
)
from awf.service.disk import DiskCheck
from awf.service.pr_monitor_adoption import PullRequestMonitorAdoptionService
from awf.service.profile_metadata import network_posture_from_profile_snapshot
from awf.service.provider_readiness import (
    HttpGet,
    SubprocessRun,
    provider_readiness_preflight_from_task_policy,
    selected_provider_readiness_preflight,
)
from awf.service.provider_recovery import (
    provider_recovery_metadata_from_failure,
    provider_recovery_state_for_workspace,
)
from awf.service.resource_capacity import (
    ReservedResources,
    WorkspaceResourceDefaults,
    resource_capacity_summary,
)
from awf.service.scheduler import (
    SCHEDULER_POLICY_KEY,
    scheduler_policy_snapshot,
    scheduler_retry_policy_context,
    scheduler_score_from_workspace,
    task_class_bias,
)
from awf.service.scheduler import (
    task_class_priority as scheduler_task_class_priority,
)
from awf.service.secret_leases import workspace_secret_lease_response
from awf.service.validation_observability import (
    latest_merge_candidate,
    validation_freshness_summary,
    validation_provenance_unavailable,
)
from awf.service.workspace_observability import (
    is_workspace_stale_running,
    workspace_observability_payload,
    workspace_pricing_metadata,
)
from awf.service.workspace_runtime_health import (
    RUNTIME_STRANDED_EVENT_TYPE,
    classify_runtime_snapshot,
    runtime_workspace_from_workspace,
)


class RuntimeInspection(Protocol):
    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot: ...


OWNED_PATH_OVERLAP_RISK_CODE = "OWNED_PATH_OVERLAP_RISK"
OWNED_PATH_OVERLAP_RISK_EVENT_TYPE = "workspace.owned_path_overlap_risk"
OWNED_PATH_OVERLAP_RISK_MESSAGE = (
    "Owned paths overlap active workspaces; this may require rebase or conflict resolution."
)
OWNED_PATH_OVERLAP_PAYLOAD_FIELDS = (
    "workspace_id",
    "existing_path",
    "requested_path",
)
PROVIDER_READINESS_PREFLIGHT_EVENT_TYPE = "workspace.provider_readiness_preflight"
PROVIDER_READINESS_READY_REASON = "PROVIDER_READINESS_READY"
PROVIDER_READINESS_OVERRIDE_REASON = "PROVIDER_READINESS_OVERRIDE_USED"
QUEUE_DECISION_ADMITTED = "admitted"
QUEUE_DECISION_ADMITTED_LOCAL_REASON = "ADMITTED_LOCAL"
RESOURCE_RESERVATION_PHASE_WORKSPACE = "workspace_lifecycle"
RETRYABLE_WORKSPACE_STATUSES = (
    WorkspaceStatus.failed,
    WorkspaceStatus.cancelled,
)
MAX_CONFORMANCE_RETRY_ATTEMPTS = 4
PRESERVE_RETRY_REMOTE_PUSH_BRANCH_TASK_KINDS = frozenset(
    {"monitor_release_pr", "sync_release_pr", "sync_feature_pr"}
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


_log = get_logger(__name__)
_PROFILE_APP_ENDPOINTS_ADAPTER = TypeAdapter(list[ProfileAppEndpoint])


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
                "retryable_statuses": [status.value for status in RETRYABLE_WORKSPACE_STATUSES],
            },
        )


class WorkspaceRetryExhaustedError(WorkspaceRetryError):
    error_code = "WORKSPACE_RETRY_EXHAUSTED"

    def __init__(self, attempt_count: int) -> None:
        super().__init__(
            "Conformance retry attempts exhausted.",
            detail={
                "attempt_count": attempt_count,
                "max_attempts": MAX_CONFORMANCE_RETRY_ATTEMPTS,
            },
        )


class WorkspaceRetrySalvageUnavailableError(WorkspaceRetryError):
    error_code = "WORKSPACE_RETRY_SALVAGE_UNAVAILABLE"

    def __init__(
        self,
        workspace: Workspace,
        *,
        reason_code: str,
        message: str,
        evidence: Mapping[str, Any] | None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        evidence = evidence or {}
        payload: dict[str, Any] = {
            "source_workspace_id": workspace.id,
            "source_reason_code": PLAN_CONFORMANCE_UNSATISFIED,
            "reason_code": reason_code,
            "gaps": _retry_evidence_gaps(evidence),
            "plan_path": _optional_retry_evidence_str(evidence.get("plan_path")),
            "report_path": _optional_retry_evidence_str(evidence.get("report_path")),
            "recovery_guidance": (
                "AWF could not automatically preserve the failed conformance "
                "attempt's implementation diff. Inspect the source worktree or "
                "relaunch the original task explicitly."
            ),
        }
        if detail:
            payload.update(dict(detail))
        super().__init__(message, detail=payload)


class WorkspaceProviderReadinessBlockedError(WorkspaceRetryError):
    error_code = "PROVIDER_READINESS_PRECHECK_FAILED"

    def __init__(self, preflight: Mapping[str, Any]) -> None:
        super().__init__(
            "Selected provider readiness blocked workspace launch.",
            detail={"provider_readiness_preflight": dict(preflight)},
        )


@dataclass(frozen=True)
class WorkspaceRetryResult:
    source_workspace_id: str
    new_workspace: Workspace
    operation: Operation
    attempt_number: int


@dataclass(frozen=True)
class _ConformanceRetryContext:
    reason_code: str
    evidence: Mapping[str, Any]
    evidence_ref: dict[str, str]


@dataclass(frozen=True)
class _PlanningScopeRetryContext:
    reason_code: str
    evidence: Mapping[str, Any]
    evidence_ref: dict[str, str]
    recovery_strategy: str
    salvage_policy: str
    salvage: dict[str, str] | None = None
    fallback_model: dict[str, str] | None = None


@dataclass(frozen=True)
class ResourceReservationPlan:
    node_id: str
    steady_cpu: float
    steady_memory_gb: float
    peak_cpu: float
    peak_memory_gb: float
    disk_mb: int | None
    dind_slots: int
    dind_mode: str
    phase: str = RESOURCE_RESERVATION_PHASE_WORKSPACE

    def summary(
        self,
        *,
        settings: Settings,
        disk_check: DiskCheck | None = None,
        reserved_resources: ReservedResources | None = None,
    ) -> dict[str, Any]:
        capacity = resource_capacity_summary(
            settings=settings,
            reserved=reserved_resources
            or ReservedResources(
                active_workspace_count=1,
                steady_cpu=self.steady_cpu,
                steady_memory_gb=self.steady_memory_gb,
                peak_cpu=self.peak_cpu,
                peak_memory_gb=self.peak_memory_gb,
                disk_mb=self.disk_mb or 0,
                dind_slots=self.dind_slots,
            ),
            resource_defaults=WorkspaceResourceDefaults(
                steady_cpu=settings.workspace_steady_cpu,
                steady_memory_gb=settings.workspace_steady_memory_gb,
                peak_cpu=settings.workspace_peak_cpu,
                peak_memory_gb=settings.workspace_peak_memory_gb,
            ),
            disk_check=disk_check,
        )
        pressure_reasons = [
            reason for reason in capacity.pressure_reasons if not reason.endswith("_UNKNOWN")
        ]
        return {
            "node_id": self.node_id,
            "steady_cpu": self.steady_cpu,
            "steady_memory_gb": self.steady_memory_gb,
            "peak_cpu": self.peak_cpu,
            "peak_memory_gb": self.peak_memory_gb,
            "disk_mb": self.disk_mb,
            "dind_slots": self.dind_slots,
            "phase": self.phase,
            "dind_mode": self.dind_mode,
            "pressure_reasons": pressure_reasons,
            "capacity": capacity.to_dict(),
        }


class WorkspaceService:
    """Domain operations shared across REST routes and MCP tools."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings | None = None,
        log_root: Path | str | None = None,
        runtime_inspector: RuntimeInspection | None = None,
        project_stopper: ProjectStopper | None = None,
        cleaner_factory: CleanerFactory | None = None,
        pr_adoption_metadata_fetcher: Any | None = None,
    ) -> None:
        self._factory = session_factory
        self._settings = settings
        self._log_root = Path(log_root).resolve() if log_root is not None else None
        self._runtime_inspector = runtime_inspector or RuntimeInspector()
        self._project_stopper = project_stopper or stop_project_containers
        self._cleaner_factory = cleaner_factory or default_cleaner
        self._pr_adoption_metadata_fetcher = pr_adoption_metadata_fetcher

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Expose the shared session factory for read-only parity clients."""
        return self._factory

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
            ws = await create_workspace_v2_row(s, req, settings=self._settings)
            await s.commit()
            return workspace_response(ws)

    async def retry_workspace(
        self,
        workspace_id: str,
        *,
        provider_readiness_override: bool = False,
        provider_readiness_override_reason: str | None = None,
    ) -> WorkspaceRetryResponse:
        async with self._factory() as s:
            result = await retry_workspace_row(
                s,
                workspace_id,
                provider_readiness_override=provider_readiness_override,
                provider_readiness_override_reason=provider_readiness_override_reason,
                settings=self._settings,
            )
            await s.commit()
            return workspace_retry_response(result)

    async def adopt_pull_request_monitor(
        self,
        req: PullRequestMonitorAdoptionRequest,
    ) -> PullRequestMonitorAdoptionResponse:
        async with self._factory() as s:
            result = await PullRequestMonitorAdoptionService(
                s,
                metadata_fetcher=self._pr_adoption_metadata_fetcher,
                settings=self._settings,
            ).adopt(req)
            await s.commit()
            return result

    async def get(self, workspace_id: str) -> WorkspaceResponse | None:
        async with self._factory() as s:
            ws = await WorkspaceRepository(s).get_with_operations(workspace_id)
            if ws is None:
                return None
            validation_runs = await ValidationRunRepository(s).list_for_workspace(workspace_id)
            validation_provenance = validation_freshness_summary(
                ws,
                validation_runs,
                candidate=latest_merge_candidate(ws),
            )
            try:
                audit_record = await EgressAuditRepository(s).get_latest_for_workspace(workspace_id)
            except Exception:
                _log.warning(
                    "Failed to fetch egress audit for workspace %s",
                    workspace_id,
                    exc_info=True,
                )
                egress_audit = None
            else:
                egress_audit = _egress_audit_response(audit_record) if audit_record is not None else None
            return workspace_response(
                ws,
                validation_provenance=validation_provenance,
                egress_audit=egress_audit,
            )

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
        idempotency_key: str | None = None,
    ) -> WorkspaceControlResponse:
        async with self._factory() as s:
            result = await self._controls(s).cancel_workspace(
                workspace_id,
                reason=reason,
                stop_stack=stop_stack,
                idempotency_key=idempotency_key,
            )
            await s.commit()
            return result

    async def stop_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> WorkspaceControlResponse:
        async with self._factory() as s:
            result = await self._controls(s).stop_workspace(
                workspace_id,
                reason=reason,
                idempotency_key=idempotency_key,
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
        idempotency_key: str | None = None,
    ) -> WorkspaceControlResponse:
        async with self._factory() as s:
            result = await self._controls(s).destroy_workspace(
                workspace_id,
                force=force,
                remove_volumes=remove_volumes,
                remove_worktree=remove_worktree,
                idempotency_key=idempotency_key,
            )
            await s.commit()
            return result

    async def remonitor_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> WorkspaceControlResponse:
        async with self._factory() as s:
            result = await self._controls(s).remonitor_workspace(
                workspace_id,
                reason=reason,
                idempotency_key=idempotency_key,
            )
            await s.commit()
            return result

    async def request_validate_workspace(
        self,
        workspace_id: str,
        *,
        reason: str | None = None,
        requested_tier: int | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        async with self._factory() as s:
            result = await self._controls(s).request_validate_workspace(
                workspace_id,
                reason=reason,
                requested_tier=requested_tier,
                idempotency_key=idempotency_key,
            )
            await s.commit()
            return result

    async def get_runtime(self, workspace_id: str) -> WorkspaceRuntimeResponse | None:
        async with self._factory() as s:
            workspace = await WorkspaceRepository(s).get(workspace_id)
            if workspace is None:
                return None
            compose_project_name = workspace.compose_project_name
            runtime_workspace = runtime_workspace_from_workspace(workspace)
            app_endpoints = _workspace_app_endpoint_responses(workspace)

        snapshot = await self._runtime_inspector.inspect(compose_project_name)
        finding = classify_runtime_snapshot(runtime_workspace, snapshot)
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
            runtime_health=(
                WorkspaceRuntimeHealthResponse(**finding.to_response_dict())
                if finding is not None
                else None
            ),
            app_endpoints=app_endpoints,
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
            return OperationResponse.model_validate(operation) if operation is not None else None

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

        if self._log_root is not None and not path.resolve().is_relative_to(self._log_root):
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
    disk_check: DiskCheck | None = None,
    provider_environ: Mapping[str, str] | None = None,
    run_subprocess: SubprocessRun | None = None,
    http_get: HttpGet | None = None,
) -> Workspace:
    """Persist one v2 workspace request without committing the session."""
    resolved_settings = settings or get_settings()
    repo = WorkspaceRepository(session)
    base_task_policy = v2_task_policy_snapshot(payload)
    requested_profile, resolved_profile = v2_profile_snapshots(payload)
    preflight = await _selected_provider_preflight_for_task_async(
        resolved_settings,
        agent=payload.task.agent,
        task_policy=base_task_policy,
        override=payload.preflight.provider_readiness_override,
        override_reason=payload.preflight.provider_readiness_override_reason,
        provider_environ=provider_environ,
        run_subprocess=run_subprocess,
        http_get=http_get,
    )
    _raise_if_provider_preflight_blocks(preflight)
    overlaps = await repo.find_active_owned_path_overlaps(
        repo_url=payload.repo.url,
        branch_base=payload.repo.base_branch,
        owned_paths=payload.task.owned_paths,
    )
    task_policy = task_policy_with_coordination_warnings(
        {
            **base_task_policy,
            "provider_readiness_preflight": preflight,
        },
        owned_path_overlap_coordination_warnings(overlaps),
    )

    ws = await repo.create(
        repo_url=payload.repo.url,
        branch_base=payload.repo.base_branch,
        task_title=payload.task.title,
        task_prompt=payload.task.prompt,
        task_external_id=payload.task.external_id,
        task_class=(payload.task.task_class.value if payload.task.task_class is not None else None),
        owned_paths=payload.task.owned_paths,
        task_policy=task_policy,
        auto_merge=payload.task.auto_merge,
        initial_review_grace_period_seconds=(payload.task.initial_review_grace_period_seconds),
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
        task_class=(payload.task.task_class.value if payload.task.task_class is not None else None),
        owned_paths=payload.task.owned_paths,
    )
    attempt = await TaskAttemptRepository(session).create_for_workspace(task=task, workspace=ws)
    reservation_plan = resource_reservation_plan(payload, settings=resolved_settings)
    resource_summary = await _resource_reservation_summary(
        session,
        reservation_plan,
        settings=resolved_settings,
        disk_check=disk_check,
    )
    await ResourceReservationRepository(session).create(
        workspace_id=ws.id,
        attempt_id=attempt.id,
        node_id=reservation_plan.node_id,
        steady_cpu=reservation_plan.steady_cpu,
        steady_memory_gb=reservation_plan.steady_memory_gb,
        peak_cpu=reservation_plan.peak_cpu,
        peak_memory_gb=reservation_plan.peak_memory_gb,
        disk_mb=reservation_plan.disk_mb,
        dind_slots=reservation_plan.dind_slots,
        phase=reservation_plan.phase,
    )
    scheduler_score = scheduler_score_from_workspace(ws, now=ws.created_at)
    await QueueDecisionRepository(session).create(
        workspace_id=ws.id,
        task_id=task.id,
        attempt_id=attempt.id,
        decision=QUEUE_DECISION_ADMITTED,
        reason_code=QUEUE_DECISION_ADMITTED_LOCAL_REASON,
        class_priority=scheduler_score.class_priority,
        computed_priority=scheduler_score.effective_score,
        age_boost=scheduler_score.age_boost,
        retry_bonus=scheduler_score.retry_bonus,
        resource_summary=resource_summary,
        overlap_risk_summary=overlap_risk_summary(overlaps),
        score_summary=scheduler_score.score_summary,
    )
    await _record_provider_readiness_preflight(repo, ws, preflight)
    await _record_owned_path_overlap_risk(repo, ws, overlaps)
    await session.flush()
    return ws


async def retry_workspace_row(
    session: AsyncSession,
    workspace_id: str,
    *,
    provider_readiness_override: bool = False,
    provider_readiness_override_reason: str | None = None,
    settings: Settings | None = None,
    provider_environ: Mapping[str, str] | None = None,
    run_subprocess: SubprocessRun | None = None,
    http_get: HttpGet | None = None,
) -> WorkspaceRetryResult:
    """Create a fresh requested workspace cloned from a failed/cancelled attempt."""
    resolved_settings = settings or get_settings()
    repo = WorkspaceRepository(session)
    source = await repo.get_for_update(workspace_id)
    if source is None:
        raise WorkspaceRetryNotFoundError(workspace_id)

    if WorkspaceStatus(source.status) not in RETRYABLE_WORKSPACE_STATUSES:
        raise WorkspaceRetryNotAllowedError(source)

    planning_scope_context = _planning_scope_retry_context(source)
    conformance_context = (
        None if planning_scope_context is not None else _conformance_retry_context(source)
    )
    conformance_retry_requested = planning_scope_context is None and (
        conformance_context is not None or _is_plan_conformance_unsatisfied(source)
    )
    conformance_evidence: Mapping[str, Any] = (
        conformance_context.evidence if conformance_context is not None else {}
    )

    if planning_scope_context is not None:
        retried_prompt = build_planning_scope_retry_prompt(
            task_prompt=source.task_prompt,
            evidence=planning_scope_context.evidence,
        )
    else:
        retried_prompt = source.task_prompt

    overlaps = await repo.find_active_owned_path_overlaps(
        repo_url=source.repo_url,
        branch_base=source.branch_base,
        owned_paths=list(source.owned_paths),
    )
    retried_task_policy = _retry_task_policy(
        source,
        owned_path_overlap_coordination_warnings(overlaps),
        planning_scope_context=planning_scope_context,
    )
    preflight = await _selected_provider_preflight_for_task_async(
        resolved_settings,
        agent=source.agent,
        task_policy=retried_task_policy,
        override=provider_readiness_override,
        override_reason=provider_readiness_override_reason,
        provider_environ=provider_environ,
        run_subprocess=run_subprocess,
        http_get=http_get,
    )
    preflight = {**preflight, "source_workspace_id": source.id}
    _raise_if_provider_preflight_blocks(preflight)
    conformance_salvage: dict[str, Any] | None = None
    if conformance_retry_requested:
        try:
            salvage_capture = await asyncio.to_thread(
                capture_conformance_salvage,
                work_dir=resolved_settings.work_dir,
                source_workspace_id=source.id,
                source_base_commit=source.base_commit,
                conformance_evidence=conformance_evidence,
                conformance_evidence_ref=(
                    conformance_context.evidence_ref if conformance_context is not None else None
                ),
                source_branch_name=source.branch_name,
                source_remote_push_branch=source.remote_push_branch,
                run_subprocess=run_subprocess,
            )
        except ConformanceSalvageError as exc:
            raise WorkspaceRetrySalvageUnavailableError(
                source,
                reason_code=exc.reason_code,
                message=str(exc),
                evidence=conformance_evidence,
                detail=exc.detail,
            ) from exc
        conformance_salvage = salvage_capture.as_policy()
        retried_task_policy[CONFORMANCE_SALVAGE_POLICY_KEY] = conformance_salvage
        retried_prompt = build_conformance_salvage_retry_prompt(
            task_prompt=source.task_prompt,
            evidence=conformance_evidence,
            salvage=conformance_salvage,
        )
    retried_task_policy = {
        **retried_task_policy,
        "provider_readiness_preflight": preflight,
    }

    retried = await repo.create(
        repo_url=source.repo_url,
        branch_base=source.branch_base,
        task_title=source.task_title,
        task_prompt=retried_prompt,
        task_external_id=source.task_external_id,
        task_class=source.task_class,
        owned_paths=list(source.owned_paths),
        task_policy=retried_task_policy,
        auto_merge=source.auto_merge,
        initial_review_grace_period_seconds=(source.initial_review_grace_period_seconds),
        agent=source.agent,
        env_profile=source.env_profile,
        profile_ref=source.profile_ref,
        requested_profile=deepcopy(source.requested_profile),
        resolved_profile=deepcopy(source.resolved_profile),
        test_commands=list(source.test_commands),
        requires_database=source.requires_database,
        idempotency_key=None,
        task_kind=source.task_kind,
        remote_push_branch=(
            source.remote_push_branch
            if planning_scope_context is None
            or source.task_kind in PRESERVE_RETRY_REMOTE_PUSH_BRANCH_TASK_KINDS
            else None
        ),
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
    retry_policy = dict(retried.task_policy or {})
    retry_scheduler_value = retry_policy.get(SCHEDULER_POLICY_KEY)
    retry_scheduler_policy = (
        dict(retry_scheduler_value) if isinstance(retry_scheduler_value, Mapping) else {}
    )
    retry_scheduler_policy["retry_attempt_number"] = max(0, attempt.attempt_number - 1)
    retry_policy[SCHEDULER_POLICY_KEY] = retry_scheduler_policy
    retried.task_policy = retry_policy
    latest_source_reservation = await ResourceReservationRepository(session).list_for_workspace(
        source.id, limit=1
    )
    retry_resource_summary: dict[str, Any] = {}
    if latest_source_reservation:
        source_reservation = latest_source_reservation[0]
        retry_reservation = ResourceReservationPlan(
            node_id=resolved_settings.worker_node_id or source_reservation.node_id,
            steady_cpu=resolved_settings.workspace_steady_cpu,
            steady_memory_gb=resolved_settings.workspace_steady_memory_gb,
            peak_cpu=resolved_settings.workspace_peak_cpu,
            peak_memory_gb=resolved_settings.workspace_peak_memory_gb,
            disk_mb=source_reservation.disk_mb,
            dind_slots=source_reservation.dind_slots,
            dind_mode="dind" if source_reservation.dind_slots else "none",
            phase=source_reservation.phase,
        )
        await ResourceReservationRepository(session).create(
            workspace_id=retried.id,
            attempt_id=attempt.id,
            node_id=retry_reservation.node_id,
            steady_cpu=retry_reservation.steady_cpu,
            steady_memory_gb=retry_reservation.steady_memory_gb,
            peak_cpu=retry_reservation.peak_cpu,
            peak_memory_gb=retry_reservation.peak_memory_gb,
            disk_mb=retry_reservation.disk_mb,
            dind_slots=retry_reservation.dind_slots,
            phase=retry_reservation.phase,
        )
        retry_resource_summary = retry_reservation.summary(settings=resolved_settings)
    retry_score = scheduler_score_from_workspace(retried, now=retried.created_at)
    await QueueDecisionRepository(session).create(
        workspace_id=retried.id,
        task_id=task.id,
        attempt_id=attempt.id,
        decision=QUEUE_DECISION_ADMITTED,
        reason_code=QUEUE_DECISION_ADMITTED_LOCAL_REASON,
        class_priority=retry_score.class_priority,
        computed_priority=retry_score.effective_score,
        age_boost=retry_score.age_boost,
        retry_bonus=retry_score.retry_bonus,
        resource_summary=retry_resource_summary,
        overlap_risk_summary=overlap_risk_summary(overlaps),
        score_summary=retry_score.score_summary,
    )

    operation_repo = OperationRepository(session)
    operation_payload: dict[str, Any] = {"source_workspace_id": source.id}
    if planning_scope_context is not None:
        operation_payload.update(_planning_scope_recovery_payload(planning_scope_context))
    if conformance_salvage is not None:
        operation_payload.update(
            _conformance_salvage_recovery_payload(
                conformance_context=conformance_context,
                salvage=conformance_salvage,
            )
        )
    operation = await operation_repo.create(
        workspace_id=retried.id,
        operation_type=OperationType.retry,
        status=OperationStatus.running,
        payload=operation_payload,
    )
    event_payload = {
        "source_workspace_id": source.id,
        "new_workspace_id": retried.id,
        "attempt_number": attempt.attempt_number,
        "provider_readiness_preflight": preflight,
    }
    if planning_scope_context is not None:
        event_payload.update(_planning_scope_recovery_payload(planning_scope_context))
    if conformance_salvage is not None:
        event_payload.update(
            _conformance_salvage_recovery_payload(
                conformance_context=conformance_context,
                salvage=conformance_salvage,
            )
        )
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
    await _record_provider_readiness_preflight(repo, retried, preflight)
    await _record_owned_path_overlap_risk(repo, retried, overlaps)
    await operation_repo.finish(
        operation,
        status=OperationStatus.succeeded,
        result={
            "new_workspace_id": retried.id,
            "attempt_number": attempt.attempt_number,
            "status": retried.status,
        }
        | (
            _conformance_salvage_recovery_payload(
                conformance_context=conformance_context,
                salvage=conformance_salvage,
            )
            if conformance_salvage is not None
            else {}
        )
        | (
            _planning_scope_recovery_payload(planning_scope_context)
            if planning_scope_context is not None
            else {}
        ),
    )
    await session.flush()
    return WorkspaceRetryResult(
        source_workspace_id=source.id,
        new_workspace=retried,
        operation=operation,
        attempt_number=attempt.attempt_number,
    )


def _retry_task_policy(
    source: Workspace,
    coordination_warnings: Sequence[Mapping[str, Any]],
    *,
    planning_scope_context: _PlanningScopeRetryContext | None,
) -> dict[str, Any]:
    policy = task_policy_with_coordination_warnings(
        scheduler_retry_policy_context(
            deepcopy(source.task_policy),
            source_workspace_id=source.id,
            parent_failure_reason=source.failure_reason,
        ),
        coordination_warnings,
    )
    if planning_scope_context is not None and planning_scope_context.fallback_model is not None:
        policy["agent_model"] = planning_scope_context.fallback_model["model"]
    return policy


def _planning_scope_recovery_payload(
    context: _PlanningScopeRetryContext,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source_reason_code": context.reason_code,
        "planning_scope_evidence_ref": context.evidence_ref,
        "recovery_strategy": context.recovery_strategy,
        "salvage_policy": context.salvage_policy,
    }
    if context.salvage is not None:
        payload["salvage"] = context.salvage
    if context.fallback_model is not None:
        payload["fallback_model"] = context.fallback_model
    return payload


def _conformance_salvage_recovery_payload(
    *,
    conformance_context: _ConformanceRetryContext | None,
    salvage: Mapping[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source_reason_code": PLAN_CONFORMANCE_UNSATISFIED,
        "conformance_salvage": dict(salvage),
    }
    remaining_gaps = _compact_string_list(salvage.get("remaining_gaps"))
    if remaining_gaps:
        payload["remaining_gaps"] = remaining_gaps
    if conformance_context is not None:
        payload["conformance_evidence_ref"] = conformance_context.evidence_ref
    elif salvage.get("conformance_evidence_ref") is not None:
        payload["conformance_evidence_ref"] = salvage.get("conformance_evidence_ref")
    return payload


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


def _selected_provider_preflight_for_task(
    settings: Settings,
    *,
    agent: AgentRuntime | str,
    task_policy: Mapping[str, object] | None,
    override: bool,
    override_reason: str | None,
    provider_environ: Mapping[str, str] | None,
    run_subprocess: SubprocessRun | None,
    http_get: HttpGet | None,
) -> dict[str, Any]:
    return selected_provider_readiness_preflight(
        resolve_service_settings(settings),
        agent=agent,
        task_policy=task_policy,
        override=override,
        override_reason=override_reason,
        environ=provider_environ,
        run_subprocess=run_subprocess,
        http_get=http_get,
    )


async def _selected_provider_preflight_for_task_async(
    settings: Settings,
    *,
    agent: AgentRuntime | str,
    task_policy: Mapping[str, object] | None,
    override: bool,
    override_reason: str | None,
    provider_environ: Mapping[str, str] | None,
    run_subprocess: SubprocessRun | None,
    http_get: HttpGet | None,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _selected_provider_preflight_for_task,
        settings,
        agent=agent,
        task_policy=task_policy,
        override=override,
        override_reason=override_reason,
        provider_environ=provider_environ,
        run_subprocess=run_subprocess,
        http_get=http_get,
    )


def _raise_if_provider_preflight_blocks(preflight: Mapping[str, Any]) -> None:
    if preflight.get("blocks_launch") is True:
        raise WorkspaceProviderReadinessBlockedError(preflight)


async def _record_provider_readiness_preflight(
    repo: WorkspaceRepository,
    workspace: Workspace,
    preflight: Mapping[str, Any],
) -> None:
    await repo.add_event(
        workspace,
        event_type=PROVIDER_READINESS_PREFLIGHT_EVENT_TYPE,
        reason_code=(
            PROVIDER_READINESS_OVERRIDE_REASON
            if preflight.get("override_used") is True
            else PROVIDER_READINESS_READY_REASON
        ),
        payload={"provider_readiness_preflight": dict(preflight)},
    )


def workspace_provider_readiness_preflight(
    workspace: Workspace,
) -> dict[str, Any] | None:
    return provider_readiness_preflight_from_task_policy(workspace.task_policy)


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
        provider_readiness_preflight=workspace_provider_readiness_preflight(result.new_workspace),
    )


def _provider_recovery_state_response(
    workspace: Workspace,
) -> ProviderRecoveryStateResponse | None:
    view = provider_recovery_state_for_workspace(workspace)
    if view is None:
        return None
    fallback_target_response: FallbackTargetResponse | None = None
    if view.fallback_target is not None:
        fallback_target_response = FallbackTargetResponse(
            agent=view.fallback_target.agent,
            provider=view.fallback_target.provider,
            model=view.fallback_target.model,
        )
    return ProviderRecoveryStateResponse(
        action=view.action,
        reason_code=view.reason_code,
        source_provider=view.source_provider,
        source_model=view.source_model,
        retry_attempt_number=view.retry_attempt_number,
        fallback_attempt_number=view.fallback_attempt_number,
        cooldown_until=view.cooldown_until,
        next_eligible_at=view.next_eligible_at,
        fallback_target=fallback_target_response,
        source_workspace_id=view.source_workspace_id,
        source_attempt_id=view.source_attempt_id,
        recommended_action=view.recommended_action,
        terminal=view.terminal,
    )


def workspace_response(
    workspace: Workspace,
    *,
    validation_provenance: ValidationFreshnessSummaryResponse | None = None,
    egress_audit: dict[str, Any] | None = None,
) -> WorkspaceResponse:
    computed_fields = dict(workspace_observability_payload(workspace))
    computed_fields["is_stale_running"] = is_workspace_stale_running(workspace)
    computed_fields["validation_provenance"] = (
        validation_provenance
        if validation_provenance is not None
        else validation_provenance_unavailable(workspace)
    )
    computed_fields["runtime_health"] = _workspace_runtime_health_from_events(workspace)
    computed_fields["failure_details"] = workspace_failure_details_payload(workspace)
    computed_fields["coordination_warnings"] = coordination_warnings_from_task_policy(
        workspace.task_policy
    )
    computed_fields["secret_leases"] = [
        workspace_secret_lease_response(lease) for lease in _loaded_secret_leases(workspace)
    ]
    computed_fields["app_endpoints"] = _workspace_app_endpoint_responses(workspace)
    computed_fields["requested_profile"] = _console_safe_profile_snapshot(
        getattr(workspace, "requested_profile", None)
    )
    computed_fields["resolved_profile"] = _console_safe_profile_snapshot(
        getattr(workspace, "resolved_profile", None)
    )
    computed_fields["network_posture"] = network_posture_from_profile_snapshot(
        getattr(workspace, "resolved_profile", None)
    )
    computed_fields["provider_recovery_state"] = _provider_recovery_state_response(workspace)
    computed_fields["provider_readiness_preflight"] = workspace_provider_readiness_preflight(
        workspace
    )
    computed_fields["pricing"] = _pricing_metadata_response(workspace)
    if egress_audit is not None:
        computed_fields["egress_audit"] = egress_audit
    return WorkspaceResponse.model_validate(_WorkspaceResponseSource(workspace, computed_fields))


def _workspace_app_endpoint_responses(
    workspace: Workspace,
) -> list[WorkspaceAppEndpointResponse]:
    raw_profile = getattr(workspace, "resolved_profile", None)
    if not isinstance(raw_profile, Mapping):
        return []
    try:
        app_endpoints = _PROFILE_APP_ENDPOINTS_ADAPTER.validate_python(
            raw_profile.get("app_endpoints", [])
        )
    except ValidationError:
        return []
    return [
        WorkspaceAppEndpointResponse.model_validate(endpoint)
        for endpoint in resolve_profile_app_endpoints(
            app_endpoints,
            include_internal=False,
        )
    ]


def _console_safe_profile_snapshot(raw_profile: object) -> dict[str, Any] | None:
    if not isinstance(raw_profile, Mapping):
        return None
    sanitized = _sanitize_profile_value(raw_profile, path=())
    return cast(dict[str, Any], sanitized)


def _egress_audit_response(record: EgressAuditRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "workspace_id": record.workspace_id,
        "attempt_id": record.attempt_id,
        "policy_posture": record.policy_posture,
        "decision": record.decision,
        "destination_category": record.destination_category,
        "reason_code": record.reason_code,
        "details": record.details,
        "enforced_at": record.enforced_at,
        "created_at": record.created_at,
    }


def _pricing_metadata_response(workspace: Workspace) -> dict[str, Any] | None:
    pricing = workspace_pricing_metadata(workspace)
    if pricing is None:
        return None
    return {
        "provider": pricing.provider,
        "model": pricing.model,
        "currency": pricing.currency,
        "unit": pricing.unit,
        "price_per_unit": pricing.price_per_unit,
        "timestamp": pricing.timestamp,
        "version": pricing.version,
        "is_current": pricing.is_current(),
    }


def _sanitize_profile_value(value: object, *, path: tuple[str, ...]) -> object:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for raw_key, raw_child in value.items():
            key = str(raw_key)
            if _omit_profile_response_field(path=path, key=key):
                continue
            sanitized[key] = _sanitize_profile_value(raw_child, path=(*path, key))
        return sanitized
    if isinstance(value, list):
        return [_sanitize_profile_value(item, path=(*path, "*")) for item in value]
    if isinstance(value, str):
        return _sanitize_profile_string(value)
    return value


def _omit_profile_response_field(*, path: tuple[str, ...], key: str) -> bool:
    if key == "environment":
        return True
    return key == "ref" and "secrets" in path


def _sanitize_profile_string(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or not parsed.netloc:
        return value
    sanitized_path = redact_secrets(parsed.path)
    if not (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or sanitized_path != parsed.path
    ):
        return value
    return urlunsplit(
        (
            parsed.scheme,
            _sanitized_url_netloc(parsed),
            sanitized_path,
            "",
            "",
        )
    )


def _sanitized_url_netloc(parsed: SplitResult) -> str:
    hostname = parsed.hostname
    if not hostname:
        return "<redacted>"
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        return host
    return f"{host}:{port}"


def _loaded_secret_leases(workspace: Workspace) -> list[WorkspaceSecretLease]:
    try:
        state = sa_inspect(workspace)
    except NoInspectionAvailable:
        return list(getattr(workspace, "secret_leases", []))
    if "secret_leases" in state.unloaded:
        return []
    return list(getattr(workspace, "secret_leases", []))


def workspace_failure_details_payload(workspace: Workspace) -> dict[str, Any] | None:
    event = _latest_failed_state_event(workspace)
    if event is None:
        return None
    payload = event.payload if isinstance(event.payload, dict) else {}
    reason_code = _payload_str(payload, "reason_code") or event.reason_code
    message = _payload_str(payload, "message") or workspace.failure_message
    details = payload.get("details")
    if not isinstance(details, Mapping):
        details = {}
    conformance = details.get("conformance")
    if not isinstance(conformance, Mapping):
        conformance = payload.get("conformance")
    salvage = payload.get("salvage")
    planning_scope = details.get("planning_scope")
    if not isinstance(planning_scope, Mapping):
        legacy_scope = details.get("scope")
        if isinstance(legacy_scope, Mapping):
            planning_scope = legacy_scope
        elif reason_code == AGENT_PLAN_PHASE_SCOPE_VIOLATION:
            planning_scope = {
                key: details[key]
                for key in (
                    "scope_phase",
                    "required_paths",
                    "offending_paths",
                    "offending_commands",
                    "recommended_action",
                    "recovery_strategy",
                    "salvage_policy",
                    "fallback_model",
                    "plan_artifact",
                )
                if key in details
            }

    result: dict[str, Any] = {
        "reason_code": reason_code,
        "message": message,
    }

    for field in (
        "provider",
        "model",
        "retryable",
        "recommended_action",
        "recovery_strategy",
        "salvage_policy",
        "fallback_model",
    ):
        if field in details:
            result[field] = details[field]

    provider_recovery = provider_recovery_metadata_from_failure(
        reason_code=reason_code,
        message=message,
        details=details,
        task_policy=getattr(workspace, "task_policy", None),
    )
    if provider_recovery is not None:
        result["provider_recovery"] = provider_recovery
        for field in (
            "provider",
            "model",
            "retryable",
            "recommended_action",
            "failure_type",
            "retry_after_seconds",
            "cooldown_seconds",
            "failure_fingerprint",
            "fallback_allowed",
        ):
            if field in provider_recovery:
                result[field] = provider_recovery[field]

    recovery_state_resp = _provider_recovery_state_response(workspace)
    if recovery_state_resp is not None:
        result["provider_recovery_state"] = recovery_state_resp

    conformance_payload = _compact_conformance_payload(conformance)
    if conformance_payload is not None:
        result["conformance"] = conformance_payload
    planning_scope_payload = _compact_planning_scope_payload(planning_scope)
    if planning_scope_payload is not None:
        result["planning_scope"] = planning_scope_payload
        for field in (
            "recommended_action",
            "recovery_strategy",
            "salvage_policy",
            "fallback_model",
        ):
            if field in planning_scope_payload and field not in result:
                result[field] = planning_scope_payload[field]
    salvage_payload = _compact_salvage_payload(salvage)
    if salvage_payload is not None:
        result["salvage"] = salvage_payload
    if result["reason_code"] is None and result["message"] is None and len(result) == 2:
        return None
    return result


def _latest_failed_state_event(workspace: Workspace) -> Any | None:
    for event in reversed(getattr(workspace, "events", []) or []):
        if (
            getattr(event, "event_type", None) == "workspace.state_changed"
            and getattr(event, "new_state", None) == WorkspaceStatus.failed.value
        ):
            return event
    return None


def _compact_conformance_payload(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    payload: dict[str, Any] = {}
    for key in (
        "summary",
        "reason_code",
        "report_reason_code",
        "plan_path",
        "report_path",
    ):
        item = value.get(key)
        if isinstance(item, str):
            payload[key] = item
    gaps = value.get("gaps")
    if isinstance(gaps, list):
        payload["gaps"] = [gap for gap in gaps if isinstance(gap, str)]
    for key in ("iterations_used", "max_iterations"):
        item = value.get(key)
        if isinstance(item, int):
            payload[key] = item
    return payload or None


def _compact_planning_scope_payload(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    payload: dict[str, Any] = {}
    for key in (
        "scope_phase",
        "recommended_action",
        "recovery_strategy",
        "salvage_policy",
        "plan_artifact",
    ):
        item = value.get(key)
        if isinstance(item, str) and item:
            payload[key] = item
    for key in ("required_paths", "offending_paths", "offending_commands"):
        items = _compact_string_list(value.get(key))
        if items:
            payload[key] = items
    if "offending_paths" not in payload:
        forbidden = _compact_string_list(value.get("forbidden_paths"))
        if forbidden:
            payload["offending_paths"] = forbidden
    fallback_model = _compact_fallback_model(value.get("fallback_model"))
    if fallback_model is not None:
        payload["fallback_model"] = fallback_model
    return payload or None


def _compact_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _compact_fallback_model(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    model = value.get("model")
    source = value.get("source")
    if not isinstance(model, str) or not model.strip():
        return None
    payload = {"model": model.strip()}
    if isinstance(source, str) and source.strip():
        payload["source"] = source.strip()
    return payload


def _compact_salvage_payload(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    payload = {
        key: item
        for key in ("hint", "worktree_path", "branch_name", "remote_push_branch")
        if isinstance((item := value.get(key)), str) and item
    }
    return payload or None


def _payload_str(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _is_plan_conformance_unsatisfied(workspace: Workspace) -> bool:
    details = workspace_failure_details_payload(workspace)
    if details is None:
        return False
    return details.get("reason_code") == PLAN_CONFORMANCE_UNSATISFIED


def _conformance_retry_context(workspace: Workspace) -> _ConformanceRetryContext | None:
    details = workspace_failure_details_payload(workspace)
    if details is None or details.get("reason_code") != PLAN_CONFORMANCE_UNSATISFIED:
        return None
    evidence = details.get("conformance")
    if not isinstance(evidence, Mapping):
        return None
    return _ConformanceRetryContext(
        reason_code=PLAN_CONFORMANCE_UNSATISFIED,
        evidence=evidence,
        evidence_ref={
            "source_workspace_id": workspace.id,
            "event_type": "workspace.state_changed",
            "reason_code": PLAN_CONFORMANCE_UNSATISFIED,
        },
    )


def _retry_evidence_gaps(evidence: Mapping[str, Any]) -> list[str]:
    value = evidence.get("gaps")
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _optional_retry_evidence_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _planning_scope_retry_context(workspace: Workspace) -> _PlanningScopeRetryContext | None:
    details = workspace_failure_details_payload(workspace)
    if details is None or details.get("reason_code") != AGENT_PLAN_PHASE_SCOPE_VIOLATION:
        return None
    evidence = details.get("planning_scope")
    if not isinstance(evidence, Mapping):
        return None
    recovery_strategy_value = details.get("recovery_strategy")
    recovery_strategy = (
        recovery_strategy_value
        if isinstance(recovery_strategy_value, str)
        else "discard_and_replan"
    )
    salvage_policy_value = details.get("salvage_policy")
    salvage_policy = (
        salvage_policy_value
        if isinstance(salvage_policy_value, str)
        else "explicit_salvage_required"
    )
    fallback_model = _approved_planning_scope_fallback_model(workspace)
    evidence_payload = dict(evidence)
    if fallback_model is not None:
        evidence_payload["fallback_model"] = fallback_model
    return _PlanningScopeRetryContext(
        reason_code=AGENT_PLAN_PHASE_SCOPE_VIOLATION,
        evidence=evidence_payload,
        evidence_ref={
            "source_workspace_id": workspace.id,
            "event_type": "workspace.state_changed",
            "reason_code": AGENT_PLAN_PHASE_SCOPE_VIOLATION,
        },
        recovery_strategy=recovery_strategy,
        salvage_policy=salvage_policy,
        salvage=_compact_salvage_payload(details.get("salvage")),
        fallback_model=fallback_model,
    )


def _approved_planning_scope_fallback_model(
    workspace: Workspace,
) -> dict[str, str] | None:
    task_policy = getattr(workspace, "task_policy", None)
    if not isinstance(task_policy, Mapping):
        return None
    recovery_policy = task_policy.get("planning_scope_recovery")
    if not isinstance(recovery_policy, Mapping):
        return None
    model = recovery_policy.get("approved_fallback_model")
    if not isinstance(model, str) or not model.strip():
        return None
    return {
        "model": model.strip(),
        "source": "task_policy.planning_scope_recovery.approved_fallback_model",
    }


def _workspace_runtime_health_from_events(
    workspace: Workspace,
) -> WorkspaceRuntimeHealthResponse | None:
    for event in reversed(workspace.events):
        if event.event_type != RUNTIME_STRANDED_EVENT_TYPE:
            continue
        payload = event.payload or {}
        reason_code = payload.get("reason_code") or event.reason_code
        decision = payload.get("decision")
        message = payload.get("message")
        if not isinstance(reason_code, str) or not isinstance(decision, str):
            return None
        if not isinstance(message, str):
            message = reason_code
        return WorkspaceRuntimeHealthResponse(
            status="unavailable" if reason_code == "RUNTIME_INSPECTION_UNAVAILABLE" else "stranded",
            reason_code=reason_code,
            decision=cast(Any, decision),
            message=message,
            services=_runtime_health_event_services(payload),
        )
    return None


def _runtime_health_event_services(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    runtime = payload.get("runtime")
    if not isinstance(runtime, Mapping):
        return []
    services = runtime.get("services")
    if not isinstance(services, list):
        return []
    return [
        {key: str(value) for key, value in service.items() if value is not None}
        for service in services
        if isinstance(service, Mapping)
    ]


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
    return scheduler_task_class_priority(task_class)


def computed_priority(
    *,
    base_priority: int,
    task_class: str | None,
    age_boost: int,
    retry_bonus: int,
) -> int:
    return base_priority + task_class_bias(task_class) + age_boost + retry_bonus


def resource_reservation_plan(
    payload: WorkspaceCreateV2Request,
    *,
    settings: Settings,
) -> ResourceReservationPlan:
    resources = payload.resources
    legacy_memory_gb = _parse_memory_gb(resources.memory)
    _, resolved_profile = v2_profile_snapshots(payload)
    dind_mode = _dind_mode_from_profile_snapshot(resolved_profile)
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
        dind_slots=1 if dind_mode == "dind" else 0,
        dind_mode=dind_mode,
    )


async def _resource_reservation_summary(
    session: AsyncSession,
    reservation_plan: ResourceReservationPlan,
    *,
    settings: Settings,
    disk_check: DiskCheck | None,
) -> dict[str, Any]:
    active_totals = await ResourceReservationRepository(session).active_latest_totals()
    reserved_resources = ReservedResources(
        active_workspace_count=int(active_totals["workspace_count"]) + 1,
        steady_cpu=float(active_totals["steady_cpu"]) + reservation_plan.steady_cpu,
        steady_memory_gb=(
            float(active_totals["steady_memory_gb"]) + reservation_plan.steady_memory_gb
        ),
        peak_cpu=float(active_totals["peak_cpu"]) + reservation_plan.peak_cpu,
        peak_memory_gb=(float(active_totals["peak_memory_gb"]) + reservation_plan.peak_memory_gb),
        disk_mb=int(active_totals["disk_mb"]) + (reservation_plan.disk_mb or 0),
        dind_slots=int(active_totals["dind_slots"]) + reservation_plan.dind_slots,
    )
    return reservation_plan.summary(
        settings=settings,
        disk_check=disk_check,
        reserved_resources=reserved_resources,
    )


def _dind_mode_from_profile_snapshot(profile: Mapping[str, Any] | None) -> str:
    if profile is None:
        return "unknown"
    docker = profile.get("docker")
    if isinstance(docker, Mapping) and docker.get("mode") == "dind":
        return "dind"
    return "none"


def _resource_reservation_row_summary(reservation: Any) -> dict[str, Any]:
    return {
        "node_id": reservation.node_id,
        "steady_cpu": reservation.steady_cpu,
        "steady_memory_gb": reservation.steady_memory_gb,
        "peak_cpu": reservation.peak_cpu,
        "peak_memory_gb": reservation.peak_memory_gb,
        "disk_mb": reservation.disk_mb,
        "dind_slots": reservation.dind_slots,
        "phase": reservation.phase,
    }


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
    if payload.task.priority != 0 or payload.task.human_boost != 0:
        policy["scheduler"] = scheduler_policy_snapshot(
            base_priority=payload.task.priority,
            human_boost=payload.task.human_boost,
        )
    if payload.task.model is not None:
        policy["agent_model"] = payload.task.model
    if payload.task.out_of_scope_changes is not None:
        policy["out_of_scope_changes"] = payload.task.out_of_scope_changes.model_dump(mode="json")
    if payload.task.provider_recovery is not None:
        policy["provider_recovery"] = payload.task.provider_recovery.model_dump(
            mode="json",
            exclude_none=True,
            exclude_unset=True,
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
            "validation": profile.validation.model_copy(update={"requested_tier": requested_tier})
        }
    )
