"""Shared callback target host validation policy."""

from __future__ import annotations

import ipaddress
from urllib.parse import SplitResult

_NAT64_WELL_KNOWN_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL_USE_PREFIX = ipaddress.IPv6Network("64:ff9b:1::/48")
# Only the well-known /96 translation prefix is decoded today. The RFC 6052
# extractor keeps non-/96 support for future explicitly opted-in prefixes;
# the local-use /48 namespace is blocked outright instead of decoded.
_NAT64_TRANSLATION_PREFIXES = (_NAT64_WELL_KNOWN_PREFIX,)
_SIX_TO_FOUR_PREFIX = ipaddress.IPv6Network("2002::/16")
_IPV4_COMPATIBLE_PREFIX = ipaddress.IPv6Network("::/96")
_IPV4_TRANSLATED_PREFIX = ipaddress.IPv6Network("::ffff:0:0:0/96")


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


def validate_callback_target_url_port(parsed: SplitResult) -> None:
    """Raise when a parsed callback target URL has a malformed or invalid port."""
    try:
        _port = parsed.port
    except ValueError as exc:
        raise ValueError("target_url must include a valid port") from exc


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
    if isinstance(address, ipaddress.IPv6Address):
        nat64_embedded_ipv4 = _nat64_embedded_ipv4_address(address)
        if nat64_embedded_ipv4 is not None:
            return nat64_embedded_ipv4
    return address


def _nat64_embedded_ipv4_address(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    for prefix in _NAT64_TRANSLATION_PREFIXES:
        if address in prefix:
            return _extract_nat64_embedded_ipv4_address(address, prefix.prefixlen)
    return None


def _extract_nat64_embedded_ipv4_address(
    address: ipaddress.IPv6Address,
    prefix_length: int,
) -> ipaddress.IPv4Address:
    if prefix_length == 96:
        return ipaddress.IPv4Address(address.packed[-4:])

    if prefix_length not in {32, 40, 48, 56, 64}:
        raise ValueError(f"unsupported NAT64 prefix length: {prefix_length}")

    # RFC 6052 inserts the reserved "u" octet at bits 64-71 for non-/96 prefixes.
    address_without_reserved_octet = address.packed[:8] + address.packed[9:]
    ipv4_start = prefix_length // 8
    return ipaddress.IPv4Address(address_without_reserved_octet[ipv4_start : ipv4_start + 4])


def _is_blocked_callback_target_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return isinstance(address, ipaddress.IPv6Address) and (
        address in _SIX_TO_FOUR_PREFIX
        or address in _NAT64_LOCAL_USE_PREFIX
        or address in _IPV4_COMPATIBLE_PREFIX
        or address in _IPV4_TRANSLATED_PREFIX
    )
