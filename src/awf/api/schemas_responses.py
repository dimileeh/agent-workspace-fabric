"""Control, error, and service-GC request/response schemas.

Split out of :mod:`awf.api.schemas` to keep that module under the
maintainability line limit. These are leaf request/response models that are not
referenced by the remaining models in ``schemas`` -- they are re-exported from
``awf.api.schemas`` so ``from awf.api.schemas import ...`` keeps working for both
the REST app and the MCP server.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from awf.api import schemas_operations as _schemas_operations
from awf.common.callback_events import is_valid_callback_subscription_event_type
from awf.common.callback_targets import (
    is_public_callback_target_host,
    validate_callback_target_url_port,
)
from awf.db.enums import (
    OperationStatus,
    WorkspaceStatus,
)

CallbackEventType = _schemas_operations.CallbackEventType


class CallbackSubscriptionCreateRequest(BaseModel):
    """Register an external callback target for sanitized AWF event envelopes."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Annotated[str, Field(min_length=1, max_length=128)]
    target_url: Annotated[str, Field(min_length=1, max_length=2048)]
    event_types: list[CallbackEventType] = Field(min_length=1, max_length=64)
    enabled: bool = True
    timeout_seconds: Annotated[int, Field(ge=1, le=120)] = 10
    max_attempts: Annotated[int, Field(ge=1, le=20)] = 3
    initial_backoff_seconds: Annotated[int, Field(ge=1, le=3600)] = 5

    @field_validator("target_url")
    @classmethod
    def validate_target_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("target_url must use http or https")
        if not parsed.hostname:
            raise ValueError("target_url must include a host")
        validate_callback_target_url_port(parsed)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("target_url must not include userinfo credentials")
        if parsed.fragment:
            raise ValueError("target_url must not include a fragment")
        if not is_public_callback_target_host(parsed.hostname):
            raise ValueError("target_url must use a public host")
        return value

    @field_validator("event_types")
    @classmethod
    def validate_event_types(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        normalized: list[str] = []
        for item in value:
            event_type = item.strip()
            if not is_valid_callback_subscription_event_type(event_type):
                raise ValueError(
                    "event_types must be public callback event names or public wildcards"
                )
            if event_type not in seen:
                seen.add(event_type)
                normalized.append(event_type)
        return normalized


class CallbackSubscriptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    target_url: str
    event_types: list[str]
    enabled: bool
    timeout_seconds: int
    max_attempts: int
    initial_backoff_seconds: int
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None = None


class CallbackSubscriptionListResponse(BaseModel):
    items: list[CallbackSubscriptionResponse]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = 50
    cursor: str | None = None


class WorkspaceReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: Annotated[str | None, Field(default=None, max_length=1024)]


class _WorkspaceReasonCompatibilityRequest(WorkspaceReasonRequest):
    _ignored_legacy_body_fields: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def _drop_ignored_legacy_body_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict) or not cls._ignored_legacy_body_fields:
            return data
        return {
            field_name: value
            for field_name, value in data.items()
            if field_name not in cls._ignored_legacy_body_fields
        }


class WorkspaceReasonWithLegacyStopStackRequest(_WorkspaceReasonCompatibilityRequest):
    """Reason-only request that still accepts the deprecated no-op ``stop_stack`` field."""

    _ignored_legacy_body_fields: ClassVar[frozenset[str]] = frozenset({"stop_stack"})


class WorkspaceReasonWithLegacyRequestedTierRequest(_WorkspaceReasonCompatibilityRequest):
    """Reason-only request that still accepts deprecated no-op ``requested_tier``."""

    _ignored_legacy_body_fields: ClassVar[frozenset[str]] = frozenset({"requested_tier"})


class WorkspaceControlRequest(WorkspaceReasonRequest):
    stop_stack: bool = True


