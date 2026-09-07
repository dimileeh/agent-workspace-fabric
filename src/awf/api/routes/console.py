"""Read-only authenticated console contract endpoints (schema_version=1)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session, require_api_token
from awf.api.responses import API_TOKEN_AUTH_ERROR_RESPONSES
from awf.common.config import Settings, get_settings
from awf.service.console_capabilities import (
    CONSOLE_DIAGNOSTIC_INVENTORY_ROUTES,
    CONSOLE_WIDGET_INVENTORY_ROUTES,
    CONSOLE_WIDGETS_WITHOUT_INVENTORY_ROUTE,
    build_local_console_capabilities,
)
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


def _available_item_requires_route_schema() -> dict[str, Any]:
    """OpenAPI if/then: available widget/diagnostic entries require a /v1/ route."""
    return {
        "if": {
            "properties": {"availability": {"const": "available"}},
            "required": ["availability"],
        },
        "then": {
            "required": ["route"],
            "properties": {
                "route": {
                    "type": "string",
                    "pattern": r"^/v1/(?!.*://).+$",
                }
            },
        },
    }


def _exact_inventory_route_when_available(item_id: str, route: str) -> dict[str, Any]:
    """OpenAPI if/then: available entry for ``item_id`` must use the inventory const route."""
    return {
        "if": {
            "properties": {
                "id": {"const": item_id},
                "availability": {"const": "available"},
            },
            "required": ["id", "availability"],
        },
        "then": {
            "required": ["route"],
            "properties": {"route": {"const": route}},
        },
    }


def _available_forbidden_for_id(item_id: str) -> dict[str, Any]:
    """OpenAPI if/then: ids without an inventory route cannot be advertised available."""
    return {
        "if": {
            "properties": {
                "id": {"const": item_id},
                "availability": {"const": "available"},
            },
            "required": ["id", "availability"],
        },
        "then": False,
    }


def _console_capabilities_schema_extra(schema: dict[str, Any]) -> None:
    """Encode hosted identity completeness and available-route rules in OpenAPI.

    The Python ``model_validator`` rejects hosted payloads that omit identity or
    leave ``tenant_id`` empty/null/whitespace-only. Descriptions alone are not
    machine-enforced by Draft 2020-12 clients, so publish an ``if``/``then``
    constraint that requires nonempty ``backend_id``, ``scope``, and a nonblank
    ``tenant_id`` (at least one non-whitespace character — matching
    ``str.strip()``) when ``backend_kind`` is ``hosted``. Local backends keep
    optional identity.

    Available widgets/diagnostics require the exact inventory ``/v1/...`` route for
    their id (controls intentionally omit route). Encode that per-collection on
    items so shared-schema validators cannot certify a wrong or route-less
    available entry the shipped console would reject.
    """
    nonblank_string = {"type": "string", "pattern": r".*\S.*"}
    schema["if"] = {
        "properties": {"backend_kind": {"const": "hosted"}},
        "required": ["backend_kind"],
    }
    schema["then"] = {
        "required": ["identity"],
        "properties": {
            "identity": {
                "type": "object",
                "required": ["backend_id", "scope", "tenant_id"],
                "properties": {
                    "backend_id": {"type": "string", "minLength": 1},
                    "scope": {"type": "string", "minLength": 1},
                    "tenant_id": nonblank_string,
                },
            }
        },
    }
    route_when_available = _available_item_requires_route_schema()
    inventory_by_collection = {
        "widgets": CONSOLE_WIDGET_INVENTORY_ROUTES,
        "diagnostics": CONSOLE_DIAGNOSTIC_INVENTORY_ROUTES,
    }
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for collection, inventory in inventory_by_collection.items():
            collection_schema = properties.get(collection)
            if not isinstance(collection_schema, dict):
                continue
            items = collection_schema.get("items")
            if not isinstance(items, dict):
                continue
            # Wrap $ref in allOf so Draft 2020-12 (and tooling that drops $ref
            # siblings) still applies available⇒exact-inventory-route constraints.
            extras: list[dict[str, Any]] = [items, route_when_available]
            for item_id, route in inventory.items():
                extras.append(_exact_inventory_route_when_available(item_id, route))
            if collection == "widgets":
                for item_id in sorted(CONSOLE_WIDGETS_WITHOUT_INVENTORY_ROUTE):
                    extras.append(_available_forbidden_for_id(item_id))
            collection_schema["items"] = {"allOf": extras}


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

    backend_id: Annotated[str, Field(min_length=1)]
    scope: Annotated[str, Field(min_length=1)]
    # pattern mirrors tenant_id_must_not_be_blank (strip-nonempty) in OpenAPI.
    tenant_id: Annotated[str | None, Field(default=None, pattern=r".*\S.*")] = None

    @field_validator("tenant_id")
    @classmethod
    def tenant_id_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value.strip() == "":
            raise ValueError("identity.tenant_id must be nonempty when provided")
        return value


class ConsoleCapabilitiesResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra=_console_capabilities_schema_extra,
    )

    schema_version: Literal[1]
    backend_kind: BackendKind
    generated_at: datetime
    identity: ConsoleCapabilitiesIdentityResponse | None = Field(
        default=None,
        description=(
            "Optional for backend_kind=local. Required for backend_kind=hosted with "
            "nonempty backend_id, scope, and tenant_id."
        ),
    )
    widgets: list[ConsoleCapabilityItemResponse]
    diagnostics: list[ConsoleCapabilityItemResponse]
    controls: list[ConsoleCapabilityItemResponse]

    @model_validator(mode="after")
    def hosted_requires_complete_identity(self) -> Self:
        if self.backend_kind != "hosted":
            return self
        identity = self.identity
        if identity is None:
            raise ValueError(
                "hosted console capabilities require identity with nonempty "
                "backend_id, scope, and tenant_id"
            )
        if identity.tenant_id is None or identity.tenant_id.strip() == "":
            raise ValueError("hosted console capabilities require a nonempty identity.tenant_id")
        return self

    @model_validator(mode="after")
    def available_widgets_and_diagnostics_require_route(self) -> Self:
        """Available widgets/diagnostics must advertise the exact inventory route.

        Controls intentionally omit route when available; unsupported entries omit
        route. Collection-aware and id-exact so Cloud implementers cannot certify a
        payload (including ``/v1/wrong-route``) the shipped console would reject.
        """
        for collection_name, items, inventory in (
            ("widgets", self.widgets, CONSOLE_WIDGET_INVENTORY_ROUTES),
            ("diagnostics", self.diagnostics, CONSOLE_DIAGNOSTIC_INVENTORY_ROUTES),
        ):
            for item in items:
                if item.availability != "available":
                    continue
                expected = inventory.get(item.id)
                if expected is None:
                    raise ValueError(
                        f"available console {collection_name} id={item.id} has no inventory route"
                    )
                if item.route is None:
                    raise ValueError(
                        f"available console {collection_name} require a relative /v1/... route"
                    )
                if item.route != expected:
                    raise ValueError(
                        f"available console {collection_name} id={item.id} route must be {expected}"
                    )
        return self


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

    schema_version: Literal[1]
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
