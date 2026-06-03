"""Shared CLI formatting and HTTP helpers."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import traceback
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, NoReturn, cast
from uuid import uuid4

import httpx
import typer
import typer.rich_utils as typer_rich_utils
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.logging import get_logger
from awf.common.urls import normalize_api_url, sanitize_request_url
from awf.service.gc import WorkspaceGCComposeTeardownResult, WorkspaceGCWorktreeRemoveResult

_log = get_logger(__name__)

_DEFAULT_BASE_URL = "http://localhost:8000"
_DEPRECATED_CLI_BASE_URL_NOTICE = "AWF_CLI_BASE_URL is deprecated; use AWF_BASE_URL"
_CONTROL_IDEMPOTENCY_KEY_HELP = (
    "Idempotency key for this mutating control. When omitted, generated and "
    "printed to stderr before the request; pass the same value again to safely "
    "retry after a timeout or dropped response."
)
_MIN_RICH_HELP_WIDTH = 80
_cli_base_url_deprecation_notice_emitted = False


class OutputFormat(StrEnum):
    """Output format enumeration."""

    json = "json"
    pretty = "pretty"


class MinRichHelpWidthCommand(typer.core.TyperCommand):
    """Typer command class with a wider rich help minimum."""

    def format_help(self, ctx: Any, formatter: Any) -> None:
        """Render rich help with a stable minimum width."""
        configured_width = typer_rich_utils.MAX_WIDTH
        terminal_width = shutil.get_terminal_size(fallback=(_MIN_RICH_HELP_WIDTH, 24)).columns
        typer_rich_utils.MAX_WIDTH = max(configured_width or terminal_width, _MIN_RICH_HELP_WIDTH)
        try:
            super().format_help(ctx, formatter)
        finally:
            typer_rich_utils.MAX_WIDTH = configured_width


def _request_context(response: httpx.Response) -> tuple[str | None, str | None]:
    """Get request context."""
    try:
        request_obj = cast(object, response.request)
    except RuntimeError:
        return None, None
    if not isinstance(request_obj, httpx.Request):
        return None, None
    return request_obj.method, sanitize_request_url(str(request_obj.url))


def _base_url(override: str | None) -> str:
    """Get the host-reachable AWF API base URL for CLI HTTP calls."""
    if override:
        return override
    base_url = os.environ.get("AWF_BASE_URL")
    if base_url:
        return base_url
    deprecated_base_url = os.environ.get("AWF_CLI_BASE_URL")
    if deprecated_base_url:
        _emit_deprecated_cli_base_url_notice()
        return deprecated_base_url
    host_port = os.environ.get("AWF_API_HOST_PORT")
    if host_port:
        return f"http://localhost:{_parse_api_host_port(host_port)}"
    return _DEFAULT_BASE_URL


def _parse_api_host_port(host_port: str) -> int:
    """Parse the CLI's host API port override."""

    stripped_port = host_port.strip()
    if not stripped_port.isdecimal():
        _exit_invalid_api_host_port(host_port)
    parsed_port = int(stripped_port)
    if not 1 <= parsed_port <= 65535:
        _exit_invalid_api_host_port(host_port)
    return parsed_port


def _exit_invalid_api_host_port(host_port: str) -> NoReturn:
    """Exit with a CLI usage error for invalid host API port overrides."""
    typer.echo(
        f"error: AWF_API_HOST_PORT must be an integer between 1 and 65535; got {host_port!r}",
        err=True,
    )
    raise typer.Exit(code=2)


def _emit_deprecated_cli_base_url_notice() -> None:
    """Warn once per process when the legacy CLI base URL variable is used."""
    global _cli_base_url_deprecation_notice_emitted

    if _cli_base_url_deprecation_notice_emitted:
        return
    typer.echo(_DEPRECATED_CLI_BASE_URL_NOTICE, err=True)
    _cli_base_url_deprecation_notice_emitted = True


def _api_token_headers(override: str | None) -> dict[str, str]:
    """Get API token headers."""
    token = override if override is not None else os.environ.get("AWF_API_TOKEN")
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _api_token_option() -> Any:
    """Get API token option."""
    return typer.Option(
        None,
        "--api-token",
        help="Bearer token override; defaults to AWF_API_TOKEN when set.",
    )


def _control_idempotency_key_option() -> Any:
    """Execute control idempotency key option."""

    return typer.Option(None, "--idempotency-key", help=_CONTROL_IDEMPOTENCY_KEY_HELP)


