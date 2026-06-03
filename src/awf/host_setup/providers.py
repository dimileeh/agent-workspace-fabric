"""Provider setup orchestration for AWF first-run host setup (T07).

This module ties together the foundations from T04 (the ``awf setup`` provider
selector + readiness payload aggregation) and T06 (the credential backend
abstraction in :mod:`awf.host_setup.credentials`) into a single orchestration
seam: it captures or discovers a provider credential, converts it into a *safe
reference* (``keyring://`` / ``env://`` / ``plain-file://`` — never a raw value),
rechecks the provider with the bounded, secret-redacting probes in
:mod:`awf.service.provider_readiness`, and returns a renderable, secret-free
summary plus an updated :class:`HostSetupConfig`.

GitHub is treated as **first-class** because PR creation, monitoring, and merging
depend on it: GitHub is marked ready when either ``gh auth status`` succeeds or an
environment-reference token (``AWF_GITHUB_TOKEN`` / ``GH_TOKEN`` / ``GITHUB_TOKEN``)
is configured, and a raw GitHub token is **never** stored.

Failure isolation is by construction: each provider is orchestrated
independently, so one missing/invalid credential marks only that provider
unavailable and never aborts the others. AWF Cloud is a deterministic stub slot
(no backend in v1); ``docker`` is infrastructure, not an agent-provider selector,
so it stays out of this surface.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from awf.common.logging import get_logger
from awf.host_setup.config import HostSetupConfig, ProviderConfig
from awf.host_setup.credentials import (
    CredentialBackend,
    CredentialError,
    CredentialRef,
    CredentialRequest,
    HostCredentialCapabilities,
    detect_host_credential_capabilities,
    store_provider_credential,
)
from awf.host_setup.rendering import (
    INTERACTIVE_INPUT_REQUIRED,
    PROVIDER_SETUP_AUTH_INVALID,
)
from awf.service.config import ServiceSettings
from awf.service.provider_readiness import (
    HttpGet,
    ProviderName,
    SubprocessRun,
    check_single_provider_readiness,
    default_subprocess_runner,
)

logger = get_logger(__name__)

ProviderSetupStatus = Literal["ready", "warning", "unavailable", "not_configured"]
ProviderSetupMode = Literal["all_providers", "targeted_recheck"]

# GitHub's bounded ``gh auth status`` probe reuses the same 5s budget the service
# readiness GitHub check uses, so first-run setup and runtime agree on the bound.
_GH_PROBE_TIMEOUT_SECONDS = 5.0
_GH_AUTH_STATUS_ARGS = ("gh", "auth", "status", "--hostname", "github.com")

_GhProbeOutcome = Literal["ok", "absent", "unusable"]


class ProviderSecretCapture(Protocol):
    """Resolve a captured/discovered secret value for a provider, or ``None``.

    The orchestrator calls this seam to obtain an interactively captured (or
    otherwise discovered) raw secret for a provider. It is injected so tests stay
    hermetic and never touch a real prompt; returning ``None`` means no secret was
    captured and the orchestrator falls back to an environment reference.
    """

    def __call__(self, provider: str) -> str | None:  # pragma: no cover - Protocol only.
        """Return the captured secret for ``provider`` or ``None``."""
        ...


@dataclass(frozen=True)
class ProviderSpec:
    """One setup provider's orchestration descriptor (a frozen registry entry)."""

    name: str
    readiness_provider: ProviderName | None
    env_ref_vars: tuple[str, ...] = ()
    github_first_class: bool = False
    # Whether configuring this provider needs a captured secret when neither a
    # ``gh``/env fallback nor a captured value is available; drives the
    # ``INTERACTIVE_INPUT_REQUIRED`` signal under ``--non-interactive``.
    needs_secret: bool = True
    stub: bool = False
    stub_reason_code: str = ""
    stub_summary: str = ""

    @property
    def primary_env_var(self) -> str | None:
        """Return the preferred environment-reference variable name, if any."""
        return self.env_ref_vars[0] if self.env_ref_vars else None


