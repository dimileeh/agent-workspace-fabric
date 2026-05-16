"""Shared request auth-context markers for API helpers."""

from __future__ import annotations

from typing import Final

from starlette.requests import Request

VERIFIED_BEARER_AUTH_SCOPE_KEY: Final = "awf.api.auth.verified_bearer"


def mark_bearer_auth_verified(request: Request | object | None) -> None:
    """Mark a request after upstream bearer-token verification succeeds."""
    scope = _request_scope(request)
    if scope is not None:
        scope[VERIFIED_BEARER_AUTH_SCOPE_KEY] = True


def request_has_verified_bearer_auth(request: Request | object | None) -> bool:
    """Return whether upstream auth has verified this request's bearer identity."""
    scope = _request_scope(request)
    return scope is not None and scope.get(VERIFIED_BEARER_AUTH_SCOPE_KEY) is True


def _request_scope(request: Request | object | None) -> dict[str, object] | None:
    if request is None:
        return None
    scope = getattr(request, "scope", None)
    return scope if isinstance(scope, dict) else None
