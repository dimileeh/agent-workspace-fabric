"""Local console capability advertisement (schema_version=1).

Capability advertisement is static and settings-safe. It must not probe Docker
or live health — outages are distinct from unsupported widgets.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

Availability = Literal["available", "unsupported"]
BackendKind = Literal["local", "hosted"]
CONSOLE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ConsoleCapabilityItem:
    id: str
    availability: Availability
    semantics: str
    route: str | None = None
    reason_code: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class ConsoleCapabilitiesIdentity:
    backend_id: str
    scope: str
    tenant_id: str | None = None


@dataclass(frozen=True)
class ConsoleCapabilities:
    schema_version: int
    backend_kind: BackendKind
    generated_at: datetime
    identity: ConsoleCapabilitiesIdentity
    widgets: tuple[ConsoleCapabilityItem, ...]
    diagnostics: tuple[ConsoleCapabilityItem, ...]
    controls: tuple[ConsoleCapabilityItem, ...]


def _available(item_id: str, route: str, semantics: str) -> ConsoleCapabilityItem:
    return ConsoleCapabilityItem(
        id=item_id,
        availability="available",
        route=route,
        semantics=semantics,
    )


def _unsupported(
    item_id: str,
    *,
    reason_code: str,
    message: str,
    semantics: str,
) -> ConsoleCapabilityItem:
    return ConsoleCapabilityItem(
        id=item_id,
        availability="unsupported",
        reason_code=reason_code,
        message=message,
        semantics=semantics,
    )


def build_local_console_capabilities(
    *,
    now: datetime | None = None,
) -> ConsoleCapabilities:
    """Advertise Core local console capabilities (no health probes)."""

    generated_at = now or datetime.now(UTC)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    else:
        generated_at = generated_at.astimezone(UTC)

    widgets = (
        _available(
            "fleet_summary",
            "/v1/console/dashboard-summary",
            "Authoritative fleet counters independent of capacity probes.",
        ),
        _available(
            "resource_capacity",
            "/v1/metrics/resources/saturation",
            "Local Docker/disk/runtime slot saturation.",
        ),
        _unsupported(
            "cloud_runtime",
            reason_code="backend_kind_local",
            message="Cloud Runtime evidence is hosted-only.",
            semantics="Hosted queue age, provisioning, and admission/quota evidence.",
        ),
        _unsupported(
            "telemetry",
            reason_code="not_implemented",
            message="Telemetry collectors are not implemented yet.",
            semantics="Backend-neutral telemetry presentation (not implemented).",
        ),
        _unsupported(
            "allocation",
            reason_code="not_implemented",
            message="Allocation evidence is not implemented yet.",
            semantics="Backend-neutral allocation presentation (not implemented).",
        ),
        _unsupported(
            "cost",
            reason_code="not_implemented",
            message=(
                "Cost/billing evidence is not implemented yet; "
                "shared monitor runtime is never free."
            ),
            semantics="Cost/billing presentation (not implemented).",
        ),
    )
    diagnostics = (
        _available(
            "reliability",
            "/v1/metrics/workspaces/summary",
            "Windowed reliability and stuck/reason coverage.",
        ),
        _available(
            "merge_queue",
            "/v1/merge-queue",
            "Local merge-queue candidates.",
        ),
        _available(
            "failures",
            "/v1/metrics/failures/summary",
            "Failure taxonomy and recent examples.",
        ),
    )
    controls = (
        ConsoleCapabilityItem(
            id="remonitor",
            availability="available",
            semantics="Re-enter PR monitoring for an eligible workspace.",
        ),
        ConsoleCapabilityItem(
            id="refresh",
            availability="available",
            semantics="Refresh workspace status from GitHub/control plane.",
        ),
        ConsoleCapabilityItem(
            id="revalidate",
            availability="available",
            semantics="Re-run validation tiers for an eligible workspace.",
        ),
        ConsoleCapabilityItem(
            id="cancel",
            availability="available",
            semantics="Cancel an active or paused workspace.",
        ),
    )
    return ConsoleCapabilities(
        schema_version=CONSOLE_SCHEMA_VERSION,
        backend_kind="local",
        generated_at=generated_at,
        identity=ConsoleCapabilitiesIdentity(
            backend_id="awf-core-local",
            scope="local",
        ),
        widgets=widgets,
        diagnostics=diagnostics,
        controls=controls,
    )