# Ordered registry: GitHub first (first-class), then the AWF Cloud stub slot,
# then the agent providers. Adding a future provider is one entry here, not a
# code change scattered across files. Every ``KNOWN_SETUP_PROVIDERS`` member maps
# to a readiness provider or is a declared stub slot (guarded by a unit test).
PROVIDER_REGISTRY: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        name="github",
        readiness_provider="github",
        # Order mirrors ``provider_readiness._GITHUB_TOKEN_ENV_KEYS`` and
        # ``ServiceSettings._resolve_github_token`` (``AWF_GITHUB_TOKEN`` first) so
        # ``awf setup`` accepts exactly the tokens ``awf start`` uses for PR work.
        env_ref_vars=("AWF_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"),
        github_first_class=True,
    ),
    ProviderSpec(
        name="awf_cloud",
        readiness_provider=None,
        needs_secret=False,
        stub=True,
        stub_reason_code="AWF_CLOUD_STUB",
        stub_summary=(
            "AWF Cloud is a reserved future provider slot; no setup is performed in this release."
        ),
    ),
    # Each agent provider's ``env_ref_vars`` mirror the matching
    # ``provider_readiness._<PROVIDER>_ENV_KEYS`` tuple (primary var first) so
    # ``awf setup`` discovers every alternate token ``awf start`` already accepts;
    # otherwise a config readiness would honor (e.g. CODEX_API_KEY) is rejected as
    # not_configured. ``test_registry_env_ref_vars_mirror_readiness`` guards drift.
    ProviderSpec(
        name="codex",
        readiness_provider="codex",
        env_ref_vars=("OPENAI_API_KEY", "OPENAI_API_TOKEN", "CODEX_API_KEY", "CODEX_AUTH_TOKEN"),
    ),
    ProviderSpec(
        name="claude_code",
        readiness_provider="claude_code",
        env_ref_vars=("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
    ),
    ProviderSpec(name="opencode", readiness_provider="opencode", env_ref_vars=("OLLAMA_API_KEY",)),
    ProviderSpec(
        name="gemini",
        readiness_provider="gemini",
        env_ref_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CLOUD_ACCESS_TOKEN"),
    ),
    ProviderSpec(name="cursor", readiness_provider="cursor", env_ref_vars=("CURSOR_API_KEY",)),
    ProviderSpec(name="grok", readiness_provider="grok", env_ref_vars=("XAI_API_KEY",)),
)

_SPEC_BY_NAME: Mapping[str, ProviderSpec] = {spec.name: spec for spec in PROVIDER_REGISTRY}


class ProviderSetupResult(BaseModel):
    """Per-provider orchestration outcome carrying only safe (non-secret) fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: str
    status: ProviderSetupStatus
    reason_code: str
    summary: str
    # ``backend``/``credential_ref`` carry only the non-secret backend kind and the
    # safe reference string (never a value); both stay ``None`` when nothing was
    # configured. No raw-secret field exists on this model by construction.
    backend: str | None = None
    credential_ref: str | None = None
    configured: bool = False
    rechecked: bool = False


class ProviderSetupSummary(BaseModel):
    """Renderable, secret-free summary of a provider setup orchestration run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: ProviderSetupMode
    selected: tuple[str, ...]
    providers: tuple[ProviderSetupResult, ...]
    overall_status: str

    @property
    def requires_interactive_input(self) -> bool:
        """Return whether any provider could only proceed with interactive input."""
        return any(result.reason_code == INTERACTIVE_INPUT_REQUIRED for result in self.providers)

    def result_for(self, name: str) -> ProviderSetupResult | None:
        """Return the result for ``name``, or ``None`` when it was not orchestrated."""
        for result in self.providers:
            if result.name == name:
                return result
        return None

    def to_details(self) -> dict[str, object]:
        """Return the status-only, secret-free dict for setup/start summaries.

        The shape stays compatible with ``start_commands._providers_summary``
        (top-level ``status`` plus a ``providers`` mapping of per-provider
        ``status``), so both ``awf setup`` and ``awf start`` can render it without
        any further change. Credential references are deliberately omitted — only
        the backend *kind* and status are surfaced.
        """
        return {
            "mode": self.mode,
            "selected": list(self.selected),
            "status": self.overall_status,
            "providers": {
                result.name: {
                    "status": result.status,
                    "reason_code": result.reason_code,
                    "backend": result.backend,
                    "configured": result.configured,
                    "rechecked": result.rechecked,
                }
                for result in self.providers
            },
        }


def render_provider_summary(summary: ProviderSetupSummary) -> dict[str, object]:
    """Return the status-only, secret-free summary dict for setup and start."""
    return summary.to_details()