def _control_headers(
    *,
    api_token: str | None,
    idempotency_key: str | None,
    if_match: str | None,
    action: str,
) -> dict[str, str]:
    """Execute control headers."""

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
    expectations when a terminal workspace is being removed. When the stack is
    down (or there is none), the workspace's Claude auth overlay is unmounted
    *before* GC removes the auth dir — a busy overlay mount would otherwise make
    GC's ``rmtree`` fail with ``EBUSY`` and strand the per-workspace upper.
    """
    result = _compose_down_for_terminal_workspace(candidate)
    if result.ok:
        _teardown_terminal_workspace_auth_overlay(candidate)
    return result


def _teardown_terminal_workspace_auth_overlay(candidate: Any) -> None:
    """Unmount the candidate's Claude auth overlay before GC removes its dir."""
    from awf.node.auth_mounts import teardown_workspace_auth_overlay
    from awf.service.config import resolve_service_settings

    workspace_id = getattr(candidate, "workspace_id", None)
    if not isinstance(workspace_id, str):
        return
    work_dir = Path(resolve_service_settings().work_dir).expanduser()
    try:
        teardown_workspace_auth_overlay(work_dir=work_dir, workspace_id=workspace_id)
    except (OSError, subprocess.SubprocessError):
        # The overlay unmount logged and re-raised the real busy/umount error;
        # keep the compose-down result shape intact so GC reports compose
        # outcome unchanged (GC's own rmtree will surface any residual EBUSY).
        _log.warning(
            "gc.auth_overlay_teardown_failed",
            workspace_id=workspace_id,
        )


def _compose_down_for_terminal_workspace(
    candidate: Any,
) -> WorkspaceGCComposeTeardownResult:
    """Run ``docker compose down`` for a terminal workspace candidate."""
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
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return WorkspaceGCComposeTeardownResult(
            status="failed",
            reason_code="DOCKER_COMPOSE_DOWN_FAILED",
            error="docker compose down timed out after 60s",
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


async def _run_companion_image_prune(retention_hours: int) -> dict[str, object]:
    """Prune stale cached companion images during ``service gc``.

    ``docker image prune`` never removes an image backing a live container, so
    active workspaces' companion images are protected automatically; only
    unreferenced managed companion builds older than the retention window go.
    """
    from awf.node.companion_images import companion_image_prune_command

    command = companion_image_prune_command(retention_hours)

    def _run_prune() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )

    try:
        result = await asyncio.to_thread(_run_prune)
    except subprocess.TimeoutExpired:
        return {
            "status": "failed",
            "reason_code": "COMPANION_IMAGE_PRUNE_FAILED",
            "error": "docker image prune timed out after 120s",
        }
    except OSError as exc:
        return {
            "status": "failed",
            "reason_code": "COMPANION_IMAGE_PRUNE_FAILED",
            "error": str(exc),
        }
    if result.returncode == 0:
        return {
            "status": "succeeded",
            "reason_code": "COMPANION_IMAGE_PRUNE_SUCCEEDED",
            "output": (result.stdout or "").strip(),
        }
    return {
        "status": "failed",
        "reason_code": "COMPANION_IMAGE_PRUNE_FAILED",
        "error": (result.stderr or result.stdout or "docker image prune failed")[:1000],
    }


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
    """Execute emit."""

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
    """Execute emit pretty dict."""

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
    """Execute emit profile preview pretty."""

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
    """Execute emit smoke pretty."""

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
    """Execute profile runtime summary."""

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
    """Execute profile services summary."""

    services = _list_value(profile.get("services"))
    names: list[str] = []
    for service in services:
        if isinstance(service, Mapping):
            names.append(_text_value(service.get("name"), "unnamed"))
        else:
            names.append(str(service))
    return ", ".join(names)


def _profile_phase_commands(profile: Mapping[str, object], phase_name: str) -> list[str]:
    """Execute profile phase commands."""

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
    """Execute mapping value."""

    return dict(value) if isinstance(value, Mapping) else {}


def _list_value(value: object) -> list[object]:
    """Execute list value."""

    return list(value) if isinstance(value, list | tuple) else []


def _text_value(value: object, default: str) -> str:
    """Execute text value."""

    return value if isinstance(value, str) and value else default


def _profile_coverage_target(coverage: Mapping[str, object]) -> tuple[object, bool] | None:
    """Execute profile coverage target."""

    minimum_percent = coverage.get("minimum_percent")
    if minimum_percent is not None:
        return (minimum_percent, False) if _has_positive_coverage_target(minimum_percent) else None

    legacy_target = coverage.get("target")
    if legacy_target is None or not _has_positive_coverage_target(legacy_target):
        return None
    return legacy_target, True


def _has_positive_coverage_target(value: object) -> bool:
    """Execute has positive coverage target."""

    if isinstance(value, int | float):
        return value > 0
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def _format_coverage_target(value: object, *, fractional: bool = False) -> str:
    """Execute format coverage target."""

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
    """Execute call."""

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
    """Execute handle response."""

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
