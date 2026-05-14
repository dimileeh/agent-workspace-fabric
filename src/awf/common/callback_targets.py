"""Shared callback target host validation policy."""

from __future__ import annotations

import ipaddress


def looks_like_legacy_ipv4_literal(hostname: str) -> bool:
    """Return whether hostname resembles a non-standard IPv4 numeric literal."""
    labels = hostname.split(".")
    if not labels:
        return False

    for label in labels:
        if not label:
            return False
        lower_label = label.lower()
        if lower_label.startswith("0x"):
            hex_digits = lower_label[2:]
            if not hex_digits or any(
                character not in "0123456789abcdef" for character in hex_digits
            ):
                return False
            continue
        if not lower_label.isdigit():
            return False

    return True


def is_public_callback_target_host(hostname: str) -> bool:
    """Return whether hostname is safe as an externally reachable callback target."""
    normalized = hostname.rstrip(".").lower()
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        if normalized == "localhost" or normalized.endswith(
            (".localhost", ".local", ".localdomain")
        ):
            return False
        if "." not in normalized:
            return False
        return not looks_like_legacy_ipv4_literal(normalized)

    ipv4_mapped = getattr(address, "ipv4_mapped", None)
    target_address: ipaddress.IPv4Address | ipaddress.IPv6Address = (
        ipv4_mapped if isinstance(ipv4_mapped, ipaddress.IPv4Address) else address
    )

    return target_address.is_global and not target_address.is_multicast
