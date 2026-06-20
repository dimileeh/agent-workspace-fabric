"""Read-only operational metrics endpoints."""

from __future__ import annotations

import asyncio
import inspect
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session, require_api_token
from awf.api.responses import API_TOKEN_AUTH_ERROR_RESPONSES
from awf.common.config import Settings, get_settings
from awf.common.logging import get_logger
from awf.service.disk import DiskCheck, check_disk_space
from awf.service.local_capacity import detect_local_capacity
from awf.service.metrics import (
    DEFAULT_FAILURE_EXAMPLE_LIMIT,
    DEFAULT_SUMMARY_WINDOW_HOURS,
    MAX_FAILURE_EXAMPLE_LIMIT,
    MAX_SUMMARY_WINDOW_HOURS,
    MIN_FAILURE_EXAMPLE_LIMIT,
    MIN_SUMMARY_WINDOW_HOURS,
    summarize_failure_analysis_for_session,
    summarize_resource_saturation_for_session,
    summarize_slo_metrics_for_session,
    summarize_workspace_reliability_for_session,
)
from awf.service.orphan_resources import (
    OrphanResourceSummary,
    build_orphan_resource_summary,
    scan_docker_resources,
    scan_managed_worktrees,
    unavailable_workspace_view,
    workspace_id_view_from_session,
)
from awf.service.resource_capacity import LocalCapacityLimits
from awf.service.workspace_runtime_health import (
    WorkspaceRuntimeHealthSummary,
    runtime_resource_from_detected,
    runtime_workspaces_from_session,
    summarize_runtime_health,
)

router = APIRouter(
    prefix="/v1/metrics",
    tags=["metrics"],
    dependencies=[Depends(require_api_token)],
    responses=API_TOKEN_AUTH_ERROR_RESPONSES,
)
_log = get_logger(__name__)
DiskCheckProvider = Callable[[Settings], DiskCheck]
OrphanResourceSummaryProvider = Callable[
    [Settings, AsyncSession],
    OrphanResourceSummary | Awaitable[OrphanResourceSummary],
]
RuntimeHealthSummaryProvider = Callable[
    [Settings, AsyncSession, OrphanResourceSummary],
    WorkspaceRuntimeHealthSummary | Awaitable[WorkspaceRuntimeHealthSummary],
]
LocalCapacityProvider = Callable[[Settings], LocalCapacityLimits | Awaitable[LocalCapacityLimits]]


class WorkspaceReliabilitySummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    generated_at: datetime
    window_start: datetime
    since_hours: int
    status_counts: dict[str, int]
    failure_reason_counts: dict[str, int]
    active_count: int = Field(
        description=(
            "Current count of workspaces outside terminal statuses, including "
            "workspaces in destroying until cleanup finishes."
        ),
    )
    destroying_count: int = Field(
        description="Current count of workspaces in destroying status.",
    )
    completed_count: int
    failed_count: int
    cancelled_count: int
    destroyed_count: int
    cleanup_failure_count: int
    stuck_count: int = Field(
        description="Active workspaces beyond 2x configured SLA without a fresh explanatory reason code.",
    )
    actionable_reason_count: int = Field(
        description="Count of failed/cancelled/destroying workspaces in the window with an actionable reason code.",
    )
    unactionable_reason_count: int = Field(
        description="Count of failed/cancelled/destroying workspaces in the window without an actionable reason code.",
    )


class FailureReasonGroupResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    failure_reason: str
    count: int
    retryable: bool
    recommended_action: str


class FailedWorkspaceExampleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    workspace_id: str
    title: str
    repo_url: str
    branch_base: str
    agent: str
    status: str
    failure_reason: str
    failure_message: str | None
    pr_url: str | None
    created_at: datetime
    updated_at: datetime
    reason_code: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    salvage: dict[str, Any] | None = None
    provider_recovery: dict[str, Any] | None = None


class RootCauseClusterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    agent: str
    agent_model: str | None
    failure_reason: str
    reason_code: str | None = None
    likely_cause: str
    actionable_next_action: str
    count: int
    sample_workspace_ids: list[str]
    details: dict[str, Any] = Field(default_factory=dict)
    salvage: dict[str, Any] | None = None
    provider_recovery: dict[str, Any] | None = None


class FailureAnalysisSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    generated_at: datetime
    window_start: datetime
    since_hours: int
    total_failed_workspaces: int
    failure_groups: list[FailureReasonGroupResponse] = Field(
        description=(
            "Failed workspace counts grouped by normalized failure_reason, with deterministic "
            "retry guidance for each failure class."
        ),
    )
    latest_examples: list[FailedWorkspaceExampleResponse] = Field(
        description="Most recently updated failed workspaces in the requested window.",
    )
    root_cause_clusters: list[RootCauseClusterResponse] = Field(
        default_factory=list,
        description="Identified root cause clusters from failure signals.",
    )


class SloMetricsSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    generated_at: datetime
    window_start: datetime
    since_hours: int

    creation_total: int = Field(description="Workspaces created in the window.")
    creation_succeeded: int = Field(
        description="Workspaces created in the window that reached completed."
    )
    creation_failed: int = Field(
        description="Workspaces created in the window that reached failed."
    )
    creation_cancelled: int = Field(
        description="Workspaces created in the window that reached cancelled."
    )

    cleanup_total: int = Field(description="Destroy operations finished in the window.")
    cleanup_succeeded: int = Field(
        description="Destroy operations finished in the window with succeeded status."
    )
    cleanup_failure_count: int = Field(
        description="Destroy operations finished in the window with failed status."
    )

    stuck_running_count: int = Field(
        description="Non-terminal workspaces beyond 2x SLA without a failure reason code.",
    )
    stuck_with_reason_count: int = Field(
        description="Non-terminal workspaces beyond 2x SLA with a failure reason code.",
    )

    recovery_total: int = Field(
        description="Recovery operations (remonitor/rebase/retry) created in the window."
    )
    recovery_succeeded: int = Field(description="Recovery operations that succeeded.")
    recovery_failed_count: int = Field(description="Recovery operations that failed.")

    monitor_completed_total: int = Field(
        description="Workspaces with a PR URL updated in the window (includes all statuses, not only completed).",
    )
    completed_after_monitor_count: int = Field(
        description="Workspaces in completed status updated in the window that previously entered monitoring (pr_url set).",
    )
    monitor_stuck_count: int = Field(
        description="Workspaces currently in monitoring_pr status beyond 2x SLA.",
    )

    actionable_failure_count: int = Field(
        description="Failed/cancelled workspaces updated in the window with a known FailureReason code.",
    )
    unactionable_failure_count: int = Field(
        description="Failed/cancelled workspaces updated in the window without a known FailureReason code.",
    )


class WorkspaceSaturationCountsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    by_status: dict[str, int]
    active_total: int
    requested: int
    provisioning: int
    ready: int
    running: int
    validating: int
    pushing: int
    monitoring_pr: int
    blocked: int
    recovering: int
    destroying: int
    completed: int
    failed: int
    cancelled: int
    destroyed: int


class WorkerConcurrencySettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    max_concurrent_provisions: int
    max_concurrent_executions: int


class LocalCapacitySourceResponse(BaseModel):
    """Local runtime capacity origin metadata exposed in saturation responses."""

    model_config = ConfigDict(from_attributes=True)

    cpu_cores: float | None
    memory_gb: float | None
    source: str | None
    reason_code: str | None = None
    detail: str | None = None


class WorkspaceResourceDefaultsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    steady_cpu: float
    steady_memory_gb: float
    peak_cpu: float
    peak_memory_gb: float


class ReservedResourcesResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    active_workspace_count: int
    steady_cpu: float
    steady_memory_gb: float
    peak_cpu: float
    peak_memory_gb: float
    disk_mb: int
    dind_slots: int


class QueuePlannedResourcesResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    steady_cpu: float
    steady_memory_gb: float
    peak_cpu: float
    peak_memory_gb: float
    disk_mb: int
    dind_slots: int


class CapacityDimensionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    limit: int | float | None
    reserved: int | float
    available: int | float | None
    available_after_next_default: int | float | None
    reason_code: str | None = None


class ResourceCapacitySummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    steady_cpu: CapacityDimensionResponse
    peak_cpu: CapacityDimensionResponse
    steady_memory_gb: CapacityDimensionResponse
    peak_memory_gb: CapacityDimensionResponse
    disk_mb: CapacityDimensionResponse
    dind_slots: CapacityDimensionResponse
    pressure_reasons: list[str]


class ConcurrencyLaneResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    limit: int
    in_use: int
    queued: int
    available: int


class ResourceConcurrencyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provision: ConcurrencyLaneResponse
    execution: ConcurrencyLaneResponse


class DiskCheckResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    path: str
    checked_path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent_free: float
    threshold_bytes: int
    ok: bool
    status: str
    reason: str
    detail: str | None = None


class AdmissionSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ok: bool
    status: str
    reason: str
    detail: str | None = None


class CapacityQueueSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    queued_workspace_count: int
    oldest_workspace_id: str | None = None
    oldest_wait_seconds: int | None = None
    planned_resources: QueuePlannedResourcesResponse
    blocked_reason_counts: dict[str, int] = Field(
        description=(
            "Capacity blocker reason counts for queued work. Deferred blockers count the first "
            "FIFO frontier per constraint, not every blocked workspace behind it; unsatisfiable "
            "requests count each workspace because they do not depend on allocated capacity."
        ),
    )


class ProviderCircuitBreakerSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    model: str
    state: str
    failure_count: int
    cooldown_until: datetime | None = None
    last_reason_code: str | None = None
    last_workspace_id: str | None = None


class ProviderRecoveryStateSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    pending_retry: int
    pending_fallback: int
    in_cooldown: int
    terminal_no_loop: int
    terminal_exhausted: int
    circuit_breakers_open: int


class CleanupReadinessResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ready: bool
    status: str
    reason: str
    action: str
    dry_run_only: bool


class OrphanResourceSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ok: bool
    status: str
    reason: str
    detail: str | None = None
    resource_count: int
    expected_count: int
    orphan_count: int
    unknown_count: int
    counts_by_kind: dict[str, int]
    orphan_counts_by_kind: dict[str, int]
    expected_counts_by_kind: dict[str, int]
    unknown_counts_by_kind: dict[str, int]
    orphan_classification_counts: dict[str, int]
    cleanup_readiness: CleanupReadinessResponse
    scanners: dict[str, dict[str, Any]]
    examples: list[dict[str, Any]]


class RuntimeHealthSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    scanner_available: bool
    scanner_reason: str | None = None
    scanner_detail: str | None = None
    stranded_count: int
    fail_candidate_count: int
    recoverable_count: int
    reason_counts: dict[str, int]


class ResourceSaturationSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    generated_at: datetime
    workspace_counts: WorkspaceSaturationCountsResponse = Field(
        description="Current workspace counts by status plus active non-terminal totals.",
    )
    worker: WorkerConcurrencySettingsResponse = Field(
        description="Configured local worker concurrency limits.",
    )
    local_capacity: LocalCapacitySourceResponse = Field(
        description=(
            "Local runtime capacity source used for reservation pressure reporting. "
            "For Docker-backed local AWF, this describes the Docker daemon capacity, "
            "not necessarily the host machine's physical capacity."
        ),
    )
    resource_defaults: WorkspaceResourceDefaultsResponse = Field(
        description="Configured per-workspace steady and peak resource defaults.",
    )
    reserved_resources: ReservedResourcesResponse = Field(
        description="Resource reservations implied by active workspace count and defaults.",
    )
    capacity: ResourceCapacitySummaryResponse = Field(
        description="Available local capacity and pressure reasons by reserved resource.",
    )
    allocated_resources: ReservedResourcesResponse = Field(
        description="Resource reservations for workspaces that already occupy local runtime capacity.",
    )
    allocated_capacity: ResourceCapacitySummaryResponse = Field(
        description="Available local capacity after allocated runtime reservations only.",
    )
    capacity_queue: CapacityQueueSummaryResponse = Field(
        description=(
            "Requested workspace demand and local capacity blockers for queued work. "
            "blocked_reason_counts counts the first FIFO frontier per constraint, "
            "not every blocked workspace behind it."
        ),
    )
    concurrency: ResourceConcurrencyResponse = Field(
        description="Provisioning and execution worker lane saturation.",
    )
    disk: DiskCheckResponse = Field(
        description="Disk pressure check for the AWF work directory.",
    )
    orphan_resources: OrphanResourceSummaryResponse = Field(
        description="Read-only orphan AWF Docker resource and worktree cleanup readiness.",
    )
    runtime_health: RuntimeHealthSummaryResponse = Field(
        description="Read-only active workspace runtime health and recovery-decision counts.",
    )
    admission: AdmissionSummaryResponse = Field(
        description="Actionable summary explaining whether new workspace admission is blocked.",
    )
    provider_circuit_breakers: list[ProviderCircuitBreakerSummaryResponse] = Field(
        default_factory=list,
        description="Open provider/model cooldown circuits suppressing new dispatch.",
    )
    provider_recovery_state_summary: ProviderRecoveryStateSummaryResponse | None = Field(
        default=None,
        description="Provider recovery state counts by phase.",
    )
    egress_posture_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Counts of egress audit records by policy posture.",
    )


