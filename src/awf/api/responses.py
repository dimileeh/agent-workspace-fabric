"""Shared FastAPI response metadata."""

from __future__ import annotations

from typing import Any

from awf.api.schemas import ErrorResponse, HttpExceptionErrorResponse

WWW_AUTHENTICATE_HEADER: dict[str, Any] = {
    "WWW-Authenticate": {
        "description": "Bearer challenge for the API token.",
        "schema": {"type": "string"},
    }
}
RETRY_AFTER_HEADER: dict[str, Any] = {
    "Retry-After": {
        "description": (
            "Backoff value clients should use before retrying; either delta-seconds "
            "or an HTTP-date as defined by RFC 7231."
        ),
        "schema": {"type": "string"},
    }
}

API_TOKEN_UNAUTHORIZED_RESPONSE: dict[str, Any] = {
    "model": HttpExceptionErrorResponse,
    "description": "Unauthorized",
    "headers": WWW_AUTHENTICATE_HEADER,
}
API_TOKEN_SERVICE_UNAVAILABLE_RESPONSE: dict[str, Any] = {
    "model": HttpExceptionErrorResponse,
    "description": "Service Unavailable",
}
SERVICE_UNAVAILABLE_ERROR_RESPONSE: dict[str, Any] = {
    "model": ErrorResponse,
    "description": "Service Unavailable",
}
RATE_LIMITED_ERROR_RESPONSE: dict[str, Any] = {
    "model": ErrorResponse,
    "description": "Too Many Requests",
    "headers": RETRY_AFTER_HEADER,
}
PROTECTED_SERVICE_UNAVAILABLE_ERROR_RESPONSE: dict[str, Any] = {
    "description": "Service Unavailable",
    "content": {
        "application/json": {
            "schema": {
                "anyOf": [
                    {"$ref": "#/components/schemas/HttpExceptionErrorResponse"},
                    {"$ref": "#/components/schemas/ErrorResponse"},
                ]
            }
        }
    },
}
API_TOKEN_AUTH_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: API_TOKEN_UNAUTHORIZED_RESPONSE,
    503: API_TOKEN_SERVICE_UNAVAILABLE_RESPONSE,
}