class WorkspaceGuideRequest(BaseModel):
    """Operator-guidance request (issue #447).

    ``directive`` is the first-class agent instruction injected into a live
    monitoring workspace; ``reason`` is an optional audit reason. Use the
    ``guide`` control rather than overloading ``remonitor --reason`` as the
    directive channel."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # ``pattern`` requires at least one non-whitespace character so the OpenAPI
    # schema mirrors the runtime contract: ``str_strip_whitespace`` + ``min_length``
    # already reject whitespace-only directives, but without the pattern generated
    # clients/docs would advertise ``"   "`` as valid. ``\S`` (not a lookahead) is
    # used because pydantic-core's regex engine does not support lookarounds.
    directive: Annotated[str, Field(min_length=1, max_length=1024, pattern=r"\S")]
    reason: Annotated[str | None, Field(default=None, max_length=1024)]


class WorkspaceOperationRequest(WorkspaceReasonRequest):
    requested_tier: Annotated[int | None, Field(default=None, ge=1, le=3)]


class WorkspaceControlWarningResponse(BaseModel):
    warning_code: str
    message: str


class WorkspaceControlResponse(BaseModel):
    workspace_id: str
    operation_id: str
    operation_status: OperationStatus
    status: WorkspaceStatus
    message: str
    warnings: list[WorkspaceControlWarningResponse] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    """Uniform error envelope.

    See docs/PLAN_MVP.md § Error code taxonomy for the canonical error codes.
    """

    error_code: str
    message: str
    detail: dict[str, Any] | None = None


class HTTPExceptionErrorResponse(BaseModel):
    """FastAPI ``HTTPException`` envelope for structured API errors."""

    detail: ErrorResponse


class HttpExceptionErrorResponse(BaseModel):
    """FastAPI HTTPException envelope carrying AWF's structured error detail."""

    detail: ErrorResponse


# ``superseded`` is a terminal GC status (see ``TERMINAL_WORKSPACE_GC_STATUSES``
# in ``service/gc.py``) but is not a ``WorkspaceStatus`` enum member, so GC status
# filters must accept it as a literal alongside the enum values.
GCTerminalStatus = WorkspaceStatus | Literal["superseded"]


class ServiceGCRequest(BaseModel):
    """Trigger payload for ``POST /v1/service/gc``.

    The CLI is a thin client over this route: it maps its flags to these fields
    and lets the root control-plane run the deletion (so root-owned per-workspace
    dirs are actually reclaimed and per-workspace Docker volumes are reaped).
    """

    model_config = ConfigDict(extra="forbid")

    execute: bool = Field(
        default=False,
        description="Delete selected pressure directories. Defaults to a dry-run plan.",
    )
    min_age_hours: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Only consider workspaces whose last update is at least this old. "
            "Defaults to AWF_COMPLETED_WORKSPACE_RETENTION_HOURS."
        ),
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        description="Maximum number of candidates to plan, oldest first.",
    )
    statuses: list[GCTerminalStatus] = Field(
        default_factory=list,
        description="Terminal status filter. Active statuses are always protected.",
    )
    exclude_statuses: list[GCTerminalStatus] = Field(
        default_factory=list,
        description="Status filter to remove from the eligible terminal set.",
    )
    worker_delegation_timeout_seconds: float | None = Field(
        default=None,
        ge=0,
        description=(
            "On ``execute``, how long the API waits for the worker to run the "
            "capability-gated reclaim (per-workspace auth overlays + claude-base) "
            "before returning a structured worker-delegation timeout. Defaults to "
            "the server's GC delegation budget."
        ),
    )


class ServiceGCWorkerReclaim(BaseModel):
    """Worker delegation result folded into the gc response (#582).

    ``execute`` gc delegates the capability-gated reclaim (per-workspace Claude
    auth overlays + ``_shared/claude-base``) to the worker, the only context with
    ``CAP_SYS_ADMIN``. This sub-object reports the worker's actual reclamation (or
    why it could not run) so the operator never sees a false ``deleted_path_count:
    0`` success. ``extra="allow"`` carries the worker's full reap ``report``.
    """

    model_config = ConfigDict(extra="allow")

    status: str
    reason_code: str
    deleted_path_count: int = 0
    total_estimated_bytes: int = 0
    message: str | None = None


class ServiceGCResponse(BaseModel):
    """GC result envelope returned by ``POST /v1/service/gc``.

    The stable top-level fields are documented; the full GC payload
    (``candidates``, ``preserved``, ``delete_errors``, ...) is carried through
    via ``extra="allow"`` so the CLI can render it without a server round-trip.
    """

    model_config = ConfigDict(extra="allow")

    status: str
    reason_code: str
    dry_run: bool
    candidate_count: int = 0
    preserved_count: int = 0
    deleted_path_count: int = 0
    total_estimated_bytes: int = 0
    worker_reclaim: ServiceGCWorkerReclaim | None = Field(
        default=None,
        description=(
            "Present on ``execute`` runs: the worker's capability-gated reclaim of "
            "the per-workspace auth overlays + claude-base, folded into the headline "
            "counts. Absent on dry-run. See #582."
        ),
    )
