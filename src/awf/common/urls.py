"""Shared URL helpers."""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterable

_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "token",
        "api_token",
        "access_token",
        "secret",
        "authorization",
        "api_key",
        "apikey",
        "key",
        "password",
        "passwd",
        "auth",
    }
)
_URL_SECRET_MARKER = "***"


def normalize_api_url(base_url: str, path: str) -> str:
    """Build an API URL while avoiding duplicate ``/v1`` path segments."""
    parsed_base = urllib.parse.urlsplit(base_url)
    base_path = (parsed_base.path or "").rstrip("/")
    if path.startswith("/v1/") and base_path.endswith("/v1"):
        base_path = base_path.removesuffix("/v1")
    normalized_path = f"{base_path}{path}" if base_path else path
    return urllib.parse.urlunsplit(
        (
            parsed_base.scheme,
            parsed_base.netloc,
            normalized_path,
            parsed_base.query,
            parsed_base.fragment,
        )
    )


def sanitize_request_url(url: str) -> str:
    """Redact secret-bearing request URL parts before operator-facing output."""
    parsed_url = urllib.parse.urlsplit(url)
    if not parsed_url.scheme or not parsed_url.netloc:
        return url

    sanitized_pairs = _sanitize_query_pairs(
        urllib.parse.parse_qsl(parsed_url.query, keep_blank_values=True)
    )
    query = urllib.parse.urlencode(sanitized_pairs, doseq=True)
    return urllib.parse.urlunsplit(
        (
            parsed_url.scheme,
            _sanitize_netloc(parsed_url.netloc),
            parsed_url.path,
            query,
            parsed_url.fragment,
        )
    )


def _sanitize_query_pairs(query_pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    sanitized_pairs: list[tuple[str, str]] = []
    for name, value in query_pairs:
        if name.lower() in _SENSITIVE_QUERY_KEYS:
            sanitized_pairs.append((name, _URL_SECRET_MARKER))
        else:
            sanitized_pairs.append((name, value))
    return sanitized_pairs


def _sanitize_netloc(netloc: str) -> str:
    if "@" not in netloc:
        return netloc
    _, host = netloc.rsplit("@", 1)
    return f"{_URL_SECRET_MARKER}@{host}"