def orchestrate_provider_setup(
    settings: ServiceSettings,
    *,
    selected_providers: Sequence[str],
    config: HostSetupConfig,
    allow_plain_secrets: bool,
    non_interactive: bool,
    environ: Mapping[str, str] | None = None,
    capture: ProviderSecretCapture | None = None,
    run_subprocess: SubprocessRun | None = None,
    http_get: HttpGet | None = None,
    keyring_backend: CredentialBackend | None = None,
    env_backend: CredentialBackend | None = None,
    plain_file_backend: CredentialBackend | None = None,
) -> tuple[ProviderSetupSummary, HostSetupConfig]:
    """Orchestrate provider setup and return the summary plus an updated config.

    When ``selected_providers`` is non-empty the run is a **targeted recheck**:
    only those providers are configured/probed, unselected providers are neither
    mutated in config nor probed, and the summary ``mode`` is ``targeted_recheck``.
    With an empty selection every configurable provider is orchestrated and the
    mode is ``all_providers``. All I/O (subprocess / HTTP / secret capture /
    credential backends) is injectable so callers and tests stay hermetic and
    every network probe is bounded.
    """
    env = dict(os.environ if environ is None else environ)
    selected = [name for name in selected_providers if name in _SPEC_BY_NAME]
    unknown = [name for name in selected_providers if name not in _SPEC_BY_NAME]
    if unknown:
        # Surface typos: filtering unknown names silently would flip an
        # all-typo selection to a full all-providers run, surprising the caller.
        logger.warning("host_setup.unknown_providers_ignored", providers=unknown)
    mode: ProviderSetupMode = "targeted_recheck" if selected else "all_providers"
    target_names = set(selected) if selected else set(_SPEC_BY_NAME)
    capabilities = detect_host_credential_capabilities(environ=env)
    resolved_run = run_subprocess or default_subprocess_runner()
    backends = _CredentialBackends(
        keyring=keyring_backend, env=env_backend, plain_file=plain_file_backend
    )

    providers_config = dict(config.providers)
    results: list[ProviderSetupResult] = []
    # Iterate registry order (not selection order) for a deterministic summary.
    for spec in PROVIDER_REGISTRY:
        if spec.name not in target_names:
            continue
        result, provider_config = _orchestrate_one(
            spec,
            settings=settings,
            environ=env,
            config=config,
            allow_plain_secrets=allow_plain_secrets,
            non_interactive=non_interactive,
            capabilities=capabilities,
            capture=capture,
            run_subprocess=resolved_run,
            http_get=http_get,
            backends=backends,
        )
        results.append(result)
        if provider_config is not None:
            providers_config[spec.name] = provider_config

    updated_config = config.model_copy(update={"providers": providers_config})
    summary = ProviderSetupSummary(
        mode=mode,
        selected=tuple(selected),
        providers=tuple(results),
        overall_status=_overall_status(results),
    )
    return summary, updated_config


@dataclass(frozen=True)
class _CredentialBackends:
    """Injected credential backends forwarded to ``store_provider_credential``."""

    keyring: CredentialBackend | None = None
    env: CredentialBackend | None = None
    plain_file: CredentialBackend | None = None


def _orchestrate_one(
    spec: ProviderSpec,
    *,
    settings: ServiceSettings,
    environ: dict[str, str],
    config: HostSetupConfig,
    allow_plain_secrets: bool,
    non_interactive: bool,
    capabilities: HostCredentialCapabilities,
    capture: ProviderSecretCapture | None,
    run_subprocess: SubprocessRun,
    http_get: HttpGet | None,
    backends: _CredentialBackends,
) -> tuple[ProviderSetupResult, ProviderConfig | None]:
    """Orchestrate a single provider; never raises for that provider's failure."""
    if spec.stub:
        return (
            ProviderSetupResult(
                name=spec.name,
                status="not_configured",
                reason_code=spec.stub_reason_code,
                summary=spec.stub_summary,
            ),
            None,
        )
    # A credential backend can raise ``CredentialError`` for a genuine storage or
    # consent fault (e.g. ``CREDENTIAL_REF_INVALID`` / ``INTERACTIVE_INPUT_REQUIRED``
    # that ``store_provider_credential`` deliberately propagates rather than
    # degrading). Contain it here so one provider's failure marks only that provider
    # unavailable and never aborts the registry loop — the per-provider isolation
    # this module documents by construction.
    try:
        if spec.github_first_class:
            return _orchestrate_github(
                spec,
                environ=environ,
                config=config,
                non_interactive=non_interactive,
                capabilities=capabilities,
                run_subprocess=run_subprocess,
                backends=backends,
            )
        return _orchestrate_agent_provider(
            spec,
            settings=settings,
            environ=environ,
            config=config,
            allow_plain_secrets=allow_plain_secrets,
            non_interactive=non_interactive,
            capabilities=capabilities,
            capture=capture,
            run_subprocess=run_subprocess,
            http_get=http_get,
            backends=backends,
        )
    except CredentialError as exc:
        return _credential_failure_result(spec, exc), None


