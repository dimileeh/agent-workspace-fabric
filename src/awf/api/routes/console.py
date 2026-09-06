"""Read-only authenticated console contract endpoints (schema_version=1)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session, require_api_token
from awf.api.responses import API_TOKEN_AUTH_ERROR_RESPONSES
from awf.common.config import Settings, get_settings
from awf.service.console_capabilities import build_local_console_capabilities
from awf.service.console_dashboard_summary import summarize_console_dashboard_for_session

router = APIRouter(
    prefix="/v1/console",
    tags=["console"],
    dependencies=[Depends(require_api_token)],
    responses=API_TOKEN_AUTH_ERROR_RESPONSES,
)

Availability = Literal["available", "unsupported"]
BackendKind = Literal["local", "hosted"]
CoverageStatus = Literal["complete", "partial", "unknown"]
SummaryScope = Literal["local", "tenant"]


class ConsoleCapabilityItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    availability: Availability
    semantics: str
    route: str | None = None
    reason_code: str | None = None
    message: str | None = None

    @field_validator("route")
    @classmethod
    def route_must_be_relative_v1(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value.startswith("/v1/"):
            raise ValueError("capability routes must be relative /v1/... paths")
        if "://" in value:
            raise ValueError("capability routes must not include absolute URLs")
        return value


class ConsoleCapabilitiesIdentityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend_id: str
    scope: str
    tenant_id: str | None = None


class ConsoleCapabilitiesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1)
    backend_kind: BackendKind
    generated_at: datetime
    identity: ConsoleCapabilitiesIdentityResponse | None = None
    widgets: list[ConsoleCapabilityItemResponse]
    diagnostics: list[ConsoleCapabilityItemResponse]
    controls: list[ConsoleCapabilityItemResponse]


class ConsoleDashboardWindowResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchor: Literal["generated_at"]
    since_hours: int
    start: datetime


class ConsoleDashboardCoverageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: CoverageStatus
    notes: list[str] = Field(default_factory=list)


class ConsoleDashboardCountsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: int | None
    executing: int | None
    monitoring_pr: int | None
    awaiting_operator: int | None
    awaiting_human: int | None
    retrying: int | None
    queued: int | None
    completed_last_window: int | None
    cancelled_last_window: int | None
    failed_last_window: int | None


class ConsoleDashboardOverlapResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    awaiting_human_subset_of_monitoring_pr: bool
    awaiting_operator_in_active_not_executing: bool
    retrying_in_active_not_executing: bool


class ConsoleDashboardSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1)
    scope: SummaryScope
    generated_at: datetime
    as_of: datetime
    last_success_at: datetime
    window: ConsoleDashboardWindowResponse
    coverage: ConsoleDashboardCoverageResponse
    counts: ConsoleDashboardCountsResponse
    overlap: ConsoleDashboardOverlapResponse


@router.get("/capabilities", response_model=ConsoleCapabilitiesResponse)
async def get_console_capabilities() -> ConsoleCapabilitiesResponse:
    """Advertise local console widgets/diagnostics/controls (not live health)."""

    payload = build_local_console_capabilities()
    return ConsoleCapabilitiesResponse(
        schema_version=payload.schema_version,
        backend_kind=payload.backend_kind,
        generated_at=payload.generated_at,
        identity=ConsoleCapabilitiesIdentityResponse(
            backend_id=payload.identity.backend_id,
            scope=payload.identity.scope,
            tenant_id=payload.identity.tenant_id,
        ),
        widgets=[
            ConsoleCapabilityItemResponse(
                id=item.id,
                availability=item.availability,
                semantics=item.semantics,
                route=item.route,
                reason_code=item.reason_code,
                message=item.message,
            )
            for item in payload.widgets
        ],
        diagnostics=[
            ConsoleCapabilityItemResponse(
                id=item.id,
                availability=item.availability,
                semantics=item.semantics,
                route=item.route,
                reason_code=item.reason_code,
                message=item.message,
            )
            for item in payload.diagnostics
        ],
        controls=[
            ConsoleCapabilityItemResponse(
                id=item.id,
                availability=item.availability,
                semantics=item.semantics,
                route=item.route,
                reason_code=item.reason_code,
                message=item.message,
            )
            for item in payload.controls
        ],
    )


@router.get("/dashboard-summary", response_model=ConsoleDashboardSummaryResponse)
async def get_console_dashboard_summary(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ConsoleDashboardSummaryResponse:
    """Authoritative fleet counters independent of Docker capacity probes."""

    summary = await summarize_console_dashboard_for_session(session, settings=settings)
    return ConsoleDashboardSummaryResponse(
        schema_version=summary.schema_version,
        scope=summary.scope,
        generated_at=summary.generated_at,
        as_of=summary.as_of,
        last_success_at=summary.last_success_at,
        window=ConsoleDashboardWindowResponse(
            anchor=summary.window.anchor,
            since_hours=summary.window.since_hours,
            start=summary.window.start,
        ),
        coverage=ConsoleDashboardCoverageResponse(
            status=summary.coverage.status,
            notes=list(summary.coverage.notes),
        ),
        counts=ConsoleDashboardCountsResponse(
            active=summary.counts.active,
            executing=summary.counts.executing,
            monitoring_pr=summary.counts.monitoring_pr,
            awaiting_operator=summary.counts.awaiting_operator,
            awaiting_human=summary.counts.awaiting_human,
            retrying=summary.counts.retrying,
            queued=summary.counts.queued,
            completed_last_window=summary.counts.completed_last_window,
            cancelled_last_window=summary.counts.cancelled_last_window,
            failed_last_window=summary.counts.failed_last_window,
        ),
        overlap=ConsoleDashboardOverlapResponse(
            awaiting_human_subset_of_monitoring_pr=(
                summary.overlap.awaiting_human_subset_of_monitoring_pr
            ),
            awaiting_operator_in_active_not_executing=(
                summary.overlap.awaiting_operator_in_active_not_executing
            ),
            retrying_in_active_not_executing=summary.overlap.retrying_in_active_not_executing,
        ),
    )
