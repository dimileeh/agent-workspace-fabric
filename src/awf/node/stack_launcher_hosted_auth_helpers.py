"""Hosted stack authentication helpers kept separate from stack rendering."""

from __future__ import annotations

_GOOGLE_APPLICATION_CREDENTIALS = "GOOGLE_APPLICATION_CREDENTIALS"
_GOOGLE_APPLICATION_CREDENTIALS_DEFAULT_ADC_TARGET = (
    "/home/agent/.config/gcloud/application_default_credentials.json"
)


def hosted_google_application_credentials_target(
    agent_environment: tuple[tuple[str, str], ...],
) -> str | None:
    """Return a safe absolute target for a hosted Google ADC mount."""
    raw = dict(agent_environment).get(_GOOGLE_APPLICATION_CREDENTIALS)
    if raw is None:
        return None
    if raw in (
        f"${{{_GOOGLE_APPLICATION_CREDENTIALS}}}",
        f"${_GOOGLE_APPLICATION_CREDENTIALS}",
    ):
        # Hosted render-only cannot resolve executor-local ADC paths from Core.
        return _GOOGLE_APPLICATION_CREDENTIALS_DEFAULT_ADC_TARGET
    if "$" in raw or not raw.startswith("/"):
        return None
    return raw
