"""Credential readiness checks for local service agent providers."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from awf.db.enums import AgentRuntime
from awf.node.auth_mounts import (
    claude_auth_isolation_label,
    force_copy_isolation_requested,
)
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
    "_PASSWORD",
    "_PASSWD",
    "_SECRET",
)
_SECRET_ENV_KEY_NAMES = {suffix.removeprefix("_") for suffix in _SECRET_ENV_KEY_SUFFIXES}

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
        return _probe_ollama_model(
            _ollama_tags_urls(environ),
            model=model,
            http_get=http_get,
            secrets=secrets,
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
    probe_ok = probe_status in {"ok", "unavailable"}
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


def _check_github(
    settings: ServiceSettings,
    *,
    environ: Mapping[str, str],
    host_home: Path,
    strict: bool,
    run_subprocess: SubprocessRun,
    secrets: frozenset[str],
) -> dict[str, Any]:
    token, token_signal = _github_token(settings, environ)
    if not token:
        if (host_home / ".config" / "gh").exists():
            return _provider_result(
                ok=False,
                strict=strict,
                reason="GITHUB_KEYRING_ONLY_NOT_VISIBLE_IN_COMPOSE",
                message=(
                    "GitHub CLI config is visible, but local service containers cannot "
                    "use keychain-only gh auth. Set AWF_GITHUB_TOKEN or GH_TOKEN in the "
                    "Compose environment."
                ),
                action=(
                    'Run `export AWF_GITHUB_TOKEN="$(gh auth token)"` before '
                    "starting Compose, or put AWF_GITHUB_TOKEN/GH_TOKEN in "
                    "docker/compose/.env."
                ),
                signals=["~/.config/gh"],
                secrets=secrets,
                credential_sources=[
                    _credential_source(
                        type_="path",
                        signal="~/.config/gh",
                        credential_scope="read_only_host_path",
                        isolation="read_only_bind",
                    )
                ],
                credential_scope="read_only_host_path",
                isolation="read_only_bind",
            )
        return _provider_result(
            ok=False,
            strict=strict,
            reason="GITHUB_TOKEN_ENV_MISSING",
            message=(
                "No service-visible GitHub token was found. Set AWF_GITHUB_TOKEN, "
                "GH_TOKEN, or GITHUB_TOKEN so AWF can create PRs, comment, and merge."
            ),
            action="Set AWF_GITHUB_TOKEN from `gh auth token` before starting the service.",
            secrets=secrets,
            credential_scope="not_observed",
            isolation="none",
        )

    gh_env = {**dict(environ), "AWF_GITHUB_TOKEN": token, "GH_TOKEN": token, "GITHUB_TOKEN": token}
    token_source = _credential_source(
        type_="env",
        signal=token_signal,
        credential_scope="static_env_token",
        isolation="service_env",
    )
    token_warnings = [
        _security_warning(
            "STATIC_TOKEN_FALLBACK",
            f"GitHub auth is supplied by static service environment variable {token_signal}.",
        )
    ]
    args = ["gh", "auth", "status", "--hostname", "github.com"]
    try:
        result = run_subprocess(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=_GITHUB_TIMEOUT_SECONDS,
            env=gh_env,
        )
    except FileNotFoundError:
        return _provider_result(
            ok=False,
            strict=strict,
            reason="GITHUB_CLI_NOT_FOUND",
            message="GitHub token is present, but the gh CLI is not installed in the service.",
            action="Install gh in the service image or rebuild docker/control-plane.Dockerfile.",
            signals=[token_signal],
            capabilities=["pr_create", "comment", "merge"],
            secrets=secrets,
            credential_sources=[token_source],
            credential_scope="static_env_token",
            isolation="service_env",
            warnings=token_warnings,
        )
    except subprocess.TimeoutExpired:
        return _provider_result(
            ok=False,
            strict=strict,
            reason="GITHUB_AUTH_TIMEOUT",
            message=f"`gh auth status` exceeded {_GITHUB_TIMEOUT_SECONDS:g}s.",
            signals=[token_signal, "gh auth status"],
            capabilities=["pr_create", "comment", "merge"],
            secrets=secrets,
            credential_sources=[token_source],
            credential_scope="static_env_token",
            isolation="service_env",
            warnings=token_warnings,
        )
    except Exception as exc:
        _log_redacted_exception(
            "provider_readiness.github_auth_check_exception",
            exc,
            secrets,
        )
        return _provider_result(
            ok=False,
            strict=strict,
            reason="GITHUB_AUTH_UNUSABLE",
            message="GitHub CLI auth check failed before it could complete.",
            detail=f"{type(exc).__name__}: {exc}",
            signals=[token_signal, "gh auth status"],
            capabilities=["pr_create", "comment", "merge"],
            secrets=secrets,
            credential_sources=[token_source],
            credential_scope="static_env_token",
            isolation="service_env",
            warnings=token_warnings,
        )

    if result.returncode != 0:
        return _provider_result(
            ok=False,
            strict=strict,
            reason="GITHUB_AUTH_UNUSABLE",
            message="GitHub CLI auth is not usable for local service PR operations.",
            detail=result.stderr or result.stdout or "gh auth status exited non-zero",
            signals=[token_signal, "gh auth status"],
            capabilities=["pr_create", "comment", "merge"],
            secrets=secrets,
            credential_sources=[token_source],
            credential_scope="static_env_token",
            isolation="service_env",
            warnings=token_warnings,
        )

    return _provider_result(
        ok=True,
        strict=strict,
        reason="GITHUB_AUTH_OK",
        message="GitHub CLI auth is usable for PR creation, comments, and merges.",
        signals=[token_signal, "gh auth status"],
        capabilities=["pr_create", "comment", "merge"],
        secrets=secrets,
        credential_sources=[token_source],
        credential_scope="static_env_token",
        isolation="service_env",
        warnings=token_warnings,
    )


def _check_codex(
    *,
    environ: Mapping[str, str],
    host_home: Path,
    strict: bool,
    secrets: frozenset[str],
) -> dict[str, Any]:
    file_sources = _codex_file_sources(host_home)
    if file_sources:
        return _provider_result(
            ok=True,
            strict=strict,
            reason="CODEX_FILE_AUTH_PRESENT",
            message="Codex auth files are visible for per-workspace isolated copies.",
            signals=[source["signal"] for source in file_sources],
            secrets=secrets,
            credential_sources=file_sources,
            credential_scope="isolated_workspace",
            isolation="per_workspace_copy",
            warnings=[],
        )

    signal = _first_present_env(environ, _CODEX_ENV_KEYS)
    if signal is not None:
        return _provider_result(
            ok=True,
            strict=strict,
            reason="CODEX_ENV_AUTH_PRESENT",
            message="Codex auth is visible through service environment variables.",
            signals=[signal],
            secrets=secrets,
            credential_sources=[
                _credential_source(
                    type_="env",
                    signal=signal,
                    credential_scope="static_env_token",
                    isolation="service_env",
                )
            ],
            credential_scope="static_env_token",
            isolation="service_env",
            warnings=[
                _security_warning(
                    "STATIC_TOKEN_FALLBACK",
                    f"Codex auth is supplied by static service environment variable {signal}.",
                )
            ],
        )

    return _provider_result(
        ok=False,
        strict=strict,
        reason="CODEX_AUTH_MISSING",
        message=(
            "No Codex auth signal was visible. Mount ~/.codex or set OPENAI_API_KEY, "
            "OPENAI_API_TOKEN, CODEX_API_KEY, or CODEX_AUTH_TOKEN."
        ),
        secrets=secrets,
        credential_scope="not_observed",
        isolation="none",
    )


def _check_claude(
    *,
    environ: Mapping[str, str],
    host_home: Path,
    strict: bool,
    secrets: frozenset[str],
) -> dict[str, Any]:
    # ``~/.claude`` is isolated per workspace via a shared read-only overlay base
    # + per-workspace writable upper when overlayfs is available, else a full
    # per-workspace copy. ``~/.claude.json`` is *always* a per-workspace copy
    # (the resolver never overlays it), so the overlay posture applies only to
    # the directory source — labelling the file source with the overlay label
    # would overstate its isolation/disk posture on ``.claude.json``-only hosts.
    #
    # The force-copy probe reads the *passed* ``environ``, not ``os.environ``:
    # ``awf service bootstrap`` on a non-propagating host folds
    # ``AWF_CLAUDE_AUTH_FORCE_COPY=true`` into the readiness ``environ`` dict (the
    # worker provisions with the copy fallback) rather than the CLI process
    # environment, so a default probe over ``os.environ`` would miss it and report
    # ``per_workspace_overlay`` while the worker actually uses per-workspace copies.
    directory_isolation = claude_auth_isolation_label(
        force_copy_requested=lambda: force_copy_isolation_requested(environ),
    )
    file_sources: list[dict[str, str]] = []
    if (host_home / ".claude").exists():
        file_sources.append(
            _credential_source(
                type_="path",
                signal="~/.claude",
                credential_scope="isolated_workspace",
                isolation=directory_isolation,
            )
        )
    if (host_home / ".claude.json").exists():
        file_sources.append(
            _credential_source(
                type_="path",
                signal="~/.claude.json",
                credential_scope="isolated_workspace",
                isolation="per_workspace_copy",
            )
        )
    if file_sources:
        return _provider_result(
            ok=True,
            strict=strict,
            reason="CLAUDE_FILE_AUTH_PRESENT",
            message="Claude Code auth files are visible to the local service.",
            signals=[source["signal"] for source in file_sources],
            secrets=secrets,
            credential_sources=file_sources,
            credential_scope="isolated_workspace",
            isolation=file_sources[0]["isolation"],
            warnings=[],
        )

    signal = _first_present_env(environ, _CLAUDE_ENV_KEYS)
    if signal is not None:
        return _provider_result(
            ok=True,
            strict=strict,
            reason="CLAUDE_ENV_AUTH_PRESENT",
            message="Claude Code auth is visible through service environment variables.",
            signals=[signal],
            secrets=secrets,
            credential_sources=[
                _credential_source(
                    type_="env",
                    signal=signal,
                    credential_scope="static_env_token",
                    isolation="service_env",
                )
            ],
            credential_scope="static_env_token",
            isolation="service_env",
            warnings=[
                _security_warning(
                    "STATIC_TOKEN_FALLBACK",
                    f"Claude Code auth is supplied by static service environment variable {signal}.",
                )
            ],
        )

    return _provider_result(
        ok=False,
        strict=strict,
        reason="CLAUDE_AUTH_MISSING",
        message=(
            "No Claude Code auth signal was visible. Set ANTHROPIC_API_KEY, "
            "ANTHROPIC_AUTH_TOKEN, CLAUDE_CODE_OAUTH_TOKEN, or mount ~/.claude."
        ),
        secrets=secrets,
        credential_scope="not_observed",
        isolation="none",
    )


def _check_cursor(
    *,
    environ: Mapping[str, str],
    strict: bool,
    secrets: frozenset[str],
) -> dict[str, Any]:
    """Check whether Cursor API-key auth is visible to the service."""
    signal = _first_present_env(environ, _CURSOR_ENV_KEYS)
    if signal is not None:
        return _provider_result(
            ok=True,
            strict=strict,
            reason="CURSOR_ENV_AUTH_PRESENT",
            message="Cursor auth is visible through service environment variables.",
            signals=[signal],
            secrets=secrets,
            credential_sources=[
                _credential_source(
                    type_="env",
                    signal=signal,
                    credential_scope="static_env_token",
                    isolation="service_env",
                )
            ],
            credential_scope="static_env_token",
            isolation="service_env",
            warnings=[
                _security_warning(
                    "STATIC_TOKEN_FALLBACK",
                    f"Cursor auth is supplied by static service environment variable {signal}.",
                )
            ],
        )

    return _provider_result(
        ok=False,
        strict=strict,
        reason="CURSOR_AUTH_MISSING",
        message="No Cursor auth signal was visible. Set CURSOR_API_KEY.",
        secrets=secrets,
        credential_scope="not_observed",
        isolation="none",
    )


def _check_cursor_readiness(
    settings: ServiceSettings,
    *,
    environ: Mapping[str, str],
    strict: bool,
    run_subprocess: SubprocessRun,
    secrets: frozenset[str],
) -> dict[str, Any]:
    """Combine Cursor env auth with the runtime CLI availability probe."""
    cursor_result = _check_cursor(environ=environ, strict=strict, secrets=secrets)
    if cursor_result.get("ok") is not True:
        return cursor_result

    probe = _probe_agent_runtime_cli(
        settings,
        executable="cursor-agent",
        provider="cursor",
        environ=environ,
        run_subprocess=run_subprocess,
        secrets=secrets,
    )
    runtime_cli_probe = _runtime_cli_probe_payload(probe)
    if probe.get("status") == "ok":
        cursor_result["runtime_cli_probe"] = runtime_cli_probe
        return cursor_result

    reason = str(probe.get("reason_code") or "CURSOR_RUNTIME_CLI_NOT_FOUND")
    message = str(probe.get("message") or "Cursor auth was found but cursor-agent is unavailable.")
    result = _provider_result(
        ok=False,
        strict=strict,
        reason=reason,
        message=message,
        detail=str(probe.get("detail") or "") or None,
        signals=[
            *[signal for signal in cursor_result.get("signals", []) if isinstance(signal, str)],
            "cursor-agent",
        ],
        secrets=secrets,
        credential_sources=_credential_sources(cursor_result),
        credential_scope=str(cursor_result.get("credential_scope") or "static_env_token"),
        isolation=str(cursor_result.get("isolation") or "service_env"),
        warnings=[
            *[
                warning
                for warning in cursor_result.get("warnings", [])
                if isinstance(warning, Mapping)
            ],
            _security_warning(
                reason,
                _redact(message, secrets),
                severity="error" if strict else "warning",
            ),
        ],
    )
    result["runtime_cli_probe"] = runtime_cli_probe
    return result


def _check_gemini(
    *,
    environ: Mapping[str, str],
    host_home: Path,
    strict: bool,
    secrets: frozenset[str],
) -> dict[str, Any]:
    file_sources = _existing_credential_sources(
        ((host_home / ".gemini", "~/.gemini"),),
        credential_scope="isolated_workspace",
        isolation="per_workspace_copy",
    )
    if file_sources:
        return _provider_result(
            ok=True,
            strict=strict,
            reason="GEMINI_FILE_AUTH_PRESENT",
            message="Gemini auth files are visible to the local service.",
            signals=[source["signal"] for source in file_sources],
            secrets=secrets,
            credential_sources=file_sources,
            credential_scope="isolated_workspace",
            isolation="per_workspace_copy",
            warnings=[],
        )

    signal = _first_present_env(environ, _GEMINI_ENV_KEYS)
    if signal is not None:
        return _provider_result(
            ok=True,
            strict=strict,
            reason="GEMINI_ENV_AUTH_PRESENT",
            message="Gemini auth is visible through service environment variables.",
            signals=[signal],
            secrets=secrets,
            credential_sources=[
                _credential_source(
                    type_="env",
                    signal=signal,
                    credential_scope="static_env_token",
                    isolation="service_env",
                )
            ],
            credential_scope="static_env_token",
            isolation="service_env",
            warnings=[
                _security_warning(
                    "STATIC_TOKEN_FALLBACK",
                    f"Gemini auth is supplied by static service environment variable {signal}.",
                )
            ],
        )

    credentials = environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if credentials and Path(credentials).expanduser().is_file():
        return _provider_result(
            ok=True,
            strict=strict,
            reason="GEMINI_ENV_AUTH_PRESENT",
            message="Google application credentials are visible to the local service.",
            signals=["GOOGLE_APPLICATION_CREDENTIALS"],
            secrets=secrets,
            credential_sources=[
                _credential_source(
                    type_="path",
                    signal="GOOGLE_APPLICATION_CREDENTIALS",
                    credential_scope="read_only_host_path",
                    isolation="read_only_bind",
                )
            ],
            credential_scope="read_only_host_path",
            isolation="read_only_bind",
            warnings=[],
        )

    message = (
        "No Gemini auth signal was visible. Set GEMINI_API_KEY, GOOGLE_API_KEY, "
        "GOOGLE_APPLICATION_CREDENTIALS, or mount ~/.gemini."
    )
    if credentials:
        message = (
            "GOOGLE_APPLICATION_CREDENTIALS is set but the file is not visible to "
            "the local service. Mount the file or use GEMINI_API_KEY/GOOGLE_API_KEY."
        )
    return _provider_result(
        ok=False,
        strict=strict,
        reason="GEMINI_AUTH_MISSING",
        message=message,
        signals=["GOOGLE_APPLICATION_CREDENTIALS"] if credentials else None,
        secrets=secrets,
        credential_scope="not_observed",
        isolation="none",
    )


def _check_opencode(
    *,
    environ: Mapping[str, str],
    host_home: Path,
    strict: bool,
    http_get: HttpGet,
    secrets: frozenset[str],
) -> dict[str, Any]:
    opencode_config = (host_home / ".config" / "opencode").is_dir()
    ollama_files = [
        filename for filename in _OLLAMA_AUTH_FILES if (host_home / ".ollama" / filename).is_file()
    ]
    env_signal = _first_present_env(environ, _OPENCODE_ENV_KEYS)
    signals: list[str] = []
    credential_sources: list[dict[str, str]] = []
    if opencode_config:
        signals.append("~/.config/opencode")
        credential_sources.append(
            _credential_source(
                type_="path",
                signal="~/.config/opencode",
                credential_scope="isolated_workspace",
                isolation="per_workspace_copy",
            )
        )
    if ollama_files:
        signals.append("~/.ollama auth files")
        credential_sources.extend(
            _credential_source(
                type_="path",
                signal=f"~/.ollama/{filename}",
                credential_scope="isolated_workspace",
                isolation="per_workspace_copy",
            )
            for filename in ollama_files
        )
    if env_signal is not None:
        signals.append(env_signal)
        credential_sources.append(
            _credential_source(
                type_="env",
                signal=env_signal,
                credential_scope="static_env_token",
                isolation="service_env",
            )
        )

    if not signals:
        return _provider_result(
            ok=False,
            strict=strict,
            reason="OPENCODE_OLLAMA_AUTH_MISSING",
            message=(
                "No OpenCode/Ollama auth signal was visible. Mount ~/.config/opencode, "
                "mount small ~/.ollama auth files, or set OLLAMA_API_KEY."
            ),
            secrets=secrets,
            credential_scope="not_observed",
            isolation="none",
        )

    version_urls = _ollama_version_urls(environ)
    probe = _probe_ollama(version_urls, http_get=http_get, secrets=secrets)
    if not probe["ok"]:
        probe_detail = probe.get("detail")
        return _provider_result(
            ok=False,
            strict=strict,
            reason="OLLAMA_HOST_UNREACHABLE",
            message=(
                "OpenCode/Ollama auth is visible, but the Ollama host did not answer "
                "a cheap /api/version readiness probe."
            ),
            detail=probe_detail if isinstance(probe_detail, str) else None,
            signals=[*signals, "ollama /api/version"],
            secrets=secrets,
            credential_sources=credential_sources,
            credential_scope=_primary_credential_scope(credential_sources),
            isolation=_primary_isolation(credential_sources),
            warnings=_static_env_warnings(
                provider_label="OpenCode/Ollama",
                signals=[env_signal]
                if env_signal is not None and not (opencode_config or ollama_files)
                else [],
            ),
        )

    # Keep successful readiness payloads schema-stable. `_probe_ollama` retains
    # recovered candidate failures under debug for internal diagnostics; only
    # terminal probe failures become operator-facing provider detail.
    reason = "OPENCODE_FILE_AUTH_PRESENT"
    if not opencode_config and ollama_files:
        reason = "OLLAMA_FILE_AUTH_PRESENT"
    elif not opencode_config and env_signal is not None:
        reason = "OLLAMA_ENV_AUTH_PRESENT"

    return _provider_result(
        ok=True,
        strict=strict,
        reason=reason,
        message="OpenCode/Ollama auth is visible and the Ollama host is reachable.",
        signals=[*signals, "OLLAMA_HOST_REACHABLE"],
        secrets=secrets,
        credential_sources=credential_sources,
        credential_scope=_primary_credential_scope(credential_sources),
        isolation=_primary_isolation(credential_sources),
        warnings=_static_env_warnings(
            provider_label="OpenCode/Ollama",
            signals=[env_signal]
            if env_signal is not None and not (opencode_config or ollama_files)
            else [],
        ),
    )


def _check_grok(
    *,
    environ: Mapping[str, str],
    host_home: Path,
    strict: bool,
    secrets: frozenset[str],
) -> dict[str, Any]:
    auth_json = host_home / ".grok" / "auth.json"
    if auth_json.is_file():
        file_sources = [
            _credential_source(
                type_="path",
                signal="~/.grok/auth.json",
                credential_scope="isolated_workspace",
                isolation="per_workspace_copy",
            )
        ]
        return _provider_result(
            ok=True,
            strict=strict,
            reason="GROK_FILE_AUTH_PRESENT",
            message="Grok Build auth files are visible for per-workspace isolated copies.",
            signals=[source["signal"] for source in file_sources],
            secrets=secrets,
            credential_sources=file_sources,
            credential_scope="isolated_workspace",
            isolation="per_workspace_copy",
            warnings=[],
        )

    signal = _first_present_env(environ, _XAI_ENV_KEYS)
    if signal is not None:
        return _provider_result(
            ok=True,
            strict=strict,
            reason="GROK_ENV_AUTH_PRESENT",
            message="Grok Build auth is visible through service environment variables.",
            signals=[signal],
            secrets=secrets,
            credential_sources=[
                _credential_source(
                    type_="env",
                    signal=signal,
                    credential_scope="static_env_token",
                    isolation="service_env",
                )
            ],
            credential_scope="static_env_token",
            isolation="service_env",
            warnings=_static_env_warnings(
                provider_label="Grok Build",
                signals=[signal],
            ),
        )

    return _provider_result(
        ok=False,
        strict=strict,
        reason="GROK_AUTH_MISSING",
        message="No Grok Build auth signal was visible. Mount ~/.grok or set XAI_API_KEY.",
        secrets=secrets,
        credential_scope="not_observed",
        isolation="none",
    )


from awf.service.provider_readiness_helpers import (  # noqa: E402
    _check_docker_provider,
    _codex_file_sources,
    _credential_source,
    _existing_credential_sources,
    _first_present_env,
    _github_token,
    _http_get,
    _log_redacted_exception,
    _ollama_tags_urls,
    _ollama_version_urls,
    _ordered_names,
    _primary_credential_scope,
    _primary_isolation,
    _probe_agent_runtime_cli,
    _probe_cli_auth_status,
    _probe_ollama,
    _probe_ollama_model,
    _provider_result,
    _redact,
    _redact_with_redaction_parts,
    _run_subprocess,
    _runtime_cli_probe_payload,
    _secret_values,
    _security_summary,
    _security_warning,
    _static_env_warnings,
    _truncate,
)

__all__ = [
    "ProviderName",
    "ProviderReadinessError",
    "check_single_provider_readiness",
    "collect_agent_readiness",
    "default_subprocess_runner",
    "provider_readiness_preflight_from_task_policy",
    "redact_launch_preflight_text",
    "selected_provider_readiness_preflight",
    "validate_provider_names",
    "_redact",
    "_probe_cli_auth_status",
    "_secret_values",
]
