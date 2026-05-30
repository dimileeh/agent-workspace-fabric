"""Unit coverage for pure helpers on profile model value objects."""

from __future__ import annotations

import pytest

from awf.profiles.models import (
    ProfileAppEndpoint,
    ProfileAppEndpointHealth,
    ProfileHealthCheck,
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


@pytest.mark.unit
def test_app_endpoint_health_normalizes_method_to_uppercase() -> None:
    health = ProfileAppEndpointHealth(path="/health", method="get")

    assert health.method == "GET"


@pytest.mark.unit
def test_app_endpoint_normalizes_scheme_to_lowercase() -> None:
    endpoint = ProfileAppEndpoint(name="web", service="app", port=8080, scheme="HTTPS")

    assert endpoint.scheme == "https"
