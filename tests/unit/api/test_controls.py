"""Workspace control route helper tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

import awf.api.routes.controls as controls
from awf.api.schemas import WorkspaceControlRequest, WorkspaceGuideRequest
from awf.db.session import make_session_factory
from awf.service.controls import (
    IdempotencyConflictError,
    VersionConflictError,
    WorkspaceControlError,
    WorkspaceGuideMissingPrUrlError,
    WorkspaceGuideStateError,
    WorkspaceNotFoundError,
    WorkspaceRebaseMissingCandidateError,
    WorkspaceRebaseMissingPrUrlError,
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
        (' W/  "10" ', 10),
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
            WorkspaceRebaseMissingPrUrlError(SimpleNamespace(status="monitoring_pr")),
            400,
        ),
        (
            WorkspaceGuideMissingPrUrlError(SimpleNamespace(status="monitoring_pr")),
            400,
        ),
        (
            WorkspaceGuideStateError(SimpleNamespace(status="requested")),
            409,
        ),
        (
            WorkspaceRebaseMissingCandidateError(
                SimpleNamespace(id="ws_1", pr_url="https://github.com/x/y/pull/1")
            ),
            404,
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


@pytest.mark.unit
async def test_control_route_functions_map_missing_workspace_errors(
    engine: AsyncEngine,
) -> None:
    payload = WorkspaceControlRequest(reason="operator requested", stop_stack=False)
    factory = make_session_factory(engine)

    async with factory() as session:
        with pytest.raises(HTTPException) as cancel_error:
            await controls.cancel_workspace(
                "ws_missing",
                payload,
                idempotency_key="cancel-missing",
                if_match=None,
                session=session,
            )
    async with factory() as session:
        with pytest.raises(HTTPException) as stop_error:
            await controls.stop_workspace(
                "ws_missing",
                payload,
                idempotency_key="stop-missing",
                if_match=None,
                session=session,
            )
    async with factory() as session:
        with pytest.raises(HTTPException) as remonitor_error:
            await controls.remonitor_workspace(
                "ws_missing",
                payload,
                idempotency_key="remonitor-missing",
                if_match=None,
                session=session,
            )
    async with factory() as session:
        with pytest.raises(HTTPException) as guide_error:
            await controls.guide_workspace(
                "ws_missing",
                WorkspaceGuideRequest(directive="do the thing"),
                idempotency_key="guide-missing",
                if_match=None,
                session=session,
            )
    async with factory() as session:
        with pytest.raises(HTTPException) as destroy_error:
            await controls.destroy_workspace(
                "ws_missing",
                idempotency_key="destroy-missing",
                if_match=None,
                session=session,
            )

    errors = [
        cancel_error.value,
        stop_error.value,
        remonitor_error.value,
        guide_error.value,
        destroy_error.value,
    ]
    assert [error.status_code for error in errors] == [404, 404, 404, 404, 404]
    assert [error.detail["error_code"] for error in errors] == ["NOT_FOUND"] * 5


@pytest.mark.unit
def test_guide_request_requires_non_empty_directive() -> None:
    with pytest.raises(ValidationError):
        WorkspaceGuideRequest()  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        WorkspaceGuideRequest(directive="")
    with pytest.raises(ValidationError):
        WorkspaceGuideRequest(directive="   ")

    request = WorkspaceGuideRequest(directive="implement, do not defer")
    assert request.directive == "implement, do not defer"
    assert request.reason is None
