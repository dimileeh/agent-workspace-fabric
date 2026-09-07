"""Read-only authenticated console contract endpoints (schema_version=1)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Depends
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session, require_api_token
from awf.api.responses import API_TOKEN_AUTH_ERROR_RESPONSES
from awf.common.config import Settings, get_settings
from awf.service.console_capabilities import (
    CONSOLE_CONTROL_IDS,
    CONSOLE_DIAGNOSTIC_IDS,
    CONSOLE_DIAGNOSTIC_INVENTORY_ROUTES,
    CONSOLE_UNSUPPORTED_REASON_CODES,
    CONSOLE_WIDGET_IDS,
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


def _require_iso_timestamp_string(value: Any) -> Any:
    """Reject numeric Unix timestamps so Python validation matches the TS parser.

    OpenAPI already declares ``type: string, format: date-time``. Pydantic's
    non-strict ``datetime`` field otherwise accepts ints/floats (e.g. ``0``),
    which the shipped console parsers reject and which would disable negotiation.
    In-process ``datetime`` instances remain accepted for route builders.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return value
    raise ValueError("timestamp must be an ISO-8601 string")


def _require_timezone_aware_datetime(value: datetime) -> datetime:
    """Reject timezone-less values so Python matches the shipped RFC 3339 TS parser.

    OpenAPI ``format: date-time`` and the console parsers require ``Z`` or an
    explicit ±HH:mm offset. Pydantic otherwise accepts naive datetimes and
    timezone-less ISO strings (e.g. ``2026-09-07T12:00:00``).
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone offset")
    return value


ConsoleTimestamp = Annotated[
    datetime,
    BeforeValidator(_require_iso_timestamp_string),
    AfterValidator(_require_timezone_aware_datetime),
]


def _available_item_requires_route_schema(inventory_ids: list[str]) -> dict[str, Any]:
    """OpenAPI if/then: available widget/diagnostic entries need inventory id + /v1/ route.

    Matches Python ``inventory.get`` rejection: an available entry whose id is not
    in the collection route inventory must fail closed even when ``route`` is a
    relative ``/v1/...`` string (e.g. ``unknown_audit_id`` + ``/v1/wrong-route``).
    """
    return {
        "if": {
            "properties": {"availability": {"const": "available"}},
            "required": ["availability"],
        },
        "then": {
            "required": ["id", "route"],
            "properties": {
                "id": {"enum": sorted(inventory_ids)},
                "route": {
                    "type": "string",
                    "pattern": r"^/v1/(?!.*://).+$",
                },
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


def _at_most_one_id_constraint(item_id: str) -> dict[str, Any]:
    """OpenAPI: each known id may appear at most once in a capability collection.

    Draft 2020-12 ``contains`` + ``maxContains=1`` with ``minContains=0`` encodes
    uniqueness per id without requiring the id to be present.
    """
    return {
        "contains": {
            "type": "object",
            "properties": {"id": {"const": item_id}},
            "required": ["id"],
        },
        "minContains": 0,
        "maxContains": 1,
    }


def _console_capability_item_schema_extra(schema: dict[str, Any]) -> None:
    """OpenAPI if/then: unsupported entries need bounded reason_code + message."""
    schema["if"] = {
        "properties": {"availability": {"const": "unsupported"}},
        "required": ["availability"],
    }
    schema["then"] = {
        "required": ["reason_code", "message"],
        "properties": {
            "reason_code": {
                "type": "string",
                "enum": sorted(CONSOLE_UNSUPPORTED_REASON_CODES),
            },
            "message": {"type": "string", "minLength": 1},
        },
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

    Available widgets/diagnostics require an inventory id and that id's exact
    ``/v1/...`` route (controls intentionally omit route). Encode that
    per-collection on items so shared-schema validators cannot certify a wrong,
    unknown-id, or route-less available entry the shipped console would reject.

    Duplicate ids are rejected via per-id ``contains`` + ``maxContains=1``
    (``minContains=0``) on each collection array, matching Python/TS uniqueness.
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
    inventory_by_collection = {
        "widgets": CONSOLE_WIDGET_INVENTORY_ROUTES,
        "diagnostics": CONSOLE_DIAGNOSTIC_INVENTORY_ROUTES,
    }
    known_ids_by_collection = {
        "widgets": CONSOLE_WIDGET_IDS,
        "diagnostics": CONSOLE_DIAGNOSTIC_IDS,
        "controls": CONSOLE_CONTROL_IDS,
    }
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for collection, known_ids in known_ids_by_collection.items():
            collection_schema = properties.get(collection)
            if not isinstance(collection_schema, dict):
                continue
            items = collection_schema.get("items")
            if not isinstance(items, dict):
                continue
            # Bound id for every availability state (and controls) so shared-schema
            # validators cannot certify unknown or non-inventory capability ids.
            id_bound: dict[str, Any] = {
                "properties": {"id": {"enum": sorted(known_ids)}},
                "required": ["id"],
            }
            if collection in inventory_by_collection:
                inventory = inventory_by_collection[collection]
                # Wrap $ref in allOf so Draft 2020-12 (and tooling that drops $ref
                # siblings) still applies available⇒exact-inventory-route constraints.
                route_when_available = _available_item_requires_route_schema(list(inventory))
                extras: list[dict[str, Any]] = [items, id_bound, route_when_available]
                for item_id, route in inventory.items():
                    extras.append(_exact_inventory_route_when_available(item_id, route))
                if collection == "widgets":
                    for item_id in sorted(CONSOLE_WIDGETS_WITHOUT_INVENTORY_ROUTE):
                        extras.append(_available_forbidden_for_id(item_id))
                collection_schema["items"] = {"allOf": extras}
            else:
                # Controls: no route rules; still bound id for all availability states.
                if "$ref" in items:
                    collection_schema["items"] = {"allOf": [items, id_bound]}
                else:
                    collection_schema["items"] = {"allOf": [items, id_bound]}
            # Per-id uniqueness for Cloud/OpenAPI consumers (Python/TS already reject).
            collection_schema["allOf"] = [
                _at_most_one_id_constraint(item_id) for item_id in sorted(known_ids)
            ]


class ConsoleCapabilityItemResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra=_console_capability_item_schema_extra,
    )

    id: str
    availability: Availability
    # Match shipped parseConsoleCapabilities: empty semantics fail closed.
    semantics: Annotated[str, Field(min_length=1)]
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

    @model_validator(mode="after")
    def unsupported_requires_bounded_reason(self) -> Self:
        """Unsupported entries must carry inventory reason_code + nonempty message."""
        if self.availability != "unsupported":
            return self
        if not isinstance(self.reason_code, str) or self.reason_code == "":
            raise ValueError(
                "unsupported console capability entries require a non-empty reason_code"
            )
        if self.reason_code not in CONSOLE_UNSUPPORTED_REASON_CODES:
            raise ValueError(
                f"unsupported console capability reason_code={self.reason_code!r} "
                "is outside the v1 inventory"
            )
        if not isinstance(self.message, str) or self.message == "":
            raise ValueError("unsupported console capability entries require a non-empty message")
        return self


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
    generated_at: ConsoleTimestamp
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

    @model_validator(mode="after")
    def capability_ids_known_and_unique(self) -> Self:
        """Bound every capability id and reject duplicates per collection.

        Matches the shipped console parser: unknown unsupported widgets/diagnostics,
        unknown controls, and repeated ids fail closed for all availability states.
        """
        for collection_name, items, known in (
            ("widgets", self.widgets, CONSOLE_WIDGET_IDS),
            ("diagnostics", self.diagnostics, CONSOLE_DIAGNOSTIC_IDS),
            ("controls", self.controls, CONSOLE_CONTROL_IDS),
        ):
            seen: set[str] = set()
            for item in items:
                if item.id not in known:
                    raise ValueError(f"unknown console {collection_name} id={item.id}")
                if item.id in seen:
                    raise ValueError(f"duplicate console {collection_name} id={item.id}")
                seen.add(item.id)
        return self


class ConsoleDashboardWindowResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchor: Literal["generated_at"]
    # Positive integer hours; matches shipped parseDashboardSummary.
    since_hours: Annotated[StrictInt, Field(gt=0)]
    start: ConsoleTimestamp


class ConsoleDashboardCoverageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: CoverageStatus
    notes: list[str] = Field(default_factory=list)


class ConsoleDashboardCountsResponse(BaseModel):
    """Nullable nonnegative fleet counts as strict ints (no string/bool coerce)."""

    model_config = ConfigDict(extra="forbid")

    active: StrictInt | None = Field(ge=0)
    executing: StrictInt | None = Field(ge=0)
    monitoring_pr: StrictInt | None = Field(ge=0)
    awaiting_operator: StrictInt | None = Field(ge=0)
    awaiting_human: StrictInt | None = Field(ge=0)
    retrying: StrictInt | None = Field(ge=0)
    queued: StrictInt | None = Field(ge=0)
    completed_last_window: StrictInt | None = Field(ge=0)
    cancelled_last_window: StrictInt | None = Field(ge=0)
    failed_last_window: StrictInt | None = Field(ge=0)


class ConsoleDashboardOverlapResponse(BaseModel):
    """Overlap invariant flags as strict bools (no string/int coerce)."""

    model_config = ConfigDict(extra="forbid")

    awaiting_human_subset_of_monitoring_pr: StrictBool
    awaiting_operator_in_active_not_executing: StrictBool
    retrying_in_active_not_executing: StrictBool


class ConsoleDashboardSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    scope: SummaryScope
    generated_at: ConsoleTimestamp
    as_of: ConsoleTimestamp
    last_success_at: ConsoleTimestamp
    window: ConsoleDashboardWindowResponse
    coverage: ConsoleDashboardCoverageResponse
    counts: ConsoleDashboardCountsResponse
    overlap: ConsoleDashboardOverlapResponse

    @model_validator(mode="after")
    def counts_respect_documented_subsets(self) -> Self:
        """Reject contradictory fleet tallies when related counts are non-null.

        Matches the shipped TS ``parseDashboardSummary`` so malformed hosted
        payloads fail closed rather than rendering impossible KPI relationships.
        """
        counts = self.counts
        overlap = self.overlap
        if (
            counts.active is not None
            and counts.executing is not None
            and counts.executing > counts.active
        ):
            raise ValueError("counts.executing must be <= counts.active")
        if (
            overlap.awaiting_human_subset_of_monitoring_pr
            and counts.awaiting_human is not None
            and counts.monitoring_pr is not None
            and counts.awaiting_human > counts.monitoring_pr
        ):
            raise ValueError("counts.awaiting_human must be <= counts.monitoring_pr")
        if (
            overlap.awaiting_operator_in_active_not_executing
            and counts.awaiting_operator is not None
        ):
            if counts.active is not None and counts.awaiting_operator > counts.active:
                raise ValueError("counts.awaiting_operator must be <= counts.active")
            if (
                counts.active is not None
                and counts.executing is not None
                and counts.awaiting_operator + counts.executing > counts.active
            ):
                raise ValueError(
                    "counts.awaiting_operator + counts.executing must be <= counts.active"
                )
        if overlap.retrying_in_active_not_executing and counts.retrying is not None:
            if counts.active is not None and counts.retrying > counts.active:
                raise ValueError("counts.retrying must be <= counts.active")
            if (
                counts.active is not None
                and counts.executing is not None
                and counts.retrying + counts.executing > counts.active
            ):
                raise ValueError("counts.retrying + counts.executing must be <= counts.active")
        return self


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
