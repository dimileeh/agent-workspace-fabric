"""Shared FastAPI response metadata."""

from __future__ import annotations

from typing import Any

from awf.api.schemas import ErrorResponse

WWW_AUTHENTICATE_HEADER: dict[str, Any] = {
    "WWW-Authenticate": {
        "description": "Bearer challenge for the API token.",
        "schema": {"type": "string"},
    }
}

API_TOKEN_UNAUTHORIZED_RESPONSE: dict[str, Any] = {
    "model": ErrorResponse,
    "description": "Unauthorized",
    "headers": WWW_AUTHENTICATE_HEADER,
}
SERVICE_UNAVAILABLE_ERROR_RESPONSE: dict[str, Any] = {
    "model": ErrorResponse,
    "description": "Service Unavailable",
}
API_TOKEN_AUTH_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: API_TOKEN_UNAUTHORIZED_RESPONSE,
    503: SERVICE_UNAVAILABLE_ERROR_RESPONSE,
}