def _orchestrate_github(
    spec: ProviderSpec,
    *,
    environ: dict[str, str],
    config: HostSetupConfig,
    non_interactive: bool,
    capabilities: HostCredentialCapabilities,
    run_subprocess: SubprocessRun,
    backends: _CredentialBackends,
) -> tuple[ProviderSetupResult, ProviderConfig | None]:
    """Mark GitHub ready via ``gh`` or an env ref, never storing a raw token."""
    env_var = _first_present(environ, spec.env_ref_vars)
    outcome = _probe_gh_auth(run_subprocess, _gh_probe_environ(environ, env_var))

    if outcome == "ok":
        provider_config = _env_ref_config(spec, env_var, capabilities, backends, source="gh")
        return (
            _ready_result(
                spec,
                reason_code="GITHUB_GH_AUTH_OK",
                summary="GitHub is ready via gh CLI authentication.",
                provider_config=provider_config,
                rechecked=True,
            ),
            provider_config,
        )

    if env_var is not None and outcome == "absent":
        # ``gh`` is not installed to verify, but a service-visible token reference
        # is configured; record the safe ``env://NAME`` ref (never the value).
        provider_config = _env_ref_config(spec, env_var, capabilities, backends, source="env")
        return (
            _ready_result(
                spec,
                reason_code="GITHUB_ENV_REF_CONFIGURED",
                summary=f"GitHub is ready via the {env_var} environment reference.",
                provider_config=provider_config,
                rechecked=False,
            ),
            provider_config,
        )

    if env_var is not None:
        # A token reference is configured but ``gh auth status`` could not confirm
        # it. A non-zero exit (or timeout/launch failure) collapses to "unusable"
        # and cannot be attributed to the token: it is equally an invalid token or
        # a transient ``gh``/network failure (SERVFAIL, DNS timeout). Label it
        # ``GITHUB_GH_PROBE_FAILED`` rather than ``PROVIDER_SETUP_AUTH_INVALID`` so a
        # transient outage at setup time does not brand a valid token as permanently
        # auth-invalid (which the latter reason code asserts). Still record an
        # *unavailable* config (never a ready ref) so a prior ready GitHub entry
        # cannot persist as ready while the summary shows unavailable; stay
        # non-blocking for others.
        provider_config = _env_ref_config(
            spec, env_var, capabilities, backends, source="env", status="unavailable"
        )
        return (
            ProviderSetupResult(
                name=spec.name,
                status="unavailable",
                reason_code="GITHUB_GH_PROBE_FAILED",
                summary=(
                    f"`gh auth status` could not confirm GitHub auth via {env_var} "
                    "(invalid token or a transient gh/network failure)."
                ),
                backend=provider_config.backend,
                credential_ref=provider_config.credential_ref,
                configured=False,
                rechecked=True,
            ),
            provider_config,
        )

    existing = config.providers.get(spec.name)
    if existing is not None:
        # No visible token and ``gh`` could not confirm auth, but a prior run
        # persisted a GitHub entry (always an ``env://`` ref — GitHub never stores
        # a raw token). Route it through the shared helper so a stale ``ready`` env
        # ref is degraded to ``unavailable`` and re-persisted, mirroring the
        # agent-provider path; without this the not-configured (config=None) return
        # would leave the old ready entry on disk, disagreeing with this recheck.
        return _preserved_existing_result(spec, existing)
    return _not_configured_result(spec, non_interactive=non_interactive)


