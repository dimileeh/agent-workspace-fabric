from __future__ import annotations

import pytest

from awf.common.urls import normalize_api_url


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
