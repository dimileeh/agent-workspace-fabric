"""First-run host setup CLI: read-only machine readiness pass.

``awf setup`` runs bounded, read-only host system checks (Docker, Compose, Git,
``gh``, Python, ports, disk, shell/PATH, capacity) and renders a first-run
readiness payload. It never starts Core and never writes secrets. Safe,
non-secret config (source-checkout metadata and consent flags) is persisted only
when **not** in ``--dry-run`` mode. Provider selection and the plain-file consent
gate are validated and forwarded for later setup slices (T06/T07).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from awf.cli.common import OutputFormat, _emit
from awf.host_setup.config import (
    HostSetupConfig,
    HostSetupConfigError,
    read_host_setup_config,
    write_host_setup_config,
)
from awf.host_setup.rendering import (
    FirstRunPayload,
    first_run_issue_from_reason_code,
    render_first_run_json,
    render_first_run_pretty,
)
from awf.host_setup.source_assets import (
    SourceCheckoutError,
    VerifiedSourceCheckout,
    validate_source_checkout,
)
from awf.host_setup.system_checks import (
    SetupCheckError,
    build_setup_readiness_payload,
    normalize_providers,
    require_interactive,
    run_system_checks,
)

_SETUP_COMMAND = "awf setup"

_PROVIDER_HELP = (
    "Target a single provider (repeatable). Validated and forwarded so later "
    "provider setup can recheck just that provider."
)
_DRY_RUN_HELP = "Run read-only checks only; never write config and never start Core."
_NON_INTERACTIVE_HELP = "Fail with INTERACTIVE_INPUT_REQUIRED instead of prompting for input."
_ALLOW_PLAIN_SECRETS_HELP = (
    "Consent to the opt-in plain-file secret backend for later credential setup; "
    "does not make plain-file storage the default."
)
_SOURCE_CHECKOUT_HELP = "Validate and use an AWF source checkout at this path."
_FORMAT_HELP = "Output format. JSON unlocks scripting; pretty is the default."


def setup_command(
    provider: list[str] = typer.Option([], "--provider", help=_PROVIDER_HELP),
    dry_run: bool = typer.Option(False, "--dry-run/--no-dry-run", help=_DRY_RUN_HELP),
    non_interactive: bool = typer.Option(False, "--non-interactive", help=_NON_INTERACTIVE_HELP),
    allow_plain_secrets: bool = typer.Option(
        False, "--allow-plain-secrets", help=_ALLOW_PLAIN_SECRETS_HELP
    ),
    source_checkout: Path | None = typer.Option(
        None, "--source-checkout", help=_SOURCE_CHECKOUT_HELP
    ),
    fmt: OutputFormat = typer.Option(OutputFormat.pretty, "--format", help=_FORMAT_HELP),
) -> None:
    """Prepare this machine for AWF first-run use with read-only host checks."""
    try:
        payload = _run_setup(
            providers=provider,
            dry_run=dry_run,
            non_interactive=non_interactive,
            allow_plain_secrets=allow_plain_secrets,
            source_checkout=source_checkout,
        )
    except SetupCheckError as error:
        _emit_payload(_reason_coded_payload(error.reason_code, str(error), error.details), fmt)
        raise typer.Exit(code=2) from error
    except HostSetupConfigError as error:
        _emit_payload(_reason_coded_payload(error.reason_code, error.message, error.details), fmt)
        raise typer.Exit(code=1) from error

    _emit_payload(payload, fmt)
    if payload.status in ("blocked", "failed"):
        raise typer.Exit(code=1)


def _run_setup(
    *,
    providers: list[str],
    dry_run: bool,
    non_interactive: bool,
    allow_plain_secrets: bool,
    source_checkout: Path | None,
) -> FirstRunPayload:
    """Run provider validation, host checks, and (when not dry-run) a safe write."""
    selected_providers = normalize_providers(providers)
    config = read_host_setup_config()

    verified_source: VerifiedSourceCheckout | None = None
    source_error: SourceCheckoutError | None = None
    if source_checkout is not None:
        try:
            verified_source = validate_source_checkout(source_checkout)
        except SourceCheckoutError as exc:
            source_error = exc

    results = run_system_checks(config=config)
    payload = build_setup_readiness_payload(
        results,
        selected_providers=selected_providers,
        allow_plain_secrets=allow_plain_secrets,
        dry_run=dry_run,
        source_checkout=verified_source,
        source_checkout_error=source_error,
    )

    if not dry_run:
        if selected_providers:
            # Configuring a selected provider needs interactive credential entry,
            # which T04 forwards to provider setup (T07). Under --non-interactive
            # there is no way to collect it, so surface the machine-readable signal.
            require_interactive(non_interactive, "configure the selected provider(s)")
        _persist_safe_config(
            config,
            source_checkout=verified_source,
            allow_plain_secrets=allow_plain_secrets,
        )

    return payload


def _persist_safe_config(
    config: HostSetupConfig,
    *,
    source_checkout: VerifiedSourceCheckout | None,
    allow_plain_secrets: bool,
) -> None:
    """Write only safe, non-secret config (consent flags + source metadata)."""
    consent = config.consent.model_copy(
        update={
            "plain_file_secrets": config.consent.plain_file_secrets or allow_plain_secrets,
            "source_checkout_assets": (
                config.consent.source_checkout_assets or source_checkout is not None
            ),
        }
    )
    updates: dict[str, Any] = {"consent": consent}
    if source_checkout is not None:
        updates["source_checkout"] = source_checkout.to_metadata()
    write_host_setup_config(config.model_copy(update=updates))


def _emit_payload(payload: FirstRunPayload, fmt: OutputFormat) -> None:
    """Render the first-run payload (json to stdout, pretty to stderr)."""
    if fmt == OutputFormat.json:
        _emit(render_first_run_json(payload), fmt)
    else:
        typer.echo(render_first_run_pretty(payload), err=True)


def _reason_coded_payload(
    reason_code: str,
    summary: str,
    details: dict[str, Any],
) -> FirstRunPayload:
    """Build a single-issue blocked payload for a reason-coded setup failure."""
    issue = first_run_issue_from_reason_code(
        reason_code,
        severity="blocked",
        details=details,
    )
    return FirstRunPayload(
        status="blocked",
        command=_SETUP_COMMAND,
        summary=summary,
        reason_code=reason_code,
        issues=(issue,),
    )