def _orchestrate_agent_provider(
    spec: ProviderSpec,
    *,
    settings: ServiceSettings,
    environ: dict[str, str],
    config: HostSetupConfig,
    allow_plain_secrets: bool,
    non_interactive: bool,
    capabilities: HostCredentialCapabilities,
    capture: ProviderSecretCapture | None,
    run_subprocess: SubprocessRun,
    http_get: HttpGet | None,
    backends: _CredentialBackends,
) -> tuple[ProviderSetupResult, ProviderConfig | None]:
    """Configure a captured/env credential into a ref, then recheck the provider."""
    secret = capture(spec.name) if capture is not None else None
    if secret is not None and secret.strip():
        ref, source = _store_captured_secret(
            spec,
            secret=secret,
            allow_plain_secrets=allow_plain_secrets,
            plain_file_consent=config.consent.plain_file_secrets,
            capabilities=capabilities,
            backends=backends,
        )
        recheck_environ = dict(environ)
        # Expose the captured secret to the bounded readiness probe only (a
        # transient, never-persisted copy) so the recheck reflects the real
        # credential; the raw value never reaches the ref, summary, or config.
        if spec.primary_env_var is not None and ref.backend != "env_ref":
            recheck_environ[spec.primary_env_var] = secret
    else:
        env_var = _first_present(environ, spec.env_ref_vars)
        if env_var is None:
            existing = config.providers.get(spec.name)
            if existing is not None:
                # The provider was configured by a prior run (e.g. keyring or
                # plain_file) but no fresh secret/env is available to re-probe.
                # Preserve and report that state instead of falsely reporting
                # not_configured and discarding the existing ref.
                return _preserved_existing_result(spec, existing)
            return _not_configured_result(spec, non_interactive=non_interactive)
        ref = _build_env_ref(spec, env_var, capabilities, backends)
        source = "env"
        recheck_environ = dict(environ)

    readiness_provider = spec.readiness_provider
    if readiness_provider is None:
        # Non-stub providers always map to a readiness provider — the registry
        # invariant guarded by ``test_registry_covers_every_known_setup_provider``.
        # Degrade to an unavailable result rather than ``assert``: an
        # ``AssertionError`` would escape the ``CredentialError`` containment in
        # ``_orchestrate_one`` and abort every remaining provider (breaking the
        # non-blocking contract), and ``assert`` is stripped entirely under
        # ``python -O``. An explicit return keeps per-provider isolation intact and
        # surfaces the misconfiguration as a reason-coded, secret-free result.
        return (
            ProviderSetupResult(
                name=spec.name,
                status="unavailable",
                reason_code="PROVIDER_READINESS_UNMAPPED",
                summary=(
                    f"No readiness provider is mapped for {spec.name}; "
                    "cannot recheck this provider."
                ),
                configured=False,
                rechecked=False,
            ),
            None,
        )
    readiness = check_single_provider_readiness(
        settings,
        provider=readiness_provider,
        environ=recheck_environ,
        run_subprocess=run_subprocess,
        http_get=http_get,
    )
    ok = readiness.get("ok") is True
    ref_status = "ready" if ok else "unavailable"
    provider_config = ProviderConfig(
        **ref.to_provider_config_fields(status=ref_status),
        source=source,
    )
    if ok:
        result = _ready_result(
            spec,
            reason_code=str(readiness.get("reason") or "PROVIDER_READY"),
            summary=str(readiness.get("message") or "Provider authentication is ready."),
            provider_config=provider_config,
            rechecked=True,
        )
    else:
        result = ProviderSetupResult(
            name=spec.name,
            status="unavailable",
            reason_code=PROVIDER_SETUP_AUTH_INVALID,
            summary=str(readiness.get("message") or "Provider authentication is not usable."),
            backend=provider_config.backend,
            credential_ref=provider_config.credential_ref,
            configured=True,
            rechecked=True,
        )
    return result, provider_config


def _store_captured_secret(
    spec: ProviderSpec,
    *,
    secret: str,
    allow_plain_secrets: bool,
    plain_file_consent: bool,
    capabilities: HostCredentialCapabilities,
    backends: _CredentialBackends,
) -> tuple[CredentialRef, str]:
    """Store a captured secret behind the preferred backend; return its safe ref."""
    ref = store_provider_credential(
        CredentialRequest(
            provider=spec.name,
            env_var=spec.primary_env_var,
            secret_source=lambda: secret,
        ),
        preferred=None,
        capabilities=capabilities,
        allow_plain_secrets=allow_plain_secrets,
        plain_file_consent=plain_file_consent,
        keyring_backend=backends.keyring,
        env_backend=backends.env,
        plain_file_backend=backends.plain_file,
    )
    return ref, "captured"


