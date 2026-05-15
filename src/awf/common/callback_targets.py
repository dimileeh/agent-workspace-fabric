"""Shared callback target host validation policy."""

from __future__ import annotations

import ipaddress

_NAT64_WELL_KNOWN_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL_USE_PREFIX = ipaddress.IPv6Network("64:ff9b:1::/48")
_NAT64_TRANSLATION_PREFIXES = (_NAT64_WELL_KNOWN_PREFIX, _NAT64_LOCAL_USE_PREFIX)
_SIX_TO_FOUR_PREFIX = ipaddress.IPv6Network("2002::/16")


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

    return _is_public_callback_target_address(address)


def is_public_callback_target_ip(address: str) -> bool:
    """Return whether a resolved callback target IP is publicly routable."""
    return _is_public_callback_target_address(ipaddress.ip_address(address))


def _is_public_callback_target_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    if _is_blocked_callback_target_address(address):
        return False

    public_address = _callback_target_public_address(address)
    return public_address.is_global and not public_address.is_multicast


def _callback_target_public_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    ipv4_mapped = getattr(address, "ipv4_mapped", None)
    if isinstance(ipv4_mapped, ipaddress.IPv4Address):
        return ipv4_mapped
    if isinstance(address, ipaddress.IPv6Address) and any(
        address in prefix for prefix in _NAT64_TRANSLATION_PREFIXES
    ):
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address


def _is_blocked_callback_target_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return isinstance(address, ipaddress.IPv6Address) and address in _SIX_TO_FOUR_PREFIX
