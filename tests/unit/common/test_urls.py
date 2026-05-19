from __future__ import annotations

import pytest

from awf.common.urls import normalize_api_url, sanitize_request_url


@pytest.mark.unit
@pytest.mark.parametrize(
    ("base_url", "path", "expected"),
    (
        ("http://host:8000", "/v1/workspaces", "http://host:8000/v1/workspaces"),
        ("http://host:8000/", "/v1/workspaces", "http://host:8000/v1/workspaces"),
        ("http://host:8000/awf", "/v1/workspaces", "http://host:8000/awf/v1/workspaces"),
        (
            "http://host:8000/awf/v1",
            "/v1/workspaces",
            "http://host:8000/awf/v1/workspaces",
        ),
        (
            "http://host:8000/awf/v1/",
            "/v1/workspaces",
            "http://host:8000/awf/v1/workspaces",
        ),
    ),
)
def test_normalize_api_url_handles_v1_base_suffix(
    base_url: str,
    path: str,
    expected: str,
) -> None:
    assert normalize_api_url(base_url, path) == expected


@pytest.mark.unit
def test_normalize_api_url_preserves_base_query_and_fragment() -> None:
    assert (
        normalize_api_url("http://host:8000/awf/v1?tenant=one#api", "/v1/workspaces")
        == "http://host:8000/awf/v1/workspaces?tenant=one#api"
    )


@pytest.mark.unit
def test_normalize_api_url_keeps_non_v1_paths_simple() -> None:
    assert normalize_api_url("http://host:8000/awf/", "/healthz") == "http://host:8000/awf/healthz"


@pytest.mark.unit
def test_sanitize_request_url_redacts_sensitive_query_values() -> None:
    assert (
        sanitize_request_url(
            "http://host:8000/v1/workspaces?tenant=one&access_token=top-secret&debug=#api"
        )
        == "http://host:8000/v1/workspaces?tenant=one&access_token=%2A%2A%2A&debug=#api"
    )


@pytest.mark.unit
def test_sanitize_request_url_preserves_ipv6_authority() -> None:
    assert (
        sanitize_request_url("http://[::1]:8000/v1/workspaces?token=top-secret")
        == "http://[::1]:8000/v1/workspaces?token=%2A%2A%2A"
    )


@pytest.mark.unit
def test_sanitize_request_url_returns_relative_url_unchanged() -> None:
    assert sanitize_request_url("/v1/workspaces?token=top-secret") == (
        "/v1/workspaces?token=top-secret"
    )


@pytest.mark.unit
def test_sanitize_request_url_redacts_userinfo_before_logging() -> None:
    assert (
        sanitize_request_url("https://operator:secret@host:8443/v1?secret=top-secret")
        == "https://***@host:8443/v1?secret=%2A%2A%2A"
    )
