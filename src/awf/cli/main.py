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
import re
import shutil
import subprocess
import sys
import traceback
import urllib.parse
from collections.abc import Iterable, Mapping
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import click
import httpx
import typer
import typer.rich_utils as typer_rich_utils
from click.core import ParameterSource
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.urls import normalize_api_url, sanitize_request_url
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, TaskClass, WorkspaceStatus
from awf.service.gc import WorkspaceGCComposeTeardownResult, WorkspaceGCWorktreeRemoveResult
from awf.service.logs import DEFAULT_LOG_TAIL, ServiceLogName
from awf.service.smoke import _PROFILE_MARKER_PATHS as _PROJECT_PROFILE_MARKER_PATHS

_DX_FIRST_PATH_HELP = """
For first-time users: the recommended first path is to run `awf init`
to verify prerequisites and bootstrap your local service stack, followed by
`awf init <path>` to prepare your project repository.
"""

_MUTATES_GLOBAL_HELP = """
Mutates: Local state (.env, .awf/), Docker Compose stacks, and Git/GitHub
via the async worker.
"""

_DX_HELP = "DX smoke proof: validate local service, profile, and PR path."
_PROVIDER_HELP = (
    "Repeatable provider strictness check: github, codex, claude_code, gemini, opencode, or docker."
)
_PROVIDER_HELP_PASSTHROUGH = (
    "Repeatable provider strictness check passed through to local "
    "service bootstrap: github, codex, claude_code, gemini, opencode, "
    "or docker."
)
_CONTROL_IDEMPOTENCY_KEY_HELP = (
    "Idempotency key for this mutating control. When omitted, generated and "
    "printed to stderr before the request; pass the same value again to safely "
    "retry after a timeout or dropped response."
)
_MIN_RICH_HELP_WIDTH = 80
_ENV_ASSIGNMENT_RE = re.compile(r"\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=")


class _EnvSeedMergeError(ValueError):
    """Raised when env seed merging cannot preserve dotenv semantics."""


class _MinRichHelpWidthCommand(typer.core.TyperCommand):
    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        configured_width = typer_rich_utils.MAX_WIDTH
        terminal_width = shutil.get_terminal_size(fallback=(_MIN_RICH_HELP_WIDTH, 24)).columns
        typer_rich_utils.MAX_WIDTH = max(configured_width or terminal_width, _MIN_RICH_HELP_WIDTH)
        try:
            super().format_help(ctx, formatter)
        finally:
            typer_rich_utils.MAX_WIDTH = configured_width


