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
from awf.cli.init_ops import (
    resolve_existing_service_env_file,
    resolve_service_compose_paths,
    resolve_service_runtime_env_files,
)
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
    first_run_issue_from_reason_code,
    first_run_report_payload,
    render_first_run_json,
    render_first_run_pretty,
)
from awf.host_setup.source_assets import (
    SourceCheckoutError,
    VerifiedSourceCheckout,
    validate_source_checkout,
    verified_source_from_metadata,
)
from awf.host_setup.system_checks import (
    SETUP_COMMAND,
    SetupCheckError,
    build_setup_readiness_payload,
    normalize_providers,
    require_interactive,
    run_system_checks,
)

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
_FORMAT_HELP = (
    "Output format. JSON writes to stdout for scripting; pretty writes to stderr (default)."
)


def _config_error_details(error: HostSetupConfigError) -> dict[str, Any]:
    """Merge the config file path into a config error's diagnostic details.

    The config file path operators need to fix the failure lives on
    ``error.path``, not in ``error.details``, so it would otherwise be dropped
    from both JSON and pretty output. Surface it under ``config_path`` when the
    details already carry a field-level ``path`` (for example a secret-bearing
    key or recursive alias reporting ``providers.github.token``) so the merge
    never clobbers the diagnostic that tells the operator which field to fix;
    otherwise keep the established ``path`` key for the config file path.
    """
    file_path_key = "config_path" if "path" in error.details else "path"
    return {**error.details, file_path_key: str(error.path)}


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
        details = _config_error_details(error)
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

    probe_source, source_error, explicit_source = _resolve_setup_source_checkout(
        source_checkout, config
    )

    # A selected or persisted source checkout that fails validation surfaces as a
    # blocked readiness issue *without* the default-discovery host probes, exactly
    # as ``awf start`` exits from ``_resolve_start_source_checkout`` before it
    # reaches ``_resolve_start_bootstrap_inputs``/default discovery. With
    # ``probe_source`` ``None`` here, running the checks would call
    # ``_readiness_environ(None)`` and probe the *default-discovered* compose env,
    # so setup could add unrelated port/disk blockers (for example the default
    # 8000 in use) the matching ``awf start`` would never hit -- the same divergence
    # the source-checkout resolver documents it avoids. The "no selection at all"
    # case (``source_error`` ``None``) still falls through to default discovery.
    if source_error is not None:
        blocked_payload = build_setup_readiness_payload(
            (),
            selected_providers=selected_providers,
            allow_plain_secrets=allow_plain_secrets,
            dry_run=dry_run,
            source_checkout_error=source_error,
        )
        if dry_run:
            return blocked_payload
        # The host-check path persists safe consent even when the readiness
        # status is blocked (e.g. missing Docker reaches the non-dry-run write
        # below), so an explicit, non-secret ``--allow-plain-secrets`` consent
        # must survive this blocked source-checkout early return too. Otherwise
        # an operator who passed ``--source-checkout <bad> --allow-plain-secrets``
        # would silently lose the plain-file consent and have to re-pass it after
        # fixing the checkout path -- the same silent consent loss the provider
        # interactive guard already guards against. The failed/stale checkout is
        # never persisted (``explicit_source`` is ``None`` on a resolution error,
        # so only the consent flag is recorded; any existing persisted metadata is
        # preserved untouched).
        try:
            _persist_safe_config(
                config,
                source_checkout=None,
                allow_plain_secrets=allow_plain_secrets,
            )
        except HostSetupConfigError as error:
            return _readiness_with_config_write_failure(blocked_payload, error)
        return blocked_payload

    # Probe the port/disk ``awf start`` will actually use. The documented
    # local-service flow keeps ``AWF_API_HOST_PORT``/``AWF_HOST_WORK_DIR`` in
    # ``docker/compose/.env`` for Compose interpolation; ``_readiness_environ``
    # merges that file (the resolved source checkout's copy when one is in play —
    # the ``--source-checkout`` selection or the revalidated persisted checkout)
    # so setup probes the same values ``awf start`` will honor instead of the
    # default 8000/work dir.
    results = run_system_checks(
        environ=_readiness_environ(probe_source),
    )
    payload = build_setup_readiness_payload(
        results,
        selected_providers=selected_providers,
        allow_plain_secrets=allow_plain_secrets,
        dry_run=dry_run,
        source_checkout=probe_source,
        source_checkout_error=source_error,
    )

    if not dry_run:
        # Readiness blockers win over the interactive-input guard. When the host
        # checks already failed (e.g. missing Docker), raising
        # INTERACTIVE_INPUT_REQUIRED here would mask the SETUP_READINESS_FAILED
        # issues the operator must fix first behind a misleading input-required
        # exit, so only demand interactive provider input when the host is
        # otherwise ready to proceed.
        provider_needs_input = bool(selected_providers) and payload.status not in (
            "blocked",
            "failed",
        )
        # ``--allow-plain-secrets`` and the ``--source-checkout`` selection are
        # explicit, non-secret CLI flags -- not interactive prompts -- so
        # persisting them never needs input and must survive the provider
        # interactive guard. Otherwise a ready-host
        # ``--provider X --non-interactive --allow-plain-secrets`` run aborts
        # with INTERACTIVE_INPUT_REQUIRED before the safe write, silently
        # discarding consent the operator passed explicitly. When no such
        # explicit consent was given there is nothing to record ahead of the
        # guard, so its original no-write early abort is preserved.
        explicit_consent = allow_plain_secrets or explicit_source is not None
        if provider_needs_input and not explicit_consent:
            # Configuring a selected provider needs interactive credential entry,
            # which T04 forwards to provider setup (T07). Under --non-interactive
            # there is no way to collect it, so surface the machine-readable signal.
            require_interactive(non_interactive, "configure the selected provider(s)")
        try:
            _persist_safe_config(
                config,
                source_checkout=explicit_source,
                allow_plain_secrets=allow_plain_secrets,
            )
        except HostSetupConfigError as error:
            # The safe-config write happens after the host checks finish. Folding
            # the failure into the readiness payload keeps the check blockers and
            # warnings the operator ran setup to see, rather than dropping them in
            # favour of a config-write-only diagnostic.
            return _readiness_with_config_write_failure(payload, error)
        if provider_needs_input and explicit_consent:
            # Explicit non-secret consent is now persisted; still surface the
            # interactive-input signal for the provider credential step (T07)
            # that cannot run under --non-interactive.
            require_interactive(non_interactive, "configure the selected provider(s)")

    return payload


