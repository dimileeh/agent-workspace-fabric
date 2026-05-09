"""Credential readiness checks for local service agent providers."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import traceback
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from awf.adapters.opencode import DEFAULT_OLLAMA_OPENAI_BASE_URL
from awf.db.enums import AgentRuntime
from awf.service.config import ServiceSettings
from awf.service.workspace_observability import effective_agent_identity

ProviderName = Literal["github", "codex", "claude_code", "gemini", "opencode", "docker"]

PROVIDER_NAMES: tuple[ProviderName, ...] = (
    "github",
    "codex",
    "claude_code",
    "gemini",
    "opencode",
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
_GEMINI_ENV_KEYS = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_CLOUD_ACCESS_TOKEN",
)
_OPENCODE_ENV_KEYS = ("OLLAMA_API_KEY",)
_DOCKER_AUTH_ENV_KEYS = ("DOCKER_AUTH_CONFIG",)
_KNOWN_SECRET_ENV_KEYS = frozenset(
    (
        *_GITHUB_TOKEN_ENV_KEYS,
        *_CODEX_ENV_KEYS,
        *_CLAUDE_ENV_KEYS,
        *_GEMINI_ENV_KEYS,
        *_OPENCODE_ENV_KEYS,
        *_DOCKER_AUTH_ENV_KEYS,
        "GOOGLE_APPLICATION_CREDENTIALS_JSON",
    )
)

_URL_CREDENTIAL_RE = re.compile(r"(https?://)([^/\s:@]+(?::[^/\s@]+)?@)")
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
_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(_KNOWN_TOKEN_PATTERNS) + r")(?![A-Za-z0-9])")
_LAUNCH_PROVIDER_BY_AGENT: Mapping[AgentRuntime, ProviderName] = {
    AgentRuntime.codex: "codex",
    AgentRuntime.claude_code: "claude_code",
    AgentRuntime.gemini: "gemini",
    AgentRuntime.opencode: "opencode",
}
_RedactionSegment = tuple[Literal["literal", "redaction"], str]
_log = logging.getLogger(__name__)


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

    reason_code = _preflight_reason_code(
        provider_result=provider_result,
        probe=probe,
        model=identity.model,
    )
    message = _preflight_message(
        provider_result=provider_result,
        probe=probe,
        model=identity.model,
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
    if not provider_result.get("ok") or not model:
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
        if provider in {"codex", "claude_code", "gemini"}:
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
        "gemini": "gemini",
        "opencode": "opencode",
    }.get(provider)


def _agent_runtime_cli_reason_prefix(provider: ProviderName) -> str:
    return {
        "codex": "CODEX",
        "claude_code": "CLAUDE",
        "gemini": "GEMINI",
        "opencode": "OPENCODE",
    }.get(provider, "PROVIDER")


def _probe_agent_runtime_cli(
    settings: ServiceSettings,
    *,
    executable: str,
    provider: ProviderName,
    environ: Mapping[str, str],
    run_subprocess: SubprocessRun,
    secrets: frozenset[str],
) -> dict[str, Any]:
    reason_prefix = _agent_runtime_cli_reason_prefix(provider)
    args = [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        settings.agent_runtime_image,
        "-lc",
        f"command -v {executable}",
    ]
    try:
        result = run_subprocess(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=_PROVIDER_PROBE_TIMEOUT_SECONDS,
            env=environ,
        )
    except FileNotFoundError:
        return {
            "status": "fail",
            "reason_code": "DOCKER_CLI_NOT_FOUND",
            "message": (
                "Docker CLI was not found while probing the configured agent runtime image."
            ),
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "fail",
            "reason_code": f"{reason_prefix}_RUNTIME_CLI_PROBE_TIMEOUT",
            "message": (
                f"Agent runtime CLI probe for {executable!r} exceeded "
                f"{_PROVIDER_PROBE_TIMEOUT_SECONDS:g}s."
            ),
        }
    except Exception as exc:
        _log_redacted_exception(
            "provider_readiness.agent_runtime_cli_probe_exception",
            exc,
            secrets,
        )
        detail = _redact(_truncate(f"{type(exc).__name__}: {exc}"), secrets)
        return {
            "status": "fail",
            "reason_code": f"{reason_prefix}_RUNTIME_CLI_PROBE_ERROR",
            "message": "Agent runtime CLI probe failed before completion.",
            "detail": detail,
        }

    if result.returncode == 0:
        return {
            "status": "ok",
            "reason_code": f"{reason_prefix}_RUNTIME_CLI_AVAILABLE",
            "detail": _redact(_truncate(result.stdout.strip()), secrets)
            if result.stdout.strip()
            else None,
        }

    detail = _redact(
        _truncate(result.stderr or result.stdout or f"{executable} was not found"),
        secrets,
    )
    return {
        "status": "fail",
        "reason_code": f"{reason_prefix}_RUNTIME_CLI_NOT_FOUND",
        "message": (
            f"The configured agent runtime image {settings.agent_runtime_image!r} "
            f"does not expose the {executable!r} CLI required by provider {provider!r}."
        ),
        "detail": detail,
    }


def _probe_cli_auth_status(
    *,
    provider_label: str,
    args: list[str],
    failure_reason: str,
    timeout_reason: str,
    missing_reason: str,
    error_reason: str,
    environ: Mapping[str, str],
    run_subprocess: SubprocessRun,
    secrets: frozenset[str],
) -> dict[str, Any]:
    try:
        result = run_subprocess(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=_PROVIDER_PROBE_TIMEOUT_SECONDS,
            env=environ,
        )
    except FileNotFoundError:
        return {
            "status": "fail",
            "reason_code": missing_reason,
            "message": f"{provider_label} CLI was not found for auth status probing.",
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "fail",
            "reason_code": timeout_reason,
            "message": (
                f"{provider_label} auth status probe exceeded {_PROVIDER_PROBE_TIMEOUT_SECONDS:g}s."
            ),
        }
    except Exception as exc:
        _log_redacted_exception(
            "provider_readiness.cli_auth_probe_exception",
            exc,
            secrets,
        )
        detail = _redact(_truncate(f"{type(exc).__name__}: {exc}"), secrets)
        return {
            "status": "fail",
            "reason_code": error_reason,
            "message": f"{provider_label} auth status probe failed before completion.",
            "detail": detail,
        }

    if result.returncode == 0:
        return {
            "status": "ok",
            "reason_code": f"{failure_reason.removesuffix('_FAILED')}_OK",
        }

    detail = _redact(
        _truncate(result.stderr or result.stdout or "auth status exited non-zero"),
        secrets,
    )
    return {
        "status": "fail",
        "reason_code": failure_reason,
        "message": f"{provider_label} auth status probe reported unusable auth.",
        "detail": detail,
    }


def _preflight_reason_code(
    *,
    provider_result: Mapping[str, Any],
    probe: Mapping[str, Any],
    model: str | None,
) -> str:
    if not model:
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
) -> str:
    if not model:
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
) -> dict[str, Any]:
    auth_ok = provider_result is not None and provider_result.get("ok") is True
    model_ok = bool(model)
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
    file_sources = _existing_credential_sources(
        (
            (host_home / ".claude", "~/.claude"),
            (host_home / ".claude.json", "~/.claude.json"),
        ),
        credential_scope="isolated_workspace",
        isolation="per_workspace_copy",
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
            isolation="per_workspace_copy",
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


def _check_docker_provider(
    settings: ServiceSettings,
    *,
    environ: Mapping[str, str],
    host_home: Path,
    strict: bool,
    secrets: frozenset[str],
) -> dict[str, Any]:
    credential_sources: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    docker_host_signal = "DOCKER_HOST" if environ.get("DOCKER_HOST") else None
    if docker_host_signal is None and settings.docker_host:
        docker_host_signal = "service_settings.docker_host"
    if docker_host_signal is not None:
        credential_sources.append(
            _credential_source(
                type_="docker_host",
                signal=docker_host_signal,
                credential_scope="docker_host_control",
                isolation="host_daemon",
            )
        )
        warnings.append(
            _security_warning(
                "DOCKER_HOST_BROAD_CONTROL",
                (
                    "Docker host access grants broad control of the local Docker daemon; "
                    "AWF reports this as a local least-privilege downgrade."
                ),
            )
        )

    registry_sources = _docker_registry_sources(environ=environ, host_home=host_home)
    credential_sources.extend(registry_sources)
    if any(source["signal"] == "DOCKER_AUTH_CONFIG" for source in registry_sources):
        warnings.append(
            _security_warning(
                "STATIC_TOKEN_FALLBACK",
                "Docker registry auth is supplied by static service environment variable DOCKER_AUTH_CONFIG.",
            )
        )

    if credential_sources:
        reason = (
            "DOCKER_HOST_CONFIGURED"
            if docker_host_signal is not None
            else "DOCKER_REGISTRY_AUTH_PRESENT"
        )
        return _provider_result(
            ok=True,
            strict=strict,
            reason=reason,
            message="Docker credential and control-plane signals were observed without reading secret values.",
            signals=[source["signal"] for source in credential_sources],
            secrets=secrets,
            credential_sources=credential_sources,
            credential_scope=_primary_credential_scope(credential_sources),
            isolation=_primary_isolation(credential_sources),
            warnings=warnings,
        )

    return _provider_result(
        ok=False,
        strict=strict,
        reason="DOCKER_AUTH_NOT_OBSERVED",
        message=(
            "No Docker host or registry auth signal was visible. Docker daemon "
            "readiness is still reported by the dedicated Docker resource checks."
        ),
        secrets=secrets,
        credential_scope="not_observed",
        isolation="none",
    )


def _provider_result(
    *,
    ok: bool,
    strict: bool,
    reason: str,
    message: str,
    secrets: frozenset[str],
    signals: Iterable[str] | None = None,
    capabilities: Iterable[str] | None = None,
    detail: str | None = None,
    action: str | None = None,
    credential_sources: Iterable[Mapping[str, str]] | None = None,
    credential_scope: str | None = None,
    isolation: str | None = None,
    warnings: Iterable[Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    status_value = "ok" if ok else "fail" if strict else "warn"
    severity_value = "ok" if ok else "error" if strict else "warning"
    source_list = [dict(source) for source in credential_sources or ()]
    warning_list = [_redacted_warning(warning, secrets) for warning in warnings or ()]
    if not ok and not warning_list:
        warning_list.append(
            _security_warning(
                reason,
                _redact(message, secrets),
                severity=severity_value,
            )
        )
    payload: dict[str, Any] = {
        "ok": ok,
        "status": status_value,
        "severity": severity_value,
        "reason": reason,
        "message": _redact(message, secrets),
        "credential_sources": source_list,
        "credential_scope": credential_scope or _primary_credential_scope(source_list),
        "isolation": isolation or _primary_isolation(source_list),
        "warnings": warning_list,
    }
    if signals:
        payload["signals"] = list(signals)
    if capabilities:
        payload["capabilities"] = list(capabilities)
    if detail:
        payload["detail"] = _redact(_truncate(detail), secrets)
    if action:
        payload["action"] = _redact(action, secrets)
    return payload


def _credential_source(
    *,
    type_: str,
    signal: str,
    credential_scope: str,
    isolation: str,
) -> dict[str, str]:
    return {
        "type": type_,
        "signal": signal,
        "credential_scope": credential_scope,
        "isolation": isolation,
    }


def _security_warning(
    reason: str,
    message: str,
    *,
    severity: str = "warning",
) -> dict[str, str]:
    return {"reason": reason, "message": message, "severity": severity}


def _redacted_warning(warning: Mapping[str, str], secrets: frozenset[str]) -> dict[str, str]:
    return {
        "reason": _redact(str(warning.get("reason", "UNKNOWN")), secrets),
        "message": _redact(str(warning.get("message", "")), secrets),
        "severity": _redact(str(warning.get("severity", "warning")), secrets),
    }


def _static_env_warnings(
    *,
    provider_label: str,
    signals: Iterable[str],
) -> list[dict[str, str]]:
    return [
        _security_warning(
            "STATIC_TOKEN_FALLBACK",
            f"{provider_label} auth is supplied by static service environment variable {signal}.",
        )
        for signal in signals
    ]


def _primary_credential_scope(sources: Iterable[Mapping[str, str]]) -> str:
    scopes = [str(source.get("credential_scope", "")) for source in sources]
    for preferred in (
        "isolated_workspace",
        "read_only_host_path",
        "docker_host_control",
        "static_env_token",
    ):
        if preferred in scopes:
            return preferred
    return "not_observed"


def _primary_isolation(sources: Iterable[Mapping[str, str]]) -> str:
    isolations = [str(source.get("isolation", "")) for source in sources]
    for preferred in (
        "per_workspace_copy",
        "read_only_bind",
        "host_daemon",
        "service_env",
    ):
        if preferred in isolations:
            return preferred
    return "none"


def _codex_file_sources(host_home: Path) -> list[dict[str, str]]:
    source = host_home / ".codex"
    if not source.exists():
        return []
    sources = [
        _credential_source(
            type_="path",
            signal=f"~/.codex/{filename}",
            credential_scope="isolated_workspace",
            isolation="per_workspace_copy",
        )
        for filename in _CODEX_AUTH_FILES
        if (source / filename).is_file()
    ]
    if (source / "rules").is_dir():
        sources.append(
            _credential_source(
                type_="path",
                signal="~/.codex/rules",
                credential_scope="isolated_workspace",
                isolation="per_workspace_copy",
            )
        )
    if not sources and source.is_dir():
        sources.append(
            _credential_source(
                type_="path",
                signal="~/.codex",
                credential_scope="isolated_workspace",
                isolation="per_workspace_copy",
            )
        )
    return sources


def _existing_credential_sources(
    candidates: Iterable[tuple[Path, str]],
    *,
    credential_scope: str,
    isolation: str,
) -> list[dict[str, str]]:
    return [
        _credential_source(
            type_="path",
            signal=signal,
            credential_scope=credential_scope,
            isolation=isolation,
        )
        for path, signal in candidates
        if path.exists()
    ]


def _docker_registry_sources(
    *,
    environ: Mapping[str, str],
    host_home: Path,
) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    if environ.get("DOCKER_AUTH_CONFIG"):
        sources.append(
            _credential_source(
                type_="env",
                signal="DOCKER_AUTH_CONFIG",
                credential_scope="static_env_token",
                isolation="service_env",
            )
        )

    docker_config = environ.get("DOCKER_CONFIG")
    if docker_config:
        config_path = Path(docker_config).expanduser() / "config.json"
        if config_path.is_file():
            sources.append(
                _credential_source(
                    type_="path",
                    signal="DOCKER_CONFIG/config.json",
                    credential_scope="read_only_host_path",
                    isolation="read_only_bind",
                )
            )
    elif (host_home / ".docker" / "config.json").is_file():
        sources.append(
            _credential_source(
                type_="path",
                signal="~/.docker/config.json",
                credential_scope="read_only_host_path",
                isolation="read_only_bind",
            )
        )
    return sources


def _provider_warning_values(provider: Mapping[str, Any]) -> list[Any]:
    provider_warnings = provider.get("warnings")
    if not isinstance(provider_warnings, list):
        return []
    return provider_warnings


def _security_summary(providers: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    warning_entries: list[dict[str, str]] = []
    providers_with_warnings: list[str] = []
    reason_codes: set[str] = set()

    for provider_name in PROVIDER_NAMES:
        provider = providers.get(provider_name)
        if provider is None:
            continue
        provider_warnings = _provider_warning_values(provider)
        if provider_warnings:
            providers_with_warnings.append(provider_name)
            for raw_warning in provider_warnings:
                if not isinstance(raw_warning, Mapping):
                    continue
                reason = str(raw_warning.get("reason", "UNKNOWN"))
                reason_codes.add(reason)
                warning_entries.append(
                    {
                        "provider": provider_name,
                        "reason": reason,
                        "severity": str(raw_warning.get("severity", "warning")),
                    }
                )
        if provider.get("status") in {"warn", "fail"}:
            reason_codes.add(str(provider.get("reason", "UNKNOWN")))
            if provider_name not in providers_with_warnings:
                providers_with_warnings.append(provider_name)

    return {
        "status": "warning" if warning_entries else "ok",
        "warning_count": len(warning_entries),
        "providers_with_warnings": providers_with_warnings,
        "reason_codes": sorted(reason_codes),
        "warnings": warning_entries,
    }


def _github_token(settings: ServiceSettings, environ: Mapping[str, str]) -> tuple[str | None, str]:
    for key in _GITHUB_TOKEN_ENV_KEYS:
        value = environ.get(key)
        if value:
            return value, key
    if settings.github_token:
        return settings.github_token, "AWF_GITHUB_TOKEN"
    return None, "AWF_GITHUB_TOKEN"


def _first_present_env(environ: Mapping[str, str], keys: Iterable[str]) -> str | None:
    return next((key for key in keys if environ.get(key)), None)


def _secret_values(settings: ServiceSettings, environ: Mapping[str, str]) -> frozenset[str]:
    values = {
        value
        for key, value in environ.items()
        if key.upper() in _KNOWN_SECRET_ENV_KEYS and len(value) >= 4
    }
    if settings.github_token and len(settings.github_token) >= 4:
        values.add(settings.github_token)
    return frozenset(values)


def _redact(value: str, secrets: frozenset[str]) -> str:
    redacted = value
    for secret in sorted(secrets, key=len, reverse=True):
        redacted = redacted.replace(secret, _REDACTION)
    redacted = _URL_CREDENTIAL_RE.sub(r"\1<redacted>@", redacted)
    return _TOKEN_RE.sub(_REDACTION, redacted)


def _redact_with_redaction_parts(
    value: str,
    secrets: frozenset[str],
) -> tuple[str, list[str] | None]:
    segments: list[_RedactionSegment] = [("literal", value)]
    for secret in sorted(secrets, key=len, reverse=True):
        segments = _replace_literal_redaction_spans(segments, secret)
    segments = _replace_url_credential_redaction_spans(segments)
    segments = _replace_token_redaction_spans(segments)
    return _render_redaction_segments(segments), _redaction_parts(segments)


def _replace_literal_redaction_spans(
    segments: list[_RedactionSegment],
    text: str,
) -> list[_RedactionSegment]:
    if not text:
        return segments
    replaced: list[_RedactionSegment] = []
    for kind, segment_text in segments:
        if kind == "redaction":
            replaced.append((kind, segment_text))
            continue
        parts = segment_text.split(text)
        if len(parts) == 1:
            replaced.append((kind, segment_text))
            continue
        for index, part in enumerate(parts):
            if part:
                replaced.append(("literal", part))
            if index < len(parts) - 1:
                replaced.append(("redaction", ""))
    return _merge_literal_redaction_segments(replaced)


def _replace_url_credential_redaction_spans(
    segments: list[_RedactionSegment],
) -> list[_RedactionSegment]:
    rendered = _render_redaction_segments(segments)
    replacements: list[tuple[int, int, list[_RedactionSegment]]] = []
    for match in _URL_CREDENTIAL_RE.finditer(rendered):
        replacements.append((match.start(2), match.end(2), [("redaction", ""), ("literal", "@")]))
    return _replace_rendered_redaction_spans(segments, replacements)


def _replace_token_redaction_spans(
    segments: list[_RedactionSegment],
) -> list[_RedactionSegment]:
    rendered = _render_redaction_segments(segments)
    replacements: list[tuple[int, int, list[_RedactionSegment]]] = []
    for match in _TOKEN_RE.finditer(rendered):
        replacements.append((match.start(1), match.end(1), [("redaction", "")]))
    return _replace_rendered_redaction_spans(segments, replacements)


def _replace_rendered_redaction_spans(
    segments: list[_RedactionSegment],
    replacements: list[tuple[int, int, list[_RedactionSegment]]],
) -> list[_RedactionSegment]:
    if not replacements:
        return segments

    rendered_length = len(_render_redaction_segments(segments))
    cursor = 0
    replaced: list[_RedactionSegment] = []
    for start, end, replacement in replacements:
        replaced.extend(_slice_redaction_segments(segments, cursor, start))
        replaced.extend(replacement)
        cursor = end
    replaced.extend(_slice_redaction_segments(segments, cursor, rendered_length))
    return _merge_literal_redaction_segments(replaced)


def _slice_redaction_segments(
    segments: list[_RedactionSegment],
    start: int,
    end: int,
) -> list[_RedactionSegment]:
    if start >= end:
        return []

    sliced: list[_RedactionSegment] = []
    position = 0
    for kind, segment_text in segments:
        rendered = segment_text if kind == "literal" else _REDACTION
        next_position = position + len(rendered)
        overlap_start = max(start, position)
        overlap_end = min(end, next_position)
        if overlap_start < overlap_end:
            inner_start = overlap_start - position
            inner_end = overlap_end - position
            if kind == "redaction" and inner_start == 0 and inner_end == len(_REDACTION):
                sliced.append(("redaction", ""))
            else:
                sliced.append(("literal", rendered[inner_start:inner_end]))
        position = next_position
        if position >= end:
            break
    return sliced


def _merge_literal_redaction_segments(
    segments: list[_RedactionSegment],
) -> list[_RedactionSegment]:
    merged: list[_RedactionSegment] = []
    for kind, text in segments:
        if kind == "literal" and not text:
            continue
        if kind == "literal" and merged and merged[-1][0] == "literal":
            merged[-1] = ("literal", f"{merged[-1][1]}{text}")
            continue
        merged.append((kind, text))
    return merged


def _render_redaction_segments(segments: list[_RedactionSegment]) -> str:
    return "".join(text if kind == "literal" else _REDACTION for kind, text in segments)


def _redaction_parts(segments: list[_RedactionSegment]) -> list[str] | None:
    if not any(kind == "redaction" for kind, _text in segments):
        return None

    parts = [""]
    for kind, text in segments:
        if kind == "redaction":
            parts.append("")
        else:
            parts[-1] += text
    return parts


def _log_redacted_exception(
    event: str,
    exc: Exception,
    secrets: frozenset[str],
) -> None:
    detail = _redact(_truncate(f"{type(exc).__name__}: {exc}"), secrets)
    trace = _redact(
        _truncate(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            limit=_TRACEBACK_LOG_LIMIT,
        ),
        secrets,
    )
    _log.error("%s: %s\n%s", event, detail, trace)


def _log_redacted_terminal_failure(
    event: str,
    detail: str,
    secrets: frozenset[str],
) -> None:
    _log.error(
        "%s: %s",
        event,
        _truncate(_redact(detail, secrets), limit=_TRACEBACK_LOG_LIMIT),
    )


def _truncate(value: str, *, limit: int = 240) -> str:
    stripped = value.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 1] + "…"


def _ollama_version_url(environ: Mapping[str, str]) -> str:
    return _ollama_version_urls(environ)[0]


def _ollama_version_urls(environ: Mapping[str, str]) -> tuple[str, ...]:
    return _ollama_api_urls(environ, "api/version")


def _ollama_tags_urls(environ: Mapping[str, str]) -> tuple[str, ...]:
    return _ollama_api_urls(environ, "api/tags")


def _ollama_api_urls(environ: Mapping[str, str], api_path: str) -> tuple[str, ...]:
    raw = (
        environ.get("AWF_OPENCODE_OLLAMA_BASE_URL")
        or environ.get("OLLAMA_HOST")
        or DEFAULT_OLLAMA_OPENAI_BASE_URL
    ).strip()
    if "://" not in raw:
        raw = f"http://{raw}"
    parts = urlsplit(raw)
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    suffix = api_path if api_path.startswith("/") else f"/{api_path}"
    path = f"{path}{suffix}" if path else suffix
    primary = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    if parts.hostname == "host.docker.internal":
        fallback_port = parts.port or 11434
        fallback = urlunsplit((parts.scheme, f"localhost:{fallback_port}", path, "", ""))
        return (primary, fallback)
    return (primary,)


def _ollama_probe_failure_debug(
    *,
    url: str,
    status: str,
    detail: str,
    secrets: frozenset[str],
    status_code: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "url": _redact(url, secrets),
        "status": status,
        "detail": _truncate(_redact(detail, secrets)),
    }
    if status_code is not None:
        payload["status_code"] = status_code
    return payload


def _probe_ollama(
    urls: tuple[str, ...],
    *,
    http_get: HttpGet,
    secrets: frozenset[str],
) -> dict[str, Any]:
    failures: list[str] = []
    http_failures: list[str] = []
    recovered_failures: list[dict[str, Any]] = []
    exceptions: list[Exception] = []
    for url in urls:
        try:
            response = http_get(url, timeout=_HTTP_TIMEOUT_SECONDS)
        except Exception as exc:
            exceptions.append(exc)
            detail = f"{type(exc).__name__}: {exc}"
            failures.append(f"{url}: {detail}" if len(urls) > 1 else detail)
            recovered_failures.append(
                _ollama_probe_failure_debug(
                    url=url,
                    status="exception",
                    detail=detail,
                    secrets=secrets,
                )
            )
            continue
        if 200 <= response.status_code < 300:
            payload: dict[str, Any] = {"ok": True}
            if recovered_failures:
                payload["debug"] = {"recovered_failures": recovered_failures}
            return payload
        detail = response.text or f"HTTP {response.status_code}"
        failure = f"HTTP {response.status_code}: {detail}"
        failure_detail = f"{url}: {failure}" if len(urls) > 1 else failure
        failures.append(failure_detail)
        http_failures.append(failure_detail)
        recovered_failures.append(
            _ollama_probe_failure_debug(
                url=url,
                status="http_error",
                status_code=response.status_code,
                detail=failure,
                secrets=secrets,
            )
        )
    for logged_exc in exceptions:
        _log_redacted_exception(
            "provider_readiness.ollama_probe_exception",
            logged_exc,
            secrets,
        )
    if http_failures:
        _log_redacted_terminal_failure(
            "provider_readiness.ollama_probe_exception",
            "; ".join(http_failures),
            secrets,
        )
    return {"ok": False, "detail": _redact("; ".join(failures), secrets)}


def _probe_ollama_model(
    urls: tuple[str, ...],
    *,
    model: str | None,
    http_get: HttpGet,
    secrets: frozenset[str],
) -> dict[str, Any]:
    candidates = _ollama_model_candidates(model)
    if not candidates:
        return {
            "status": "fail",
            "reason_code": "MODEL_NOT_SELECTED",
            "message": "No OpenCode/Ollama model was selected for launch.",
        }

    failures: list[str] = []
    exceptions: list[Exception] = []
    recovered_failures: list[dict[str, Any]] = []
    available_models: set[str] = set()
    saw_model_response = False
    for url in urls:
        try:
            response = http_get(url, timeout=_HTTP_TIMEOUT_SECONDS)
        except Exception as exc:
            exceptions.append(exc)
            detail = f"{type(exc).__name__}: {exc}"
            failures.append(f"{url}: {detail}" if len(urls) > 1 else detail)
            recovered_failures.append(
                _ollama_probe_failure_debug(
                    url=url,
                    status="exception",
                    detail=detail,
                    secrets=secrets,
                )
            )
            continue
        if not 200 <= response.status_code < 300:
            detail = response.text or f"HTTP {response.status_code}"
            failure = f"HTTP {response.status_code}: {detail}"
            failures.append(f"{url}: {failure}" if len(urls) > 1 else failure)
            recovered_failures.append(
                _ollama_probe_failure_debug(
                    url=url,
                    status="http_error",
                    status_code=response.status_code,
                    detail=failure,
                    secrets=secrets,
                )
            )
            continue
        try:
            payload = json.loads(response.text or "{}")
        except json.JSONDecodeError as exc:
            failures.append(
                f"{url}: invalid JSON from Ollama /api/tags: {exc}"
                if len(urls) > 1
                else f"invalid JSON from Ollama /api/tags: {exc}"
            )
            continue

        available = _ollama_model_names(payload)
        if candidates & available:
            result: dict[str, Any] = {
                "status": "ok",
                "reason_code": "OLLAMA_MODEL_AVAILABLE",
            }
            if recovered_failures:
                result["debug"] = {"recovered_failures": recovered_failures}
            return result
        saw_model_response = True
        available_models.update(available)

    if saw_model_response:
        _log_ollama_model_probe_exceptions(exceptions, secrets)
        detail = f"selected={model}; available_count={len(available_models)}"
        if failures:
            detail = f"{detail}; probe_failures={'; '.join(failures)}"
        return {
            "status": "fail",
            "reason_code": "OLLAMA_MODEL_NOT_AVAILABLE",
            "message": "Selected OpenCode/Ollama model is not available from Ollama /api/tags.",
            "detail": _truncate(_redact(detail, secrets)),
        }

    _log_ollama_model_probe_exceptions(exceptions, secrets)

    return {
        "status": "fail",
        "reason_code": "OLLAMA_MODEL_PROBE_FAILED",
        "message": "Ollama model availability probe did not complete successfully.",
        "detail": _truncate(_redact("; ".join(failures), secrets)),
    }


def _log_ollama_model_probe_exceptions(
    exceptions: Sequence[Exception],
    secrets: frozenset[str],
) -> None:
    for logged_exc in exceptions:
        _log_redacted_exception(
            "provider_readiness.ollama_model_probe_exception",
            logged_exc,
            secrets,
        )


def _ollama_model_candidates(model: str | None) -> set[str]:
    if model is None:
        return set()
    raw = model.strip()
    if not raw:
        return set()
    candidates = {raw}
    model_name = raw
    if "/" in raw:
        provider, remainder = raw.split("/", 1)
        if provider == "ollama" and remainder:
            model_name = remainder
            candidates.add(model_name)
    if ":" not in model_name:
        candidates.add(f"{model_name}:latest")
    return candidates


def _ollama_model_names(payload: object) -> set[str]:
    if not isinstance(payload, Mapping):
        return set()
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        return set()
    names: set[str] = set()
    for item in raw_models:
        if isinstance(item, str) and item:
            names.add(item)
            continue
        if not isinstance(item, Mapping):
            continue
        name = item.get("name") or item.get("model")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _ordered_names(providers: set[ProviderName]) -> list[str]:
    return [provider for provider in PROVIDER_NAMES if provider in providers]


def _run_subprocess(
    args: list[str],
    *,
    check: bool,
    capture_output: bool,
    text: Literal[True],
    timeout: float,
    env: Mapping[str, str],
) -> CompletedProcessLike:
    return subprocess.run(
        args,
        check=check,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        env=env,
    )


def _http_get(url: str, *, timeout: float) -> HttpResponseLike:
    return httpx.get(url, timeout=timeout)