app = typer.Typer(
    name="awf",
    help=f"Agent Workspace Fabric — CLI operator surface.\n{_DX_FIRST_PATH_HELP}{_MUTATES_GLOBAL_HELP}",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

workspace_app = typer.Typer(
    help=f"Workspace lifecycle (create/inspect/destroy).\n{_DX_FIRST_PATH_HELP}"
)
profile_app = typer.Typer(help="Workspace profile inspection.")
service_app = typer.Typer(help="Local service operations.")
locks_app = typer.Typer(help="Owned-path reservation and overlap-risk visibility.")
operations_app = typer.Typer(help="Global operation history inspection.")
smoke_app = typer.Typer(help=_DX_HELP)
app.add_typer(workspace_app, name="workspace")
app.add_typer(profile_app, name="profile")
app.add_typer(service_app, name="service")
app.add_typer(locks_app, name="locks")
app.add_typer(operations_app, name="operations")
app.add_typer(smoke_app, name="smoke")


class OutputFormat(StrEnum):
    json = "json"
    pretty = "pretty"


_DEFAULT_BASE_URL = "http://localhost:8000"


def _request_context(response: httpx.Response) -> tuple[str | None, str | None]:
    try:
        request_obj = cast(object, response.request)
    except RuntimeError:
        return None, None
    if not isinstance(request_obj, httpx.Request):
        return None, None
    return request_obj.method, sanitize_request_url(str(request_obj.url))


def _base_url(override: str | None) -> str:
    return override or os.environ.get("AWF_CLI_BASE_URL", _DEFAULT_BASE_URL)


def _api_token_headers(override: str | None) -> dict[str, str]:
    token = override if override is not None else os.environ.get("AWF_API_TOKEN")
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _api_token_option() -> Any:
    return typer.Option(
        None,
        "--api-token",
        help="Bearer token override; defaults to AWF_API_TOKEN when set.",
    )


def _control_idempotency_key_option() -> Any:
    return typer.Option(None, "--idempotency-key", help=_CONTROL_IDEMPOTENCY_KEY_HELP)


def _control_headers(
    *,
    api_token: str | None,
    idempotency_key: str | None,
    if_match: str | None,
    action: str,
) -> dict[str, str]:
    generated = idempotency_key is None
    resolved_key = (
        idempotency_key if idempotency_key is not None else f"awf-cli-{action}-{uuid4().hex}"
    )
    if generated:
        typer.echo(f"Generated Idempotency-Key: {resolved_key}", err=True)
    headers = {
        **_api_token_headers(api_token),
        "Idempotency-Key": resolved_key,
    }
    if if_match is not None:
        headers["If-Match"] = if_match
    return headers


def _run_terminal_workspace_compose_teardown(
    candidate: Any,
) -> WorkspaceGCComposeTeardownResult:
    """Tear down the compose stack for a terminal workspace candidate.

    This keeps the `service gc` cleanup command consistent with operational
    expectations when a terminal workspace is being removed.
    """
    compose_file = getattr(candidate, "compose", None)
    compose_path = None if compose_file is None else getattr(compose_file, "path", None)
    candidate_compose_file_path = getattr(candidate, "compose_file_path", None)
    workspace_id = getattr(candidate, "workspace_id", None)
    compose_project_name = getattr(candidate, "compose_project_name", None)
    compose_project_name = compose_project_name if isinstance(compose_project_name, str) else None
    compose_file_path = (
        candidate_compose_file_path.expanduser()
        if isinstance(candidate_compose_file_path, Path)
        else (
            Path(candidate_compose_file_path).expanduser()
            if isinstance(candidate_compose_file_path, str)
            else compose_path
        )
    )
    if not isinstance(compose_file_path, Path) or not isinstance(workspace_id, str):
        return WorkspaceGCComposeTeardownResult(
            status="failed",
            reason_code="DOCKER_COMPOSE_DOWN_FAILED",
            error="candidate had unexpected workspace_id/compose shape",
        )
    if not compose_file_path.exists():
        return WorkspaceGCComposeTeardownResult(
            status="skipped",
            reason_code="NO_COMPOSE_STACK",
        )
    if compose_file_path.is_dir():
        candidate_compose_paths = (
            compose_file_path / "compose.yml",
            compose_file_path / "compose.yaml",
            compose_file_path / "docker-compose.yml",
            compose_file_path / "docker-compose.yaml",
        )
        compose_file_path = next(
            (path for path in candidate_compose_paths if path.exists()),
            None,
        )
        if compose_file_path is None:
            return WorkspaceGCComposeTeardownResult(
                status="failed",
                reason_code="DOCKER_COMPOSE_DOWN_FAILED",
                error="compose stack file not found",
            )
    compose_name = compose_project_name or f"awf_{workspace_id}"

    command = [
        "docker",
        "compose",
        "-p",
        compose_name,
        "-f",
        str(compose_file_path),
        "down",
        "--remove-orphans",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return WorkspaceGCComposeTeardownResult(
            status="failed",
            reason_code="DOCKER_COMPOSE_DOWN_FAILED",
            error=str(exc),
        )
    if result.returncode == 0:
        return WorkspaceGCComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )
    return WorkspaceGCComposeTeardownResult(
        status="failed",
        reason_code="DOCKER_COMPOSE_DOWN_FAILED",
        error=(result.stderr or result.stdout or "docker compose down failed")[:1000],
    )


async def _run_terminal_workspace_worktree_remove(
    candidate: Any,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> WorkspaceGCWorktreeRemoveResult:
    """Remove the git worktree for a terminal workspace candidate.

    This keeps the ``service gc`` cleanup command consistent with operational
    expectations: the GC path calls ``git worktree remove`` before issuing
    ``shutil.rmtree``, so stale worktree metadata in the bare mirror is
    cleaned up atomically.

    The caller must provide a shared ``session_factory`` so that a batch GC
    run does not open and close a fresh connection pool per candidate.
    """
    from awf.db.models import Workspace as WsModel
    from awf.node.git_manager import GitManager
    from awf.service.config import resolve_service_settings

    workspace_id = getattr(candidate, "workspace_id", None)
    if not isinstance(workspace_id, str):
        return WorkspaceGCWorktreeRemoveResult(
            status="skipped",
            reason_code="NO_WORKSPACE_ID",
        )
    async with session_factory() as session:
        workspace = await session.get(WsModel, workspace_id)
    if workspace is None or not workspace.repo_url:
        return WorkspaceGCWorktreeRemoveResult(
            status="skipped",
            reason_code="NO_REPO_URL",
        )
    settings = resolve_service_settings()
    git_manager = GitManager(Path(settings.work_dir).expanduser().resolve() / "git")
    try:
        await git_manager.remove_worktree(
            workspace_id=workspace_id,
            repo_url=workspace.repo_url,
        )
        return WorkspaceGCWorktreeRemoveResult(
            status="succeeded",
            reason_code="WORKTREE_REMOVE_SUCCEEDED",
        )
    except Exception as exc:
        return WorkspaceGCWorktreeRemoveResult(
            status="failed",
            reason_code="GIT_WORKTREE_REMOVE_FAILED",
            error="".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[:1000],
        )


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


def _emit_pretty_dict(d: dict[str, Any], *, prefix: str = "") -> None:
    for key in sorted(d.keys()):
        pretty_key = f"{prefix}.{key}" if prefix else key
        value = d[key]
        if isinstance(value, dict):
            _emit_pretty_dict(value, prefix=pretty_key)
            continue
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            for i, item in enumerate(value):
                _emit_pretty_dict(item, prefix=f"{pretty_key}[{i}]")
            continue
        typer.echo(f"  {pretty_key}: {value}")


def _emit_profile_preview_pretty(payload: dict[str, Any]) -> None:
    profile = _mapping_value(payload.get("profile"))
    profile_name = _text_value(profile.get("name"), "unknown")
    confidence = _text_value(profile.get("confidence"), "unknown")
    source = _text_value(profile.get("source"), "unknown")
    typer.echo(f"Profile: {profile_name}")
    typer.echo(f"Source: {source} ({confidence} confidence)")

    runtime = _profile_runtime_summary(profile)
    if runtime:
        typer.echo(f"Runtime: {runtime}")

    services = _profile_services_summary(profile)
    typer.echo(f"Services: {services or 'none declared'}")

    setup = _profile_phase_commands(profile, "setup")
    if setup:
        typer.echo(f"Setup: {'; '.join(setup)}")

    validation = _profile_phase_commands(profile, "validate")
    typer.echo(f"Validation: {'; '.join(validation) if validation else 'none declared'}")

    coverage = _mapping_value(_mapping_value(profile.get("validation")).get("coverage"))
    coverage_target = _profile_coverage_target(coverage)
    if coverage_target is not None:
        target, fractional = coverage_target
        typer.echo(f"Coverage target: {_format_coverage_target(target, fractional=fractional)}")

    network_posture_value = payload.get("network_posture")
    if isinstance(network_posture_value, Mapping):
        network_posture = _mapping_value(network_posture_value)
        status = _text_value(network_posture.get("status"), "unknown")
        reason = _text_value(network_posture.get("reason"), "")
        typer.echo(f"Network posture: {status}{f' ({reason})' if reason else ''}")
    else:
        network_posture_status = _text_value(network_posture_value, "")
        if network_posture_status:
            typer.echo(f"Network posture: {network_posture_status}")

    findings = _list_value(payload.get("lint_findings"))
    if findings:
        typer.echo(f"Profile lint: {len(findings)} finding(s)")
        for finding in findings[:3]:
            if not isinstance(finding, Mapping):
                continue
            severity = _text_value(finding.get("severity"), "info")
            message = _text_value(finding.get("message"), str(finding))
            typer.echo(f"  - [{severity}] {message}")
        if len(findings) > 3:
            typer.echo(f"  - ... {len(findings) - 3} more")
    else:
        typer.echo("Profile lint: clean")

    reason = _text_value(payload.get("reason"), "")
    if reason:
        typer.echo(f"Reason: {reason}")

    typer.echo(
        "Next: awf init <path> --include-smoke-request; "
        "awf smoke run --mocked-local --format pretty"
    )


def _emit_smoke_pretty(payload: dict[str, Any]) -> None:
    status = _text_value(payload.get("status"), "unknown")
    mode = _text_value(payload.get("mode"), "unknown")
    project = _text_value(payload.get("project"), "unknown")
    typer.echo(f"AWF smoke: {status}")
    typer.echo(f"Project: {project}")
    typer.echo(f"Mode: {mode}")

    console_links = _mapping_value(payload.get("console_links"))
    if console_links:
        ui = _text_value(console_links.get("ui"), "")
        api_docs = _text_value(console_links.get("api_docs"), "")
        if ui:
            typer.echo(f"Console: {ui}")
        if api_docs:
            typer.echo(f"API docs: {api_docs}")

    phases = _list_value(payload.get("phases"))
    if phases:
        typer.echo("")
        typer.echo("Phases:")
        for phase in phases:
            if not isinstance(phase, Mapping):
                continue
            phase_status = _text_value(phase.get("status"), "unknown")
            name = _text_value(phase.get("name"), "unknown")
            message = _text_value(phase.get("message"), "")
            reason = _text_value(phase.get("reason_code"), "")
            header = f"  [{phase_status}] {name}"
            typer.echo(f"{header}: {message}" if message else header)
            if reason:
                typer.echo(f"        reason: {reason}")
            action = _text_value(phase.get("action"), "")
            if action and action not in {"No action required.", "none"}:
                typer.echo(f"        action: {action}")

    next_actions = [
        str(action)
        for action in _list_value(payload.get("next_actions"))
        if str(action) and str(action) != "No action required."
    ]
    if next_actions:
        typer.echo("")
        typer.echo("Next actions:")
        for action in next_actions:
            typer.echo(f"  - {action}")


def _profile_runtime_summary(profile: Mapping[str, object]) -> str:
    runtime = _mapping_value(profile.get("runtime"))
    if not runtime:
        return ""
    parts: list[str] = []
    for key in ("image", "dockerfile", "base_image"):
        value = runtime.get(key)
        if value:
            parts.append(f"{key}={value}")
    if parts:
        return " ".join(parts)
    for key, value in sorted(runtime.items()):
        if value in (None, "", (), [], {}):
            continue
        if isinstance(value, Mapping):
            parts.append(f"{key}={len(value)} value(s)")
        elif isinstance(value, list | tuple):
            parts.append(f"{key}={len(value)} item(s)")
        else:
            parts.append(f"{key}={value}")
    return " ".join(parts) if parts else "default"


def _profile_services_summary(profile: Mapping[str, object]) -> str:
    services = _list_value(profile.get("services"))
    names: list[str] = []
    for service in services:
        if isinstance(service, Mapping):
            names.append(_text_value(service.get("name"), "unnamed"))
        else:
            names.append(str(service))
    return ", ".join(names)


def _profile_phase_commands(profile: Mapping[str, object], phase_name: str) -> list[str]:
    phases = _mapping_value(profile.get("phases"))
    commands = _list_value(phases.get(phase_name))
    rendered: list[str] = []
    for command in commands:
        if isinstance(command, Mapping):
            value = command.get("command")
            if isinstance(value, str) and value:
                rendered.append(value)
        elif isinstance(command, str) and command:
            rendered.append(command)
    return rendered


def _mapping_value(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list_value(value: object) -> list[object]:
    return list(value) if isinstance(value, list | tuple) else []


def _text_value(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default


def _profile_coverage_target(coverage: Mapping[str, object]) -> tuple[object, bool] | None:
    minimum_percent = coverage.get("minimum_percent")
    if minimum_percent is not None:
        return (minimum_percent, False) if _has_positive_coverage_target(minimum_percent) else None

    legacy_target = coverage.get("target")
    if legacy_target is None or not _has_positive_coverage_target(legacy_target):
        return None
    return legacy_target, True


def _has_positive_coverage_target(value: object) -> bool:
    if isinstance(value, int | float):
        return value > 0
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def _format_coverage_target(value: object, *, fractional: bool = False) -> str:
    if isinstance(value, int | float):
        percent = float(value)
        if fractional and 0 <= percent <= 1:
            percent *= 100
        return f"{percent:.1f}%"
    return str(value)


def _parse_json_option(flag: str, value: str) -> dict[str, Any]:
    """Parse a command-line JSON object option.

    Returns the parsed object for valid JSON maps; exits the CLI with status 2
    and prints an error when parsing fails or the payload is not an object.
    """
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        typer.echo(
            f"error: invalid value for {flag}; must be valid JSON: {exc}",
            err=True,
        )
        raise typer.Exit(code=2) from exc
    if not isinstance(parsed, dict):
        typer.echo(
            f"error: invalid value for {flag}; must be a JSON object",
            err=True,
        )
        raise typer.Exit(code=2)
    return parsed


def _call(method: str, path: str, *, base_url: str, **kwargs: Any) -> httpx.Response:
    url = normalize_api_url(base_url, path)
    try:
        return httpx.request(method, url, timeout=30.0, **kwargs)
    except httpx.RequestError as exc:
        typer.echo(
            f"error: could not reach AWF API at {sanitize_request_url(url)}: {exc}",
            err=True,
        )
        raise typer.Exit(code=2) from exc


def _handle_response(
    response: httpx.Response,
    fmt: OutputFormat,
    *,
    pretty_items: bool = False,
) -> None:
    method, request_url = _request_context(response)
    if response.status_code >= 400:
        if request_url is not None:
            method = method or "HTTP"
            typer.echo(
                f"error: {method} {request_url} -> HTTP {response.status_code}",
                err=True,
            )
        try:
            typer.echo(json.dumps(response.json(), indent=2), err=True)
        except ValueError:
            typer.echo(response.text, err=True)
        raise typer.Exit(code=1)
    if response.status_code == 204 or not response.content:
        return
    payload = response.json()
    if (
        pretty_items
        and fmt == OutputFormat.pretty
        and isinstance(payload, dict)
        and isinstance(payload.get("items"), list)
    ):
        _emit(payload["items"], fmt)
        return
    _emit(payload, fmt)


# ── Commands ─────────────────────────────────────────────────────────────


_DEFAULT_INIT_BOOTSTRAP_TIMEOUT_SECONDS = 180.0
_DEFAULT_INIT_BOOTSTRAP_POLL_INTERVAL_SECONDS = 2.0


@app.command(
    "init",
    help=f"Bootstrap AWF on this machine, or run local onboarding checks for a project path.\n{_DX_FIRST_PATH_HELP}",
)
def init(
    path: Path | None = typer.Argument(
        None,
        help=(
            "Path to a checked-out repository. Omit to bootstrap the local "
            "AWF service stack on this machine."
        ),
    ),
    include_smoke_request: bool = typer.Option(
        False,
        "--include-smoke-request",
        help="Include a smoke-workspace request payload (does not submit).",
    ),
    write_env: bool = typer.Option(
        True,
        "--write-env/--no-write-env",
        help=(
            "When bootstrapping the local service, seed the Compose env target "
            "if it is missing. Target path: docker/compose/.env. Uses existing "
            "`.env` values before example templates, and uses `.env` when "
            "Compose assets are unavailable. Has no effect in project-onboarding mode."
        ),
    ),
    timeout_seconds: float = typer.Option(
        _DEFAULT_INIT_BOOTSTRAP_TIMEOUT_SECONDS,
        "--timeout-seconds",
        min=0.0,
        help="Local service bootstrap: maximum time to wait for readiness.",
    ),
    poll_interval_seconds: float = typer.Option(
        _DEFAULT_INIT_BOOTSTRAP_POLL_INTERVAL_SECONDS,
        "--poll-interval-seconds",
        min=0.01,
        help="Local service bootstrap: seconds between readiness polls.",
    ),
    skip_agent_runtime_build: bool = typer.Option(
        False,
        "--skip-agent-runtime-build",
        help="Local service bootstrap: skip building the agent runtime image.",
    ),
    provider: list[str] = typer.Option(
        [],
        "--provider",
        help=_PROVIDER_HELP_PASSTHROUGH,
    ),
    fmt: OutputFormat = typer.Option(
        OutputFormat.pretty,
        "--format",
        help="Output format. JSON unlocks scripting; pretty is the default.",
    ),
) -> None:
    """Bootstrap the local AWF service or run project-onboarding checks."""
    if path is None:
        if include_smoke_request:
            typer.echo(
                "error: onboarding-only flag --include-smoke-request requires a "
                "project path; pass `awf init <path> --include-smoke-request`.",
                err=True,
            )
            raise typer.Exit(code=2)
        _run_init_service_bootstrap(
            write_env=write_env,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            skip_agent_runtime_build=skip_agent_runtime_build,
            providers=provider,
            fmt=fmt,
        )
        return

    ctx = click.get_current_context()

    def _explicit(name: str) -> bool:
        """Return whether a bootstrap-only option was explicitly supplied."""
        return ctx.get_parameter_source(name) == ParameterSource.COMMANDLINE

    bootstrap_only_flags: list[str] = []
    if _explicit("skip_agent_runtime_build"):
        bootstrap_only_flags.append("--skip-agent-runtime-build")
    if _explicit("provider"):
        bootstrap_only_flags.append("--provider")
    if _explicit("timeout_seconds"):
        bootstrap_only_flags.append("--timeout-seconds")
    if _explicit("poll_interval_seconds"):
        bootstrap_only_flags.append("--poll-interval-seconds")
    if _explicit("write_env"):
        bootstrap_only_flags.append("--write-env" if write_env else "--no-write-env")
    if _explicit("fmt"):
        bootstrap_only_flags.append("--format")
    if bootstrap_only_flags:
        typer.echo(
            "error: bootstrap-only flag(s) "
            f"{', '.join(bootstrap_only_flags)} require running `awf init` "
            "without a project path.",
            err=True,
        )
        raise typer.Exit(code=2)

    _run_init_project_onboarding(
        path,
        include_smoke_request=include_smoke_request,
    )


def _run_init_project_onboarding(
    path: Path,
    *,
    include_smoke_request: bool,
) -> None:
    from awf.profiles.onboarding import preview_project_onboarding
    from awf.service.config import (
        ServiceSettings,
        local_service_environ,
        resolve_service_settings,
    )
    from awf.service.doctor import collect_doctor_report, render_doctor_pretty
    from awf.service.doctor.models import DoctorReport
    from awf.service.status import collect_service_status

    repository = path.expanduser().resolve()

    if not repository.exists():
        typer.echo(f"error: project path does not exist: {repository}", err=True)
        raise typer.Exit(code=2)
    if not repository.is_dir():
        typer.echo(f"error: project path is not a directory: {repository}", err=True)
        raise typer.Exit(code=2)

    existing_profile_path = _existing_project_profile_path(repository)

    try:
        settings = resolve_service_settings()
        service_env = local_service_environ()
        service_name = getattr(settings, "service_name", "unknown")

        async def _collect_reports() -> tuple[dict[str, object], DoctorReport]:
            service_status_task = asyncio.create_task(
                collect_service_status(
                    settings,
                    strict_providers=frozenset(),
                    provider_environ=service_env,
                )
            )

            async def _collect_cached_service_status(
                settings: ServiceSettings,
                *,
                strict_providers: Iterable[str] | None = None,
                provider_environ: Mapping[str, str] | None = None,
            ) -> dict[str, object]:
                _ = settings, strict_providers, provider_environ
                try:
                    return await service_status_task
                except Exception as exc:
                    return {
                        "service": service_name,
                        "status": "fail",
                        "checks": {},
                        "agent_readiness": {"status": "fail"},
                        "detail": str(exc),
                    }

            service_status_result, doctor_report = await asyncio.gather(
                service_status_task,
                collect_doctor_report(
                    settings,
                    strict_providers=frozenset(),
                    provider_environ=service_env,
                    environ=service_env,
                    status_collector=_collect_cached_service_status,
                ),
                return_exceptions=True,
            )

            service_status: dict[str, object]
            if isinstance(service_status_result, BaseException):
                service_status = {
                    "service": service_name,
                    "status": "fail",
                    "checks": {},
                    "agent_readiness": {"status": "fail"},
                    "detail": str(service_status_result),
                }
            else:
                service_status = service_status_result
            if isinstance(doctor_report, BaseException):
                raise doctor_report

            return service_status, doctor_report

        service_status, doctor_report = asyncio.run(_collect_reports())
        preview = preview_project_onboarding(
            repository,
            include_smoke_request=include_smoke_request,
        )
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:
        typer.echo(f"error: could not collect local checks: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    doctor_status = getattr(doctor_report, "status", "unknown")
    service_ok = service_status.get("status") == "ok"
    doctor_ok = doctor_status != "fail"

    typer.echo("AWF init: local onboarding readiness check")
    typer.echo(f"  repository: {repository}")
    typer.echo(f"  detected profile template: {preview.draft.template}")
    typer.echo(f"  service status: {service_status.get('status', 'unknown')}")
    typer.echo(f"  doctor status: {doctor_status}")
    typer.echo("")
    typer.echo(render_doctor_pretty(doctor_report), nl=False)

    if include_smoke_request and preview.smoke_request is not None:
        typer.echo("")
        typer.echo("Smoke request payload (local-only, not submitted):")
        typer.echo(json.dumps(preview.smoke_request, indent=2, sort_keys=True, default=str))

    typer.echo("")
    typer.echo("Suggested next steps:")
    if existing_profile_path is not None:
        typer.echo(f"  - AWF profile already exists: {existing_profile_path}")
        typer.echo(
            "  - Run `awf profile preview <path> --profile auto --format pretty` "
            "to inspect profile resolution."
        )
        typer.echo("  - Run `awf smoke run --mocked-local --format pretty` for a local DX proof.")
    else:
        typer.echo("  - Run `awf profile init <path> --write` to create `.awf/workspace.yml`.")
        typer.echo(
            "  - Run `awf profile preview <path> --profile <name> --format pretty` "
            "to inspect profile resolution."
        )
    typer.echo(
        "  - Optional: generate a smoke workspace request locally with "
        "`awf init <path> --include-smoke-request`; this prints the payload inline and does "
        "not submit a workspace."
    )

    if not service_ok or not doctor_ok:
        typer.echo(
            "\nLocal prerequisites are not fully ready yet; fix the issues above before "
            "creating or retrying workspaces."
        )
        raise typer.Exit(code=1)


def _existing_project_profile_path(repository: Path) -> Path | None:
    for relative_path in _PROJECT_PROFILE_MARKER_PATHS:
        candidate = repository / relative_path
        if candidate.is_file():
            return candidate
    return None


def _resolve_state_directory(env: Mapping[str, str]) -> Path:
    """Resolve the AWF host state directory matching the Compose default."""
    raw = env.get("AWF_HOST_WORK_DIR") or ""
    if raw:
        return Path(raw).expanduser().resolve()
    home = env.get("HOME", "~")
    return (Path(home) / ".awf" / "service").expanduser().resolve()


def _resolve_service_compose_paths() -> tuple[Path, Path, Path]:
    """Return the compose, env, and env seed source files used by service commands.

    If the verified source checkout contains local Compose assets, return those
    assets as absolute paths. Compose-specific examples are the seed base when
    present. Otherwise, the root example remains the seed base when it exists so
    template-only defaults are preserved; an existing root `.env` is applied as
    an overlay during seeding and remains the fallback read source until the
    compose `.env` exists.
    """

    from awf.service import bootstrap as bootstrap_mod
    from awf.service.config import LOCAL_SERVICE_COMPOSE_ENV_FILE, LOCAL_SERVICE_COMPOSE_FILE

    asset_root = bootstrap_mod.get_bootstrap_asset_root()
    if asset_root is not None:
        resolved_asset_root = asset_root.resolve()
        compose_local_service = resolved_asset_root / LOCAL_SERVICE_COMPOSE_FILE
        # get_bootstrap_asset_root() verifies this in production; keep the
        # guard so tests or stubs that bypass validation fall back to root .env.
        if not compose_local_service.is_file():
            return LOCAL_SERVICE_COMPOSE_FILE, Path(".env"), Path(".env.example")
        compose_file = compose_local_service
        compose_env = resolved_asset_root / LOCAL_SERVICE_COMPOSE_ENV_FILE
        root_env = resolved_asset_root / ".env"
        compose_example = compose_env.with_name(".env.example")
        fallback_example = resolved_asset_root / ".env.example"
        if compose_example.exists():
            return compose_file, compose_env, compose_example
        if fallback_example.exists():
            return compose_file, compose_env, fallback_example
        if root_env.exists():
            return compose_file, compose_env, root_env
        return compose_file, compose_env, fallback_example

    return LOCAL_SERVICE_COMPOSE_FILE, Path(".env"), Path(".env.example")


def _resolve_existing_service_env_file(
    env_file: Path,
    *,
    allow_current_compose_env_without_asset_root: bool = False,
) -> Path:
    """Return the existing env file service commands should read."""

    if env_file.exists():
        return env_file
    root_env = _compose_root_env_file(env_file)
    if root_env is not None and root_env.exists():
        return root_env
    if env_file == Path(".env"):
        compose_env = _resolve_existing_local_service_compose_env_file(
            allow_current_directory=allow_current_compose_env_without_asset_root,
        )
        if compose_env is not None:
            return compose_env
    return env_file


def _resolve_service_env_files(
    env_file: Path,
    *,
    allow_current_compose_env_without_asset_root: bool = False,
) -> tuple[Path, Path | None]:
    """Return the env read source and actual Compose env-file path."""

    active_env_file = _resolve_existing_service_env_file(
        env_file,
        allow_current_compose_env_without_asset_root=allow_current_compose_env_without_asset_root,
    )
    return active_env_file, _service_compose_env_file(active_env_file)


def _service_compose_env_file(active_env_file: Path) -> Path | None:
    """Return the env file that should be passed to Docker Compose, if any."""

    if not active_env_file.exists():
        return None
    if _is_local_service_compose_env_file(active_env_file):
        return active_env_file
    return None


def _is_local_service_compose_env_file(path: Path) -> bool:
    """Return true for docker/compose/.env paths discovered from the current tree."""

    return (
        path.name == ".env"
        and path.parent.name == "compose"
        and path.parent.parent.name == "docker"
    )


def _resolve_existing_local_service_compose_env_file(
    *,
    allow_current_directory: bool = False,
) -> Path | None:
    """Return the local Compose env file when it exists in an allowed location."""

    from awf.service import bootstrap as bootstrap_mod
    from awf.service.config import LOCAL_SERVICE_COMPOSE_ENV_FILE, LOCAL_SERVICE_COMPOSE_FILE

    asset_root = bootstrap_mod.get_bootstrap_asset_root()
    if asset_root is not None:
        resolved_asset_root = asset_root.resolve()
        compose_file = (
            LOCAL_SERVICE_COMPOSE_FILE
            if LOCAL_SERVICE_COMPOSE_FILE.is_absolute()
            else resolved_asset_root / LOCAL_SERVICE_COMPOSE_FILE
        )
        if not compose_file.is_file():
            return None
        compose_env = (
            LOCAL_SERVICE_COMPOSE_ENV_FILE
            if LOCAL_SERVICE_COMPOSE_ENV_FILE.is_absolute()
            else resolved_asset_root / LOCAL_SERVICE_COMPOSE_ENV_FILE
        )
        return compose_env if compose_env.exists() else None

    if not allow_current_directory:
        return None

    compose_file = LOCAL_SERVICE_COMPOSE_FILE
    compose_env = LOCAL_SERVICE_COMPOSE_ENV_FILE
    if not compose_file.is_file():
        return None
    if compose_env.is_absolute():
        return compose_env if compose_env.exists() else None
    return compose_env.resolve() if compose_env.exists() else None


def _init_env_error_payload(
    *,
    operation: str,
    path: Path,
    env_file: Path,
    env_example: Path,
    exc: Exception,
) -> dict[str, str]:
    """Return a machine-readable env seeding failure without env contents."""

    return {
        "operation": operation,
        "path": _init_display_path(path),
        "env_file": _init_display_path(env_file),
        "env_example": _init_display_path(env_example),
        "message": str(exc),
    }


def _compose_root_env_file(env_file: Path) -> Path | None:
    """Return the root `.env` paired with the local Compose env file."""

    if (
        env_file.name == ".env"
        and env_file.parent.name == "compose"
        and env_file.parent.parent.name == "docker"
    ):
        return env_file.parent.parent.parent / ".env"
    return None


def _init_env_overlay_source(env_file: Path, env_example: Path) -> Path | None:
    """Return the root `.env` overlay used when seeding compose env files."""

    root_env = _compose_root_env_file(env_file)
    if root_env is None or env_example == root_env or not root_env.exists():
        return None
    compose_example = env_file.with_name(".env.example")
    root_example = root_env.with_name(".env.example")
    if env_example not in (compose_example, root_example):
        return None
    return root_env


def _env_assignment_key(line: str) -> str | None:
    """Return the key from an env assignment line, ignoring comments."""

    if line.lstrip().startswith("#"):
        return None
    match = _ENV_ASSIGNMENT_RE.match(line)
    if match is None:
        return None
    return match.group("key")


def _env_value_has_same_line_closing_quote(value: str, quote: str) -> bool:
    """Return whether a quoted dotenv value closes on its assignment line."""

    escaped = False
    for char in value[1:]:
        if quote == '"' and char == "\\" and not escaped:
            escaped = True
            continue
        if char == quote and not escaped:
            return True
        escaped = False
    return False


def _env_contents_have_multiline_values(text: str) -> bool:
    """Return true when dotenv assignments span physical lines."""

    for line in text.splitlines(keepends=True):
        key = _env_assignment_key(line)
        if key is None:
            continue
        _assignment, _separator, value = line.partition("=")
        stripped_value = value.lstrip()
        if not stripped_value:
            continue
        quote = stripped_value[0]
        if quote in {"'", '"'} and not _env_value_has_same_line_closing_quote(
            stripped_value,
            quote,
        ):
            return True
    return False


def _env_context_looks_like_file_header(lines: list[str]) -> bool:
    """Return whether leading non-assignment lines look like a file header."""

    comment_count = 0
    for line in lines:
        stripped = line.strip()
        if stripped == "":
            return True
        if stripped.startswith("#"):
            comment_count += 1
    return comment_count > 1


def _merge_env_seed_contents_with_overlay_keys(
    seed_contents: bytes,
    overlay_contents: bytes,
) -> tuple[bytes, tuple[str, ...]]:
    """Return merged env contents plus root-only keys appended from the overlay."""

    try:
        seed_text = seed_contents.decode("utf-8")
        overlay_text = overlay_contents.decode("utf-8")
    except UnicodeDecodeError:
        return seed_contents, ()

    # This merge is deliberately line-oriented to preserve comments and ordering.
    # Multi-line dotenv values are unsupported in seed and overlay files; keep
    # template entries single-line unless this is replaced with a dotenv parser.
    if _env_contents_have_multiline_values(seed_text) or _env_contents_have_multiline_values(
        overlay_text
    ):
        raise _EnvSeedMergeError(
            "unsupported multi-line dotenv values; env seeding merge only supports "
            "single-line assignments"
        )
    seed_lines = seed_text.splitlines(keepends=True)
    overlay_lines = overlay_text.splitlines(keepends=True)

    overlay_assignments: dict[str, str] = {}
    for line in overlay_lines:
        key = _env_assignment_key(line)
        if key is not None:
            overlay_assignments[key] = line

    seed_keys: set[str] = set()
    for line in seed_lines:
        key = _env_assignment_key(line)
        if key is not None:
            seed_keys.add(key)
    seed_has_leading_context = bool(seed_lines and _env_assignment_key(seed_lines[0]) is None)

    overlay_last_assignment_index: dict[str, int] = {}
    for index, line in enumerate(overlay_lines):
        key = _env_assignment_key(line)
        if key is not None:
            overlay_last_assignment_index[key] = index

    seed_leading_context: dict[str, list[str]] = {}
    seed_trailing_context: dict[str, list[str]] = {}
    file_header_context: list[str] = []
    overlay_only_lines: list[str] = []
    overlay_only_keys: list[str] = []
    pending_context: list[str] = []
    last_assignment_key: str | None = None
    for index, line in enumerate(overlay_lines):
        key = _env_assignment_key(line)
        if key is None:
            pending_context.append(line)
            continue
        if (
            last_assignment_key is None
            and pending_context
            and _env_context_looks_like_file_header(pending_context)
        ):
            file_header_context = pending_context
            pending_context = []
        if overlay_last_assignment_index.get(key) == index:
            if key in seed_keys:
                if last_assignment_key is None and seed_has_leading_context:
                    pending_context = []
                seed_leading_context[key] = pending_context
            else:
                overlay_only_lines.extend(pending_context)
                overlay_only_lines.append(line)
                overlay_only_keys.append(key)
        pending_context = []
        last_assignment_key = key
    if pending_context and last_assignment_key is not None and last_assignment_key in seed_keys:
        seed_trailing_context[last_assignment_key] = pending_context
    elif pending_context:
        overlay_only_lines.extend(pending_context)

    merged_lines: list[str] = [] if seed_has_leading_context else list(file_header_context)
    emitted_seed_leading_context: set[str] = set()
    emitted_seed_trailing_context: set[str] = set()
    for line in seed_lines:
        key = _env_assignment_key(line)
        if key is None:
            merged_lines.append(line)
            continue
        if key in seed_leading_context and key not in emitted_seed_leading_context:
            merged_lines.extend(seed_leading_context[key])
            emitted_seed_leading_context.add(key)
        merged_lines.append(overlay_assignments.get(key, line))
        if key in seed_trailing_context and key not in emitted_seed_trailing_context:
            merged_lines.extend(seed_trailing_context[key])
            emitted_seed_trailing_context.add(key)

    if overlay_only_lines and merged_lines and not merged_lines[-1].endswith(("\n", "\r")):
        merged_lines[-1] = f"{merged_lines[-1]}\n"
    merged_lines.extend(overlay_only_lines)

    return "".join(merged_lines).encode("utf-8"), tuple(overlay_only_keys)


def _merge_env_seed_contents(seed_contents: bytes, overlay_contents: bytes) -> bytes:
    """Return compose-template env contents with root env assignments overlaid."""

    merged_contents, _overlay_only_keys = _merge_env_seed_contents_with_overlay_keys(
        seed_contents,
        overlay_contents,
    )
    return merged_contents


def _seed_env_file(
    env_file: Path,
    env_example: Path,
    *,
    env_overlay: Path | None = None,
) -> tuple[str, dict[str, str] | None, tuple[str, ...]]:
    """Seed an env file and return action, failure payload, and copied overlay keys."""

    if env_file.exists():
        return "kept_existing", None, ()

    if not env_example.exists():
        return "no_example", None, ()

    try:
        env_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return (
            "write_failed",
            _init_env_error_payload(
                operation="create_parent_directory",
                path=env_file.parent,
                env_file=env_file,
                env_example=env_example,
                exc=exc,
            ),
            (),
        )

    try:
        env_contents = env_example.read_bytes()
    except OSError as exc:
        return (
            "write_failed",
            _init_env_error_payload(
                operation="read_example",
                path=env_example,
                env_file=env_file,
                env_example=env_example,
                exc=exc,
            ),
            (),
        )

    env_overlay_keys: tuple[str, ...] = ()
    if env_overlay is not None and env_overlay.exists():
        try:
            overlay_contents = env_overlay.read_bytes()
        except OSError as exc:
            return (
                "write_failed",
                _init_env_error_payload(
                    operation="read_overlay",
                    path=env_overlay,
                    env_file=env_file,
                    env_example=env_example,
                    exc=exc,
                ),
                (),
            )
        try:
            env_contents, env_overlay_keys = _merge_env_seed_contents_with_overlay_keys(
                env_contents,
                overlay_contents,
            )
        except _EnvSeedMergeError as exc:
            return (
                "write_failed",
                _init_env_error_payload(
                    operation="merge_overlay",
                    path=env_overlay,
                    env_file=env_file,
                    env_example=env_example,
                    exc=exc,
                ),
                (),
            )

    try:
        with env_file.open("xb") as handle:
            handle.write(env_contents)
    except FileExistsError:
        return "kept_existing", None, ()
    except OSError as exc:
        return (
            "write_failed",
            _init_env_error_payload(
                operation="write_env",
                path=env_file,
                env_file=env_file,
                env_example=env_example,
                exc=exc,
            ),
            (),
        )

    return "wrote_from_example", None, env_overlay_keys


def _init_display_path(path: Path | str) -> str:
    """Return a stable human-readable init path from the launch directory."""

    candidate = Path(path)
    if not candidate.is_absolute():
        return str(candidate)
    try:
        return os.path.relpath(candidate, Path.cwd())
    except ValueError:
        return str(candidate)


def _init_env_warning(env_error: Mapping[str, str]) -> str:
    """Return the pretty warning for an env seeding failure payload."""

    operation = env_error["operation"]
    message = env_error["message"]
    env_file = env_error["env_file"]
    env_example = env_error["env_example"]
    if operation == "create_parent_directory":
        parent = env_error["path"]
        return f"  warning: could not create parent directory {parent} for {env_file}: {message}"
    if operation == "read_example":
        return f"  warning: could not read {env_example} while seeding {env_file}: {message}"
    if operation == "read_overlay":
        overlay = env_error["path"]
        return (
            f"  warning: could not read {overlay} while seeding {env_file} "
            f"from {env_example}: {message}"
        )
    if operation == "merge_overlay":
        overlay = env_error["path"]
        return (
            f"  warning: could not merge {overlay} while seeding {env_file} "
            f"from {env_example}: {message}"
        )
    return f"  warning: could not write {env_file} from {env_example}: {message}"


def _add_init_env_overlay_keys(
    payload: dict[str, object],
    env_overlay_keys: tuple[str, ...],
) -> None:
    """Add non-secret env overlay audit metadata to a JSON init payload."""

    if env_overlay_keys:
        payload["env_overlay_keys"] = list(env_overlay_keys)


def _init_env_example_search_paths(env_file: Path, env_example: Path) -> tuple[Path, ...]:
    """Return the env template paths that explain why init skipped seeding."""

    search_paths: list[Path] = []
    candidates = [env_file.with_name(".env.example"), env_example]
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        search_paths.append(candidate)
    return tuple(search_paths)


def _docker_diagnostic_from_report(report: object) -> object | None:
    """Return the docker diagnostic entry from a readiness report if present."""

    from typing import cast

    diagnostics = getattr(report, "diagnostics", ())
    for diagnostic in diagnostics:
        if getattr(diagnostic, "id", None) == "docker":
            return cast(object, diagnostic)
    return None


def _init_preflight_environ(
    environ: Mapping[str, str],
    *,
    provider_secret_keys: frozenset[str],
) -> dict[str, str]:
    """Return init preflight env without provider credentials."""

    secret_keys = {key.upper() for key in provider_secret_keys}
    return {key: value for key, value in environ.items() if key.upper() not in secret_keys}


def _run_init_service_bootstrap(
    *,
    write_env: bool,
    timeout_seconds: float,
    poll_interval_seconds: float,
    skip_agent_runtime_build: bool,
    providers: list[str],
    fmt: OutputFormat,
) -> None:
    """Run local-service bootstrap with environment seeding and status output."""
    from awf.common.config import Settings
    from awf.service.bootstrap import (
        ServiceBootstrapError,
        ServiceBootstrapOptions,
        run_service_bootstrap,
    )
    from awf.service.config import local_service_environ, resolve_service_settings
    from awf.service.doctor import collect_doctor_report
    from awf.service.provider_readiness import (
        KNOWN_SECRET_ENV_KEYS,
        ProviderReadinessError,
        validate_provider_names,
    )

    try:
        strict_providers = validate_provider_names(providers)
    except ProviderReadinessError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    pretty = fmt == OutputFormat.pretty
    compose_file, env_file, env_example = _resolve_service_compose_paths()
    env_action = "skipped"
    env_error: dict[str, str] | None = None
    env_overlay_keys: tuple[str, ...] = ()
    if write_env:
        env_action, env_error, env_overlay_keys = _seed_env_file(
            env_file,
            env_example,
            env_overlay=_init_env_overlay_source(env_file, env_example),
        )
    active_env_file, compose_env_file = _resolve_service_env_files(env_file)

    if pretty:
        typer.echo("AWF init: local service bootstrap")
        if write_env:
            if env_action == "write_failed" and env_error is not None:
                typer.echo(_init_env_warning(env_error))
            elif env_action == "kept_existing":
                typer.echo(f"  kept existing {_init_display_path(env_file)}")
            elif env_action == "wrote_from_example":
                typer.echo(
                    f"  wrote {_init_display_path(env_file)} from {_init_display_path(env_example)}"
                )
                if env_overlay_keys:
                    typer.echo(
                        "  added root .env keys to "
                        f"{_init_display_path(env_file)}: {', '.join(env_overlay_keys)}"
                    )
            elif env_action == "no_example":
                search_paths = ", ".join(
                    _init_display_path(path)
                    for path in _init_env_example_search_paths(env_file, env_example)
                )
                typer.echo(
                    "  no env template found; skipped "
                    f"{_init_display_path(env_file)} "
                    f"creation (looked for {search_paths}; run `awf init` from "
                    "the AWF repository root if you expected one)"
                )

    try:
        service_env = local_service_environ(env_file=active_env_file)
        preflight_env = _init_preflight_environ(
            service_env,
            provider_secret_keys=KNOWN_SECRET_ENV_KEYS,
        )
        settings = resolve_service_settings(
            Settings(_env_file=active_env_file, github_token=None),
            environ=preflight_env,
        )

        docker_report = asyncio.run(
            collect_doctor_report(
                settings,
                strict_providers=frozenset(),
                provider_environ=preflight_env,
                environ=preflight_env,
                compose_file=compose_file,
                compose_env_file=compose_env_file,
            )
        )
        docker_diag = _docker_diagnostic_from_report(docker_report)
        docker_status = getattr(docker_diag, "status", None)
        docker_unknown = docker_diag is None or docker_status is None
        if docker_unknown or docker_status == "fail":
            if docker_unknown:
                message = "Docker availability could not be determined from the doctor report."
                action = "Run `awf service doctor` to investigate the local environment."
                reason = "DOCKER_DIAGNOSTIC_MISSING"
            else:
                message = getattr(docker_diag, "message", "Docker is not available.")
                action = getattr(docker_diag, "action", "")
                reason = getattr(docker_diag, "reason", "DOCKER_DAEMON_UNREACHABLE")
            if pretty:
                typer.echo(f"  docker: {message}")
                if action:
                    typer.echo(f"  action: {action}")
                typer.echo("")
                typer.echo("Docker is not available; cannot bootstrap local service.")
            else:
                docker_payload: dict[str, object] = {
                    "status": "failed",
                    "reason_code": reason,
                    "message": message,
                    "action": action,
                    "env_action": env_action,
                }
                if env_error is not None:
                    docker_payload["env_error"] = env_error
                _add_init_env_overlay_keys(docker_payload, env_overlay_keys)
                _emit(docker_payload, fmt)
            raise typer.Exit(code=1)

        state_dir = _resolve_state_directory(service_env)
        created = not state_dir.exists()
        state_dir.mkdir(parents=True, exist_ok=True)
    except typer.Exit:
        raise
    except Exception as exc:
        if pretty:
            typer.echo(f"error: could not collect local checks: {exc}", err=True)
        else:
            local_checks_payload: dict[str, object] = {
                "status": "failed",
                "reason_code": "BOOTSTRAP_LOCAL_CHECKS_FAILED",
                "message": str(exc),
                "env_action": env_action,
            }
            if env_error is not None:
                local_checks_payload["env_error"] = env_error
            _add_init_env_overlay_keys(local_checks_payload, env_overlay_keys)
            _emit(local_checks_payload, fmt)
        raise typer.Exit(code=1) from exc

    if pretty:
        typer.echo(f"  state directory: {state_dir}")
        typer.echo(f"  created: {'true' if created else 'false'}")

    options = ServiceBootstrapOptions(
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        skip_agent_runtime_build=skip_agent_runtime_build,
        strict_providers=frozenset(strict_providers),
    )
    try:
        result = asyncio.run(
            run_service_bootstrap(
                settings,
                options=options,
                compose_file=compose_file,
                env_file=compose_env_file,
                service_environ=service_env,
            ),
        )
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    except ServiceBootstrapError as exc:
        payload = exc.to_dict()
        if env_error is not None:
            payload["env_error"] = env_error
        _add_init_env_overlay_keys(payload, env_overlay_keys)
        _emit(payload, fmt)
        raise typer.Exit(code=1) from None

    if pretty:
        typer.echo("  bootstrap status: ok")
        typer.echo("")
        typer.echo("Next steps:")
        typer.echo('  - export AWF_GITHUB_TOKEN="$(gh auth token)" so the worker can create PRs.')
        typer.echo("  - Run `awf service status --format pretty` to verify readiness.")
        typer.echo(
            "  - Optional console: `npm --prefix apps/console run dev`, then open http://localhost:3000."
        )
        typer.echo("  - Run `awf init <path>` to onboard a project repository.")
    else:
        payload = result.to_dict()
        payload["state_directory"] = str(state_dir)
        payload["state_directory_created"] = created
        payload["env_action"] = env_action
        if env_error is not None:
            payload["env_error"] = env_error
        _add_init_env_overlay_keys(payload, env_overlay_keys)
        _emit(payload, fmt)


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
    provider: list[str] = typer.Option(
        [],
        "--provider",
        help=_PROVIDER_HELP,
    ),
) -> None:
    """Check local AWF service dependencies."""
    from awf.common.config import Settings
    from awf.service.config import local_service_environ, resolve_service_settings
    from awf.service.provider_readiness import ProviderReadinessError, validate_provider_names
    from awf.service.status import collect_service_status

    try:
        strict_providers = validate_provider_names(provider)
    except ProviderReadinessError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    compose_file, env_file, _ = _resolve_service_compose_paths()
    env_file, compose_env_file = _resolve_service_env_files(
        env_file,
        allow_current_compose_env_without_asset_root=True,
    )
    service_env = local_service_environ(env_file=env_file)
    settings = resolve_service_settings(
        Settings(_env_file=env_file),
        environ=service_env,
    )
    payload = asyncio.run(
        collect_service_status(
            settings,
            strict_providers=strict_providers,
            provider_environ=service_env,
            compose_file=compose_file,
            compose_env_file=compose_env_file,
        )
    )
    _emit(payload, fmt)
    if payload.get("status") != "ok":
        raise typer.Exit(code=1)


@service_app.command("doctor")
def service_doctor(
    fmt: OutputFormat = typer.Option(OutputFormat.pretty, "--format"),
    provider: list[str] = typer.Option(
        [],
        "--provider",
        help=_PROVIDER_HELP,
    ),
    bundle: bool = typer.Option(
        False,
        "--bundle",
        help="Write a telemetry-free redacted support bundle to the current directory.",
    ),
) -> None:
    """Run operator-friendly local AWF diagnostics."""
    from awf.common.config import Settings
    from awf.service.config import local_service_environ, resolve_service_settings
    from awf.service.doctor import collect_doctor_report, render_doctor_pretty
    from awf.service.provider_readiness import ProviderReadinessError, validate_provider_names
    from awf.service.support_bundle import collect_support_bundle, write_support_bundle

    try:
        strict_providers = validate_provider_names(provider)
    except ProviderReadinessError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    compose_file, env_file, _ = _resolve_service_compose_paths()
    env_file, compose_env_file = _resolve_service_env_files(
        env_file,
        allow_current_compose_env_without_asset_root=True,
    )
    service_env = local_service_environ(env_file=env_file)
    settings = resolve_service_settings(
        Settings(_env_file=env_file),
        environ=service_env,
    )

    if bundle:
        bundle_payload = asyncio.run(
            collect_support_bundle(
                settings,
                strict_providers=strict_providers,
                provider_environ=service_env,
                environ=service_env,
                compose_file=compose_file,
                compose_env_file=compose_env_file,
            )
        )
        path = write_support_bundle(bundle_payload)
        if fmt == OutputFormat.json:
            _emit({"support_bundle_path": str(path)}, fmt)
        else:
            typer.echo(f"Support bundle written to: {path}")
        return

    report = asyncio.run(
        collect_doctor_report(
            settings,
            strict_providers=strict_providers,
            provider_environ=service_env,
            environ=service_env,
            compose_file=compose_file,
            compose_env_file=compose_env_file,
        )
    )

    if fmt == OutputFormat.json:
        _emit(report.to_dict(), fmt)
    else:
        typer.echo(render_doctor_pretty(report), nl=False)
        if report.status == "fail":
            typer.echo(
                "\nDiagnostics reported failures. To collect a safe support bundle, run:\n"
                "  awf service doctor --bundle\n"
                "\nFor bug reports, use the template at:\n"
                "  .github/ISSUE_TEMPLATE/bug_report.yml"
            )
    if report.status == "fail":
        raise typer.Exit(code=1)


@service_app.command("release-readiness")
@service_app.command("readiness")
def service_readiness(
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
    demo_path: Path | None = typer.Option(
        None,
        "--demo-path",
        help="Path to the maintained AWF Core golden-path demo project.",
    ),
    failure_window_hours: int = typer.Option(
        24,
        "--failure-window-hours",
        min=1,
        max=168,
        help="Recent failure-analysis window used by the release gate.",
    ),
    slo_window_hours: int = typer.Option(
        168,
        "--slo-window-hours",
        min=1,
        max=720,
        help="Rolling PRD SLO metrics window used by the release gate.",
    ),
    allow_generic_failures: bool = typer.Option(
        False,
        "--allow-generic-failures/--no-allow-generic-failures",
        help=(
            "Permit generic recent failure reasons in the scorecard. Use only with "
            "a written release rationale."
        ),
    ),
    allow_slo_breach: bool = typer.Option(
        False,
        "--allow-slo-breach/--no-allow-slo-breach",
        help=(
            "Permit PRD SLO threshold breaches in the scorecard. Use only with "
            "a written release rationale."
        ),
    ),
    provider: list[str] = typer.Option(
        [],
        "--provider",
        help=_PROVIDER_HELP,
    ),
) -> None:
    """Run the executable local AWF Core release-readiness gate."""
    from awf.common.config import Settings
    from awf.service.config import local_service_environ, resolve_service_settings
    from awf.service.provider_readiness import ProviderReadinessError, validate_provider_names
    from awf.service.readiness import (
        DEFAULT_DEMO_PATH,
        collect_core_readiness_report,
        render_core_readiness_pretty,
    )

    try:
        strict_providers = validate_provider_names(provider)
    except ProviderReadinessError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    compose_file, env_file, _ = _resolve_service_compose_paths()
    env_file, compose_env_file = _resolve_service_env_files(
        env_file,
        allow_current_compose_env_without_asset_root=True,
    )
    service_env = local_service_environ(env_file=env_file)
    settings = resolve_service_settings(
        Settings(_env_file=env_file),
        environ=service_env,
    )
    report = asyncio.run(
        collect_core_readiness_report(
            settings=settings,
            demo_path=demo_path if demo_path is not None else DEFAULT_DEMO_PATH,
            failure_window_hours=failure_window_hours,
            slo_window_hours=slo_window_hours,
            strict_providers=frozenset(strict_providers),
            provider_environ=service_env,
            environ=service_env,
            compose_file=compose_file,
            compose_env_file=compose_env_file,
            allow_generic_failures=allow_generic_failures,
            allow_slo_breach=allow_slo_breach,
        )
    )
    if fmt == OutputFormat.pretty:
        typer.echo(render_core_readiness_pretty(report), nl=False)
    else:
        _emit(report.to_dict(), fmt)
    if report.status == "fail":
        raise typer.Exit(code=1)


@service_app.command(
    "bootstrap",
    help=f"Start local Postgres, migrations, API, worker, and verify readiness.\n{_DX_FIRST_PATH_HELP}",
)
def service_bootstrap(
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
    timeout_seconds: float = typer.Option(
        180.0,
        "--timeout-seconds",
        min=0.0,
        help="Maximum time to wait for final service readiness.",
    ),
    poll_interval_seconds: float = typer.Option(
        2.0,
        "--poll-interval-seconds",
        min=0.01,
        help="Seconds between readiness polls.",
    ),
    skip_agent_runtime_build: bool = typer.Option(
        False,
        "--skip-agent-runtime-build",
        help="Skip building the configured AWF agent runtime image.",
    ),
    provider: list[str] = typer.Option(
        [],
        "--provider",
        help=_PROVIDER_HELP,
    ),
) -> None:
    """Start the local AWF service stack and emit structured bootstrap output."""

    from awf.common.config import Settings
    from awf.service.bootstrap import (
        ServiceBootstrapError,
        ServiceBootstrapOptions,
        run_service_bootstrap,
    )
    from awf.service.config import local_service_environ, resolve_service_settings
    from awf.service.provider_readiness import ProviderReadinessError, validate_provider_names

    try:
        strict_providers = validate_provider_names(provider)
    except ProviderReadinessError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    options = ServiceBootstrapOptions(
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        skip_agent_runtime_build=skip_agent_runtime_build,
        strict_providers=frozenset(strict_providers),
    )
    compose_file, env_file, _ = _resolve_service_compose_paths()
    env_file, compose_env_file = _resolve_service_env_files(
        env_file,
        allow_current_compose_env_without_asset_root=True,
    )
    service_env = local_service_environ(env_file=env_file)
    settings = resolve_service_settings(
        Settings(_env_file=env_file),
        environ=service_env,
    )
    try:
        result = asyncio.run(
            run_service_bootstrap(
                settings,
                options=options,
                compose_file=compose_file,
                env_file=compose_env_file,
                service_environ=service_env,
            )
        )
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    except ServiceBootstrapError as exc:
        _emit(exc.to_dict(), fmt)
        raise typer.Exit(code=1) from None

    _emit(result.to_dict(), fmt)


@service_app.command("config")
def service_config(
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Print resolved local service settings with secrets redacted."""
    from awf.service.config import resolve_service_settings, service_config_payload

    _emit(service_config_payload(resolve_service_settings()), fmt)


@service_app.command("logs")
def service_logs(
    tail: int = typer.Option(
        DEFAULT_LOG_TAIL,
        "--tail",
        min=0,
        help="Number of log lines to show per service.",
    ),
    service: list[ServiceLogName] = typer.Option(
        [],
        "--service",
        help="Repeatable service filter.",
    ),
    follow: bool = typer.Option(
        False,
        "--follow/--no-follow",
        help="Stream logs until interrupted.",
    ),
) -> None:
    """Tail local AWF service Compose logs."""
    from awf.service.config import local_service_environ
    from awf.service.logs import ServiceLogsError, run_service_logs

    compose_file, env_file, _ = _resolve_service_compose_paths()
    env_file, compose_env_file = _resolve_service_env_files(
        env_file,
        allow_current_compose_env_without_asset_root=True,
    )
    service_env = local_service_environ(env_file=env_file)
    try:
        result = run_service_logs(
            services=service,
            tail=tail,
            follow=follow,
            compose_file=compose_file,
            compose_env_file=compose_env_file,
            service_environ=service_env,
        )
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    except ServiceLogsError as exc:
        typer.echo(
            f"error: docker compose logs failed (exit {exc.returncode}): {exc.detail}",
            err=True,
        )
        raise typer.Exit(code=1) from None

    if result.stdout:
        typer.echo(result.stdout, nl=False)
    if result.stderr:
        typer.echo(result.stderr, err=True, nl=False)


@service_app.command("gc")
def service_gc(
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Delete selected worktree, compose, and auth directories. Defaults to dry-run.",
    ),
    min_age_hours: float | None = typer.Option(
        None,
        "--min-age-hours",
        "--retention-hours",
        min=0,
        help=(
            "Only consider workspaces whose last update is at least this old. "
            "Defaults to AWF_COMPLETED_WORKSPACE_RETENTION_HOURS."
        ),
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        min=1,
        help="Maximum number of candidates to plan, oldest first.",
    ),
    status: list[WorkspaceStatus] = typer.Option(
        [],
        "--status",
        help=(
            "Repeatable terminal status filter. Active statuses are always protected "
            "even when requested."
        ),
    ),
    exclude_status: list[WorkspaceStatus] = typer.Option(
        [],
        "--exclude-status",
        help="Repeatable status filter to remove from the eligible terminal set.",
    ),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Plan or execute filesystem GC for terminal service workspaces."""
    from awf.db.session import make_engine, make_session_factory
    from awf.service.config import resolve_service_settings
    from awf.service.gc import run_terminal_workspace_gc

    settings = resolve_service_settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    retention_hours = (
        settings.completed_workspace_retention_hours if min_age_hours is None else min_age_hours
    )
    candidate_limit = limit if limit is not None else settings.workspace_cleanup_batch_limit

    async def _run() -> object:
        try:
            result = await run_terminal_workspace_gc(
                session_factory,
                work_dir=Path(settings.work_dir).expanduser().resolve(),
                min_age_hours=retention_hours,
                limit=candidate_limit,
                include_statuses=status or None,
                exclude_statuses=exclude_status or None,
                execute=execute,
                cleanup_enabled=settings.workspace_cleanup_enabled,
                compose_teardown=_run_terminal_workspace_compose_teardown,
                worktree_remover=partial(
                    _run_terminal_workspace_worktree_remove,
                    session_factory=session_factory,
                ),
            )
            return result.to_dict()
        finally:
            await engine.dispose()

    payload = asyncio.run(_run())
    _emit(payload, fmt)
    if isinstance(payload, dict) and payload.get("status") == "partial":
        raise typer.Exit(code=1)


@service_app.command("reconcile-target")
def service_reconcile_target(
    repo_url: str = typer.Option(..., "--repo-url", help="Repository Git URL."),
    branch: str = typer.Option(
        "development",
        "--branch",
        help="Target branch to inspect and repair.",
    ),
    work_dir: Path | None = typer.Option(
        None,
        "--work-dir",
        help="Override AWF_WORK_DIR for target-branch checkout state.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Detect and render resolver output without committing or pushing.",
    ),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Run one target-branch reconciliation pass.

    The first resolver is Python/Alembic-specific: if the integrated branch
    has multiple Alembic heads, AWF writes and pushes a merge revision.
    """
    from awf.common.commands import AsyncioSubprocessRunner
    from awf.service.config import resolve_service_settings
    from awf.service.target_branch_monitor import (
        TargetBranchMonitorError,
        TargetBranchMonitorResult,
        run_target_branch_reconcile_once,
    )

    settings = resolve_service_settings()
    state_dir = (work_dir or Path(settings.work_dir)).expanduser().resolve()

    async def _run() -> TargetBranchMonitorResult:
        return await run_target_branch_reconcile_once(
            runner=AsyncioSubprocessRunner(),
            work_dir=state_dir,
            repo_url=repo_url,
            branch=branch,
            dry_run=dry_run,
        )

    try:
        result = asyncio.run(_run())
    except TargetBranchMonitorError as exc:
        payload = {
            "status": "failed",
            "operation": exc.operation,
            "returncode": exc.result.returncode,
            "stdout": exc.result.stdout,
            "stderr": exc.result.stderr,
        }
        _emit(payload, fmt)
        raise typer.Exit(code=1) from None

    _emit(result.to_dict(), fmt)


@workspace_app.command("create")
def workspace_create(
    repo_url: str = typer.Option(..., "--repo", help="Git URL."),
    task_title: str = typer.Option(..., "--title"),
    task_prompt: str = typer.Option(..., "--prompt"),
    branch_base: str = typer.Option("development", "--base"),
    agent: str = typer.Option("codex", "--agent"),
    model: str | None = typer.Option(None, "--model"),
    task_class: TaskClass | None = typer.Option(None, "--task-class"),
    priority: int | None = typer.Option(None, "--priority"),
    human_boost: int | None = typer.Option(None, "--human-boost"),
    out_of_scope_changes_json: str | None = typer.Option(
        None,
        "--out-of-scope-changes-json",
        "--out_of_scope_changes_json",
        help="JSON payload for task out_of_scope_changes policy.",
    ),
    provider_recovery_json: str | None = typer.Option(
        None,
        "--provider-recovery-json",
        "--provider_recovery_json",
        help="JSON payload for task provider-recovery policy.",
    ),
    owned_paths: list[str] | None = typer.Option(None, "--owned-path", help="Repeatable."),
    external_id: str | None = typer.Option(None, "--external-id"),
    cpu: float | None = typer.Option(None, "--cpu"),
    memory: str | None = typer.Option(None, "--memory"),
    steady_state_cpu_cores: float | None = typer.Option(None, "--steady-state-cpu-cores"),
    steady_state_memory_gb: float | None = typer.Option(None, "--steady-state-memory-gb"),
    peak_cpu_cores: float | None = typer.Option(None, "--peak-cpu-cores"),
    peak_memory_gb: float | None = typer.Option(None, "--peak-memory-gb"),
    disk_mb: int | None = typer.Option(None, "--disk-mb"),
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
    provider_readiness_override: bool = typer.Option(
        False,
        "--provider-readiness-override",
        help="Explicitly admit launch when selected provider readiness is not ready.",
    ),
    provider_readiness_override_reason: str | None = typer.Option(
        None,
        "--provider-readiness-override-reason",
        help="Audit reason for --provider-readiness-override.",
    ),
    idempotency_key: str | None = typer.Option(None, "--idempotency-key"),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Submit a workspace creation request."""
    body: dict[str, Any] = {
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
        "preflight": {
            "provider_readiness_override": provider_readiness_override,
            "provider_readiness_override_reason": provider_readiness_override_reason,
        },
    }

    if model is not None:
        body["task"]["model"] = model
    if task_class is not None:
        body["task"]["task_class"] = task_class.value
    if external_id is not None:
        body["task"]["external_id"] = external_id
    if priority is not None:
        body["task"]["priority"] = priority
    if human_boost is not None:
        body["task"]["human_boost"] = human_boost
    if out_of_scope_changes_json is not None:
        body["task"]["out_of_scope_changes"] = _parse_json_option(
            "--out-of-scope-changes-json",
            out_of_scope_changes_json,
        )
    if provider_recovery_json is not None:
        body["task"]["provider_recovery"] = _parse_json_option(
            "--provider-recovery-json",
            provider_recovery_json,
        )
    if owned_paths is not None:
        body["task"]["owned_paths"] = owned_paths

    if cpu is not None:
        body["resources"]["cpu"] = cpu
    if memory is not None:
        body["resources"]["memory"] = memory
    if steady_state_cpu_cores is not None:
        body["resources"]["steady_state_cpu_cores"] = steady_state_cpu_cores
    if steady_state_memory_gb is not None:
        body["resources"]["steady_state_memory_gb"] = steady_state_memory_gb
    if peak_cpu_cores is not None:
        body["resources"]["peak_cpu_cores"] = peak_cpu_cores
    if peak_memory_gb is not None:
        body["resources"]["peak_memory_gb"] = peak_memory_gb
    if disk_mb is not None:
        body["resources"]["disk_mb"] = disk_mb

    headers = _api_token_headers(api_token)
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
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
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Fetch the current state of one workspace."""
    response = _call(
        "GET",
        f"/v1/workspaces/{workspace_id}",
        base_url=_base_url(base_url),
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@workspace_app.command("retry")
def workspace_retry(
    workspace_id: str = typer.Argument(...),
    provider_readiness_override: bool = typer.Option(
        False,
        "--provider-readiness-override",
        help="Explicitly admit retry when selected provider readiness is not ready.",
    ),
    provider_readiness_override_reason: str | None = typer.Option(
        None,
        "--provider-readiness-override-reason",
        help="Audit reason for --provider-readiness-override.",
    ),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Retry a failed or cancelled workspace as a fresh attempt."""
    response = _call(
        "POST",
        f"/v1/workspaces/{workspace_id}/retry",
        base_url=_base_url(base_url),
        headers=_api_token_headers(api_token),
        params=(
            {
                "provider_readiness_override": provider_readiness_override,
                "provider_readiness_override_reason": provider_readiness_override_reason,
            }
            if provider_readiness_override or provider_readiness_override_reason is not None
            else None
        ),
    )
    _handle_response(response, fmt)


@workspace_app.command("remonitor")
def workspace_remonitor(
    workspace_id: str = typer.Argument(...),
    reason: str | None = typer.Option(None, "--reason", help="Operator audit reason."),
    idempotency_key: str | None = _control_idempotency_key_option(),
    if_match: str | None = typer.Option(
        None,
        "--if-match",
        help="Optional expected workspace version or ETag.",
    ),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Request PR monitor recovery for a monitoring workspace."""
    headers = _control_headers(
        api_token=api_token,
        idempotency_key=idempotency_key,
        if_match=if_match,
        action="remonitor",
    )
    response = _call(
        "POST",
        f"/v1/workspaces/{workspace_id}/remonitor",
        base_url=_base_url(base_url),
        json={"reason": reason},
        headers=headers,
    )
    _handle_response(response, fmt)


@workspace_app.command("cancel")
def workspace_cancel(
    workspace_id: str = typer.Argument(...),
    reason: str | None = typer.Option(None, "--reason", help="Operator audit reason."),
    stop_stack: bool = typer.Option(
        True,
        "--stop-stack/--no-stop-stack",
        help="Whether to stop workspace runtime resources before cancellation.",
    ),
    idempotency_key: str | None = _control_idempotency_key_option(),
    if_match: str | None = typer.Option(
        None,
        "--if-match",
        help="Optional expected workspace version or ETag.",
    ),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Request cancellation for one workspace."""
    headers = _control_headers(
        api_token=api_token,
        idempotency_key=idempotency_key,
        if_match=if_match,
        action="cancel",
    )
    response = _call(
        "POST",
        f"/v1/workspaces/{workspace_id}/cancel",
        base_url=_base_url(base_url),
        json={"reason": reason, "stop_stack": stop_stack},
        headers=headers,
    )
    _handle_response(response, fmt)


@workspace_app.command("stop")
def workspace_stop(
    workspace_id: str = typer.Argument(...),
    reason: str | None = typer.Option(None, "--reason", help="Operator audit reason."),
    idempotency_key: str | None = _control_idempotency_key_option(),
    if_match: str | None = typer.Option(
        None,
        "--if-match",
        help="Optional expected workspace version or ETag.",
    ),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Request stack stop for one workspace."""
    headers = _control_headers(
        api_token=api_token,
        idempotency_key=idempotency_key,
        if_match=if_match,
        action="stop",
    )
    response = _call(
        "POST",
        f"/v1/workspaces/{workspace_id}/stop",
        base_url=_base_url(base_url),
        json={"reason": reason},
        headers=headers,
    )
    _handle_response(response, fmt)


@workspace_app.command("destroy")
def workspace_destroy(
    workspace_id: str = typer.Argument(...),
    force: bool = typer.Option(
        False,
        "--force",
        help="Force destroy even when workspace state is active.",
    ),
    remove_volumes: bool = typer.Option(
        True,
        "--remove-volumes/--no-remove-volumes",
        help="Whether to remove workspace volumes.",
    ),
    remove_worktree: bool = typer.Option(
        True,
        "--remove-worktree/--no-remove-worktree",
        help="Whether to remove workspace worktree.",
    ),
    idempotency_key: str | None = _control_idempotency_key_option(),
    if_match: str | None = typer.Option(
        None,
        "--if-match",
        help="Optional expected workspace version or ETag.",
    ),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Request destruction of a workspace and optional related resources."""
    headers = _control_headers(
        api_token=api_token,
        idempotency_key=idempotency_key,
        if_match=if_match,
        action="destroy",
    )
    response = _call(
        "DELETE",
        f"/v1/workspaces/{workspace_id}",
        base_url=_base_url(base_url),
        params={
            "force": force,
            "remove_volumes": remove_volumes,
            "remove_worktree": remove_worktree,
        },
        headers=headers,
    )
    _handle_response(response, fmt)


@workspace_app.command("refresh")
def workspace_refresh(
    workspace_id: str = typer.Argument(...),
    reason: str | None = typer.Option(None, "--reason", help="Operator audit reason."),
    idempotency_key: str | None = _control_idempotency_key_option(),
    if_match: str | None = typer.Option(
        None,
        "--if-match",
        help="Optional expected workspace version or ETag.",
    ),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Trigger drift refresh for one workspace."""
    headers = _control_headers(
        api_token=api_token,
        idempotency_key=idempotency_key,
        if_match=if_match,
        action="refresh",
    )
    response = _call(
        "POST",
        f"/v1/workspaces/{workspace_id}/refresh",
        base_url=_base_url(base_url),
        json={"reason": reason},
        headers=headers,
    )
    _handle_response(response, fmt)


@workspace_app.command("validate")
def workspace_validate(
    workspace_id: str = typer.Argument(...),
    reason: str | None = typer.Option(None, "--reason", help="Operator audit reason."),
    requested_tier: int | None = typer.Option(
        None,
        "--requested-tier",
        min=1,
        max=3,
        help="Optional validation tier (1-3).",
    ),
    idempotency_key: str | None = _control_idempotency_key_option(),
    if_match: str | None = typer.Option(
        None,
        "--if-match",
        help="Optional expected workspace version or ETag.",
    ),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Request revalidation for one workspace."""
    headers = _control_headers(
        api_token=api_token,
        idempotency_key=idempotency_key,
        if_match=if_match,
        action="validate",
    )
    response = _call(
        "POST",
        f"/v1/workspaces/{workspace_id}/validate",
        base_url=_base_url(base_url),
        json={"reason": reason, "requested_tier": requested_tier},
        headers=headers,
    )
    _handle_response(response, fmt)


@workspace_app.command("rebase")
def workspace_rebase(
    workspace_id: str = typer.Argument(...),
    reason: str | None = typer.Option(None, "--reason", help="Operator audit reason."),
    idempotency_key: str | None = _control_idempotency_key_option(),
    if_match: str | None = typer.Option(
        None,
        "--if-match",
        help="Optional expected workspace version or ETag.",
    ),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Request workspace rebase onto the current target branch."""
    headers = _control_headers(
        api_token=api_token,
        idempotency_key=idempotency_key,
        if_match=if_match,
        action="rebase",
    )
    response = _call(
        "POST",
        f"/v1/workspaces/{workspace_id}/rebase",
        base_url=_base_url(base_url),
        json={"reason": reason},
        headers=headers,
    )
    _handle_response(response, fmt)


@workspace_app.command("adopt-pr", cls=_MinRichHelpWidthCommand)
def workspace_adopt_pr(
    repo: str | None = typer.Option(
        None,
        "--repo",
        help="GitHub repo slug or URL. Use with --pr.",
    ),
    pr_number: int | None = typer.Option(
        None,
        "--pr",
        min=1,
        help="Pull request number. Use with --repo.",
    ),
    pr_url: str | None = typer.Option(
        None,
        "--pr-url",
        help="Full GitHub pull request URL.",
    ),
    agent: str = typer.Option("codex", "--agent"),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Optional model override for the adopted PR monitor's selected agent.",
    ),
    effort: str | None = typer.Option(
        None,
        "--effort",
        help="Optional reasoning effort override for the adopted PR monitor.",
    ),
    profile_ref: str | None = typer.Option("auto", "--profile"),
    auto_merge: bool = typer.Option(
        True,
        "--auto-merge/--no-auto-merge",
        help="Allow the adopted PR monitor to merge when gates are green.",
    ),
    initial_review_grace_period_seconds: float | None = typer.Option(
        None,
        "--initial-review-grace-period-seconds",
        min=0,
        max=86400,
        help="Override profile monitor grace; omit to use the profile setting.",
    ),
    task_title: str | None = typer.Option(None, "--title"),
    task_prompt: str | None = typer.Option(None, "--prompt"),
    reason: str | None = typer.Option(None, "--reason", help="Operator audit reason."),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Adopt an already-open GitHub PR into AWF PR monitoring."""
    body = {
        "repo_url": repo if repo and "github.com" in repo else None,
        "repo_slug": repo if repo and "github.com" not in repo else None,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "agent": agent,
        "profile_ref": profile_ref,
        "profile": None,
        "auto_merge": auto_merge,
        "initial_review_grace_period_seconds": initial_review_grace_period_seconds,
        "task_title": task_title,
        "task_prompt": task_prompt,
        "reason": reason,
    }
    if model is not None:
        body["model"] = model
    if effort is not None:
        body["effort"] = effort
    response = _call(
        "POST",
        "/v1/workspaces/adopt-pr",
        base_url=_base_url(base_url),
        json=body,
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@workspace_app.command("list")
def workspace_list(
    status: list[WorkspaceStatus] | None = typer.Option(None, "--status"),
    agent: AgentRuntime | None = typer.Option(None, "--agent"),
    repo_url: str | None = typer.Option(None, "--repo-url"),
    limit: int = typer.Option(50, "--limit"),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """List workspaces (newest first)."""
    params_list: list[tuple[str, Any]] = [("limit", limit)]
    if status:
        for s in status:
            params_list.append(("status", s.value))
    if agent is not None:
        params_list.append(("agent", agent.value))
    if repo_url is not None:
        params_list.append(("repo_url", repo_url))

    response = _call(
        "GET",
        "/v1/workspaces",
        base_url=_base_url(base_url),
        params=params_list,
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@locks_app.command("list")
def locks_list(
    repo_url: str | None = typer.Option(None, "--repo-url"),
    task_class: TaskClass | None = typer.Option(None, "--task-class"),
    status: WorkspaceStatus | None = typer.Option(None, "--status"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """List workspace owned-path reservations and overlap risks."""
    params: dict[str, Any] = {"limit": limit}
    if repo_url is not None:
        params["repo_url"] = repo_url
    if task_class is not None:
        params["task_class"] = task_class.value
    if status is not None:
        params["status"] = status.value
    response = _call(
        "GET",
        "/v1/locks",
        base_url=_base_url(base_url),
        params=params,
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt, pretty_items=True)


@workspace_app.command("events")
def workspace_events(
    workspace_id: str = typer.Argument(...),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    event_type: str | None = typer.Option(None, "--event-type"),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """List immutable events for one workspace."""
    params: dict[str, Any] = {"limit": limit}
    if event_type is not None:
        params["event_type"] = event_type
    response = _call(
        "GET",
        f"/v1/workspaces/{workspace_id}/events",
        base_url=_base_url(base_url),
        params=params,
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@workspace_app.command("runtime")
def workspace_runtime(
    workspace_id: str = typer.Argument(...),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Fetch runtime/container state for one workspace."""
    response = _call(
        "GET",
        f"/v1/workspaces/{workspace_id}/runtime",
        base_url=_base_url(base_url),
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@workspace_app.command("operations")
def workspace_operations(
    workspace_id: str = typer.Argument(...),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    cursor: str | None = typer.Option(None, "--cursor", "--after"),
    status: OperationStatus | None = typer.Option(None, "--status"),
    operation_type: OperationType | None = typer.Option(None, "--type"),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """List operations for one workspace."""
    params: dict[str, Any] = {"limit": limit}
    if cursor is not None:
        params["cursor"] = cursor
    if status is not None:
        params["status"] = status.value
    if operation_type is not None:
        params["type"] = operation_type.value
    response = _call(
        "GET",
        f"/v1/workspaces/{workspace_id}/operations",
        base_url=_base_url(base_url),
        params=params,
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@operations_app.command("list")
def operations_list(
    workspace_id: str | None = typer.Option(None, "--workspace-id"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    cursor: str | None = typer.Option(None, "--cursor", "--after"),
    status: OperationStatus | None = typer.Option(None, "--status"),
    operation_type: OperationType | None = typer.Option(None, "--type"),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """List global operations, optionally filtered by workspace."""
    params: dict[str, Any] = {"limit": limit}
    if workspace_id is not None:
        params["workspace_id"] = workspace_id
    if cursor is not None:
        params["cursor"] = cursor
    if status is not None:
        params["status"] = status.value
    if operation_type is not None:
        params["type"] = operation_type.value
    response = _call(
        "GET",
        "/v1/operations",
        base_url=_base_url(base_url),
        params=params,
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@operations_app.command("show")
def operations_show(
    operation_id: str = typer.Argument(...),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Fetch one operation by id."""
    operation_ref = urllib.parse.quote(operation_id, safe="")
    response = _call(
        "GET",
        f"/v1/operations/{operation_ref}",
        base_url=_base_url(base_url),
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@workspace_app.command("logs")
def workspace_logs(
    workspace_id: str = typer.Argument(...),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """List durable log streams for one workspace."""
    response = _call(
        "GET",
        f"/v1/workspaces/{workspace_id}/logs",
        base_url=_base_url(base_url),
        headers=_api_token_headers(api_token),
    )
    _handle_response(response, fmt)


@workspace_app.command("log")
def workspace_log(
    workspace_id: str = typer.Argument(...),
    stream_id: str = typer.Argument(...),
    offset: int = typer.Option(0, "--offset", min=0),
    limit_bytes: int = typer.Option(65_536, "--limit-bytes", min=1, max=1_048_576),
    api_token: str | None = _api_token_option(),
    base_url: str | None = typer.Option(None, "--base-url"),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Read a bounded durable log chunk for one stream."""
    encoded_stream_id = urllib.parse.quote(stream_id, safe="")
    response = _call(
        "GET",
        f"/v1/workspaces/{workspace_id}/logs/{encoded_stream_id}",
        base_url=_base_url(base_url),
        params={"offset": offset, "limit_bytes": limit_bytes},
        headers=_api_token_headers(api_token),
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
    payload = resolution.model_dump(mode="json", by_alias=True)
    if fmt == OutputFormat.pretty:
        _emit_profile_preview_pretty(payload)
    else:
        _emit(payload, fmt)


@profile_app.command("init")
def profile_init(
    path: Path = typer.Argument(..., help="Path to the repository to inspect."),
    template: str = typer.Option("auto", "--template", help="Template override or auto."),
    write: bool = typer.Option(
        False,
        "--write",
        help="Write .awf/workspace.yml. Defaults to preview only.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing profile."),
    include_smoke_request: bool = typer.Option(
        False,
        "--include-smoke-request",
        help="Include an example workspace request body without launching it.",
    ),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
) -> None:
    """Inspect a project and preview or create a draft .awf/workspace.yml."""
    from awf.profiles.onboarding import preview_project_onboarding, write_workspace_profile

    try:
        preview = preview_project_onboarding(
            path.expanduser().resolve(),
            template=template,
            include_smoke_request=include_smoke_request,
        )
        payload = preview.to_dict()
        if write:
            written_path = write_workspace_profile(preview, force=force)
            payload["written_path"] = str(written_path)
    except FileExistsError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from None

    _emit(payload, fmt)


@smoke_app.command("run", help=_DX_HELP)
def smoke_run(
    project: Path = typer.Option(
        Path(),
        "--project",
        help="Path to the project to smoke.",
    ),
    fmt: OutputFormat = typer.Option(OutputFormat.json, "--format"),
    mocked_local: bool = typer.Option(
        False,
        "--mocked-local",
        help="Run in mocked-local mode without live external services.",
    ),
    demo_path: Path | None = typer.Option(
        None,
        "--demo-path",
        help="Fallback project path when --project has no profile.",
    ),
) -> None:
    from awf.service.config import resolve_service_settings
    from awf.service.smoke import collect_smoke_report

    resolved = project.expanduser().resolve()
    resolved_demo = demo_path.expanduser().resolve() if demo_path is not None else None
    settings = resolve_service_settings()

    report = asyncio.run(
        collect_smoke_report(
            project=resolved,
            settings=settings,
            mocked_local=mocked_local,
            demo_path=resolved_demo,
        )
    )
    if fmt == OutputFormat.pretty:
        _emit_smoke_pretty(report)
    else:
        _emit(report, fmt)
    if report["status"] == "fail":
        raise typer.Exit(code=1)


if __name__ == "__main__":  # pragma: no cover - entry point
    app(prog_name="awf")
    sys.exit(0)
