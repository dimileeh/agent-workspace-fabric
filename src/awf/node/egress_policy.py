"""Local Docker egress policy decisions.

This module intentionally only models the local Docker Compose backend. It can
represent open public networking and internal-only workspace networking; finer
destination filtering is deferred to a future proxy/firewall backend.
"""

from __future__ import annotations

from dataclasses import dataclass

from awf.profiles.models import EgressMode, ProfileEgress


@dataclass(frozen=True)
class LocalEgressPlan:
    """Local Compose network settings derived from a profile egress policy."""

    __hash__ = None  # type: ignore[assignment]

    mode: EgressMode
    network_internal: bool
    host_gateway_enabled: bool
    reason_code: str
    details: dict[str, object]


class LocalEgressPolicyError(Exception):
    """Raised when local Docker cannot safely enforce a declared egress mode."""

    def __init__(
        self,
        *,
        reason_code: str,
        mode: EgressMode,
        message: str,
        details: dict[str, object],
    ) -> None:
        self.reason_code = reason_code
        self.mode = mode.value
        self.details = details
        super().__init__(f"{reason_code}: {message}")


def local_egress_plan(egress: ProfileEgress) -> LocalEgressPlan:
    """Return local Compose settings for a profile egress declaration.

    ``restricted`` is conservative in the local backend: it uses the same
    internal-only network posture as ``offline`` while destination-level
    controls are deferred.
    """
    if egress.mode == EgressMode.open:
        return LocalEgressPlan(
            mode=egress.mode,
            network_internal=False,
            host_gateway_enabled=True,
            reason_code="LOCAL_EGRESS_OPEN_UNRESTRICTED",
            details={
                "network_posture": egress.mode.value,
                "internet_access": "unrestricted",
            },
        )
    if egress.mode == EgressMode.offline:
        return LocalEgressPlan(
            mode=egress.mode,
            network_internal=True,
            host_gateway_enabled=False,
            reason_code="LOCAL_EGRESS_OFFLINE_NETWORK",
            details={
                "network_posture": egress.mode.value,
                "internet_access": "disabled",
            },
        )
    if egress.mode == EgressMode.restricted:
        details: dict[str, object] = {
            "network_posture": egress.mode.value,
            "internet_access": "internal_only",
            "destination_filtering": "deferred",
        }
        if egress.allowlist_templates:
            details["allowlist_templates"] = [
                template.value for template in egress.allowlist_templates
            ]
        return LocalEgressPlan(
            mode=egress.mode,
            network_internal=True,
            host_gateway_enabled=False,
            reason_code="LOCAL_EGRESS_RESTRICTED_LOCAL_ONLY",
            details=details,
        )
    raise LocalEgressPolicyError(  # pragma: no cover - enum validation prevents this.
        reason_code="LOCAL_EGRESS_MODE_UNSUPPORTED",
        mode=egress.mode,
        message=f"local Docker backend does not support egress mode {egress.mode.value}",
        details={"network_posture": egress.mode.value},
    )