def _resolve_setup_source_checkout(
    source_checkout: Path | None,
    config: HostSetupConfig,
) -> tuple[
    VerifiedSourceCheckout | None, SourceCheckoutError | None, VerifiedSourceCheckout | None
]:
    """Resolve the source checkout whose env the readiness probe should honor.

    Returns ``(probe_source, source_error, explicit_source)``. ``probe_source``
    drives the readiness env probe and the rendered payload; ``explicit_source``
    is only set for an explicit ``--source-checkout`` selection and drives
    metadata persistence, so re-running ``awf setup`` without the flag never
    rewrites or refreshes the persisted metadata.

    The resolution mirrors ``awf start``'s ``_resolve_start_source_checkout`` so
    setup probes the same port/work dir ``awf start`` will use: an explicit
    ``--source-checkout`` is validated directly, otherwise persisted host-config
    metadata is revalidated when present. A stale or invalid checkout surfaces as
    a blocked readiness issue — the same failure ``awf start`` would hit — instead
    of silently falling back to default discovery and clearing/blocking on a port
    or disk path the matching ``awf start`` would not use.
    """
    if source_checkout is not None:
        try:
            explicit = validate_source_checkout(source_checkout)
        except SourceCheckoutError as exc:
            return None, exc, None
        return explicit, None, explicit

    if config.source_checkout is None:
        return None, None, None
    try:
        return verified_source_from_metadata(config.source_checkout), None, None
    except SourceCheckoutError as exc:
        return None, exc, None


