"""Profile metadata extraction tests."""

from __future__ import annotations

import pytest

from awf.service.profile_metadata import (
    egress_allowlist_templates_from_profile_snapshot,
    network_posture_from_profile_snapshot,
)


@pytest.mark.unit
def test_extracts_allowlist_templates_from_profile_snapshot() -> None:
    profile = {
        "name": "test",
        "security": {
            "egress": {
                "mode": "restricted",
                "allowlist_templates": ["github", "model_providers"],
            },
        },
    }
    assert egress_allowlist_templates_from_profile_snapshot(profile) == [
        "github",
        "model_providers",
    ]


@pytest.mark.unit
def test_returns_none_for_missing_templates() -> None:
    assert egress_allowlist_templates_from_profile_snapshot({}) is None
    assert egress_allowlist_templates_from_profile_snapshot({"security": {"egress": {}}}) is None
    assert (
        egress_allowlist_templates_from_profile_snapshot({"security": {"egress": {"mode": "open"}}})
        is None
    )


@pytest.mark.unit
def test_returns_none_for_non_mapping_profile() -> None:
    assert egress_allowlist_templates_from_profile_snapshot(None) is None
    assert egress_allowlist_templates_from_profile_snapshot([]) is None
    assert egress_allowlist_templates_from_profile_snapshot("profile") is None


@pytest.mark.unit
def test_returns_none_for_non_list_templates() -> None:
    assert (
        egress_allowlist_templates_from_profile_snapshot(
            {"security": {"egress": {"allowlist_templates": "github"}}}
        )
        is None
    )


@pytest.mark.unit
def test_returns_none_for_missing_security_or_egress() -> None:
    assert egress_allowlist_templates_from_profile_snapshot({"security": None}) is None
    assert egress_allowlist_templates_from_profile_snapshot({"security": {}}) is None
    assert network_posture_from_profile_snapshot({"security": None}) is None
    assert network_posture_from_profile_snapshot({"security": {}}) is None
    assert network_posture_from_profile_snapshot({"security": {"egress": {"mode": 123}}}) is None


@pytest.mark.unit
def test_network_posture_backward_compatibility() -> None:
    assert (
        network_posture_from_profile_snapshot({"security": {"egress": {"mode": "open"}}}) == "open"
    )
    assert network_posture_from_profile_snapshot(None) is None
    assert network_posture_from_profile_snapshot([]) is None
    assert network_posture_from_profile_snapshot(123) is None
    assert network_posture_from_profile_snapshot({"security": None}) is None
    assert network_posture_from_profile_snapshot({"security": {}}) is None
    assert network_posture_from_profile_snapshot({"security": {"egress": {"mode": 123}}}) is None
