"""Shared callback target policy tests."""

from __future__ import annotations

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
        ("::ffff:127.0.0.1", False),
        ("::ffff:169.254.169.254", False),
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
        ("::ffff:127.0.0.1", False),
        ("64:ff9b::a9fe:a9fe", False),
        ("2002:c0a8:0101::1", False),
    ],
)
def test_callback_target_ip_publicness_policy(address: str, expected: bool) -> None:
    assert callback_targets.is_public_callback_target_ip(address) is expected


@pytest.mark.unit
def test_locally_assigned_nat64_callback_targets_unmask_embedded_ipv4() -> None:
    assert callback_targets.is_public_callback_target_ip("64:ff9b:1::0808:0808") is True
    assert callback_targets.is_public_callback_target_ip("64:ff9b:1::c0a8:0101") is False


@pytest.mark.unit
def test_legacy_ipv4_literal_detector_rejects_malformed_legacy_hosts() -> None:
    assert (
        callback_targets.looks_like_legacy_ipv4_literal(  # type: ignore[arg-type]
            _NoLegacyIPv4Labels()
        )
        is False
    )
