"""Shared URL helpers."""

from __future__ import annotations

import urllib.parse


def normalize_api_url(base_url: str, path: str) -> str:
    """Build an API URL while avoiding duplicate ``/v1`` path segments."""
    if not path.startswith("/v1/"):
        return f"{base_url.rstrip('/')}{path}"

    parsed_base = urllib.parse.urlsplit(base_url)
    base_path = (parsed_base.path or "").rstrip("/")
    if base_path.endswith("/v1"):
        base_path = base_path.removesuffix("/v1")
    base_path = base_path.rstrip("/")
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
