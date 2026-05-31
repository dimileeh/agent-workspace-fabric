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
    first_run_report_payload,
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
        # Merge the config file path operators need to fix the failure; it lives
        # on error.path, not in error.details, so it would otherwise be dropped
        # from both JSON and pretty output.
        details = {**error.details, "path": str(error.path)}
        _emit_payload(_reason_coded_payload(error.reason_code, error.message, details), fmt)
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

    # Probe the port ``awf start`` will actually publish. The documented
    # local-service flow keeps ``AWF_API_HOST_PORT`` in ``docker/compose/.env``
    # for Compose interpolation, so merge that file like the service path does;
    # reading only ``os.environ`` would falsely block on the default 8000 when an
    # operator moved the published port there. ``local_service_environ`` falls
    # back to the process env when no ``.env`` exists yet (true first run).
    from awf.service.config import local_service_environ

    results = run_system_checks(config=config, environ=local_service_environ())
    payload = build_setup_readiness_payload(
        results,
        selected_providers=selected_providers,
        allow_plain_secrets=allow_plain_secrets,
        dry_run=dry_run,
        source_checkout=verified_source,
        source_checkout_error=source_error,
    )

    if not dry_run:
        # Readiness blockers win over the interactive-input guard. When the host
        # checks already failed (e.g. missing Docker), raising
        # INTERACTIVE_INPUT_REQUIRED here would mask the SETUP_READINESS_FAILED
        # issues the operator must fix first behind a misleading input-required
        # exit, so only demand interactive provider input when the host is
        # otherwise ready to proceed.
        if selected_providers and payload.status not in ("blocked", "failed"):
            # Configuring a selected provider needs interactive credential entry,
            # which T04 forwards to provider setup (T07). Under --non-interactive
            # there is no way to collect it, so surface the machine-readable signal.
            require_interactive(non_interactive, "configure the selected provider(s)")
        try:
            _persist_safe_config(
                config,
                source_checkout=verified_source,
                allow_plain_secrets=allow_plain_secrets,
            )
        except HostSetupConfigError as error:
            # The safe-config write happens after the host checks finish. Folding
            # the failure into the readiness payload keeps the check blockers and
            # warnings the operator ran setup to see, rather than dropping them in
            # favour of a config-write-only diagnostic.
            return _readiness_with_config_write_failure(payload, error)

    return payload


def _readiness_with_config_write_failure(
    payload: FirstRunPayload,
    error: HostSetupConfigError,
) -> FirstRunPayload:
    """Fold a config-write failure into the readiness payload as a blocked issue.

    The write error path lives on ``error.path`` (not ``error.details``), so it
    is merged in alongside the existing diagnostic details; the readiness
    issues, check provenance, and details are preserved so a failed write never
    hides the host-check report.
    """
    write_issue = first_run_issue_from_reason_code(
        error.reason_code,
        severity="blocked",
        details={**error.details, "path": str(error.path)},
    )
    return first_run_report_payload(
        command=_SETUP_COMMAND,
        summary=f"{payload.summary} {error.message}",
        issues=(*payload.issues, write_issue),
        details=payload.details,
        next_steps=("Fix the reported blockers above, then re-run awf setup.",),
    )


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