def _build_env_ref(
    spec: ProviderSpec,
    env_var: str,
    capabilities: HostCredentialCapabilities,
    backends: _CredentialBackends,
) -> CredentialRef:
    """Encode an ``env://NAME`` reference for a discovered env credential."""
    return store_provider_credential(
        CredentialRequest(provider=spec.name, env_var=env_var),
        preferred="env_ref",
        capabilities=capabilities,
        env_backend=backends.env,
    )


def _env_ref_config(
    spec: ProviderSpec,
    env_var: str | None,
    capabilities: HostCredentialCapabilities,
    backends: _CredentialBackends,
    *,
    source: str,
    status: str = "ready",
) -> ProviderConfig:
    """Build a ``ProviderConfig`` for GitHub (gh-managed or env-ref)."""
    if env_var is None:
        # gh-managed auth with no service-visible token: ready, but there is no
        # reference to store (the credential lives in the gh keychain).
        return ProviderConfig(status=status, source=source)
    ref = _build_env_ref(spec, env_var, capabilities, backends)
    return ProviderConfig(**ref.to_provider_config_fields(status=status), source=source)


def _ready_result(
    spec: ProviderSpec,
    *,
    reason_code: str,
    summary: str,
    provider_config: ProviderConfig,
    rechecked: bool,
) -> ProviderSetupResult:
    """Build a ready provider result mirroring its persisted (safe) ref fields."""
    return ProviderSetupResult(
        name=spec.name,
        status="ready",
        reason_code=reason_code,
        summary=summary,
        backend=provider_config.backend,
        credential_ref=provider_config.credential_ref,
        configured=True,
        rechecked=rechecked,
    )


def _not_configured_result(
    spec: ProviderSpec,
    *,
    non_interactive: bool,
) -> tuple[ProviderSetupResult, None]:
    """Build a not-configured result, signalling interactive input when blocked."""
    if non_interactive and spec.needs_secret:
        return (
            ProviderSetupResult(
                name=spec.name,
                status="not_configured",
                reason_code=INTERACTIVE_INPUT_REQUIRED,
                summary=(
                    f"{spec.name} needs a credential that requires interactive input to capture."
                ),
            ),
            None,
        )
    return (
        ProviderSetupResult(
            name=spec.name,
            status="not_configured",
            reason_code=f"{spec.name.upper()}_NOT_CONFIGURED",
            summary=f"No credential was found for {spec.name}.",
        ),
        None,
    )


def _preserved_existing_result(
    spec: ProviderSpec,
    existing: ProviderConfig,
) -> tuple[ProviderSetupResult, ProviderConfig]:
    """Preserve an already-configured provider when no fresh credential is supplied.

    A recheck that finds neither a captured secret nor an env reference must not
    erase or misreport a provider that a prior run configured (keyring / plain_file
    / env_ref). Without this, the summary would surface ``not_configured`` while the
    persisted config still holds a ready ref. Report the last-known state verbatim
    with ``rechecked=False`` (it was not re-probed this run) and return the existing
    config unchanged.

    An ``env_ref``-backed entry is the exception: its readiness depends entirely on
    the backing environment variable being visible, and this branch is only reached
    when no such variable is present (``_first_present`` returned ``None``). Carrying
    a prior ``ready`` ``env://`` ref forward would report stale readiness after an
    operator removed the token (e.g. ``OPENAI_API_KEY``), while the service readiness
    path would fail because no token is visible. Degrade such an entry to
    ``unavailable`` so ``awf setup`` and runtime agree.
    """
    backend_label = existing.backend or "stored"
    if existing.backend == "env_ref" and _as_setup_status(existing.status) == "ready":
        degraded = existing.model_copy(update={"status": "unavailable"})
        return (
            ProviderSetupResult(
                name=spec.name,
                status="unavailable",
                reason_code=f"{spec.name.upper()}_ENV_REF_MISSING",
                summary=(
                    f"Previously ready {spec.name} {backend_label} reference is no longer "
                    "backed by a visible environment variable; marking unavailable."
                ),
                backend=existing.backend,
                credential_ref=existing.credential_ref,
                configured=True,
                rechecked=True,
            ),
            degraded,
        )
    return (
        ProviderSetupResult(
            name=spec.name,
            status=_as_setup_status(existing.status),
            reason_code=f"{spec.name.upper()}_PRESERVED",
            summary=f"Preserved existing {backend_label} {spec.name} configuration.",
            backend=existing.backend,
            credential_ref=existing.credential_ref,
            configured=True,
            rechecked=False,
        ),
        existing,
    )


