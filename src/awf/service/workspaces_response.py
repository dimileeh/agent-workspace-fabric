"""Workspace service operations shared by REST routes and MCP tools."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import SplitResult, urlsplit, urlunsplit

from pydantic import ValidationError
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import NoInspectionAvailable

from awf.api.schemas import (
    FallbackTargetResponse,
    ProviderRecoveryStateResponse,
    ValidationFreshnessSummaryResponse,
    WorkspaceAppEndpointResponse,
    WorkspaceResponse,
    WorkspaceRetryResponse,
    WorkspaceRuntimeHealthResponse,
)
from awf.common.audit import redact_audit_value
from awf.common.redaction import redact_secrets
from awf.db.enums import WorkspaceStatus
from awf.db.models import (
    EgressAuditRecord,
    Workspace,
    WorkspaceSecretLease,
)
from awf.profiles.compose import resolve_profile_app_endpoints
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
)
from awf.service.coordination import (
    coordination_warnings_from_task_policy,
)
from awf.service.profile_metadata import network_posture_from_profile_snapshot
from awf.service.provider_recovery import (
    provider_recovery_metadata_from_failure,
    provider_recovery_state_for_workspace,
)
from awf.service.secret_leases import workspace_secret_lease_response
from awf.service.validation_observability import (
    validation_provenance_unavailable,
)
from awf.service.workspace_observability import (
    is_workspace_stale_running,
    workspace_observability_payload,
    workspace_pricing_metadata,
)
from awf.service.workspace_runtime_health import (
    ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
    ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
    ACTIVE_RUNTIME_HEALTH_STATUSES,
    OPERATOR_REFRESH_EVENT_TYPE,
    OPERATOR_REFRESH_REASON_CODE,
    RUNTIME_STRANDED_EVENT_TYPE,
)
from awf.service.workspaces_create import workspace_provider_readiness_preflight
from awf.service.workspaces_retry import (
    _compact_conformance_payload,
    _compact_planning_scope_payload,
    _compact_salvage_payload,
    _latest_failed_state_event,
    _payload_str,
)

if TYPE_CHECKING:
    from awf.service.workspaces import WorkspaceRetryResult


def _workspace_response_source(
    workspace: Workspace,
    computed_fields: Mapping[str, Any],
) -> Any:
    """Build a workspace response source wrapper from the workspace service module."""
    from awf.service.workspaces import _WorkspaceResponseSource

    return _WorkspaceResponseSource(workspace, computed_fields)


def _profile_app_endpoint_adapter() -> Any:
    """Load the profile app endpoint adapter lazily to avoid module-load cycles."""
    from awf.service.workspaces import _PROFILE_APP_ENDPOINTS_ADAPTER

    return _PROFILE_APP_ENDPOINTS_ADAPTER


def workspace_retry_response(result: WorkspaceRetryResult) -> WorkspaceRetryResponse:
    """Build an API response payload from a retry result."""
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
    """Build a provider recovery state API response from the workspace, if any."""
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
    """Build a full API response payload for a workspace including computed fields."""
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
    computed_fields["env_profile"] = getattr(workspace, "env_profile", None) or getattr(
        workspace, "profile_ref", None
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
    return WorkspaceResponse.model_validate(_workspace_response_source(workspace, computed_fields))


def _workspace_app_endpoint_responses(
    workspace: Workspace,
) -> list[WorkspaceAppEndpointResponse]:
    """Build the list of app endpoint responses from the workspace's resolved profile."""
    raw_profile = getattr(workspace, "resolved_profile", None)
    if not isinstance(raw_profile, Mapping):
        return []
    try:
        app_endpoints = _profile_app_endpoint_adapter().validate_python(
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
    """Return a sanitised profile snapshot suitable for console/API responses."""
    if not isinstance(raw_profile, Mapping):
        return None
    sanitized = _sanitize_profile_value(raw_profile, path=())
    return cast(dict[str, Any], sanitized)


def _egress_audit_response(record: EgressAuditRecord) -> dict[str, Any]:
    """Build an egress audit response dict from an audit record."""
    details = redact_audit_value(record.details)
    return {
        "id": record.id,
        "workspace_id": record.workspace_id,
        "attempt_id": record.attempt_id,
        "policy_posture": record.policy_posture,
        "decision": record.decision,
        "destination_category": record.destination_category,
        "reason_code": record.reason_code,
        "details": details if isinstance(details, dict) else {},
        "enforced_at": record.enforced_at,
        "created_at": record.created_at,
    }


def _pricing_metadata_response(workspace: Workspace) -> dict[str, Any] | None:
    """Build a pricing metadata response dict from the workspace, if pricing data exists."""
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
    """Recursively sanitise a profile value, omitting sensitive fields and redacting URLs."""
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
    """Determine whether a profile response field should be omitted for security."""
    if key == "environment":
        return True
    return key == "ref" and "secrets" in path


def _sanitize_profile_string(value: str) -> str:
    """Sanitise a profile string value by redacting secrets in URLs."""
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
    """Reconstruct a URL netloc string with credentials stripped, preserving host and port."""
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
    """Return the list of secret leases loaded on the workspace, handling lazy-loading."""
    try:
        state = sa_inspect(workspace)
    except NoInspectionAvailable:
        return list(getattr(workspace, "secret_leases", []))
    if "secret_leases" in state.unloaded:
        return []
    return list(getattr(workspace, "secret_leases", []))


def workspace_failure_details_payload(workspace: Workspace) -> dict[str, Any] | None:
    """Extract structured failure details from a workspace's most recent failed-state event."""
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


def _workspace_runtime_health_from_events(
    workspace: Workspace,
) -> WorkspaceRuntimeHealthResponse | None:
    """Derive runtime health status from workspace events (stranded and preserved)."""
    return _workspace_runtime_health_from_matching_events(
        workspace,
        event_types=frozenset(
            {
                RUNTIME_STRANDED_EVENT_TYPE,
                ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
            }
        ),
    )


def _workspace_preserved_runtime_health_from_events(
    workspace: Workspace,
) -> WorkspaceRuntimeHealthResponse | None:
    """Derive runtime health status from preserved-execution events only."""
    return _workspace_runtime_health_from_matching_events(
        workspace,
        event_types=frozenset({ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE}),
    )


def _workspace_runtime_health_from_matching_events(
    workspace: Workspace,
    *,
    event_types: frozenset[str],
) -> WorkspaceRuntimeHealthResponse | None:
    """Scan workspace events in reverse to find the latest runtime health status matching the given types."""
    preserved_event_floor = (
        _active_runtime_health_event_floor(workspace)
        if ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE in event_types
        else None
    )
    for event in reversed(workspace.events):
        if event.event_type not in event_types:
            continue
        if (
            event.event_type == ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE
            and _event_occurred_before_floor(event.occurred_at, preserved_event_floor)
        ):
            continue
        payload = event.payload or {}
        reason_code = payload.get("reason_code") or event.reason_code
        decision = payload.get("decision")
        message = payload.get("message")
        if not isinstance(reason_code, str) or not isinstance(decision, str):
            return None
        if not isinstance(message, str):
            message = reason_code
        status = "stranded"
        if reason_code == "RUNTIME_INSPECTION_UNAVAILABLE":
            status = "unavailable"
        if reason_code == ACTIVE_EXECUTION_PRESERVED_REASON_CODE:
            if workspace.status not in ACTIVE_RUNTIME_HEALTH_STATUSES:
                continue
            event_workspace_status = payload.get("workspace_status")
            if (
                not isinstance(event_workspace_status, str)
                or event_workspace_status != workspace.status
            ):
                continue
            status = "ok"
        return WorkspaceRuntimeHealthResponse(
            status=cast(Any, status),
            reason_code=reason_code,
            decision=cast(Any, decision),
            message=message,
            services=_runtime_health_event_services(payload),
        )
    return None


def _active_runtime_health_event_floor(workspace: Workspace) -> datetime | None:
    """Compute the earliest relevant timestamp floor for filtering stale preserved-health events."""
    status = str(workspace.status)
    status_floors = [
        _utc_datetime(event.occurred_at)
        for event in workspace.events
        if isinstance(event.occurred_at, datetime)
        and event.event_type == "workspace.state_changed"
        and event.new_state == status
    ]
    status_started_at = max(status_floors) if status_floors else None
    floors = [status_started_at] if status_started_at is not None else []
    for event in workspace.events:
        if (
            not isinstance(event.occurred_at, datetime)
            or event.event_type != OPERATOR_REFRESH_EVENT_TYPE
            or event.reason_code != OPERATOR_REFRESH_REASON_CODE
        ):
            continue
        occurred_at = _utc_datetime(event.occurred_at)
        if status_started_at is None or occurred_at >= status_started_at:
            floors.append(occurred_at)
    claim_expires_at = getattr(workspace, "execution_claim_expires_at", None)
    if claim_expires_at is not None:
        claim_floor = _utc_datetime(claim_expires_at)
        if claim_floor <= datetime.now(UTC):
            floors.append(claim_floor)
    return max(floors) if floors else None


def _event_occurred_before_floor(
    occurred_at: datetime | None,
    floor: datetime | None,
) -> bool:
    """Check whether an event's timestamp precedes the computed floor time."""
    return (
        isinstance(occurred_at, datetime)
        and floor is not None
        and _utc_datetime(occurred_at) < floor
    )


def _utc_datetime(value: datetime) -> datetime:
    """Coerce a datetime to a timezone-aware UTC datetime."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _runtime_health_event_services(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """Extract a list of service dicts from a runtime health event payload."""
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
