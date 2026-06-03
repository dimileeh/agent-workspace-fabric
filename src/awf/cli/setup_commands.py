"""First-run host setup CLI: read-only machine readiness pass.

``awf setup`` runs bounded, read-only host system checks (Docker, Compose, Git,
``gh``, Python, ports, disk, shell/PATH, capacity) and renders a first-run
readiness payload. It never starts Core and never writes secrets. Safe,
non-secret config (source-checkout metadata and consent flags) is persisted only
when **not** in ``--dry-run`` mode. Provider selection and the plain-file consent
gate are validated and forwarded for later setup slices (T06/T07).
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from awf.cli.common import OutputFormat, _emit
from awf.cli.init_ops import (
    resolve_existing_service_env_file,
    resolve_service_compose_paths,
    resolve_service_runtime_env_files,
)
from awf.host_setup.clients import (
    default_client_command_runner,
    normalize_clients,
    setup_clients,
)
from awf.host_setup.config import (
    HostSetupConfig,
    HostSetupConfigError,
    read_host_setup_config,
    write_host_setup_config,
)
from awf.host_setup.rendering import (
    INTERACTIVE_INPUT_REQUIRED,
    SETUP_CLIENT_UNKNOWN,
    SETUP_PLAIN_SECRETS_CLIENT_CONFLICT,
    SETUP_PROVIDER_CLIENT_CONFLICT,
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
_CLIENT_HELP = (
    "Register AWF's MCP server into a client config (repeatable). Supported "
    "clients: claude, codex. Shows a diff and writes a backup; --dry-run never "
    "mutates. Never reads or stores provider tokens."
)

# Dependency-injection seams for the client-integration dispatch, mirroring the
# hermetic readiness seams. Tests monkeypatch these to point at temp homes and
# fake CLI detection/execution; production resolves the real host home, the
# ``which`` detector, the bounded official-CLI runner, and the wall clock.
_client_which = shutil.which
_client_run = default_client_command_runner


def _client_home() -> Path:
    """Return the host home directory the client config paths anchor under."""
    return Path.home()


def _client_now() -> datetime:
    """Return the current UTC time used to stamp config backups."""
    return datetime.now(UTC)


def _client_env() -> Mapping[str, str]:
    """Return the process environment used to resolve client config home overrides.

    Threaded into ``setup_clients`` so a client that relocates its config via an
    environment variable (Codex's ``CODEX_HOME``) is planned/written at the path
    its official CLI actually loads, not the hard-coded home-anchored default.
    """
    return os.environ


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
    client: list[str] = typer.Option([], "--client", help=_CLIENT_HELP),
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
        if client:
            # T08 owns only the client-selector dispatch; it returns before the
            # readiness path so the no-``--client`` flow is unchanged from T04.
            if provider:
                # The client dispatch never consumes ``--provider``; failing fast
                # here keeps the provider argument from being silently discarded
                # behind a successful client-setup result. The conflict is reported
                # as SETUP_PROVIDER_CLIENT_CONFLICT rather than SETUP_PROVIDER_UNKNOWN
                # so the catalog remediation and next-step hint describe the
                # mutually exclusive flags instead of a (possibly valid) provider
                # name that AWF never evaluated.
                raise SetupCheckError(
                    "--provider is not supported with --client; re-run without --provider.",
                    reason_code=SETUP_PROVIDER_CLIENT_CONFLICT,
                    details={"providers": provider, "clients": client},
                )
            if allow_plain_secrets:
                # The client dispatch returns before ``_run_setup``, the only path
                # that persists plain-file consent via ``_persist_safe_config``.
                # Accepting ``--allow-plain-secrets`` here would exit successfully
                # while silently dropping the operator's opt-in, leaving later
                # credential setup blocked despite the flag having been "accepted".
                # Reject the combination (like ``--provider``) so the operator
                # records consent on the readiness/provider path that actually
                # persists it instead of losing it to a no-op client run.
                raise SetupCheckError(
                    "--allow-plain-secrets is not supported with --client; "
                    "re-run without --allow-plain-secrets.",
                    reason_code=SETUP_PLAIN_SECRETS_CLIENT_CONFLICT,
                    details={"clients": client},
                )
            payload = _run_client_setup(
                clients=client,
                dry_run=dry_run,
                source_checkout=source_checkout,
            )
        else:
            payload = _run_setup(
                providers=provider,
                dry_run=dry_run,
                non_interactive=non_interactive,
                allow_plain_secrets=allow_plain_secrets,
                source_checkout=source_checkout,
            )
    except SetupCheckError as error:
        if isinstance(error, _ReadinessInteractiveBlockError):
            # The interactive guard fired after the host checks ran; surface the
            # readiness warnings alongside the block instead of discarding them.
            error_payload = _readiness_with_interactive_block(error.readiness_payload, error)
        else:
            error_payload = _reason_coded_payload(error.reason_code, str(error), error.details)
        _emit_payload(error_payload, fmt)
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
    # Mirror ``awf start``'s ``_resolve_start_source_checkout``: an explicit
    # ``--source-checkout`` is validated without reading host config, so a corrupt
    # or secret-bearing ``~/.awf/config.yml`` cannot abort a read-only dry-run probe
    # that never consumes that config. Host config is still read wherever it is
    # actually needed -- to resolve persisted source metadata (no explicit
    # ``--source-checkout``) or to persist safe config (non-dry-run, where a corrupt
    # config is a legitimate blocker because it cannot be safely merged) -- so only
    # the explicit-checkout dry-run path skips the read. ``config`` is therefore
    # ``None`` *only* on that path, which dereferences it nowhere below.
    config = read_host_setup_config() if source_checkout is None or not dry_run else None

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
        # Past the dry-run return this is a non-dry-run path, so the guarded read
        # above resolved ``config`` (it skips the read only for explicit-checkout
        # dry-run) and the consent write below can rely on it.
        assert config is not None
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
        #
        # Guard the write on there being consent to record: this branch only ever
        # records the plain-file flag (``source_checkout`` is ``None`` here), so
        # with neither an explicit ``--allow-plain-secrets`` nor a plain-file
        # consent already on disk (the common fresh-machine state) the write would
        # merely re-persist an identical config. Skip it rather than create/touch
        # the config file for nothing.
        if allow_plain_secrets or config.consent.plain_file_secrets:
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
        bootstrap_assets_missing=_default_discovery_bootstrap_assets_missing(probe_source),
    )

    if not dry_run:
        # The guarded read above always resolves ``config`` on a non-dry-run path
        # (it skips the read only for explicit-checkout dry-run), so the safe write
        # below can rely on it.
        assert config is not None
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
            _require_provider_interactive(non_interactive, payload)
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
            _require_provider_interactive(non_interactive, payload)

    return payload


def _run_client_setup(
    *,
    clients: list[str],
    dry_run: bool,
    source_checkout: Path | None,
) -> FirstRunPayload:
    """Dispatch the selected MCP client integrations and render one payload.

    Unknown client selectors raise ``SetupCheckError(SETUP_CLIENT_UNKNOWN)``
    (handled by ``setup_command`` as an exit-2 usage error). The selected clients
    are processed through ``setup_clients`` with the injected home/which/run/now
    seams, which plans every client before applying any so a conflict never
    leaves a partial write; a single client returns its payload verbatim,
    multiple are folded into one multi-issue report.
    """
    selected = normalize_clients(clients)
    try:
        env_file = _resolve_client_env_file(source_checkout)
    except SourceCheckoutError as error:
        # An explicit/persisted source checkout that fails validation must block
        # with the same reason the readiness flow surfaces instead of writing a
        # client config diff pointing the MCP server at a non-checkout env file.
        return _client_source_checkout_blocked_payload(error)
    home = _client_home()
    # Plan all selected clients before applying any so a single conflicting
    # client cannot leave earlier/later clients partially written while the run
    # reports blocked overall (see setup_clients).
    payloads = setup_clients(
        selected,
        env_file=env_file,
        dry_run=dry_run,
        home=home,
        which=_client_which,
        run=_client_run,
        now=_client_now,
        env=_client_env(),
    )
    if len(payloads) == 1:
        return payloads[0]
    return _combine_client_payloads(selected, payloads)


def _resolve_client_env_file(source_checkout: Path | None) -> Path:
    """Return the env-file path the registered MCP server should read.

    Reuses the same source-checkout / packaged-asset resolution ``awf start``
    uses so the registered ``--env-file`` matches the ``docker/compose/.env``
    ``awf start`` / ``awf setup`` actually honor: an explicit ``--source-checkout``
    is validated (``validate_source_checkout``, exactly like the readiness flow's
    ``_resolve_setup_source_checkout``) and pins that checkout's
    ``docker/compose/.env``; otherwise a previously persisted, still-valid source
    checkout (revalidated like ``awf start``'s ``_resolve_start_source_checkout``)
    pins *its* compose env; only with neither does it fall back to the
    packaged/default ``.env`` from ``resolve_service_compose_paths``. Both
    source-checkout branches resolve through ``resolve_existing_service_env_file``
    so a not-yet-bootstrapped checkout (``docker/compose/.env`` absent but the
    checkout root ``.env`` present) pins the same root ``.env`` ``awf start`` reads
    via ``_resolve_start_bootstrap_inputs`` -- otherwise ``awf mcp serve`` would
    reject the registered ``--env-file`` with "env file does not exist". Validating
    the explicit checkout keeps an invalid or stale path from registering an MCP
    ``--env-file`` that points at a non-checkout env file ``awf start`` would
    itself reject; the failure raises ``SourceCheckoutError`` for the caller to
    surface as a blocked payload. The file is never opened here -- only its path
    is threaded into the client config -- so a dry-run diff needs no env file on
    disk and no provider token is ever read.
    """
    if source_checkout is not None:
        compose_env = validate_source_checkout(source_checkout).root / "docker" / "compose" / ".env"
        return resolve_existing_service_env_file(compose_env)
    persisted = _persisted_client_source_checkout()
    if persisted is not None:
        return resolve_existing_service_env_file(persisted.root / "docker" / "compose" / ".env")
    _compose_file, raw_env_file, _env_example = resolve_service_compose_paths()
    # The packaged/default fallback may be a relative ``Path(".env")``. The
    # explicit and persisted checkout branches above already pin absolute paths;
    # the registered ``--env-file`` is read later by ``awf mcp serve``, which
    # resolves it against the client process cwd (mcp_commands._resolve_mcp_settings),
    # so a relative path would break Claude/Codex sessions launched from any other
    # directory. Pin the fallback to an absolute path here as well.
    return raw_env_file.resolve()


def _persisted_client_source_checkout() -> VerifiedSourceCheckout | None:
    """Return the persisted, revalidated source checkout, or ``None``.

    Mirrors ``awf start``'s ``_resolve_start_source_checkout`` so the MCP client
    ``--env-file`` honors source-checkout metadata stored by an earlier
    ``awf setup --source-checkout`` run instead of always defaulting to the
    packaged ``.env`` while ``awf start`` runs from the checkout's compose env. A
    missing/unreadable host config or stale metadata (the checkout moved or no
    longer matches the asset contract) falls back to default discovery rather than
    pinning a path ``awf start`` would itself reject.
    """
    try:
        config = read_host_setup_config()
    except HostSetupConfigError:
        return None
    if config.source_checkout is None:
        return None
    try:
        return verified_source_from_metadata(config.source_checkout)
    except SourceCheckoutError:
        return None


def _client_source_checkout_blocked_payload(error: SourceCheckoutError) -> FirstRunPayload:
    """Render a blocked client payload for an invalid/stale source checkout.

    Mirrors the readiness flow's ``_source_checkout_issue`` so the ``--client``
    path surfaces the same SOURCE_CHECKOUT_INVALID / SOURCE_CHECKOUT_ASSETS_STALE
    reason, root, and missing markers instead of registering an MCP ``--env-file``
    under a checkout ``awf start`` would reject.
    """
    details: dict[str, Any] = {"check": "source_checkout", "root": str(error.root)}
    if error.missing_markers:
        details["missing_markers"] = list(error.missing_markers)
    for key, value in error.details.items():
        details.setdefault(key, value)
    issue = first_run_issue_from_reason_code(
        error.reason_code,
        severity="blocked",
        details=details,
        problem=error.message,
    )
    return first_run_report_payload(
        command=SETUP_COMMAND,
        summary="AWF could not resolve the source checkout for MCP client setup.",
        issues=(issue,),
        next_steps=(
            "Fix the reported --source-checkout path above, then re-run "
            "awf setup --client <client>.",
        ),
    )


def _combine_client_payloads(
    clients: list[str],
    payloads: list[FirstRunPayload],
) -> FirstRunPayload:
    """Fold per-client payloads into one multi-issue report payload."""
    issues = tuple(issue for payload in payloads for issue in payload.issues)
    clients_detail = {
        client: {"status": payload.status, "summary": payload.summary, **dict(payload.details)}
        for client, payload in zip(clients, payloads, strict=True)
    }
    blocked = [payload for payload in payloads if payload.status in ("blocked", "failed")]
    if blocked:
        summary = (
            f"awf setup processed {len(clients)} MCP client(s) with {len(blocked)} blocker(s)."
        )
        next_steps: tuple[str, ...] = (
            "Resolve the reported client blocker(s) above, then re-run setup for that client.",
        )
    else:
        summary = f"awf setup processed {len(clients)} MCP client(s) successfully."
        next_steps = ("Restart the client sessions to load the AWF MCP server.",)
    return first_run_report_payload(
        command=SETUP_COMMAND,
        summary=summary,
        issues=issues,
        details={"clients": clients_detail},
        next_steps=next_steps,
    )


def _resolve_setup_source_checkout(
    source_checkout: Path | None,
    config: HostSetupConfig | None,
) -> tuple[
    VerifiedSourceCheckout | None, SourceCheckoutError | None, VerifiedSourceCheckout | None
]:
    """Resolve the source checkout whose env the readiness probe should honor.

    Returns ``(probe_source, source_error, explicit_source)``. ``probe_source``
    drives the readiness env probe and the rendered payload; ``explicit_source``
    is only set for an explicit ``--source-checkout`` selection and drives
    metadata persistence, so re-running ``awf setup`` without the flag never
    rewrites or refreshes the persisted metadata.

    ``config`` is consulted only on the no-explicit-``--source-checkout`` path (to
    revalidate persisted metadata), so the caller passes ``None`` when an explicit
    checkout was selected for a dry-run probe and no host config was read; that
    case never reaches the ``config``-dependent branch below.

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

    if config is None or config.source_checkout is None:
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
        # Only the read-env (first return value) is used here; the trusted
        # Compose env-file half is discarded. The default-discovery paths are not
        # verified to exist either (the no-asset-root fallback yields relative
        # ``.env``/``.env.example``), so pass ``paths_verified=False`` rather than
        # assert a verification setup never performs. Unlike ``awf start`` /
        # ``init`` / ``service``, which consume the Compose env-file half and pass
        # ``True``, the flag has no effect on the read-env this branch returns.
        default_read_env, _ = resolve_service_runtime_env_files(
            compose_file,
            raw_env_file,
            paths_verified=False,
        )
        return local_service_environ(env_file=default_read_env)

    compose_env_candidate = verified_source.root / "docker" / "compose" / ".env"
    resolved_read_env = resolve_existing_service_env_file(compose_env_candidate)
    read_env_file = resolved_read_env if resolved_read_env.exists() else None
    return local_service_environ(env_file=read_env_file)


def _default_discovery_bootstrap_assets_missing(
    probe_source: VerifiedSourceCheckout | None,
) -> bool:
    """Return whether default-discovery setup would leave ``awf start`` unable to bootstrap.

    Only the default-discovery path (no ``--source-checkout`` and no persisted
    source metadata, so ``probe_source`` is ``None``) can reach ``awf start``'s
    ``run_service_bootstrap`` with no resolvable asset root: its
    ``_resolve_bootstrap_assets`` then raises SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND
    (``awf start`` maps it to START_COMPOSE_ASSETS_MISSING). A verified source
    checkout — selected via ``--source-checkout`` or revalidated from persisted
    metadata — already pins a valid asset root that ``awf start`` reuses, so it
    never trips this. Mirror that exact ``get_bootstrap_asset_root()`` probe here
    so setup blocks instead of reporting a ready host and telling the operator to
    run a start that cannot resolve its compose/runtime assets.
    """
    if probe_source is not None:
        return False
    from awf.service.bootstrap import get_bootstrap_asset_root

    return get_bootstrap_asset_root() is None


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


class _ReadinessInteractiveBlockError(SetupCheckError):
    """Interactive-input block that carries the readiness payload to surface.

    The provider interactive guard fires only after the host checks have run, so
    this wraps the raised ``SetupCheckError`` with the already-built readiness
    payload. ``setup_command`` then folds the host-check report into the
    INTERACTIVE_INPUT_REQUIRED output instead of dropping the warnings (low disk,
    missing ``gh``, below-floor CPU/memory) the operator ran setup to see.
    """

    def __init__(self, error: SetupCheckError, readiness_payload: FirstRunPayload) -> None:
        """Re-wrap an interactive-guard error with the readiness payload."""
        super().__init__(str(error), reason_code=error.reason_code, details=error.details)
        self.readiness_payload = readiness_payload


def _require_provider_interactive(non_interactive: bool, payload: FirstRunPayload) -> None:
    """Demand interactive provider input, surfacing host checks on the block.

    When ``--non-interactive`` trips the guard the host checks have already run,
    so re-raise carrying the readiness payload (see ``_ReadinessInteractiveBlockError``)
    rather than letting the bare error discard the readiness warnings.
    """
    try:
        require_interactive(non_interactive, "configure the selected provider(s)")
    except SetupCheckError as error:
        raise _ReadinessInteractiveBlockError(error, payload) from error


def _readiness_with_interactive_block(
    payload: FirstRunPayload,
    error: SetupCheckError,
) -> FirstRunPayload:
    """Fold an interactive-input block into the readiness payload.

    Mirrors ``_readiness_with_config_write_failure``: the readiness issues and
    check provenance are preserved and the INTERACTIVE_INPUT_REQUIRED block is
    appended, so a scripting operator who keys on exit code 2 still sees the host
    warnings from that run rather than only the reason-coded input-required error.
    """
    interactive_issue = first_run_issue_from_reason_code(
        error.reason_code,
        severity="blocked",
        details=error.details,
    )
    return first_run_report_payload(
        command=SETUP_COMMAND,
        summary=f"{payload.summary} {error}",
        issues=(*payload.issues, interactive_issue),
        details=payload.details,
        next_steps=_reason_coded_next_steps(error.reason_code),
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
    if reason_code == SETUP_CLIENT_UNKNOWN:
        return (
            "Re-run awf setup with a supported --client; the accepted names are "
            "listed under known_clients in the issue details.",
        )
    if reason_code == SETUP_PROVIDER_CLIENT_CONFLICT:
        return (
            "Re-run awf setup with either --provider or --client, not both; the "
            "rejected selectors are listed under providers and clients in the "
            "issue details.",
        )
    if reason_code == SETUP_PLAIN_SECRETS_CLIENT_CONFLICT:
        return (
            "Re-run awf setup --client without --allow-plain-secrets; plain-file "
            "consent is recorded by the readiness/provider path (awf setup), not the "
            "client path.",
        )
    return ("Fix the reported issue above, then re-run awf setup --dry-run.",)