def _readiness_environ(verified_source: VerifiedSourceCheckout | None) -> dict[str, str]:
    """Resolve the merged environment the read-only host probes should see.

    Reading only ``os.environ`` would falsely block on the default 8000 / work
    dir when an operator moved ``AWF_API_HOST_PORT``/``AWF_HOST_WORK_DIR`` into
    ``docker/compose/.env``, so merge that file like the service path does.

    When a verified source checkout is in play — selected via ``--source-checkout``
    or revalidated from persisted host config (see
    ``_resolve_setup_source_checkout``) — read *that* checkout's
    ``docker/compose/.env`` (with the checkout-root ``.env`` fallback, exactly like
    ``awf start``'s ``_resolve_start_bootstrap_inputs``). Otherwise setup would
    probe the default-discovered ``.env`` while ``awf start`` later honors the
    selected/persisted checkout's values, so a checkout-local
    ``AWF_API_HOST_PORT``/``AWF_HOST_WORK_DIR`` could make setup block on a port
    or disk path the matching start would not use.

    With no verified source checkout, resolve the env file the same way ``awf
    start``'s default-discovery branch does — through ``resolve_service_compose_paths``
    and ``resolve_service_runtime_env_files`` — so setup honors the packaged
    bootstrap asset root's ``docker/compose/.env``. A bare ``local_service_environ()``
    only searches the cwd and nearby source-tree markers, so from a typical install
    cwd it would probe the default 8000/work dir while ``awf start`` uses the
    bundled env file's values. ``local_service_environ`` falls back to the process
    env when no ``.env`` exists yet (true first run).
    """
    from awf.service.config import local_service_environ

    if verified_source is None:
        compose_file, raw_env_file, _ = resolve_service_compose_paths()
        default_read_env, _ = resolve_service_runtime_env_files(
            compose_file,
            raw_env_file,
            paths_verified=True,
        )
        return local_service_environ(env_file=default_read_env)

    compose_env_candidate = verified_source.root / "docker" / "compose" / ".env"
    resolved_read_env = resolve_existing_service_env_file(compose_env_candidate)
    read_env_file = resolved_read_env if resolved_read_env.exists() else None
    return local_service_environ(env_file=read_env_file)


def _readiness_with_config_write_failure(
    payload: FirstRunPayload,
    error: HostSetupConfigError,
) -> FirstRunPayload:
    """Fold a config-write failure into the readiness payload as a blocked issue.

    The write error path lives on ``error.path`` (not ``error.details``), so it
    is merged in via ``_config_error_details`` alongside the existing diagnostic
    details (without clobbering a field-level ``path``); the readiness issues,
    check provenance, and details are preserved so a failed write never hides the
    host-check report.
    """
    write_issue = first_run_issue_from_reason_code(
        error.reason_code,
        severity="blocked",
        details=_config_error_details(error),
    )
    return first_run_report_payload(
        command=SETUP_COMMAND,
        summary=f"{payload.summary} {error.message}",
        issues=(*payload.issues, write_issue),
        details=payload.details,
        next_steps=("Fix the reported blockers above, then re-run awf setup --dry-run.",),
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
    """Build a single-issue blocked payload for a reason-coded setup failure.

    The readiness happy/blocked paths always populate top-level ``next_steps``
    (see ``_readiness_next_steps``), so mirror that here: derive machine-readable
    guidance from ``reason_code`` rather than leaving these error exits with a
    silent empty ``next_steps``. An operator (or calling script) hitting
    SETUP_PROVIDER_UNKNOWN, INTERACTIVE_INPUT_REQUIRED, or a host-setup config
    failure then gets a concrete pointer to the accepted provider names / how to
    rerun, not just the per-issue remediation text.
    """
    issue = first_run_issue_from_reason_code(
        reason_code,
        severity="blocked",
        details=details,
    )
    return FirstRunPayload(
        status="blocked",
        command=SETUP_COMMAND,
        summary=summary,
        reason_code=reason_code,
        issues=(issue,),
        next_steps=_reason_coded_next_steps(reason_code),
    )


def _reason_coded_next_steps(reason_code: str) -> tuple[str, ...]:
    """Return operator next-step guidance for a reason-coded setup failure."""
    if reason_code == SETUP_PROVIDER_UNKNOWN:
        return (
            "Re-run awf setup with a supported --provider; the accepted names are "
            "listed under known_providers in the issue details.",
        )
    if reason_code == INTERACTIVE_INPUT_REQUIRED:
        return (
            "Re-run without --non-interactive to supply the required input, or pass "
            "--dry-run for a read-only readiness check.",
        )
    return ("Fix the reported issue above, then re-run awf setup --dry-run.",)
