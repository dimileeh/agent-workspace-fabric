"""Workspace control route helper tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import awf.api.routes.controls as controls
from awf.service.controls import (
    IdempotencyConflictError,
    VersionConflictError,
    WorkspaceControlError,
    WorkspaceNotFoundError,
    WorkspaceRemonitorMissingPrUrlError,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("7", 7),
        ('"8"', 8),
        ('W/"9"', 9),
        (" W/  \"10\" ", 10),
    ],
)
def test_parse_if_match_accepts_bare_quoted_and_weak_versions(
    header: str | None,
    expected: int | None,
) -> None:
    assert controls._parse_if_match(header) == expected


@pytest.mark.unit
def test_parse_if_match_rejects_non_integer_versions() -> None:
    with pytest.raises(HTTPException) as exc_info:
        controls._parse_if_match("abc")

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {
        "error_code": "INVALID_REQUEST",
        "message": "If-Match must be a workspace version integer.",
    }


@pytest.mark.unit
@pytest.mark.parametrize("value", [None, "", "   "])
def test_require_idempotency_key_rejects_missing_or_blank_values(value: str | None) -> None:
    with pytest.raises(HTTPException) as exc_info:
        controls._require_idempotency_key(value)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error_code"] == "INVALID_REQUEST"


@pytest.mark.unit
def test_require_idempotency_key_strips_valid_values() -> None:
    assert controls._require_idempotency_key("  key-1  ") == "key-1"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (
            WorkspaceNotFoundError("ws_missing"),
            404,
        ),
        (
            WorkspaceRemonitorMissingPrUrlError(SimpleNamespace(status="monitoring_pr")),
            400,
        ),
        (
            IdempotencyConflictError(),
            409,
        ),
        (
            VersionConflictError(expected_version=1, actual_version=2),
            409,
        ),
    ],
)
def test_http_error_maps_control_errors_to_structured_responses(
    error: WorkspaceControlError,
    status_code: int,
) -> None:
    http_error = controls._http_error(error)

    assert http_error.status_code == status_code
    assert http_error.detail["error_code"]
    assert http_error.detail["message"]
