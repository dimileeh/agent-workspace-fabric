"""Safe profile-derived metadata extraction for operator surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

NetworkPosture = Literal["offline", "restricted", "open"]

_NETWORK_POSTURES: frozenset[str] = frozenset(("offline", "restricted", "open"))


def network_posture_from_profile_snapshot(profile: object) -> NetworkPosture | None:
    """Extract a resolved workspace network posture from profile JSON."""

    if not isinstance(profile, Mapping):
        return None
    security = profile.get("security")
    if not isinstance(security, Mapping):
        return None
    egress = security.get("egress")
    if not isinstance(egress, Mapping):
        return None
    mode = egress.get("mode")
    if isinstance(mode, str) and mode in _NETWORK_POSTURES:
        return cast(NetworkPosture, mode)
    return None


def egress_allowlist_templates_from_profile_snapshot(profile: object) -> list[str] | None:
    """Extract declared egress allowlist templates from a resolved-profile JSON snapshot."""

    if not isinstance(profile, Mapping):
        return None
    security = profile.get("security")
    if not isinstance(security, Mapping):
        return None
    egress = security.get("egress")
    if not isinstance(egress, Mapping):
        return None
    templates = egress.get("allowlist_templates")
    if isinstance(templates, list):
        return [str(t) for t in templates]
    return None
