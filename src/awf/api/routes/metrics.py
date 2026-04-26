"""Read-only operational metrics endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session
from awf.common.config import Settings, get_settings
from awf.service.disk import DiskCheck, check_disk_space
from awf.service.metrics import (
    DEFAULT_SUMMARY_WINDOW_HOURS,
    MAX_SUMMARY_WINDOW_HOURS,
    MIN_SUMMARY_WINDOW_HOURS,
    summarize_failure_analysis_for_session,
    summarize_resource_saturation_for_session,
    summarize_workspace_reliability_for_session,
)

router = APIRouter(prefix="/v1/metrics", tags=["metrics"])
DiskCheckProvider = Callable[[Settings], DiskCheck]


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
    destroying: int
    completed: int
    failed: int
    cancelled: int
    destroyed: int


class WorkerConcurrencySettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    max_concurrent_provisions: int
    max_concurrent_executions: int


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


class ResourceSaturationSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    generated_at: datetime
    workspace_counts: WorkspaceSaturationCountsResponse = Field(
        description="Current workspace counts by status plus active non-terminal totals.",
    )
    worker: WorkerConcurrencySettingsResponse = Field(
        description="Configured local worker concurrency limits.",
    )
    resource_defaults: WorkspaceResourceDefaultsResponse = Field(
        description="Configured per-workspace steady and peak resource defaults.",
    )
    reserved_resources: ReservedResourcesResponse = Field(
        description="Resource reservations implied by active workspace count and defaults.",
    )
    concurrency: ResourceConcurrencyResponse = Field(
        description="Provisioning and execution worker lane saturation.",
    )
    disk: DiskCheckResponse = Field(
        description="Disk pressure check for the AWF work directory.",
    )
    admission: AdmissionSummaryResponse = Field(
        description="Actionable summary explaining whether new workspace admission is blocked.",
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
    session: AsyncSession = Depends(get_db_session),
) -> FailureAnalysisSummaryResponse:
    summary = await summarize_failure_analysis_for_session(
        session,
        since_hours=since_hours,
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
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceReliabilitySummaryResponse:
    summary = await summarize_workspace_reliability_for_session(
        session,
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
    summary = await summarize_resource_saturation_for_session(
        session,
        settings=settings,
        disk_check=disk_check,
    )
    return ResourceSaturationSummaryResponse.model_validate(summary)


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
