"""Provider REST clients; model discovery never invokes an agent locally."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
import yaml

from awf.api.schemas_providers import ProviderConnectionTestRequest, ProviderQueryRequest
from awf.cli.common import OutputFormat, _base_url, _call, _handle_response
from awf.db.enums import AgentRuntime

provider_app = typer.Typer(
    help="Discover selected provider models and run isolated coding diagnostics."
)


def _profile(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    value = yaml.safe_load(path.read_text())
    return value.get("awf", value) if isinstance(value, dict) else None


@provider_app.command("models")
def models(
    agent: AgentRuntime = typer.Option(..., "--agent"),
    provider: str = typer.Option(..., "--provider"),
    profile_ref: str | None = typer.Option(None, "--profile-ref"),
    profile: Path | None = typer.Option(None, "--profile"),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    request = ProviderQueryRequest.model_validate(
        {
            "agent": agent,
            "provider": provider,
            "profile_ref": profile_ref,
            "profile": _profile(profile),
        }
    )
    _handle_response(
        _call(
            "POST",
            "/v1/providers/models/query",
            base_url=_base_url(base_url),
            json=request.model_dump(mode="json"),
        ),
        fmt,
    )


@provider_app.command("test")
def connection_test(
    agent: AgentRuntime = typer.Option(..., "--agent"),
    provider: str = typer.Option(..., "--provider"),
    model: str = typer.Option(..., "--model"),
    idempotency_key: str = typer.Option(..., "--idempotency-key"),
    effort: str | None = typer.Option(None, "--effort"),
    deadline_seconds: int = typer.Option(180, "--deadline-seconds", min=1, max=900),
    profile_ref: str | None = typer.Option(None, "--profile-ref"),
    profile: Path | None = typer.Option(None, "--profile"),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    request = ProviderConnectionTestRequest.model_validate(
        {
            "agent": agent,
            "provider": provider,
            "model": model,
            "effort": effort,
            "deadline_seconds": deadline_seconds,
            "profile_ref": profile_ref,
            "profile": _profile(profile),
        }
    )
    _handle_response(
        _call(
            "POST",
            "/v1/providers/connection-tests",
            base_url=_base_url(base_url),
            headers={"Idempotency-Key": idempotency_key},
            json=request.model_dump(mode="json"),
        ),
        fmt,
    )