def _as_setup_status(status: str) -> ProviderSetupStatus:
    """Narrow a persisted provider status to a setup status, never over-reporting.

    Persisted provider configs only ever record ``ready``/``unavailable`` (the
    schema default is ``missing``). Map only an exact ``ready`` to ``ready``; any
    other value — including a hand-edited config — degrades to ``unavailable`` so a
    preserved, un-reprobed entry can never over-report readiness.
    """
    return "ready" if status == "ready" else "unavailable"


def _credential_failure_result(spec: ProviderSpec, exc: CredentialError) -> ProviderSetupResult:
    """Build an unavailable result from a contained credential-backend failure.

    The error is reason-coded and secret-free by construction (see
    :class:`CredentialError`), so its ``reason_code``/``message`` flow straight into
    the result. No config is persisted for the provider: storage failed, so there is
    no safe ref to record, and the existing config (if any) stays untouched.
    """
    logger.warning(
        "provider_setup.credential_error",
        provider=spec.name,
        reason_code=exc.reason_code,
    )
    return ProviderSetupResult(
        name=spec.name,
        status="unavailable",
        reason_code=exc.reason_code,
        summary=exc.message,
        configured=False,
        rechecked=False,
    )


def _gh_probe_environ(environ: Mapping[str, str], env_var: str | None) -> Mapping[str, str]:
    """Map a resolved env-ref token into the names ``gh`` actually reads.

    GitHub CLI only honors ``GH_TOKEN``/``GITHUB_TOKEN`` for non-interactive auth,
    but AWF documents ``AWF_GITHUB_TOKEN`` as the service token. Mirror
    :func:`awf.service.provider_readiness._check_github`, which copies the resolved
    token into both ``gh`` names before probing, so an operator who exports only
    ``AWF_GITHUB_TOKEN`` is not falsely reported as GitHub-unavailable. When no
    env ref is present the environment passes through unchanged so a keychain-only
    ``gh`` login is still probed as-is.
    """
    if env_var is None:
        return environ
    token = environ.get(env_var)
    if token is None or not token.strip():  # pragma: no cover - _first_present guarantees a value.
        return environ
    return {**dict(environ), "GH_TOKEN": token, "GITHUB_TOKEN": token}


def _probe_gh_auth(run_subprocess: SubprocessRun, environ: Mapping[str, str]) -> _GhProbeOutcome:
    """Run a bounded ``gh auth status`` probe and classify the outcome.

    Returns ``"ok"`` on a zero exit, ``"absent"`` when ``gh`` is not installed,
    and ``"unusable"`` for a non-zero exit, timeout, or any other launch failure.
    The probe is non-blocking: it never raises, so one provider's failure cannot
    abort the others.
    """
    try:
        result = run_subprocess(
            list(_GH_AUTH_STATUS_ARGS),
            check=False,
            capture_output=True,
            text=True,
            timeout=_GH_PROBE_TIMEOUT_SECONDS,
            env=environ,
        )
    except FileNotFoundError:
        return "absent"
    except (OSError, subprocess.SubprocessError):
        # A non-zero exit, timeout, or launch failure all mean ``gh`` could not
        # confirm usable auth. Record a secret-free signal and degrade rather than
        # crash the orchestration.
        logger.warning("host_setup.github_probe_failed")
        return "unusable"
    return "ok" if result.returncode == 0 else "unusable"


def _first_present(environ: Mapping[str, str], names: Sequence[str]) -> str | None:
    """Return the first env var name in ``names`` present and non-empty in env."""
    for name in names:
        value = environ.get(name)
        if value is not None and value.strip():
            return name
    return None


def _overall_status(results: Sequence[ProviderSetupResult]) -> str:
    """Aggregate per-provider statuses without ever blocking on one failure."""
    if results and all(result.status == "ready" for result in results):
        return "ready"
    if any(result.status == "ready" for result in results):
        return "partial"
    return "not_ready"


__all__ = [
    "PROVIDER_REGISTRY",
    "ProviderSecretCapture",
    "ProviderSetupMode",
    "ProviderSetupResult",
    "ProviderSetupStatus",
    "ProviderSetupSummary",
    "ProviderSpec",
    "orchestrate_provider_setup",
    "render_provider_summary",
]
