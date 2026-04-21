"""``awf`` CLI entrypoint.

Two command groups:

- ``awf serve``        — run the AWF API (FastAPI) process.
- ``awf workspace ...``— inspect and manage workspaces via the REST API.

Kept deliberately thin: each workspace subcommand is an httpx call whose
output is JSON by default, so other shell tooling can pipe to jq. Human-
friendly formatting is opt-in via ``--format pretty``.
"""

from __future__ import annotations

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
app.add_typer(workspace_app, name="workspace")


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


@workspace_app.command("create")
def workspace_create(
    repo_url: str = typer.Option(..., "--repo", help="Git URL."),
    task_title: str = typer.Option(..., "--title"),
    task_prompt: str = typer.Option(..., "--prompt"),
    branch_base: str = typer.Option("development", "--base"),
    agent: str = typer.Option("codex", "--agent"),
    test_commands: list[str] = typer.Option([], "--test", help="Repeatable."),
    requires_database: bool = typer.Option(False, "--with-db"),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Submit a workspace creation request."""
    body = {
        "repo_url": repo_url,
        "branch_base": branch_base,
        "task_title": task_title,
        "task_prompt": task_prompt,
        "agent": agent,
        "test_commands": test_commands,
        "requires_database": requires_database,
    }
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    response = _call(
        "POST",
        "/v1/workspaces",
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


if __name__ == "__main__":  # pragma: no cover - entry point
    app(prog_name="awf")
    sys.exit(0)
