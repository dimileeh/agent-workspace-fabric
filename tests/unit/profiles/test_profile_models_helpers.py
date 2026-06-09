"""Unit coverage for pure helpers on profile model value objects."""

from __future__ import annotations

import pytest

from awf.profiles.models import (
    ProfileAppEndpoint,
    ProfileAppEndpointHealth,
    ProfileHealthCheck,
    normalize_inline_profile_snapshot,
)


@pytest.mark.unit
def test_command_healthcheck_display_and_target_return_command() -> None:
    check = ProfileHealthCheck(name="db", command="pg_isready -U awf")

    assert check.display_command() == "pg_isready -U awf"
    assert check.target() == "pg_isready -U awf"


@pytest.mark.unit
def test_url_healthcheck_target_passes_through_when_no_userinfo() -> None:
    check = ProfileHealthCheck(name="api", url="http://api:8080/health")

    assert check.target() == "http://api:8080/health"
    assert check.display_command() == "GET http://api:8080/health expected 200"


@pytest.mark.unit
def test_url_healthcheck_target_redacts_userinfo() -> None:
    check = ProfileHealthCheck(name="api", url="http://user:secret@api:8080/health")

    assert check.target() == "http://api:8080/health"
    assert check.display_command() == "GET http://api:8080/health expected 200"


@pytest.mark.unit
def test_app_endpoint_health_normalizes_method_to_uppercase() -> None:
    health = ProfileAppEndpointHealth(path="/health", method="get")

    assert health.method == "GET"


@pytest.mark.unit
def test_app_endpoint_normalizes_scheme_to_lowercase() -> None:
    endpoint = ProfileAppEndpoint(name="web", service="app", port=8080, scheme="HTTPS")

    assert endpoint.scheme == "https"


@pytest.mark.unit
def test_normalize_inline_profile_snapshot_passes_through_none() -> None:
    assert normalize_inline_profile_snapshot(None) is None


@pytest.mark.unit
def test_normalize_inline_profile_snapshot_defaults_missing_forge_to_auto() -> None:
    """A pre-forge legacy snapshot lacks the key; normalization adds the input
    default so it compares equal to a fresh replay that dumps ``forge="auto"``."""
    legacy = {"name": "inline"}

    normalized = normalize_inline_profile_snapshot(legacy)

    assert normalized == {"name": "inline", "forge": "auto"}
    # The input snapshot (a live ORM attribute at the call sites) must not mutate.
    assert legacy == {"name": "inline"}


@pytest.mark.unit
def test_normalize_inline_profile_snapshot_preserves_present_forge() -> None:
    explicit = {"name": "inline", "forge": "github"}

    normalized = normalize_inline_profile_snapshot(explicit)

    assert normalized == {"name": "inline", "forge": "github"}
    assert normalized is not explicit
