"""Shared provider/model failure classification.

The adapter layer and persisted-failure recovery both need the same vocabulary:
reason code, provider/model, retryability, short redacted fingerprint, and
operator guidance. Keep this module dependency-light so it can be used from
runtime, service, and metrics code without importing adapter implementations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from awf.common.redaction import redact_secrets

AGENT_AUTH_FAILED = "AGENT_AUTH_FAILED"
AGENT_PROVIDER_CAPACITY_EXHAUSTED = "AGENT_PROVIDER_CAPACITY_EXHAUSTED"
AGENT_TIMEOUT = "AGENT_TIMEOUT"
AGENT_IDLE_TIMEOUT = "AGENT_IDLE_TIMEOUT"

ProviderFailureType = Literal[
    "auth",
    "quota",
    "capacity",
    "usage_limit",
    "timeout",
    "idle_timeout",
]

_AUTH_FAILURE_MARKERS = (
    "not logged in",
    "please run /login",
    "please set an auth method",
    "manual authorization is required",
    "could not authenticate",
    "error authenticating",
    "invalid_grant",
    "anthropic_api_key",
    "gemini_api_key",
    "google_api_key",
    "google_genai_use_vertexai",
    "google_genai_use_gca",
    "ollama api key",
    "ollama cloud authentication",
    "opencode auth",
    "xai_api_key",
    "xai api key",
    "grok auth",
    "unauthorized",
)

_QUOTA_FAILURE_MARKERS = (
    "resource_exhausted",
    "retryablequotaerror",
    "http 429",
    "429 too many requests",
    " 429 ",
    "rate limit",
    "rate_limit",
    "quota exceeded",
    "quota exhausted",
)

_CAPACITY_FAILURE_MARKERS = (
    "model_capacity_exhausted",
    "capacity exhausted",
    "model capacity",
    "server overloaded",
    "temporarily overloaded",
)

_USAGE_LIMIT_MARKERS = (
    "usage limit",
    "hit your usage limit",
    "you've hit your usage limit",
    "switch to another model",
)

_GOOGLE_MARKERS = (
    "gemini",
    "google_api_key",
    "gemini_api_key",
    "resource_exhausted",
    "retryablequotaerror",
)
_OPENAI_MARKERS = ("codex", "openai", "gpt-", "o3", "o4")
_ANTHROPIC_MARKERS = ("claude", "anthropic", "sonnet", "opus", "haiku")
_OLLAMA_MARKERS = ("ollama",)
_XAI_MARKERS = ("grok", "xai", "grok-build")

_RETRY_AFTER_RE = re.compile(
    r"\bretry(?:-|\s*)after\b\s*(?:[:=]|\bin\b)?\s*(?P<seconds>\d{1,6})",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ProviderFailureClassification:
    reason_code: str
    failure_type: ProviderFailureType
    provider: str | None
    model: str | None
    retryable: bool
    retry_after_seconds: int | None
    cooldown_seconds: int | None
    failure_fingerprint: str
    recommended_action: str
    fallback_allowed: bool

    def to_metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "reason_code": self.reason_code,
            "failure_type": self.failure_type,
            "provider": self.provider,
            "model": self.model,
            "retryable": self.retryable,
            "retry_after_seconds": self.retry_after_seconds,
            "cooldown_seconds": self.cooldown_seconds,
            "failure_fingerprint": self.failure_fingerprint,
            "recommended_action": self.recommended_action,
            "fallback_allowed": self.fallback_allowed,
        }
        return {key: value for key, value in payload.items() if value is not None}


def classify_provider_failure(
    *,
    reason_code: str | None,
    stdout: str | None,
    stderr: str | None,
    provider: str | None,
    model: str | None,
) -> ProviderFailureClassification | None:
    """Classify a provider failure from reason code plus output fingerprints."""

    output = f"{stderr or ''}\n{stdout or ''}"
    normalized = output.lower()
    inferred_provider = _normalize_optional(provider) or infer_provider(
        model=model,
        output=output,
    )
    normalized_model = _normalize_optional(model)

    failure_type: ProviderFailureType | None = None
    normalized_reason = _normalize_optional(reason_code)
    if normalized_reason == AGENT_AUTH_FAILED:
        failure_type = "auth"
    elif normalized_reason == AGENT_PROVIDER_CAPACITY_EXHAUSTED:
        failure_type = _capacity_failure_type(normalized)
    elif normalized_reason == AGENT_TIMEOUT:
        failure_type = "timeout"
    elif normalized_reason == AGENT_IDLE_TIMEOUT:
        failure_type = "idle_timeout"
    elif any(marker in normalized for marker in _AUTH_FAILURE_MARKERS):
        failure_type = "auth"
    elif any(marker in normalized for marker in _USAGE_LIMIT_MARKERS):
        failure_type = "usage_limit"
    elif any(marker in normalized for marker in _CAPACITY_FAILURE_MARKERS):
        failure_type = "capacity"
    elif any(marker in normalized for marker in _QUOTA_FAILURE_MARKERS):
        failure_type = "quota"

    if failure_type is None:
        return None
    if failure_type in {"timeout", "idle_timeout"} and not (inferred_provider or normalized_model):
        return None

    final_reason_code = _reason_code_for_type(failure_type)
    retry_after_seconds = _retry_after_seconds(output)
    cooldown_seconds = retry_after_seconds or _default_cooldown_seconds(failure_type)
    fingerprint = failure_fingerprint(
        reason_code=final_reason_code,
        failure_type=failure_type,
        provider=inferred_provider,
        model=normalized_model,
        output=output,
    )
    return ProviderFailureClassification(
        reason_code=final_reason_code,
        failure_type=failure_type,
        provider=inferred_provider,
        model=normalized_model,
        retryable=True,
        retry_after_seconds=retry_after_seconds,
        cooldown_seconds=cooldown_seconds,
        failure_fingerprint=fingerprint,
        recommended_action=_recommended_action(failure_type),
        fallback_allowed=True,
    )


def infer_provider(*, model: str | None, output: str | None = None) -> str | None:
    text = f"{model or ''}\n{output or ''}".lower()
    if any(marker in text for marker in _GOOGLE_MARKERS):
        return "google"
    if any(marker in text for marker in _ANTHROPIC_MARKERS):
        return "anthropic"
    if any(marker in text for marker in _OLLAMA_MARKERS):
        return "ollama"
    if any(marker in text for marker in _XAI_MARKERS):
        return "xai"
    if any(marker in text for marker in _OPENAI_MARKERS):
        return "openai"
    return None


def failure_fingerprint(
    *,
    reason_code: str,
    failure_type: str,
    provider: str | None,
    model: str | None,
    output: str | None,
    limit: int = 360,
) -> str:
    redacted = redact_secrets(output or "")
    compact = _WHITESPACE_RE.sub(" ", redacted).strip().lower()
    if len(compact) > limit:
        compact = compact[:limit].rstrip()
    parts = [
        reason_code,
        failure_type,
        provider or "unknown-provider",
        model or "unknown-model",
        compact,
    ]
    return "|".join(parts)


def _capacity_failure_type(output: str) -> ProviderFailureType:
    if any(marker in output for marker in _USAGE_LIMIT_MARKERS):
        return "usage_limit"
    if any(marker in output for marker in _CAPACITY_FAILURE_MARKERS):
        return "capacity"
    return "quota"


def _reason_code_for_type(failure_type: ProviderFailureType) -> str:
    if failure_type == "auth":
        return AGENT_AUTH_FAILED
    if failure_type == "timeout":
        return AGENT_TIMEOUT
    if failure_type == "idle_timeout":
        return AGENT_IDLE_TIMEOUT
    return AGENT_PROVIDER_CAPACITY_EXHAUSTED


def _retry_after_seconds(output: str) -> int | None:
    match = _RETRY_AFTER_RE.search(output)
    if match is None:
        return None
    try:
        return int(match.group("seconds"))
    except ValueError:  # pragma: no cover - regex only matches digits
        return None


def _default_cooldown_seconds(failure_type: ProviderFailureType) -> int:
    if failure_type == "auth":
        return 900
    if failure_type in {"timeout", "idle_timeout"}:
        return 120
    return 300


def _recommended_action(failure_type: ProviderFailureType) -> str:
    if failure_type == "auth":
        return "Refresh provider credentials or dispatch an approved fallback provider."
    return "Retry after provider cooldown or dispatch an approved fallback model."


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
