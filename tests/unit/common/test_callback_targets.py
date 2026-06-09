"""Shared callback target policy tests."""

from __future__ import annotations

import ipaddress

import pytest

from awf.common import callback_targets


class _NoLegacyIPv4Labels:
    def split(self, separator: str) -> list[str]:
        assert separator == "."
        return []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("hostname", "expected"),
    [
        ("operator.example.com", True),
        ("operator.example.com.", True),
        ("localhost", False),
        ("api.localhost", False),
        ("service.local", False),
        ("internal", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("::10.0.0.1", False),
        ("::169.254.169.254", False),
        ("::ffff:127.0.0.1", False),
        ("::ffff:169.254.169.254", False),
        ("::ffff:0:169.254.169.254", False),
        ("::ffff:0:8.8.8.8", False),
        ("2002:c0a8:0101::1", False),
        ("0300.0250.0001.0001", False),
        ("0xc0.0xa8.0x01.0x01", False),
        ("224.0.0.1", False),
    ],
)
def test_callback_target_host_publicness_policy(hostname: str, expected: bool) -> None:
    assert callback_targets.is_public_callback_target_host(hostname) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("1.1.1.1", True),
        ("2606:4700:4700::1111", True),
        ("127.0.0.1", False),
        ("::10.0.0.1", False),
        ("::169.254.169.254", False),
        ("::ffff:127.0.0.1", False),
        ("::ffff:0:169.254.169.254", False),
        ("::ffff:0:8.8.8.8", False),
        ("64:ff9b::a9fe:a9fe", False),
        ("64:ff9b:1:c001::c0a8:0101", False),
        ("2002:c0a8:0101::1", False),
    ],
)
def test_callback_target_ip_publicness_policy(address: str, expected: bool) -> None:
    assert callback_targets.is_public_callback_target_ip(address) is expected


@pytest.mark.unit
def test_locally_assigned_nat64_callback_targets_are_blocked() -> None:
    assert callback_targets.is_public_callback_target_ip("64:ff9b:1:808:8:800::") is False
    assert callback_targets.is_public_callback_target_ip("64:ff9b:1:a00:0:100:808:808") is False
    assert callback_targets.is_public_callback_target_ip("64:ff9b:1:c0a8:1:100::") is False
    assert callback_targets.is_public_callback_target_ip("64:ff9b:1:c001::c0a8:0101") is False


@pytest.mark.unit
def test_legacy_ipv4_literal_detector_rejects_malformed_legacy_hosts() -> None:
    assert (
        callback_targets.looks_like_legacy_ipv4_literal(  # type: ignore[arg-type]
            _NoLegacyIPv4Labels()
        )
        is False
    )


@pytest.mark.unit
def test_nat64_extraction_rejects_unsupported_prefix_lengths() -> None:
    with pytest.raises(ValueError, match="unsupported NAT64 prefix length"):
        callback_targets._extract_nat64_embedded_ipv4_address(  # noqa: SLF001
            ipaddress.IPv6Address("64:ff9b::808:808"),
            24,
        )


@pytest.mark.unit
def test_nat64_extraction_handles_reserved_octet_prefix_lengths() -> None:
    assert callback_targets._extract_nat64_embedded_ipv4_address(  # noqa: SLF001
        ipaddress.IPv6Address("64:ff9b:1:c0a8:1:100::"),
        48,
    ) == ipaddress.IPv4Address("192.168.1.1")
