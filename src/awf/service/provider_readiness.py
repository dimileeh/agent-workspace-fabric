"""Credential readiness checks for local service agent providers."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from awf.db.enums import AgentRuntime
from awf.service.config import ServiceSettings
from awf.service.workspace_observability import effective_agent_identity

ProviderName = Literal[
    "github",
    "codex",
    "claude_code",
    "cursor",
    "gemini",
    "opencode",
    "grok",
    "docker",
]

PROVIDER_NAMES: tuple[ProviderName, ...] = (
    "github",
    "codex",
    "claude_code",
    "cursor",
    "gemini",
    "opencode",
    "grok",
    "docker",
)

_GITHUB_TIMEOUT_SECONDS = 5.0
_HTTP_TIMEOUT_SECONDS = 2.0
_PROVIDER_PROBE_TIMEOUT_SECONDS = 5.0
# Wall/read bound for a streamed ``POST /api/pull``. Multi-GB models take
# minutes, so the bound is generous; a stalled stream still cannot hang forever
# because the bound is passed through to the injected HTTP stream seam.
_OLLAMA_PULL_TIMEOUT_SECONDS = 1800.0
_TRACEBACK_LOG_LIMIT = 4000
_REDACTION = "<redacted>"
_CODEX_AUTH_FILES = ("auth.json", "config.toml", "installation_id")
_OLLAMA_AUTH_FILES = ("config.json", "id_ed25519", "id_ed25519.pub")

_GITHUB_TOKEN_ENV_KEYS = ("AWF_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
_CODEX_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "CODEX_API_KEY",
    "CODEX_AUTH_TOKEN",
)
_CLAUDE_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
_CURSOR_ENV_KEYS = ("CURSOR_API_KEY",)
_GEMINI_ENV_KEYS = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_CLOUD_ACCESS_TOKEN",
)
_OPENCODE_ENV_KEYS = ("OLLAMA_API_KEY",)
_XAI_ENV_KEYS = ("XAI_API_KEY",)
_DOCKER_AUTH_ENV_KEYS = ("DOCKER_AUTH_CONFIG",)
KNOWN_SECRET_ENV_KEYS = frozenset(
    (
        *_GITHUB_TOKEN_ENV_KEYS,
        *_CODEX_ENV_KEYS,
        *_CLAUDE_ENV_KEYS,
        *_CURSOR_ENV_KEYS,
        *_GEMINI_ENV_KEYS,
        *_OPENCODE_ENV_KEYS,
        *_XAI_ENV_KEYS,
        *_DOCKER_AUTH_ENV_KEYS,
        "GOOGLE_APPLICATION_CREDENTIALS_JSON",
    )
)
_SECRET_ENV_KEY_SUFFIXES = (
    "_TOKEN",
    "_API_KEY",
    "_API_TOKEN",
    "_ACCESS_KEY",
    "_PRIVATE_KEY",
    "_PASSWORD",
    "_PASSWD",
    "_SECRET",
)
_SECRET_ENV_KEY_NAMES = {
    *{suffix.removeprefix("_") for suffix in _SECRET_ENV_KEY_SUFFIXES},
    "ACCESSKEY",
    "APIKEY",
    "PRIVATEKEY",
}

URL_CREDENTIAL_RE = re.compile(r"(https?://)([^/\s:@]+(?::[^/\s@]+)?@)")
_GITHUB_TOKEN_PATTERNS = (
    r"gh[pousr]_[A-Za-z0-9_]{8,}",
    r"github_pat_[A-Za-z0-9_]{8,}",
)
_OPENAI_TOKEN_PATTERNS = (r"sk-proj-[A-Za-z0-9_-]{8,}",)
_ANTHROPIC_TOKEN_PATTERNS = (r"sk-ant-[A-Za-z0-9_-]{8,}",)
_LEGACY_OPENAI_TOKEN_PATTERNS = (r"sk-[A-Za-z0-9]{20,}",)
_GOOGLE_TOKEN_PATTERNS = (r"AIza[A-Za-z0-9_-]{12,}",)
_SLACK_TOKEN_PATTERNS = (r"xox[baprs]-[A-Za-z0-9-]{8,}",)
# Keep provider-specific sk-* families before the legacy sk-* fallback.
_KNOWN_TOKEN_PATTERNS = (
    *_GITHUB_TOKEN_PATTERNS,
    *_OPENAI_TOKEN_PATTERNS,
    *_ANTHROPIC_TOKEN_PATTERNS,
    *_LEGACY_OPENAI_TOKEN_PATTERNS,
    *_GOOGLE_TOKEN_PATTERNS,
    *_SLACK_TOKEN_PATTERNS,
)
TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(_KNOWN_TOKEN_PATTERNS) + r")(?![A-Za-z0-9])")
_LAUNCH_PROVIDER_BY_AGENT: Mapping[AgentRuntime, ProviderName] = {
    AgentRuntime.codex: "codex",
    AgentRuntime.claude_code: "claude_code",
    AgentRuntime.cursor: "cursor",
    AgentRuntime.gemini: "gemini",
    AgentRuntime.opencode: "opencode",
    AgentRuntime.grok: "grok",
}
_RedactionSegment = tuple[Literal["literal", "redaction"], str]
_log = logging.getLogger(__name__)


def is_secret_env_key(key: str) -> bool:
    """Return true when an env key conventionally carries a secret value."""
    normalized = key.upper().replace("-", "_")
    return (
        normalized in KNOWN_SECRET_ENV_KEYS
        or normalized in _SECRET_ENV_KEY_NAMES
        or normalized.endswith(_SECRET_ENV_KEY_SUFFIXES)
    )


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


class SubprocessRun(Protocol):
    def __call__(  # pragma: no cover - Protocol method declaration only.
        self,
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: Literal[True],
        timeout: float,
        env: Mapping[str, str],
    ) -> CompletedProcessLike: ...


class HttpResponseLike(Protocol):
    @property
    def status_code(self) -> int: ...  # pragma: no cover - Protocol declaration only.

    @property
    def text(self) -> str: ...  # pragma: no cover - Protocol declaration only.


class HttpGet(Protocol):
    def __call__(  # pragma: no cover - Protocol method declaration only.
        self,
        url: str,
        *,
        timeout: float,
    ) -> HttpResponseLike: ...


class HttpStreamResponseLike(Protocol):
    @property
    def status_code(self) -> int: ...  # pragma: no cover - Protocol declaration only.

    def iter_lines(  # pragma: no cover - Protocol method declaration only.
        self,
    ) -> Iterator[str]: ...


class HttpPostStream(Protocol):
    def __call__(  # pragma: no cover - Protocol method declaration only.
        self,
        url: str,
        *,
        json: Mapping[str, Any],
        timeout: float,
    ) -> AbstractContextManager[HttpStreamResponseLike]: ...


class ProviderReadinessError(ValueError):
    """Raised when a strict provider selector is not recognized."""


def collect_agent_readiness(
    settings: ServiceSettings,
    *,
    environ: Mapping[str, str] | None = None,
    strict_providers: Iterable[str] | None = None,
    validated_strict_providers: set[ProviderName] | None = None,
    run_subprocess: SubprocessRun | None = None,
    http_get: HttpGet | None = None,
) -> dict[str, Any]:
    """Return redacted local-service readiness for agent provider credentials.

    ``strict_providers`` accepts raw operator input and is validated here.
    ``validated_strict_providers`` is for callers that already validated the
    names before entering a concurrent readiness fan-out.
    """

    env = os.environ if environ is None else environ
    strict = (
        set(validated_strict_providers)
        if validated_strict_providers is not None
        else validate_provider_names(strict_providers or ())
    )
    host_home = Path(settings.host_home or "~").expanduser()
    secrets = _secret_values(settings, env)
    resolved_run = run_subprocess or _run_subprocess
    resolved_http_get = http_get or _http_get

    providers: dict[str, dict[str, Any]] = {
        provider: _check_provider_readiness(
            provider,
            settings,
            environ=env,
            host_home=host_home,
            strict=provider in strict,
            run_subprocess=resolved_run,
            http_get=resolved_http_get,
            secrets=secrets,
        )
        for provider in PROVIDER_NAMES
    }
    return {
        "status": "fail"
        if any(provider["status"] == "fail" for provider in providers.values())
        else "ok",
        "strict_providers": _ordered_names(strict),
        "providers": providers,
        "security": _security_summary(providers),
    }


def check_single_provider_readiness(
    settings: ServiceSettings,
    *,
    provider: ProviderName,
    environ: Mapping[str, str] | None = None,
    run_subprocess: SubprocessRun | None = None,
    http_get: HttpGet | None = None,
) -> dict[str, Any]:
    """Return the redacted, strict readiness result for a single provider.

    This is an additive integration seam for provider setup orchestration (T07):
    it probes exactly one provider with the same bounded, secret-redacting checks
    ``collect_agent_readiness`` runs, but without touching the other providers, so
    a targeted recheck never invokes an unselected provider's subprocess/HTTP
    probe. Existing readiness callers are unchanged.
    """

    env = os.environ if environ is None else environ
    host_home = Path(settings.host_home or "~").expanduser()
    secrets = _secret_values(settings, env)
    return _check_provider_readiness(
        provider,
        settings,
        environ=env,
        host_home=host_home,
        strict=True,
        run_subprocess=run_subprocess or _run_subprocess,
        http_get=http_get or _http_get,
        secrets=secrets,
    )


def default_subprocess_runner() -> SubprocessRun:
    """Return the default bounded subprocess runner used by readiness probes.

    A stable public factory so callers outside this package (e.g. host setup
    provider orchestration) can reuse the exact ``subprocess.run`` wrapper the
    readiness checks default to — without importing the private ``_run_subprocess``
    helper and coupling to a name that may move or be renamed during a refactor.
    """
    return _run_subprocess


def selected_provider_readiness_preflight(
    settings: ServiceSettings,
    *,
    agent: AgentRuntime | str,
    task_policy: Mapping[str, object] | None,
    override: bool = False,
    override_reason: str | None = None,
    environ: Mapping[str, str] | None = None,
    run_subprocess: SubprocessRun | None = None,
    http_get: HttpGet | None = None,
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    """Return launch admission readiness for the selected LLM provider.

    This is intentionally stricter than the broad service readiness report:
    only the selected agent provider is strict, the effective model is included,
    and providers with stale/non-portable file auth get a cheap non-prompt probe.
    """

    env = os.environ if environ is None else environ
    secrets = _secret_values(settings, env)
    host_home = Path(settings.host_home or "~").expanduser()
    identity = effective_agent_identity(agent=agent, task_policy=task_policy)
    runtime = _coerce_launch_agent(agent)
    checked = checked_at or datetime.now(UTC)
    resolved_run = run_subprocess or _run_subprocess
    resolved_http_get = http_get or _http_get

    if runtime is None or runtime not in _LAUNCH_PROVIDER_BY_AGENT:
        return _launch_preflight_payload(
            agent=str(agent),
            provider="unknown",
            model=identity.model,
            model_source=identity.model_source,
            provider_result=None,
            probe={"status": "skipped", "reason_code": "UNSUPPORTED_AGENT_RUNTIME"},
            reason_code="UNSUPPORTED_AGENT_RUNTIME",
            message=f"Agent runtime {agent!s} is not supported for launch preflight.",
            override=override,
            override_reason=override_reason,
            checked_at=checked,
            secrets=secrets,
        )

    provider = _LAUNCH_PROVIDER_BY_AGENT[runtime]
    if provider == "opencode" and _opencode_model_targets_non_ollama_provider(identity.model):
        # A provider-qualified non-Ollama model is served by an OpenCode cloud
        # provider, which needs an OpenCode/provider credential. With none visible
        # (#554), fail create-time admission up front with a clear reason —
        # symmetric to OPENCODE_OLLAMA_AUTH_MISSING — instead of deferring to the
        # provider and surfacing a confusing agent-CLI error later. No probe runs
        # in the no-creds path (mirroring how OPENCODE_OLLAMA_AUTH_MISSING blocks
        # before any probe).
        creds_present, _creds_signal = _opencode_provider_credentials_present(
            identity.model, env, host_home
        )
        if not creds_present:
            target_provider = (identity.model or "").strip().partition("/")[0].lower()
            provider_env_hint = (
                " / ".join(_OPENCODE_PROVIDER_ENV_KEYS.get(target_provider, ()))
                or "the provider API key"
            )
            auth_missing_message = (
                f"OpenCode model {identity.model!r} targets the {target_provider!r} "
                "provider but no OpenCode/provider credentials were visible. Mount "
                f"~/.config/opencode or set {provider_env_hint}."
            )
            provider_result = _provider_result(
                ok=False,
                strict=True,
                reason="OPENCODE_PROVIDER_AUTH_MISSING",
                message=auth_missing_message,
                secrets=secrets,
                credential_scope="not_observed",
                isolation="none",
            )
            return _launch_preflight_payload(
                agent=runtime.value,
                provider=provider,
                model=identity.model,
                model_source=identity.model_source,
                provider_result=provider_result,
                probe={"status": "skipped", "reason_code": "OPENCODE_PROVIDER_AUTH_MISSING"},
                reason_code="OPENCODE_PROVIDER_AUTH_MISSING",
                message=auth_missing_message,
                override=override,
                override_reason=override_reason,
                checked_at=checked,
                secrets=secrets,
            )
        # OpenCode can run a provider-qualified non-Ollama model (``openai/...``
        # or ``anthropic/...``) served by the selected provider rather than the
        # local Ollama daemon. ``_check_opencode`` only knows how to probe Ollama
        # auth/host, so running it here would reject the workspace with an Ollama
        # reason code at create time — before the executor's pre-agent step could
        # skip it. Skip only the Ollama auth/daemon checks; the OpenCode CLI must
        # still be present in the runtime image regardless of which provider serves
        # the model, so keep the generic runtime-CLI availability probe — otherwise
        # a runtime image missing the ``opencode`` binary would be admitted as
        # ready here and only fail later as an agent command failure.
        deferred_message = (
            f"OpenCode model {identity.model!r} targets a non-Ollama provider; the "
            "Ollama auth/daemon preflight does not apply and is skipped."
        )
        provider_result = _provider_result(
            ok=True,
            strict=True,
            reason="OPENCODE_NON_OLLAMA_PROVIDER_SELECTED",
            message=deferred_message,
            secrets=secrets,
            credential_scope="deferred_to_provider",
            isolation="none",
        )
        cli_probe = _probe_agent_runtime_cli(
            settings,
            executable="opencode",
            provider=provider,
            environ=env,
            run_subprocess=resolved_run,
            secrets=secrets,
        )
        if cli_probe.get("status") == "ok":
            probe: dict[str, Any] = {
                "status": "unavailable",
                "reason_code": "OPENCODE_NON_OLLAMA_PROVIDER_SELECTED",
            }
            reason_code = "OPENCODE_NON_OLLAMA_PROVIDER_SELECTED"
            message = deferred_message
        else:
            probe = cli_probe
            reason_code = str(cli_probe.get("reason_code") or "PROVIDER_PROBE_FAILED")
            message = str(
                cli_probe.get("message")
                or "OpenCode runtime CLI is not available in the configured runtime image."
            )
        return _launch_preflight_payload(
            agent=runtime.value,
            provider=provider,
            model=identity.model,
            model_source=identity.model_source,
            provider_result=provider_result,
            probe=probe,
            reason_code=reason_code,
            message=message,
            override=override,
            override_reason=override_reason,
            checked_at=checked,
            secrets=secrets,
        )
    if (
        provider == "opencode"
        and not _ollama_url_host_reachable_from_worker(env)
        and _opencode_ollama_host_probe_deferrable(identity.model, env, host_home)
    ):
        # #569 symmetry: this create/retry admission path runs in the worker/service
        # process off ``awf_net`` and cannot reach a workspace Compose service DNS
        # name such as ``http://ollama-sidecar:11434``. A worker-side ``/api/version``
        # / ``/api/tags`` probe of such a host would falsely reject the workspace
        # with ``OLLAMA_HOST_UNREACHABLE`` (auth visible) or ``OPENCODE_OLLAMA_AUTH_
        # MISSING`` (authless local, daemon reachability cannot be verified to waive)
        # before the executor pre-agent step — which already skips the same probe for
        # *any* non-host-reachable URL — could defer it. Skip the Ollama auth/daemon
        # preflight here too and defer to the agent container where the sidecar daemon
        # IS reachable. ``_opencode_ollama_host_probe_deferrable`` covers a local model
        # (authless) and a ``:cloud`` model whose Cloud credential is already visible,
        # while a credential-less cloud model and the non-Ollama provider model (both
        # handled above / via ``_check_opencode``) still fall through to their gates.
        return _opencode_local_ollama_host_deferred_preflight(
            settings,
            runtime=runtime,
            model=identity.model,
            model_source=identity.model_source,
            env=env,
            run_subprocess=resolved_run,
            secrets=secrets,
            override=override,
            override_reason=override_reason,
            checked=checked,
        )
    provider_result = _check_provider_readiness(
        provider,
        settings,
        environ=env,
        host_home=host_home,
        strict=True,
        run_subprocess=resolved_run,
        http_get=resolved_http_get,
        secrets=secrets,
        probe_runtime_cli=False,
        # Pass the effective model so the OpenCode/Ollama check can apply the
        # local-Ollama authless carve-out at launch admission. Broad readiness
        # callers (collect_agent_readiness / check_single_provider_readiness)
        # have no selected model, so they keep the strict AUTH_MISSING gate.
        model=identity.model,
    )
    probe = _selected_launch_probe(
        provider,
        settings=settings,
        provider_result=provider_result,
        model=identity.model,
        environ=env,
        run_subprocess=resolved_run,
        http_get=resolved_http_get,
        secrets=secrets,
    )
    model_required = _selected_model_required(provider=provider, model=identity.model)

    reason_code = _preflight_reason_code(
        provider_result=provider_result,
        probe=probe,
        model=identity.model,
        model_required=model_required,
    )
    message = _preflight_message(
        provider_result=provider_result,
        probe=probe,
        model=identity.model,
        model_required=model_required,
    )

    return _launch_preflight_payload(
        agent=runtime.value,
        provider=provider,
        model=identity.model,
        model_source=identity.model_source,
        provider_result=provider_result,
        probe=probe,
        reason_code=reason_code,
        message=message,
        model_required=model_required,
        override=override,
        override_reason=override_reason,
        checked_at=checked,
        secrets=secrets,
    )


def _opencode_local_ollama_host_deferred_preflight(
    settings: ServiceSettings,
    *,
    runtime: AgentRuntime,
    model: str | None,
    model_source: str,
    env: Mapping[str, str],
    run_subprocess: SubprocessRun,
    secrets: frozenset[str],
    override: bool,
    override_reason: str | None,
    checked: datetime,
) -> dict[str, Any]:
    """Admit a local-Ollama workspace whose daemon URL the worker cannot reach.

    Symmetric to the executor pre-agent skip (#569): when the resolved Ollama base
    URL is a workspace Compose service DNS name (e.g. ``http://ollama-sidecar:11434``)
    the worker/service cannot reach it, so a worker-side ``/api/version`` /
    ``/api/tags`` probe would falsely block the workspace before launch. Skip the
    Ollama auth/daemon preflight and defer to the agent container where the sidecar
    daemon IS reachable. The OpenCode CLI must still be present in the runtime image
    regardless of which daemon serves the model, so keep the generic runtime-CLI
    availability probe (mirroring the non-Ollama provider skip) — a runtime image
    missing the ``opencode`` binary must still block here rather than be admitted as
    ready and only fail later as an agent command failure.
    """

    deferred_message = (
        f"OpenCode model {model!r} targets an Ollama daemon URL the worker cannot "
        "reach; the worker-side Ollama auth/daemon preflight is skipped and deferred "
        "to the agent container."
    )
    provider_result = _provider_result(
        ok=True,
        strict=True,
        reason="OPENCODE_OLLAMA_HOST_NOT_WORKER_REACHABLE",
        message=deferred_message,
        secrets=secrets,
        credential_scope="deferred_to_provider",
        isolation="none",
    )
    cli_probe = _probe_agent_runtime_cli(
        settings,
        executable="opencode",
        provider="opencode",
        environ=env,
        run_subprocess=run_subprocess,
        secrets=secrets,
    )
    if cli_probe.get("status") == "ok":
        probe: dict[str, Any] = {
            "status": "unavailable",
            "reason_code": "OPENCODE_OLLAMA_HOST_NOT_WORKER_REACHABLE",
        }
        reason_code = "OPENCODE_OLLAMA_HOST_NOT_WORKER_REACHABLE"
        message = deferred_message
    else:
        probe = cli_probe
        reason_code = str(cli_probe.get("reason_code") or "PROVIDER_PROBE_FAILED")
        message = str(
            cli_probe.get("message")
            or "OpenCode runtime CLI is not available in the configured runtime image."
        )
    return _launch_preflight_payload(
        agent=runtime.value,
        provider="opencode",
        model=model,
        model_source=model_source,
        provider_result=provider_result,
        probe=probe,
        reason_code=reason_code,
        message=message,
        override=override,
        override_reason=override_reason,
        checked_at=checked,
        secrets=secrets,
    )


def _opencode_model_targets_non_ollama_provider(model: str | None) -> bool:
    """Return whether an OpenCode model names a non-Ollama provider.

    Mirrors ``OpenCodeAdapter.get_provider``: a provider-qualified model such as
    ``openai/gpt-oss`` or ``anthropic/claude-sonnet`` carries a ``<provider>/``
    prefix, while a bare or ``ollama/``-prefixed reference targets the local
    Ollama daemon. Only the latter is probed/pulled against Ollama, so a
    non-Ollama provider model must skip the Ollama auth/host preflight.
    """

    provider, slash, _remainder = (model or "").strip().partition("/")
    return bool(slash) and provider != "ollama"


def provider_readiness_preflight_from_task_policy(
    task_policy: Mapping[str, object] | None,
) -> dict[str, Any] | None:
    """Return the persisted preflight snapshot from task policy, if present."""

    if not isinstance(task_policy, Mapping):
        return None
    value = task_policy.get("provider_readiness_preflight")
    return dict(value) if isinstance(value, Mapping) else None


def redact_launch_preflight_text(
    settings: ServiceSettings,
    value: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Normalize launch preflight text with the same redaction used for snapshots."""

    env = os.environ if environ is None else environ
    return _redact(value, _secret_values(settings, env))


def validate_provider_names(values: Iterable[str]) -> set[ProviderName]:
    """Normalize and validate provider names accepted by strict checks."""

    providers: set[ProviderName] = set()
    unknown: list[str] = []
    for raw in values:
        normalized = raw.strip().lower().replace("-", "_")
        if normalized == "claude":
            normalized = "claude_code"
        if normalized in PROVIDER_NAMES:
            providers.add(normalized)
        elif normalized:
            unknown.append(raw)
    if unknown:
        expected = ", ".join(PROVIDER_NAMES)
        raise ProviderReadinessError(
            f"unknown provider(s): {', '.join(sorted(unknown))}; expected one of: {expected}"
        )
    return providers


def _coerce_launch_agent(agent: AgentRuntime | str) -> AgentRuntime | None:
    try:
        return agent if isinstance(agent, AgentRuntime) else AgentRuntime(str(agent))
    except ValueError:
        return None


def _check_provider_readiness(
    provider: ProviderName,
    settings: ServiceSettings,
    *,
    environ: Mapping[str, str],
    host_home: Path,
    strict: bool,
    run_subprocess: SubprocessRun,
    http_get: HttpGet,
    secrets: frozenset[str],
    probe_runtime_cli: bool = True,
    model: str | None = None,
) -> dict[str, Any]:
    if provider == "github":
        return _check_github(
            settings,
            environ=environ,
            host_home=host_home,
            strict=strict,
            run_subprocess=run_subprocess,
            secrets=secrets,
        )
    if provider == "codex":
        return _check_codex(
            environ=environ,
            host_home=host_home,
            strict=strict,
            secrets=secrets,
        )
    if provider == "claude_code":
        return _check_claude(
            environ=environ,
            host_home=host_home,
            work_dir=Path(settings.work_dir).expanduser(),
            strict=strict,
            secrets=secrets,
        )
    if provider == "cursor":
        if probe_runtime_cli:
            return _check_cursor_readiness(
                settings,
                environ=environ,
                strict=strict,
                run_subprocess=run_subprocess,
                secrets=secrets,
            )
        return _check_cursor(
            environ=environ,
            strict=strict,
            secrets=secrets,
        )
    if provider == "gemini":
        return _check_gemini(
            environ=environ,
            host_home=host_home,
            strict=strict,
            secrets=secrets,
        )
    if provider == "opencode":
        return _check_opencode(
            environ=environ,
            host_home=host_home,
            strict=strict,
            http_get=http_get,
            secrets=secrets,
            model=model,
        )
    if provider == "grok":
        return _check_grok(
            environ=environ,
            host_home=host_home,
            strict=strict,
            secrets=secrets,
        )
    if provider == "docker":
        return _check_docker_provider(
            settings,
            environ=environ,
            host_home=host_home,
            strict=strict,
            secrets=secrets,
        )
    raise AssertionError(f"unsupported provider: {provider}")


def _selected_launch_probe(
    provider: ProviderName,
    *,
    settings: ServiceSettings,
    provider_result: Mapping[str, Any],
    model: str | None,
    environ: Mapping[str, str],
    run_subprocess: SubprocessRun,
    http_get: HttpGet,
    secrets: frozenset[str],
) -> dict[str, Any]:
    if not provider_result.get("ok"):
        return {"status": "skipped"}
    if not model and provider != "cursor":
        return {"status": "skipped"}
    executable = _agent_runtime_cli_executable(provider)
    if executable is not None:
        runtime_probe = _probe_agent_runtime_cli(
            settings,
            executable=executable,
            provider=provider,
            environ=environ,
            run_subprocess=run_subprocess,
            secrets=secrets,
        )
        if runtime_probe.get("status") != "ok":
            return runtime_probe
        if provider in {"codex", "claude_code", "cursor", "gemini", "grok"}:
            return runtime_probe
    if provider == "opencode":
        # Create-time admission must never block on (or perform) a pull: a
        # ``:cloud`` model is served remotely and an absent non-cloud model is
        # pulled later in the async executor pre-agent step. Both are reported
        # as non-blocking dispositions here; only an unreachable daemon blocks.
        return _probe_ollama_model(
            _ollama_tags_urls(environ),
            model=model,
            http_get=http_get,
            secrets=secrets,
            allow_cloud=True,
            pull_pending_ok=True,
        )
    return {"status": "unavailable", "reason_code": "PROVIDER_PROBE_UNAVAILABLE"}


def _agent_runtime_cli_executable(provider: ProviderName) -> str | None:
    return {
        "codex": "codex",
        "claude_code": "claude",
        "cursor": "cursor-agent",
        "gemini": "gemini",
        "opencode": "opencode",
        "grok": "grok",
    }.get(provider)


def _preflight_reason_code(
    *,
    provider_result: Mapping[str, Any],
    probe: Mapping[str, Any],
    model: str | None,
    model_required: bool = True,
) -> str:
    if model_required and not model:
        return "MODEL_NOT_SELECTED"
    if provider_result.get("ok") is not True:
        return str(provider_result.get("reason") or "PROVIDER_AUTH_NOT_READY")
    if probe.get("status") == "fail":
        return str(probe.get("reason_code") or "PROVIDER_PROBE_FAILED")
    if probe.get("status") == "pending":
        # Non-blocking: the model is absent locally but will be auto-pulled
        # before the agent runs. Surface the disposition so the console can show
        # *why* the workspace is not yet fully ready without blocking launch.
        return str(probe.get("reason_code") or "PROVIDER_PROBE_PENDING")
    return "PROVIDER_READY"


def _preflight_message(
    *,
    provider_result: Mapping[str, Any],
    probe: Mapping[str, Any],
    model: str | None,
    model_required: bool = True,
) -> str:
    if model_required and not model:
        return "No effective model was selected for the workspace agent."
    if provider_result.get("ok") is not True:
        return str(
            provider_result.get("message") or "Selected provider authentication is not ready."
        )
    if probe.get("status") == "fail":
        return str(probe.get("message") or "Selected provider auth probe did not report readiness.")
    if probe.get("status") == "pending":
        return str(
            probe.get("message")
            or "Selected provider model is not present locally yet; AWF will pull it before launch."
        )
    return "Selected provider authentication and model readiness are sufficient for launch."


def _launch_preflight_payload(
    *,
    agent: str,
    provider: str,
    model: str | None,
    model_source: str,
    provider_result: Mapping[str, Any] | None,
    probe: Mapping[str, Any],
    reason_code: str,
    message: str,
    override: bool,
    override_reason: str | None,
    checked_at: datetime,
    secrets: frozenset[str],
    model_required: bool = True,
) -> dict[str, Any]:
    auth_ok = provider_result is not None and provider_result.get("ok") is True
    model_ok = bool(model) or not model_required
    probe_status = str(probe.get("status") or "skipped")
    # ``pending`` is a non-blocking disposition: the requested Ollama model is
    # absent locally but will be auto-pulled in the executor pre-agent step, so
    # it must not require an override or block launch admission.
    probe_ok = probe_status in {"ok", "unavailable", "pending"}
    override_required = not (auth_ok and model_ok and probe_ok)
    override_used = bool(override and override_required)
    blocks_launch = override_required and not override_used
    readiness_status = (
        "ready"
        if not override_required
        else "admitted_with_override"
        if override_used
        else "blocked"
    )
    credential_sources = _credential_sources(provider_result)
    redacted_override_reason: str | None = None
    override_reason_redaction_parts: list[str] | None = None
    if override_reason:
        (
            redacted_override_reason,
            override_reason_redaction_parts,
        ) = _redact_with_redaction_parts(override_reason, secrets)

    payload: dict[str, Any] = {
        "provider": provider,
        "agent": agent,
        "model": model,
        "model_source": model_source,
        "readiness_status": readiness_status,
        "auth_status": _auth_status(provider_result),
        "auth_source": _auth_source(provider_result),
        "credential_scope": _provider_field(provider_result, "credential_scope", "not_observed"),
        "isolation": _provider_field(provider_result, "isolation", "none"),
        "probe_status": probe_status,
        "reason_code": reason_code,
        "message": _redact(message, secrets),
        "override_required": override_required,
        "override_requested": bool(override),
        "override_used": override_used,
        "override_reason": redacted_override_reason,
        "blocks_launch": blocks_launch,
        "checked_at": checked_at.isoformat(),
        "credential_sources": credential_sources,
    }
    if override_reason_redaction_parts is not None:
        payload["override_reason_redaction_parts"] = override_reason_redaction_parts
    probe_detail = probe.get("detail")
    if isinstance(probe_detail, str) and probe_detail:
        payload["probe_detail"] = _redact(_truncate(probe_detail), secrets)
    warnings = provider_result.get("warnings") if provider_result is not None else None
    if isinstance(warnings, list):
        payload["warnings"] = [warning for warning in warnings if isinstance(warning, Mapping)]
    return payload


def _selected_model_required(*, provider: ProviderName, model: str | None) -> bool:
    """Return whether launch admission requires an AWF-selected model."""

    return not (provider == "cursor" and model is None)


def _auth_status(provider_result: Mapping[str, Any] | None) -> str:
    value = provider_result.get("status") if provider_result is not None else None
    return value if isinstance(value, str) and value else "unknown"


def _auth_source(provider_result: Mapping[str, Any] | None) -> str:
    sources = _credential_sources(provider_result)
    if sources:
        signal = sources[0].get("signal")
        if isinstance(signal, str) and signal:
            return signal
    return _provider_field(provider_result, "credential_scope", "not_observed")


def _provider_field(
    provider_result: Mapping[str, Any] | None,
    key: str,
    default: str,
) -> str:
    value = provider_result.get(key) if provider_result is not None else None
    return value if isinstance(value, str) and value else default


def _credential_sources(
    provider_result: Mapping[str, Any] | None,
) -> list[dict[str, str]]:
    raw = provider_result.get("credential_sources") if provider_result is not None else None
    if not isinstance(raw, list):
        return []
    sources: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        source: dict[str, str] = {}
        for key in ("type", "signal", "credential_scope", "isolation"):
            value = item.get(key)
            if isinstance(value, str):
                source[key] = value
        if source:
            sources.append(source)
    return sources


from awf.service.provider_readiness_helpers import (  # noqa: E402
    _OPENCODE_PROVIDER_ENV_KEYS,
    _check_docker_provider,
    _check_grok,
    _check_opencode,
    _http_get,
    _http_post_stream,
    _is_cloud_model,
    _ollama_pull_urls,
    _ollama_tags_urls,
    _ollama_url_host_reachable_from_worker,
    _opencode_ollama_host_probe_deferrable,
    _opencode_provider_credentials_present,
    _ordered_names,
    _primary_credential_scope,
    _primary_isolation,
    _probe_agent_runtime_cli,
    _probe_cli_auth_status,
    _provider_result,
    _run_subprocess,
    _secret_values,
    _security_summary,
    overlay_profile_ollama_base_url,
    overlay_profile_provider_credentials,
)

# Imported from ``provider_readiness_ollama`` (their defining module after the
# extraction) rather than via ``provider_readiness_helpers`` so mypy treats them
# as explicit exports; the helpers module re-exports the same names for its own
# ``_check_opencode`` call site and existing test namespace access.
from awf.service.provider_readiness_ollama import (  # noqa: E402
    _probe_ollama,
    _probe_ollama_model,
    ensure_ollama_model_available,
)

# Imported after the helper/redaction re-exports above so the per-provider check
# helpers (extracted into ``provider_readiness_provider_checks`` to satisfy the
# maintainability line limit) can resolve the names they pull back from this
# module. ``_check_provider_readiness`` above reaches them via this namespace.
from awf.service.provider_readiness_provider_checks import (  # noqa: E402
    _check_claude,
    _check_codex,
    _check_cursor,
    _check_cursor_readiness,
    _check_gemini,
    _check_github,
)
from awf.service.provider_readiness_redaction import (  # noqa: E402
    _redact,
    _redact_with_redaction_parts,
    _truncate,
)

__all__ = [
    "HttpPostStream",
    "HttpStreamResponseLike",
    "ProviderName",
    "ProviderReadinessError",
    "check_single_provider_readiness",
    "collect_agent_readiness",
    "default_subprocess_runner",
    "ensure_ollama_model_available",
    "overlay_profile_ollama_base_url",
    "overlay_profile_provider_credentials",
    "provider_readiness_preflight_from_task_policy",
    "redact_launch_preflight_text",
    "selected_provider_readiness_preflight",
    "validate_provider_names",
    "_http_post_stream",
    "_is_cloud_model",
    "_ollama_pull_urls",
    "_ollama_tags_urls",
    "_redact",
    "_primary_credential_scope",
    "_primary_isolation",
    "_probe_cli_auth_status",
    "_probe_ollama",
    "_secret_values",
]
