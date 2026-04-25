"""``awf`` CLI entrypoint.

Two command groups:

- ``awf serve``        — run the AWF API (FastAPI) process.
- ``awf workspace ...``— inspect and manage workspaces via the REST API.

Kept deliberately thin: each workspace subcommand is an httpx call whose
output is JSON by default, so other shell tooling can pipe to jq. Human-
friendly formatting is opt-in via ``--format pretty``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from enum import StrEnum
from typing import Any

import httpx
import typer

app = typer.Typer(
    name="awf",
    help="Aira Agent Workspace Fabric — CLI operator surface.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

workspace_app = typer.Typer(help="Workspace lifecycle (create/inspect/destroy).")
profile_app = typer.Typer(help="Workspace profile inspection.")
service_app = typer.Typer(help="Local service operations.")
app.add_typer(workspace_app, name="workspace")
app.add_typer(profile_app, name="profile")
app.add_typer(service_app, name="service")


class OutputFormat(StrEnum):
    json = "json"
    pretty = "pretty"


_DEFAULT_BASE_URL = "http://localhost:8000"


def _base_url(override: str | None) -> str:
    return override or os.environ.get("AWF_CLI_BASE_URL", _DEFAULT_BASE_URL)


def _emit(payload: object, fmt: OutputFormat) -> None:
    if fmt == OutputFormat.json:
        typer.echo(json.dumps(payload, indent=2, default=str))
        return
    # "pretty" is a light human view — one line per key, sorted keys for
    # determinism. This is not a dashboard; it's the minimum that makes
    # copy-paste from a terminal readable.
    if isinstance(payload, list):
        for i, item in enumerate(payload):
            typer.echo(f"--- #{i + 1} ---")
            _emit_pretty_dict(item if isinstance(item, dict) else {"value": item})
        return
    if isinstance(payload, dict):
        _emit_pretty_dict(payload)
        return
    typer.echo(str(payload))


def _emit_pretty_dict(d: dict[str, Any]) -> None:
    for key in sorted(d.keys()):
        typer.echo(f"  {key}: {d[key]}")


def _call(method: str, path: str, *, base_url: str, **kwargs: Any) -> httpx.Response:
    url = f"{base_url.rstrip('/')}{path}"
    try:
        return httpx.request(method, url, timeout=30.0, **kwargs)
    except httpx.RequestError as exc:
        typer.echo(f"error: could not reach AWF API at {url}: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def _handle_response(response: httpx.Response, fmt: OutputFormat) -> None:
    if response.status_code >= 400:
        try:
            typer.echo(json.dumps(response.json(), indent=2), err=True)
        except ValueError:
            typer.echo(response.text, err=True)
        raise typer.Exit(code=1)
    if response.status_code == 204 or not response.content:
        return
    _emit(response.json(), fmt)


# ── Commands ─────────────────────────────────────────────────────────────


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", "-h"),  # noqa: S104
    port: int = typer.Option(8000, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Run the AWF API server (FastAPI under uvicorn)."""
    import uvicorn

    uvicorn.run(
        "awf.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
    )


@app.command("worker")
def worker(
    once: bool = typer.Option(
        False,
        "--once",
        help="Run one poll batch and exit.",
        hidden=True,
    ),
) -> None:
    """Run the AWF control worker."""
    from awf.service.config import resolve_service_settings
    from awf.service.worker import run_worker

    asyncio.run(run_worker(resolve_service_settings(), once=once))


@service_app.command("status")
def service_status(
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Check local AWF service dependencies."""
    from awf.service.config import resolve_service_settings
    from awf.service.status import collect_service_status

    payload = asyncio.run(collect_service_status(resolve_service_settings()))
    _emit(payload, fmt)
    if payload.get("status") != "ok":
        raise typer.Exit(code=1)


@service_app.command("config")
def service_config(
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Print resolved local service settings with secrets redacted."""
    from awf.service.config import resolve_service_settings, service_config_payload

    _emit(service_config_payload(resolve_service_settings()), fmt)


@workspace_app.command("create")
def workspace_create(
    repo_url: str = typer.Option(..., "--repo", help="Git URL."),
    task_title: str = typer.Option(..., "--title"),
    task_prompt: str = typer.Option(..., "--prompt"),
    branch_base: str = typer.Option("development", "--base"),
    agent: str = typer.Option("codex", "--agent"),
    profile_ref: str = typer.Option("auto", "--profile"),
    test_commands: list[str] = typer.Option([], "--test", help="Repeatable."),
    requires_database: bool = typer.Option(
        False,
        "--with-db",
        help="Deprecated v1 shortcut; selects the aira profile when set.",
    ),
    auto_merge: bool = typer.Option(
        True,
        "--auto-merge/--no-auto-merge",
        help="Allow the monitor to merge when PR gates are green.",
    ),
    initial_review_grace_period_seconds: float | None = typer.Option(
        None,
        "--initial-review-grace-period-seconds",
        min=0,
        max=86400,
        help="Override profile monitor grace; omit to use the profile setting.",
    ),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Submit a workspace creation request."""
    body = {
        "repo": {"url": repo_url, "base_branch": branch_base},
        "task": {
            "title": task_title,
            "prompt": task_prompt,
            "agent": agent,
            "kind": "feature_branch_pr",
            "auto_merge": auto_merge,
            "initial_review_grace_period_seconds": initial_review_grace_period_seconds,
        },
        "workspace": {"profile_ref": "aira" if requires_database else profile_ref, "profile": None},
        "validation": {"commands": test_commands, "requested_tier": 1},
        "resources": {},
    }
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    response = _call(
        "POST",
        "/v2/workspaces",
        base_url=_base_url(base_url),
        json=body,
        headers=headers,
    )
    _handle_response(response, fmt)


@workspace_app.command("show")
def workspace_show(
    workspace_id: str = typer.Argument(...),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Fetch the current state of one workspace."""
    response = _call("GET", f"/v1/workspaces/{workspace_id}", base_url=_base_url(base_url))
    _handle_response(response, fmt)


@workspace_app.command("list")
def workspace_list(
    limit: int = typer.Option(50, "--limit"),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """List workspaces (newest first)."""
    response = _call(
        "GET",
        "/v1/workspaces",
        base_url=_base_url(base_url),
        params={"limit": limit},
    )
    _handle_response(response, fmt)


@profile_app.command("preview")
def profile_preview(
    path: str = typer.Argument(..., help="Path to a checked-out repository."),
    profile_ref: str = typer.Option("auto", "--profile"),
    validation_command: list[str] = typer.Option([], "--validation-command", help="Repeatable."),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Preview the resolved workspace profile for a local checkout."""
    from pathlib import Path

    from awf.profiles.resolver import resolve_workspace_profile

    resolution = resolve_workspace_profile(
        worktree_path=Path(path).expanduser().resolve(),
        profile_ref=profile_ref,
        validation_commands=validation_command,
    )
    _emit(resolution.model_dump(mode="json", by_alias=True), fmt)


if __name__ == "__main__":  # pragma: no cover - entry point
    app(prog_name="awf")
    sys.exit(0)
