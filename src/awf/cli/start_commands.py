"""First-run local Core start CLI: a friendly wrapper over service bootstrap.

``awf start`` delegates to the existing ``run_service_bootstrap`` engine to start
local AWF Core, then renders a first-run-contract success panel or a reason-coded
failure. It never reimplements service startup; ``awf service bootstrap``,
``awf service status``, and ``awf service doctor`` remain the expert commands.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import typer

from awf.cli.common import OutputFormat, _emit
from awf.host_setup.rendering import (
    START_COMPOSE_ASSETS_MISSING,
    START_HEALTH_TIMEOUT,
    START_MIGRATION_FAILED,
    START_PORT_CONFLICT,
    FirstRunIssue,
    FirstRunPayload,
    FirstRunRemediation,
    first_run_failure_payload,
    first_run_success_payload,
    render_first_run_json,
    render_first_run_pretty,
)
from awf.host_setup.source_assets import (
    SOURCE_CHECKOUT_ASSETS_STALE,
    SOURCE_CHECKOUT_INVALID,
)

if TYPE_CHECKING:
    from awf.host_setup.source_assets import SourceCheckoutError, VerifiedSourceCheckout
    from awf.service.bootstrap import ServiceBootstrapError, ServiceBootstrapResult
    from awf.service.config import ServiceSettings

_DEFAULT_START_TIMEOUT_SECONDS = 180.0
_LOCAL_DISPLAY_HOST = "127.0.0.1"
_LOCAL_HOST_ALIASES = frozenset({"localhost", "0.0.0.0", "127.0.0.1", "::1"})

# Known Docker port-bind failure signatures used to classify a stage failure as a
# port conflict while preserving the original bootstrap diagnostic.
_PORT_CONFLICT_SIGNATURES = (
    "port is already allocated",
    "address already in use",
    "bind: address already in use",
    "failed to bind host port",
)
_PORT_RE = re.compile(r"(?:(?:\d{1,3}\.){3}\d{1,3}|\[?::\]?):(?P<port>\d{2,5})")


@dataclass(frozen=True)
class _StartBootstrapInputs:
    """Resolved inputs for one ``run_service_bootstrap`` delegation."""

    compose_file: Path
    compose_env_file: Path | None
    asset_root: Path | None
    service_env: dict[str, str]
    settings: ServiceSettings


def start_command(
    rebuild: bool = typer.Option(
        False,
        "--rebuild",
        help=(
            "Rebuild the agent-runtime image from scratch (docker build --no-cache). "
            "Cannot be combined with --skip-agent-runtime-build."
        ),
    ),
    skip_agent_runtime_build: bool = typer.Option(
        False,
        "--skip-agent-runtime-build",
        help="Skip building the AWF agent-runtime image for a faster restart.",
    ),
    timeout_seconds: float = typer.Option(
        _DEFAULT_START_TIMEOUT_SECONDS,
        "--timeout-seconds",
        min=0.0,
        help="Maximum time to wait for local AWF Core readiness.",
    ),
    source_checkout: Path | None = typer.Option(
        None,
        "--source-checkout",
        help=(
            "Start from verified AWF source-checkout assets at PATH instead of "
            "discovered package/source assets."
        ),
    ),
    fmt: OutputFormat = typer.Option(
        OutputFormat.pretty,
        "--format",
        help="Output format. JSON unlocks scripting; pretty is the default.",
    ),
) -> None:
    """Start local AWF Core by wrapping the existing service bootstrap engine."""
    if rebuild and skip_agent_runtime_build:
        typer.echo(
            "error: --rebuild cannot be combined with --skip-agent-runtime-build; "
            "choose a full rebuild or a build-skipping restart.",
            err=True,
        )
        raise typer.Exit(code=2)

    from awf.host_setup.source_assets import SourceCheckoutError
    from awf.service.bootstrap import (
        ServiceBootstrapError,
        ServiceBootstrapOptions,
        run_service_bootstrap,
    )

    try:
        verified = _resolve_start_source_checkout(source_checkout)
    except SourceCheckoutError as exc:
        _render_start_payload(_source_checkout_failure_payload(exc), fmt, success=False)
        raise typer.Exit(code=1) from None

    inputs = _resolve_start_bootstrap_inputs(verified)
    options = ServiceBootstrapOptions(
        timeout_seconds=timeout_seconds,
        skip_agent_runtime_build=skip_agent_runtime_build,
        force_rebuild=rebuild,
    )
    try:
        result = asyncio.run(
            run_service_bootstrap(
                inputs.settings,
                options=options,
                compose_file=inputs.compose_file,
                env_file=inputs.compose_env_file,
                asset_root=inputs.asset_root,
                service_environ=inputs.service_env,
            )
        )
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    except ServiceBootstrapError as exc:
        _render_start_payload(_start_failure_payload(exc), fmt, success=False)
        raise typer.Exit(code=1) from None

    _render_start_payload(_start_success_payload(inputs.settings, result), fmt, success=True)


def _resolve_start_source_checkout(source_checkout: Path | None) -> VerifiedSourceCheckout | None:
    """Resolve verified source-checkout assets, or None for default discovery.

    Explicit ``--source-checkout`` is validated directly. Otherwise stored host
    setup config metadata is revalidated when present; a stale/moved checkout
    fails loudly rather than silently falling back to package assets. A missing
    or unreadable config (when no source assets were requested) falls through to
    default discovery.
    """
    from awf.host_setup.source_assets import (
        validate_source_checkout,
        verified_source_from_metadata,
    )

    if source_checkout is not None:
        return validate_source_checkout(source_checkout)

    from awf.host_setup.config import HostSetupConfigError, read_host_setup_config

    try:
        config = read_host_setup_config()
    except HostSetupConfigError:
        return None
    if config.source_checkout is None:
        return None
    return verified_source_from_metadata(config.source_checkout)


def _resolve_start_bootstrap_inputs(
    verified: VerifiedSourceCheckout | None,
) -> _StartBootstrapInputs:
    """Resolve compose paths, env files, and settings for one bootstrap run."""
    from awf.common.config import Settings
    from awf.service.config import local_service_environ, resolve_service_settings

    if verified is not None:
        from awf.cli.init_ops import _resolve_existing_service_env_file

        compose_file = verified.compose_file
        asset_root: Path | None = verified.root
        compose_env_candidate = verified.root / "docker" / "compose" / ".env"
        compose_env_file: Path | None = (
            compose_env_candidate if compose_env_candidate.exists() else None
        )
        # Before the compose .env is seeded the checkout root .env carries tokens
        # and DB settings, so read it as a fallback exactly like
        # `awf service bootstrap` does via `_resolve_existing_service_env_file`.
        # The compose --env-file (compose_env_file) stays pinned to the compose
        # .env, so the root .env is used for reads only, never forwarded to Docker.
        resolved_read_env = _resolve_existing_service_env_file(compose_env_candidate)
        read_env_file = resolved_read_env if resolved_read_env.exists() else None
    else:
        from awf.cli.init_ops import (
            _resolve_service_compose_paths,
            _resolve_service_runtime_env_files,
        )

        compose_file, raw_env_file, _ = _resolve_service_compose_paths()
        read_env_file, compose_env_file = _resolve_service_runtime_env_files(
            compose_file,
            raw_env_file,
            paths_verified=True,
        )
        asset_root = None

    service_env = local_service_environ(env_file=read_env_file)
    settings = resolve_service_settings(
        Settings(_env_file=read_env_file),
        environ=service_env,
    )
    return _StartBootstrapInputs(
        compose_file=compose_file,
        compose_env_file=compose_env_file,
        asset_root=asset_root,
        service_env=service_env,
        settings=settings,
    )


def _render_start_payload(payload: FirstRunPayload, fmt: OutputFormat, *, success: bool) -> None:
    """Emit a first-run payload: JSON to stdout; pretty to stdout/stderr by status."""
    if fmt == OutputFormat.json:
        _emit(render_first_run_json(payload), fmt)
        return
    typer.echo(render_first_run_pretty(payload), err=not success)


def _start_success_payload(
    settings: ServiceSettings,
    result: ServiceBootstrapResult,
) -> FirstRunPayload:
    """Build the operator success panel from resolved settings and bootstrap result."""
    from awf.service.smoke import DEFAULT_LOCAL_CONSOLE_URL

    service_status = result.service_status if isinstance(result.service_status, Mapping) else {}
    checks = service_status.get("checks")
    docker_check = checks.get("docker") if isinstance(checks, Mapping) else None
    api_url = _normalize_local_url(str(settings.api_base_url))
    console_url = _normalize_local_url(str(settings.console_url or DEFAULT_LOCAL_CONSOLE_URL))
    details: dict[str, Any] = {
        "api_url": api_url,
        "console_url": console_url,
        "docker": _docker_summary(docker_check),
        "providers": _providers_summary(service_status.get("agent_readiness")),
        "health": service_status.get("status", "unknown"),
    }
    next_steps = (
        # Lead with the provider-free local health proof: a skeptic can verify
        # local Core health without handing AWF GitHub/PR authority or a token.
        "Run awf smoke run --mocked-local to prove local AWF Core health "
        "without provider credentials or GitHub access.",
        "Run awf init <path> to onboard a project repository.",
        "Run awf service status --format pretty to inspect local Core health.",
        f"Open the console at {console_url} to use the local UI.",
    )
    return first_run_success_payload(
        command="awf start",
        summary="Local AWF Core is running.",
        details=details,
        next_steps=next_steps,
    )


def _start_failure_payload(exc: ServiceBootstrapError) -> FirstRunPayload:
    """Translate a structured bootstrap failure into a first-run failure payload.

    The full ``exc.to_dict()`` diagnostic is always embedded under
    ``details["bootstrap"]`` so existing diagnostics are preserved and redacted
    by the first-run renderer. No bootstrap failure is dropped or mislabeled.
    """
    diagnostic = exc.to_dict()
    details: dict[str, Any] = {"bootstrap": diagnostic}
    reason_code = _classify_start_failure(exc)
    if reason_code is None:
        return _unclassified_start_failure_payload(exc, details)

    if reason_code == START_PORT_CONFLICT:
        if exc.stage is not None:
            details["stage"] = exc.stage
        port = _detect_conflict_port(exc)
        if port is not None:
            details["port"] = port

    return first_run_failure_payload(
        command="awf start",
        reason_code=reason_code,
        summary=_START_FAILURE_SUMMARIES[reason_code],
        details=details,
    )


_START_FAILURE_SUMMARIES = {
    START_COMPOSE_ASSETS_MISSING: (
        "awf start could not locate the AWF bootstrap assets for local Core."
    ),
    START_HEALTH_TIMEOUT: "awf start timed out waiting for local AWF Core to become healthy.",
    START_MIGRATION_FAILED: "awf start could not complete local AWF Core database migrations.",
    START_PORT_CONFLICT: "awf start hit a local port conflict while starting AWF Core.",
}


def _classify_start_failure(exc: ServiceBootstrapError) -> str | None:
    """Return the START_* reason code for a bootstrap error, or None if unclassified."""
    from awf.service.bootstrap import (
        SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND,
        SERVICE_BOOTSTRAP_STAGE_FAILED,
        SERVICE_BOOTSTRAP_TIMEOUT,
    )

    if exc.reason_code == SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND:
        return START_COMPOSE_ASSETS_MISSING
    if exc.reason_code == SERVICE_BOOTSTRAP_TIMEOUT:
        return START_HEALTH_TIMEOUT
    if exc.reason_code == SERVICE_BOOTSTRAP_STAGE_FAILED:
        if exc.stage == "migrate":
            return START_MIGRATION_FAILED
        if _looks_like_port_conflict(exc):
            return START_PORT_CONFLICT
    return None


def _unclassified_start_failure_payload(
    exc: ServiceBootstrapError,
    details: dict[str, Any],
) -> FirstRunPayload:
    """Build a failure payload that echoes an unclassified bootstrap reason code."""
    remediation = FirstRunRemediation(
        problem=exc.message or "awf start failed during local Core bootstrap.",
        cause=_unclassified_cause(exc),
        fix=(
            "Inspect the embedded bootstrap diagnostic, then retry awf start or use "
            "awf service bootstrap/doctor for expert recovery."
        ),
        docs_link="docs/REASON_CATALOG.md",
        related_command="awf service doctor",
    )
    issue = FirstRunIssue(
        reason_code=exc.reason_code,
        severity="failed",
        remediation=remediation,
        details=details,
    )
    return FirstRunPayload(
        status="failed",
        command="awf start",
        summary=f"awf start failed: {exc.message}".strip(),
        reason_code=exc.reason_code,
        issues=(issue,),
    )


def _unclassified_cause(exc: ServiceBootstrapError) -> str:
    """Return a non-empty cause string for an unclassified bootstrap failure."""
    if exc.stage is not None:
        returncode = "unknown" if exc.returncode is None else str(exc.returncode)
        return f"Bootstrap stage {exc.stage} failed with exit code {returncode}."
    return "Local Core bootstrap failed before completing startup."


def _looks_like_port_conflict(exc: ServiceBootstrapError) -> bool:
    """Return whether bootstrap output matches a known Docker port-bind signature."""
    haystack = f"{exc.stdout}\n{exc.stderr}".lower()
    return any(signature in haystack for signature in _PORT_CONFLICT_SIGNATURES)


def _detect_conflict_port(exc: ServiceBootstrapError) -> int | None:
    """Best-effort extraction of the conflicting host port from bootstrap output."""
    match = _PORT_RE.search(f"{exc.stdout}\n{exc.stderr}")
    if match is None:
        return None
    try:
        return int(match.group("port"))
    except ValueError:  # pragma: no cover - regex guarantees digits
        return None


def _source_checkout_failure_payload(exc: SourceCheckoutError) -> FirstRunPayload:
    """Translate a source-checkout validation error into a first-run failure payload."""
    details: dict[str, Any] = {"root": str(exc.root)}
    if exc.missing_markers:
        details["missing_markers"] = list(exc.missing_markers)
    if exc.details:
        details["source_checkout"] = dict(exc.details)
    reason_code = (
        exc.reason_code
        if exc.reason_code in {SOURCE_CHECKOUT_INVALID, SOURCE_CHECKOUT_ASSETS_STALE}
        else SOURCE_CHECKOUT_INVALID
    )
    return first_run_failure_payload(
        command="awf start",
        reason_code=reason_code,
        summary="awf start could not verify the selected AWF source-checkout assets.",
        details=details,
    )


def _normalize_local_url(url: str) -> str:
    """Return ``url`` with a local-loopback host rewritten to 127.0.0.1 for display."""
    try:
        parsed = urlsplit(url)
        # ``urlsplit`` accepts a malformed port lazily; ``parsed.port`` only
        # validates (and may raise ValueError) on access, so read it here.
        port = parsed.port
    except ValueError:
        return url
    if parsed.hostname is None or parsed.hostname not in _LOCAL_HOST_ALIASES:
        return url
    netloc = _LOCAL_DISPLAY_HOST
    if port is not None:
        netloc = f"{_LOCAL_DISPLAY_HOST}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _docker_summary(docker_check: object) -> dict[str, Any]:
    """Summarize the docker status check for the success panel."""
    if not isinstance(docker_check, Mapping):
        return {"status": "unknown"}
    summary: dict[str, Any] = {
        "ok": bool(docker_check.get("ok")),
        "status": docker_check.get("status", "unknown"),
    }
    version = docker_check.get("version")
    if version is not None:
        summary["version"] = version
    reason = docker_check.get("reason")
    if reason is not None:
        summary["reason"] = reason
    return summary


def _providers_summary(agent_readiness: object) -> dict[str, Any]:
    """Summarize provider readiness (status-only) for the success panel.

    Only per-provider status strings are surfaced; the first-run renderer redacts
    any provider refs or tokens so no secret can leak through the summary.
    """
    if not isinstance(agent_readiness, Mapping):
        return {"status": "unknown"}
    summary: dict[str, Any] = {"status": agent_readiness.get("status", "unknown")}
    providers = agent_readiness.get("providers")
    if isinstance(providers, Mapping):
        summary["providers"] = {
            str(name): (
                provider.get("status", "unknown") if isinstance(provider, Mapping) else "unknown"
            )
            for name, provider in providers.items()
        }
    return summary
