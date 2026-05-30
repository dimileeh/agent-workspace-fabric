"""First-run host setup wizard shell for ``awf setup``.

This command runs bounded, local-first host readiness checks and emits a
reason-coded readiness payload. It never starts AWF Core and never writes
secrets. Non-dry-run, non-blocked runs may persist safe, non-secret config only
(consent flags and verified source-checkout metadata).
"""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import typer

from awf.cli.common import OutputFormat, _emit
from awf.host_setup.config import (
    HostSetupConfig,
    HostSetupConfigError,
    read_host_setup_config,
    write_host_setup_config,
)
from awf.host_setup.rendering import (
    INTERACTIVE_INPUT_REQUIRED,
    SETUP_PROVIDER_UNKNOWN,
    FirstRunPayload,
    first_run_failure_payload,
    first_run_setup_readiness_payload,
    render_first_run_json,
    render_first_run_pretty,
)
from awf.host_setup.source_assets import (
    SourceCheckoutError,
    VerifiedSourceCheckout,
    validate_source_checkout,
)
from awf.host_setup.system_checks import run_system_checks

_SETUP_COMMAND = "awf setup"

# Credential-bearing providers a setup recheck can target. ``docker`` is a host
# system check, not a provider; ``claude`` is accepted as an alias.
_SETUP_PROVIDER_NAMES: tuple[str, ...] = (
    "github",
    "codex",
    "claude_code",
    "gemini",
    "opencode",
)
_PROVIDER_ALIASES: dict[str, str] = {"claude": "claude_code"}


def setup_command(
    provider: list[str] = typer.Option(
        [],
        "--provider",
        help=(
            "Provider to scope a targeted recheck (repeatable). Omit to evaluate all "
            "configured providers."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Run host readiness checks without writing config or starting Core.",
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Fail instead of prompting when interactive input would be required.",
    ),
    allow_plain_secrets: bool = typer.Option(
        False,
        "--allow-plain-secrets",
        help="Consent to plain-file secret storage for later credential setup slices.",
    ),
    source_checkout: Path | None = typer.Option(
        None,
        "--source-checkout",
        help="Path to an AWF source checkout to validate for first-run assets.",
    ),
    fmt: OutputFormat = typer.Option(
        OutputFormat.pretty,
        "--format",
        help="Output format. JSON unlocks scripting; pretty is the default.",
    ),
) -> None:
    """Prepare this machine for AWF first-run use."""
    providers, unknown = _normalize_setup_providers(provider)
    if unknown:
        _emit_first_run(_unknown_provider_payload(unknown), fmt, exit_code=2)

    try:
        config = read_host_setup_config()
    except HostSetupConfigError as exc:
        _emit_first_run(_config_error_payload(exc), fmt, exit_code=1)

    verified_source: VerifiedSourceCheckout | None = None
    if source_checkout is not None:
        try:
            verified_source = validate_source_checkout(source_checkout)
        except SourceCheckoutError as exc:
            _emit_first_run(_source_checkout_error_payload(exc), fmt, exit_code=1)

    if non_interactive and providers and not dry_run:
        _emit_first_run(_interactive_required_payload(providers), fmt, exit_code=1)

    report = run_system_checks(host_port=config.api.host_port, work_dir=config.work_dir)
    payload = first_run_setup_readiness_payload(
        report,
        command=_SETUP_COMMAND,
        dry_run=dry_run,
        providers=providers,
        allow_plain_secrets=allow_plain_secrets,
        source_checkout_root=str(verified_source.root) if verified_source is not None else None,
    )

    blocked = report.status == "fail"
    if not dry_run and not blocked:
        _write_safe_setup_config(
            config,
            allow_plain_secrets=allow_plain_secrets,
            verified_source=verified_source,
        )

    _emit_first_run(payload, fmt, exit_code=1 if blocked else 0)


def _normalize_setup_providers(values: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(validated providers, unknown selectors)``; order-stable and deduped."""
    providers: list[str] = []
    unknown: list[str] = []
    for raw in values:
        name = raw.strip().lower().replace("-", "_")
        name = _PROVIDER_ALIASES.get(name, name)
        if not name:
            continue
        if name not in _SETUP_PROVIDER_NAMES:
            if raw not in unknown:
                unknown.append(raw)
            continue
        if name not in providers:
            providers.append(name)
    return tuple(providers), tuple(unknown)


def _unknown_provider_payload(unknown: tuple[str, ...]) -> FirstRunPayload:
    """Build the usage-error payload for unsupported ``--provider`` selectors."""
    return first_run_failure_payload(
        command=_SETUP_COMMAND,
        reason_code=SETUP_PROVIDER_UNKNOWN,
        summary="AWF setup was asked to configure an unsupported provider.",
        status="failed",
        details={
            "unknown_providers": list(unknown),
            "supported_providers": list(_SETUP_PROVIDER_NAMES),
        },
        next_steps=(
            "Use a supported provider name, or omit --provider to evaluate all providers.",
        ),
    )


def _config_error_payload(exc: HostSetupConfigError) -> FirstRunPayload:
    """Build a failure payload from a reason-coded host setup config error."""
    return first_run_failure_payload(
        command=_SETUP_COMMAND,
        reason_code=exc.reason_code,
        summary=exc.message,
        status="failed",
        details=dict(exc.details) or None,
    )


def _source_checkout_error_payload(exc: SourceCheckoutError) -> FirstRunPayload:
    """Build a failure payload from a reason-coded source-checkout error."""
    details: dict[str, object] = {"root": str(exc.root)}
    if exc.missing_markers:
        details["missing_markers"] = list(exc.missing_markers)
    details.update(exc.details)
    return first_run_failure_payload(
        command=_SETUP_COMMAND,
        reason_code=exc.reason_code,
        summary=exc.message,
        status="failed",
        details=details,
    )


def _interactive_required_payload(providers: tuple[str, ...]) -> FirstRunPayload:
    """Build the failure payload for non-interactive runs that need input."""
    return first_run_failure_payload(
        command=_SETUP_COMMAND,
        reason_code=INTERACTIVE_INPUT_REQUIRED,
        summary="AWF setup needs interactive input that is unavailable in this run.",
        status="failed",
        details={
            "providers": list(providers),
            "non_interactive": True,
            "dry_run": False,
        },
        next_steps=(
            "Re-run interactively, supply safe env/keychain references, or add --dry-run.",
        ),
    )


def _write_safe_setup_config(
    config: HostSetupConfig,
    *,
    allow_plain_secrets: bool,
    verified_source: VerifiedSourceCheckout | None,
) -> None:
    """Persist non-secret consent flags and verified source metadata only."""
    consent = config.consent.model_copy(
        update={
            "plain_file_secrets": config.consent.plain_file_secrets or allow_plain_secrets,
            "source_checkout_assets": (
                config.consent.source_checkout_assets or verified_source is not None
            ),
        }
    )
    updates: dict[str, object] = {"consent": consent}
    if verified_source is not None:
        updates["source_checkout"] = verified_source.to_metadata()
    write_host_setup_config(config.model_copy(update=updates))


def _emit_first_run(payload: FirstRunPayload, fmt: OutputFormat, *, exit_code: int) -> NoReturn:
    """Emit a first-run payload and exit with the given code.

    JSON always goes to stdout for scripting. Pretty success (exit 0) goes to
    stdout; pretty failures go to stderr.
    """
    if fmt == OutputFormat.json:
        _emit(render_first_run_json(payload), fmt)
    else:
        typer.echo(render_first_run_pretty(payload), err=exit_code != 0)
    raise typer.Exit(code=exit_code)