@router.get(
    "/failures/summary",
    response_model=FailureAnalysisSummaryResponse,
)
async def get_failure_analysis_summary(
    since_hours: Annotated[
        int,
        Query(
            ge=MIN_SUMMARY_WINDOW_HOURS,
            le=MAX_SUMMARY_WINDOW_HOURS,
        ),
    ] = DEFAULT_SUMMARY_WINDOW_HOURS,
    limit: Annotated[
        int,
        Query(
            ge=MIN_FAILURE_EXAMPLE_LIMIT,
            le=MAX_FAILURE_EXAMPLE_LIMIT,
            description="Maximum number of latest failed workspace examples to include.",
        ),
    ] = DEFAULT_FAILURE_EXAMPLE_LIMIT,
    session: AsyncSession = Depends(get_db_session),
) -> FailureAnalysisSummaryResponse:
    summary = await summarize_failure_analysis_for_session(
        session,
        since_hours=since_hours,
        failure_example_limit=limit,
    )
    return FailureAnalysisSummaryResponse.model_validate(summary)


@router.get(
    "/workspaces/summary",
    response_model=WorkspaceReliabilitySummaryResponse,
)
async def get_workspace_reliability_summary(
    since_hours: Annotated[
        int,
        Query(
            ge=MIN_SUMMARY_WINDOW_HOURS,
            le=MAX_SUMMARY_WINDOW_HOURS,
        ),
    ] = DEFAULT_SUMMARY_WINDOW_HOURS,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceReliabilitySummaryResponse:
    summary = await summarize_workspace_reliability_for_session(
        session,
        settings=settings,
        since_hours=since_hours,
    )
    return WorkspaceReliabilitySummaryResponse.model_validate(summary)


@router.get(
    "/resources/saturation",
    response_model=ResourceSaturationSummaryResponse,
)
async def get_resource_saturation_summary(
    request: Request,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> ResourceSaturationSummaryResponse:
    disk_check = await _resource_saturation_disk_check(request, settings)
    local_capacity = await _resource_saturation_local_capacity(request, settings)
    orphan_resources = await _resource_saturation_orphan_resources(
        request,
        settings,
        session,
    )
    runtime_health = await _resource_saturation_runtime_health(
        request,
        settings,
        session,
        orphan_resources,
    )
    summary = await summarize_resource_saturation_for_session(
        session,
        settings=settings,
        disk_check=disk_check,
        detected_local_capacity=local_capacity,
        orphan_resources=orphan_resources,
        runtime_health=runtime_health,
    )
    response = ResourceSaturationSummaryResponse.model_validate(summary)
    try:
        response.egress_posture_counts = await _egress_posture_counts(session)
    except Exception:
        _log.warning(
            "Failed to fetch egress posture counts",
            exc_info=True,
        )
    return response


@router.get(
    "/slo",
    response_model=SloMetricsSummaryResponse,
)
async def get_slo_metrics_summary(
    since_hours: Annotated[
        int,
        Query(
            ge=MIN_SUMMARY_WINDOW_HOURS,
            le=MAX_SUMMARY_WINDOW_HOURS,
        ),
    ] = DEFAULT_SUMMARY_WINDOW_HOURS,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> SloMetricsSummaryResponse:
    summary = await summarize_slo_metrics_for_session(
        session,
        settings=settings,
        since_hours=since_hours,
    )
    return SloMetricsSummaryResponse.model_validate(summary)


async def _resource_saturation_disk_check(request: Request, settings: Settings) -> DiskCheck:
    provider = cast(
        DiskCheckProvider | None,
        getattr(request.app.state, "workspace_admission_disk_check", None),
    )
    if provider is not None:
        return await asyncio.to_thread(provider, settings)
    return await asyncio.to_thread(
        check_disk_space,
        settings.work_dir,
        min_free_bytes=settings.min_free_disk_bytes,
    )


async def _resource_saturation_local_capacity(
    request: Request,
    settings: Settings,
) -> LocalCapacityLimits:
    provider = cast(
        LocalCapacityProvider | None,
        getattr(request.app.state, "local_capacity_detector", None),
    )
    if provider is not None:
        result = provider(settings)
        if inspect.isawaitable(result):
            return await result
        return result
    if (
        settings.local_capacity_cpu_cores is not None
        and settings.local_capacity_memory_gb is not None
    ):
        return LocalCapacityLimits()
    return await asyncio.to_thread(detect_local_capacity, settings)


async def _resource_saturation_orphan_resources(
    request: Request,
    settings: Settings,
    session: AsyncSession,
) -> OrphanResourceSummary:
    provider = cast(
        OrphanResourceSummaryProvider | None,
        getattr(request.app.state, "orphan_resource_summary_provider", None),
    )
    if provider is not None:
        result = provider(settings, session)
        if inspect.isawaitable(result):
            return await result
        return result
    return await _default_orphan_resource_summary(settings, session)


async def _resource_saturation_runtime_health(
    request: Request,
    settings: Settings,
    session: AsyncSession,
    orphan_resources: OrphanResourceSummary,
) -> WorkspaceRuntimeHealthSummary:
    provider = cast(
        RuntimeHealthSummaryProvider | None,
        getattr(request.app.state, "runtime_health_summary_provider", None),
    )
    if provider is not None:
        result = provider(settings, session, orphan_resources)
        if inspect.isawaitable(result):
            return await result
        return result

    workspaces = await runtime_workspaces_from_session(session)
    docker_scan = orphan_resources.scanners.get("docker", {})
    scanner_available = bool(docker_scan.get("ok", True))
    return summarize_runtime_health(
        workspaces=workspaces,
        resources=(
            runtime_resource_from_detected(record.resource)
            for record in orphan_resources.records
            if record.resource.kind == "container"
        ),
        scanner_available=scanner_available,
        scanner_reason=str(docker_scan.get("reason") or "RUNTIME_INSPECTION_UNAVAILABLE"),
        scanner_detail=cast(str | None, docker_scan.get("detail")),
    )


async def _default_orphan_resource_summary(
    settings: Settings,
    session: AsyncSession,
) -> OrphanResourceSummary:
    try:
        workspace_view = await workspace_id_view_from_session(
            session,
            min_retention_hours=settings.completed_workspace_retention_hours,
        )
    except Exception:
        workspace_view = unavailable_workspace_view()
    docker_scan, worktree_scan = await asyncio.gather(
        asyncio.to_thread(
            scan_docker_resources,
            docker_host=settings.docker_host,
            run_subprocess=_run_subprocess,
        ),
        asyncio.to_thread(scan_managed_worktrees, settings.work_dir),
    )
    return build_orphan_resource_summary(
        docker_scan=docker_scan,
        worktree_scan=worktree_scan,
        workspace_view=workspace_view,
        auto_cleanup_orphans=settings.auto_cleanup_orphans,
    )


def _run_subprocess(
    args: list[str],
    *,
    check: bool,
    capture_output: bool,
    text: Literal[True],
    timeout: float,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        env=env,
    )


async def _egress_posture_counts(session: AsyncSession) -> dict[str, int]:
    from awf.db.repositories import EgressAuditRepository

    return await EgressAuditRepository(session).summary_counts_by_posture()
