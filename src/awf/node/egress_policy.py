"""Local Docker egress policy decisions.

This module intentionally only models the local Docker Compose backend. It
fails closed for profile modes that require destination filtering because
Compose networks cannot enforce generic domain allowlists without a proxy,
firewall, or backend-specific policy controller.
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

    Supported local decisions are limited to open networking and internal-only
    workspace networking. Destination allowlisting is rejected before Compose
    resources are created because the local backend cannot enforce it
    generically.
    """
    details = _policy_details(egress)
    if egress.mode == EgressMode.open:
        return LocalEgressPlan(
            mode=egress.mode,
            network_internal=False,
            host_gateway_enabled=True,
            reason_code="LOCAL_EGRESS_OPEN",
            details=details,
        )
    if egress.mode == EgressMode.offline:
        return LocalEgressPlan(
            mode=egress.mode,
            network_internal=True,
            host_gateway_enabled=False,
            reason_code="LOCAL_EGRESS_OFFLINE_NETWORK",
            details=details,
        )
    if egress.mode == EgressMode.mirrored:
        if egress.allowlist:
            raise LocalEgressPolicyError(
                reason_code="LOCAL_EGRESS_MIRRORED_ALLOWLIST_UNSUPPORTED",
                mode=egress.mode,
                message=(
                    "local Docker mirrored egress with external destinations requires "
                    "a future proxy or firewall backend"
                ),
                details=details,
            )
        return LocalEgressPlan(
            mode=egress.mode,
            network_internal=True,
            host_gateway_enabled=False,
            reason_code="LOCAL_EGRESS_MIRRORED_OFFLINE_NETWORK",
            details=details,
        )
    if egress.mode == EgressMode.allowlist:
        raise LocalEgressPolicyError(
            reason_code="LOCAL_EGRESS_ALLOWLIST_UNSUPPORTED",
            mode=egress.mode,
            message=(
                "local Docker allowlist egress requires a future proxy or firewall backend"
            ),
            details=details,
        )
    raise LocalEgressPolicyError(  # pragma: no cover - enum validation prevents this.
        reason_code="LOCAL_EGRESS_MODE_UNSUPPORTED",
        mode=egress.mode,
        message=f"local Docker backend does not support egress mode {egress.mode.value}",
        details=details,
    )


def _policy_details(egress: ProfileEgress) -> dict[str, object]:
    allowlist = egress.allowlist or []
    return {
        "mode": egress.mode.value,
        "allowlist_count": len(allowlist),
    }
