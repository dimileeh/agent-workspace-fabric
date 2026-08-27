"""Hosted delegation configuration resolution."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from awf.common.config import Settings

HOSTED_DELEGATION_MISSING_BASE_URL = "AWF_HOSTED_DELEGATION_BASE_URL"
HOSTED_DELEGATION_MISSING_TOKEN = (
    "AWF_HOSTED_DELEGATION_BEARER_TOKEN or AWF_HOSTED_DELEGATION_BEARER_TOKEN_ENV"
)


class HostedDelegationConfigError(ValueError):
    """Raised when hosted mode is requested without complete delegation settings."""

    def __init__(self, *, missing: tuple[str, ...]) -> None:
        """Capture missing hosted delegation settings for structured diagnostics."""

        self.missing = missing
        super().__init__("Hosted delegation is not configured.")

    def detail(self) -> dict[str, list[str]]:
        """Return a JSON-serializable summary of missing config fields."""

        return {"missing": list(self.missing)}


@dataclass(frozen=True, slots=True)
class HostedDelegationConfig:
    """Resolved hosted delegation settings with secret values kept in memory only."""

    base_url: str
    bearer_token: str
    poll_interval_seconds: float
    operation_timeout_seconds: float
    request_timeout_seconds: float
    cancel_timeout_seconds: float
    max_output_bytes: int

    def redacted_payload(self) -> dict[str, Any]:
        """Return a secret-free diagnostic/config projection."""

        return {
            "base_url": self.base_url,
            "bearer_token": "<redacted>",
            "poll_interval_seconds": self.poll_interval_seconds,
            "operation_timeout_seconds": self.operation_timeout_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
            "cancel_timeout_seconds": self.cancel_timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
        }


def hosted_delegation_config_from_settings(
    settings: Settings,
    *,
    environ: Mapping[str, str] | None = None,
) -> HostedDelegationConfig:
    """Resolve hosted delegation config or raise a redacted diagnostic error."""

    return hosted_delegation_config_from_values(
        base_url=settings.hosted_delegation_base_url,
        bearer_token=settings.hosted_delegation_bearer_token,
        bearer_token_env=settings.hosted_delegation_bearer_token_env,
        environ=environ,
        poll_interval_seconds=settings.hosted_delegation_poll_interval_seconds,
        operation_timeout_seconds=settings.hosted_delegation_operation_timeout_seconds,
        request_timeout_seconds=settings.hosted_delegation_request_timeout_seconds,
        cancel_timeout_seconds=settings.hosted_delegation_cancel_timeout_seconds,
        max_output_bytes=settings.hosted_delegation_max_output_bytes,
    )


def hosted_delegation_config_from_values(
    *,
    base_url: str | None,
    bearer_token: str | None,
    bearer_token_env: str | None = None,
    environ: Mapping[str, str] | None = None,
    poll_interval_seconds: float,
    operation_timeout_seconds: float,
    request_timeout_seconds: float,
    cancel_timeout_seconds: float,
    max_output_bytes: int,
) -> HostedDelegationConfig:
    """Resolve hosted delegation config from already-selected settings values."""

    env = os.environ if environ is None else environ
    missing: list[str] = []
    resolved_base_url = _normalized_url(base_url)
    if resolved_base_url is None:
        missing.append(HOSTED_DELEGATION_MISSING_BASE_URL)
    token = _normalized_secret(bearer_token)
    token_env = _normalized_env_name(bearer_token_env)
    if token is None and token_env is not None:
        token = _normalized_secret(env.get(token_env))
    if token is None:
        missing.append(HOSTED_DELEGATION_MISSING_TOKEN)
    if missing:
        raise HostedDelegationConfigError(missing=tuple(missing))
    assert resolved_base_url is not None
    assert token is not None
    return HostedDelegationConfig(
        base_url=resolved_base_url,
        bearer_token=token,
        poll_interval_seconds=poll_interval_seconds,
        operation_timeout_seconds=operation_timeout_seconds,
        request_timeout_seconds=request_timeout_seconds,
        cancel_timeout_seconds=cancel_timeout_seconds,
        max_output_bytes=max_output_bytes,
    )


def _normalized_url(value: str | None) -> str | None:
    """Return a validated HTTPS base URL without embedded credentials."""

    if value is None:
        return None
    normalized = value.strip().rstrip("/")
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.query or parsed.fragment:
        return None
    return normalized


def _normalized_secret(value: str | None) -> str | None:
    """Return a non-empty secret string or ``None`` when unset."""

    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalized_env_name(value: str | None) -> str | None:
    """Return a non-empty environment-variable name or ``None`` when unset."""

    if value is None:
        return None
    normalized = value.strip()
    return normalized or None
