"""Provider readiness credential, redaction, and local probe helpers."""

from __future__ import annotations

import ipaddress
import re
import subprocess
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx
from pydantic import ValidationError

from awf.adapters.opencode import DEFAULT_OLLAMA_OPENAI_BASE_URL
from awf.profiles.models import WorkspaceProfile
from awf.service.config import ServiceSettings
from awf.service.environment import ComposeEnvInterpolationError, compose_expand_value

# Env keys that select the Ollama daemon. The OpenCode launcher resolves the
# base URL from these (``AWF_OPENCODE_OLLAMA_BASE_URL`` first, then ``OLLAMA_HOST``)
# inside the rendered workspace container, where a profile's ``runtime.environment``
# value wins over the worker-env placeholder (``merge_agent_environment`` is
# first-writer-wins). Mirror that resolution wherever a probe/pull must target the
# same daemon the agent will reach.
_OLLAMA_BASE_URL_ENV_KEYS = ("AWF_OPENCODE_OLLAMA_BASE_URL", "OLLAMA_HOST")


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


# Provider prefix -> env keys that satisfy that provider for OpenCode. All of
# these are already in ``KNOWN_SECRET_ENV_KEYS``, so detection keeps redaction
# intact. A provider prefix absent from this map is satisfied only by the
# OpenCode credential store (``~/.config/opencode``). Only list keys OpenCode
# actually reads: its OpenAI path uses the AI SDK OpenAI provider, whose apiKey
# default is ``OPENAI_API_KEY`` — the Codex-style ``OPENAI_API_TOKEN`` is *not*
# consumed, so admitting it would pass preflight only for the agent to launch
# without the credential OpenCode reads and fail later.
_OPENCODE_PROVIDER_ENV_KEYS: Mapping[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "xai": ("XAI_API_KEY",),
}


def _opencode_provider_credentials_present(
    model: str | None,
    environ: Mapping[str, str],
    host_home: Path,
) -> tuple[bool, str | None]:
    """Return whether OpenCode has a credential for a non-Ollama provider model.

    Symmetric to the OpenCode/Ollama auth gate: a provider-qualified non-Ollama
    model (``openai/...``, ``anthropic/...``) is served by an OpenCode cloud
    provider, so create-time readiness requires a usable credential. ``~/.config/
    opencode`` is OpenCode's own multi-provider credential store and satisfies any
    provider; otherwise the provider API key matching the model's ``<provider>/``
    prefix must be visible. An unknown provider prefix not in
    ``_OPENCODE_PROVIDER_ENV_KEYS`` is satisfied only by ``~/.config/opencode``.
    """

    if (host_home / ".config" / "opencode").is_dir():
        return True, "~/.config/opencode"
    provider = (model or "").strip().partition("/")[0].lower()
    signal = _first_present_env(environ, _OPENCODE_PROVIDER_ENV_KEYS.get(provider, ()))
    return (signal is not None), signal


def _secret_values(settings: ServiceSettings, environ: Mapping[str, str]) -> frozenset[str]:
    values = {
        value
        for key, value in environ.items()
        if key.upper() in KNOWN_SECRET_ENV_KEYS and len(value) >= 4
    }
    if settings.github_token and len(settings.github_token) >= 4:
        values.add(settings.github_token)
    return frozenset(values)


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


def _agent_runtime_cli_reason_prefix(provider: ProviderName) -> str:
    return {
        "codex": "CODEX",
        "claude_code": "CLAUDE",
        "cursor": "CURSOR",
        "gemini": "GEMINI",
        "opencode": "OPENCODE",
        "grok": "GROK",
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


def _runtime_cli_probe_payload(probe: Mapping[str, Any]) -> dict[str, str]:
    """Return the public string fields from a runtime CLI probe result."""
    return {
        key: value
        for key in ("status", "reason_code", "message", "detail")
        if isinstance((value := probe.get(key)), str) and value
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


def _ollama_version_url(environ: Mapping[str, str]) -> str:
    return _ollama_version_urls(environ)[0]


def _ollama_version_urls(environ: Mapping[str, str]) -> tuple[str, ...]:
    return _ollama_api_urls(environ, "api/version")


def _ollama_tags_urls(environ: Mapping[str, str]) -> tuple[str, ...]:
    return _ollama_api_urls(environ, "api/tags")


def _ollama_pull_urls(environ: Mapping[str, str]) -> tuple[str, ...]:
    return _ollama_api_urls(environ, "api/pull")


def _ollama_pull_name(model: str | None) -> str:
    """Return the bare Ollama model reference for a ``POST /api/pull`` body."""
    raw = (model or "").strip()
    if "/" in raw:
        provider, remainder = raw.split("/", 1)
        if provider == "ollama" and remainder:
            return remainder
    return raw


def _is_cloud_model(model: str | None) -> bool:
    """Return whether the model is an Ollama Cloud model (served remotely).

    Ollama Cloud models carry a ``cloud`` tag in either form the daemon
    publishes: the bare ``:cloud`` tag (e.g. ``glm-5.1:cloud``) or a
    size-qualified tag ending in ``-cloud`` (e.g. ``gpt-oss:120b-cloud``,
    ``gemma4:31b-cloud``). Match on the tag portion so both are treated as
    served-remotely (no local ``/api/pull``).

    The size qualifier always begins with a digit (a parameter count such as
    ``31b`` or ``120b``), so a plain suffix match on ``-cloud`` would be too
    loose: a local tag like ``:not-cloud`` must NOT be classified as cloud, or
    readiness would skip pull-pending and the executor would never pull it.
    """
    pull_name = _ollama_pull_name(model)
    tag = pull_name.rpartition(":")[2] if ":" in pull_name else ""
    return tag == "cloud" or re.fullmatch(r"[0-9][0-9.]*[a-z]*-cloud", tag) is not None


def _opencode_model_is_local_ollama(model: str | None) -> bool:
    """Return whether an OpenCode model is an authless local Ollama model.

    A bare or ``ollama/``-prefixed reference that is NOT an Ollama Cloud
    (``:cloud``) model is served by the local host Ollama daemon, whose
    ``/api/tags`` and ``/api/pull`` endpoints need no OpenCode/Ollama Cloud
    credential. A provider-qualified non-Ollama model (handled separately by
    ``OPENCODE_NON_OLLAMA_PROVIDER_SELECTED``) and a ``:cloud`` model both
    return ``False`` — the latter is served remotely and still requires the
    cloud credential.
    """
    raw = (model or "").strip()
    if not raw:
        return False
    if _opencode_model_targets_non_ollama_provider(raw):
        return False
    return not _is_cloud_model(raw)


def _opencode_ollama_credentials_present(
    environ: Mapping[str, str],
    host_home: Path,
) -> bool:
    """Return whether an OpenCode/Ollama Cloud credential is visible to the worker.

    Mirrors the credential-signal detection in ``_check_opencode``: OpenCode's own
    credential store (``~/.config/opencode``), mounted ``~/.ollama`` auth files, or
    the ``OLLAMA_API_KEY`` env. A ``:cloud`` Ollama model is served remotely and
    needs one of these (unlike a local model, which is authless), so the
    non-worker-reachable host-probe defer is gated on this for cloud models.
    """

    if (host_home / ".config" / "opencode").is_dir():
        return True
    if any((host_home / ".ollama" / filename).is_file() for filename in _OLLAMA_AUTH_FILES):
        return True
    return _first_present_env(environ, _OPENCODE_ENV_KEYS) is not None


def _opencode_ollama_host_probe_deferrable(
    model: str | None,
    environ: Mapping[str, str],
    host_home: Path,
) -> bool:
    """Return whether the worker-side Ollama daemon probe can be deferred.

    Used by create/retry admission when the resolved Ollama base URL is not
    reachable from the worker (a workspace Compose service DNS name such as
    ``http://ollama-sidecar:11434``). The probe defers to the agent container —
    where the sidecar daemon IS reachable — for an Ollama-served model:

    - a local model is authless, so it always defers (see
      ``_opencode_model_is_local_ollama``);
    - a ``:cloud`` model is served remotely *through* the daemon and still needs the
      OpenCode/Ollama Cloud credential, so it defers only once that credential is
      visible. Without it the caller falls through to ``_check_opencode``, which
      blocks with ``OPENCODE_OLLAMA_AUTH_MISSING`` (no host probe) — the correct
      create-time disposition.

    A provider-qualified non-Ollama model is handled by the dedicated
    ``OPENCODE_NON_OLLAMA_PROVIDER_SELECTED`` path and never reaches here.
    """

    if _opencode_model_is_local_ollama(model):
        return True
    if _opencode_model_targets_non_ollama_provider(model):
        return False
    return _is_cloud_model(model) and _opencode_ollama_credentials_present(environ, host_home)


def _parse_ollama_base_url(environ: Mapping[str, str]) -> SplitResult:
    """Resolve and parse the Ollama base URL, normalizing malformed input.

    Both the worker reachability classifier and the probe/pull URL builder need the
    *same* resolution of ``AWF_OPENCODE_OLLAMA_BASE_URL`` / ``OLLAMA_HOST`` (falling
    back to the default) and the *same* behavior for a garbled value. ``urlsplit``
    raises ``ValueError`` for an unbalanced IPv6 literal (e.g. ``http://[::1``), and
    its lazy ``.hostname`` / ``.port`` accessors raise for an invalid IPv6 host or a
    non-numeric port. Force those accessors here so a malformed value normalizes to
    the ``host.docker.internal`` default exactly once — host-reachable, never a crash
    during readiness — instead of escaping as a ``ValueError`` from whichever caller
    happens to touch the offending attribute first.
    """

    raw = (
        environ.get("AWF_OPENCODE_OLLAMA_BASE_URL")
        or environ.get("OLLAMA_HOST")
        or DEFAULT_OLLAMA_OPENAI_BASE_URL
    ).strip()
    if "://" not in raw:
        raw = f"http://{raw}"
    try:
        parts = urlsplit(raw)
        # Trigger the lazy netloc parse so an invalid IPv6 host / non-numeric port
        # surfaces here rather than at an arbitrary downstream attribute access.
        _ = parts.hostname
        _ = parts.port
    except ValueError:
        parts = urlsplit(DEFAULT_OLLAMA_OPENAI_BASE_URL)
    return _default_ollama_port(parts)


def _default_ollama_port(parts: SplitResult) -> SplitResult:
    """Default a port-less authority to Ollama's daemon port (11434).

    The OpenCode launcher prelude normalizes a port-less ``OLLAMA_HOST`` (a bare
    Compose service name such as ``ollama-sidecar``, or ``localhost`` / ``0.0.0.0``)
    to ``:11434`` before handing the agent its base URL. The worker-side probe/pull
    URL builder must resolve the *same* daemon, so mirror that defaulting here —
    otherwise a port-less value would probe the scheme default (port 80) while the
    agent talks to 11434, failing or hitting the wrong daemon at create/retry
    preflight. A value that already carries a port (or has no host, e.g. ``://``) is
    returned unchanged.
    """

    if parts.port is not None or not parts.hostname:
        return parts
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    userinfo = ""
    if parts.username is not None:
        userinfo = parts.username
        if parts.password is not None:
            userinfo = f"{userinfo}:{parts.password}"
        userinfo = f"{userinfo}@"
    return parts._replace(netloc=f"{userinfo}{host}:11434")


def _ollama_base_url_malformed(environ: Mapping[str, str]) -> bool:
    """Return whether an *explicitly set* Ollama base URL is unparseable.

    ``_parse_ollama_base_url`` normalizes a malformed value to the
    ``host.docker.internal`` default so readiness never crashes. That is the right
    disposition for a blank/missing value (which resolves to the always-valid
    default), but not for an *explicit* ``AWF_OPENCODE_OLLAMA_BASE_URL`` /
    ``OLLAMA_HOST`` that fails to parse: the OpenCode launcher passes the explicit
    value through to the agent verbatim, so probing/pulling the default daemon would
    admit a workspace — and even pull a model — against a daemon the agent never
    uses, only for the agent to fail later against the invalid URL. Surface such
    explicit-malformed config to the admission gate so it can fail-close instead.

    Mirrors ``_parse_ollama_base_url``'s resolution/parse exactly (precedence,
    scheme defaulting, and the lazy ``hostname`` / ``port`` accessors that raise on
    an invalid IPv6 host or non-numeric port) but reports the malformed value rather
    than swallowing it into the default.
    """

    raw = environ.get("AWF_OPENCODE_OLLAMA_BASE_URL") or environ.get("OLLAMA_HOST")
    if raw is None or not raw.strip():
        return False
    candidate = raw.strip()
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    try:
        parts = urlsplit(candidate)
        _ = parts.hostname
        _ = parts.port
    except ValueError:
        return True
    return False


def _ollama_api_urls(environ: Mapping[str, str], api_path: str) -> tuple[str, ...]:
    parts = _parse_ollama_base_url(environ)
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


# Hostnames that resolve to the host the worker runs on (Docker host gateway
# aliases and localhost). An Ollama base URL pointing at one of these — or any
# loopback IP — is reachable from the worker; anything else (a workspace Compose
# service DNS name, a routable LAN IP) is not. ``0.0.0.0`` is a valid Ollama bind
# address (``OLLAMA_HOST=0.0.0.0:11434``) that, as a *connection* target, routes
# to the loopback just like ``localhost``; classify it reachable so a broken
# host-bound daemon is still caught at create time rather than silently skipped
# (``ip_address("0.0.0.0").is_loopback`` is ``False`` — the unspecified address is
# not a loopback IP — so it would otherwise fall through to the conservative skip).
_WORKER_HOST_REACHABLE_HOSTNAMES = frozenset(
    {
        "host.docker.internal",
        "gateway.docker.internal",
        "localhost",
        "0.0.0.0",
    }
)


def _ollama_url_host_reachable_from_worker(environ: Mapping[str, str]) -> bool:
    """Return whether the resolved Ollama base URL is reachable from the worker.

    The worker runs off ``awf_net`` and cannot reach a workspace Compose service
    DNS name (e.g. ``http://ollama-sidecar:11434``). Mirror ``_ollama_api_urls``'s
    URL resolution, then classify the host: the Docker host gateway aliases,
    ``localhost``, and loopback IPs are host-reachable; any other DNS name (a
    Compose service) or routable IP is not. A missing/blank/garbled value resolves
    to the ``host.docker.internal`` default and is therefore host-reachable (no
    crash). The skip is conservative: a non-host-reachable URL defers the model
    probe to the agent container rather than risk a false ``OLLAMA_MODEL_PROBE_FAILED``.
    """

    # ``_parse_ollama_base_url`` normalizes a malformed value (unbalanced IPv6
    # brackets, non-numeric port) to the ``host.docker.internal`` default, which is
    # host-reachable — so a garbled URL is treated like any other garbled value
    # (host-reachable, never a crash) without a guard here.
    host = (_parse_ollama_base_url(environ).hostname or "").lower()
    if not host:
        return True
    if host in _WORKER_HOST_REACHABLE_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def overlay_profile_ollama_base_url(
    environ: Mapping[str, str],
    profile_snapshot: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Return *environ* overlaid with the profile's agent Ollama base URL.

    The OpenCode launcher derives ``AWF_OPENCODE_OLLAMA_BASE_URL`` / ``OLLAMA_HOST``
    from the agent container environment, where a profile's ``runtime.environment``
    value wins over the worker-env placeholder. If an Ollama probe/pull derived
    those URLs from the worker process env alone, AWF could target a different
    daemon than the agent reaches — admitting a workspace for a daemon the agent
    cannot use, or recording readiness against the wrong host. Overlay the
    profile-declared base URL so create-time admission and the executor pre-agent
    step both probe the same daemon. A workspace without a resolved profile (or an
    unvalidatable snapshot) falls back to *environ* unchanged.
    """

    result: dict[str, str] = dict(environ)
    if not isinstance(profile_snapshot, Mapping):
        return result
    try:
        profile = WorkspaceProfile.model_validate(dict(profile_snapshot))
    except ValidationError:  # pragma: no cover - persisted snapshots are pre-validated
        return result
    profile_env = profile.runtime.environment
    # Profile env values may carry Compose-style ``${NAME}`` placeholders that the
    # agent container resolves via Docker Compose host-env substitution. The
    # worker-side probe/pull does not pass through Compose, so resolve each declared
    # value against ``environ`` here — otherwise a literal ``${OLLAMA_HOST}`` would
    # be probed as ``http://${OLLAMA_HOST}/api/tags`` and block/fail the workspace
    # before launch. A placeholder that resolves to empty is treated as undeclared.
    # The required-interpolation form (``${OLLAMA_URL:?set OLLAMA_URL}``) raises when
    # the variable is absent from ``environ``; since this overlay runs during
    # create/retry admission before provider readiness — paths that only translate
    # profile/readiness exceptions — an unhandled raise would escape as a 500. The
    # worker environ is a best-effort approximation of the agent's Compose context
    # (the agent container may still resolve the variable), so treat an unresolvable
    # required placeholder the same as one resolving to empty: undeclared, fall back.
    declared: dict[str, str] = {}
    for key in _OLLAMA_BASE_URL_ENV_KEYS:
        raw = profile_env.get(key)
        if not raw:
            continue
        try:
            expanded = compose_expand_value(raw, environ=environ).strip()
        except ComposeEnvInterpolationError:
            continue
        if expanded:
            declared[key] = expanded
    if not declared:
        return result
    # The profile owns the Ollama daemon selection. ``_ollama_api_urls`` (and the
    # OpenCode launcher) resolve the daemon from the first non-empty key in
    # precedence order, so a higher-precedence worker-env value the profile did
    # *not* declare would shadow the profile's chosen daemon — e.g. a profile that
    # declares only ``OLLAMA_HOST`` while the worker env carries
    # ``AWF_OPENCODE_OLLAMA_BASE_URL``. Apply the profile's declared keys and clear
    # any higher-precedence worker value so the profile-selected daemon wins.
    top = next(i for i, key in enumerate(_OLLAMA_BASE_URL_ENV_KEYS) if key in declared)
    for index, key in enumerate(_OLLAMA_BASE_URL_ENV_KEYS):
        if key in declared:
            result[key] = declared[key]
        elif index < top:
            result.pop(key, None)
    return result


_ENV_SECRET_REF_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _env_secret_ref_name(ref: str | None) -> str | None:
    """Return the host env var name a profile ``kind="env"`` secret lease reads from.

    Mirrors ``node.secret_mounts`` ref parsing: an optional ``env/`` prefix is stripped
    and the remainder must be a valid env identifier. Anything else (a path-style ref, a
    malformed name, ``None``) yields ``None`` so the overlay treats it as unresolvable.
    """

    if ref is None:
        return None
    stripped = ref.strip()
    if stripped.startswith("env/"):
        stripped = stripped[len("env/") :]
    if not _ENV_SECRET_REF_NAME_RE.fullmatch(stripped):
        return None
    return stripped


def overlay_profile_provider_credentials(
    environ: Mapping[str, str],
    profile_snapshot: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Return *environ* overlaid with the profile's OpenCode provider API keys.

    The non-Ollama OpenCode create-time gate (``_opencode_provider_credentials_present``)
    detects a usable provider credential from the readiness environ, but a provider API
    key declared solely in a profile's ``runtime.environment`` (e.g. ``OPENAI_API_KEY``)
    reaches the *agent* container — not the worker process this admission check runs in.
    Without this overlay such a workspace would be blocked with
    ``OPENCODE_PROVIDER_AUTH_MISSING`` despite the credential the agent would use. The
    OpenCode/Ollama Cloud credential (``OLLAMA_API_KEY``, ``_OPENCODE_ENV_KEYS``) is
    overlaid for the same reason: a profile that points a ``:cloud`` Ollama model at a
    sidecar daemon unreachable from the worker needs that key visible for the host-probe
    defer path (``_opencode_ollama_host_probe_deferrable``); otherwise admission falls
    through to ``_check_opencode`` and blocks with ``OPENCODE_OLLAMA_AUTH_MISSING``.
    Symmetric to ``overlay_profile_ollama_base_url``: bring the profile-declared provider
    key into the environ so create/retry admission sees the same credential the agent
    reaches. Compose-style ``${NAME}`` placeholders are resolved against *environ*; a
    value resolving empty or to an unset required placeholder is treated as undeclared
    (the worker env is a best-effort approximation of the agent's Compose context).
    A profile may instead supply the same credential as a ``kind="env"`` secret lease
    (e.g. ``target="OPENAI_API_KEY"``, ``ref="env/HOST_OPENAI_KEY"``), which the launcher
    merges into the agent env; such leases are reflected here too by resolving each lease's
    host source against *environ* (``runtime.environment`` wins on conflict).
    A workspace without a resolved profile falls back to *environ* unchanged.
    """

    result: dict[str, str] = dict(environ)
    if not isinstance(profile_snapshot, Mapping):
        return result
    try:
        profile = WorkspaceProfile.model_validate(dict(profile_snapshot))
    except ValidationError:  # pragma: no cover - persisted snapshots are pre-validated
        return result
    profile_env = profile.runtime.environment
    candidate_keys = (
        *(key for keys in _OPENCODE_PROVIDER_ENV_KEYS.values() for key in keys),
        *_OPENCODE_ENV_KEYS,
    )
    runtime_declared: set[str] = set()
    for key in candidate_keys:
        if key not in profile_env:
            continue
        raw = profile_env[key]
        # ``runtime.environment`` wins over secret leases in the launcher's agent-env
        # merge (``merge_agent_environment`` is first-writer-wins), so a key declared
        # here owns the agent's slot even when its value cannot be resolved from the
        # worker environ — an unset required placeholder (``${MISSING:?set}``), one
        # resolving empty, or a literal empty string (``OPENAI_API_KEY: ""``). The
        # launcher keeps that empty/failing runtime value (the key is *present* in the
        # base env, so the skip-existing rule drops the lease addition), so record the
        # declaration on *presence* in ``profile_env`` rather than truthiness of the
        # value — and *before* attempting expansion. Otherwise the lease loop below
        # would overlay a host credential the launcher then drops in favour of the empty
        # runtime slot, admitting a workspace whose agent never receives a usable credential.
        runtime_declared.add(key)
        try:
            expanded = compose_expand_value(raw, environ=environ).strip()
        except ComposeEnvInterpolationError:
            expanded = ""
        if expanded:
            result[key] = expanded
        else:
            # The profile owns this slot but its declared value resolves empty (a literal
            # empty string, an unset required placeholder, or one resolving empty). The
            # launcher renders only the profile value into the agent, so an inherited
            # worker/service value of the same key — copied from ``environ`` into ``result``
            # above — never reaches the agent. Drop it so the credential gate does not pass
            # on a credential the agent will not receive (or a placeholder Compose cannot
            # resolve).
            result.pop(key, None)
    # A provider credential may instead be declared as a profile ``kind="env"`` secret
    # lease (e.g. ``target="OPENAI_API_KEY"``, ``ref="env/HOST_OPENAI_KEY"``). The stack
    # launcher merges the resolved lease environment into the agent env
    # (``agent_environment_with_declared_secret_leases``), so the agent receives that
    # credential — but it never appears in ``runtime.environment``. Reflect such leases
    # here too, resolving each lease's host source against ``environ``, so admission does
    # not block the workspace with ``OPENCODE_PROVIDER_AUTH_MISSING`` (or, for
    # ``OLLAMA_API_KEY``, ``OPENCODE_OLLAMA_AUTH_MISSING``) before the lease is resolved.
    # ``runtime.environment`` wins (the launcher merge is first-writer-wins), so a lease
    # whose target is declared in ``runtime.environment`` is skipped — even when that
    # runtime declaration could not be resolved here — because the launcher keeps the
    # runtime value and drops the lease. A lease whose host source is absent/empty is also
    # treated as undeclared: the launcher omits an optional lease and fails a required one,
    # but this best-effort overlay only surfaces a credential it can actually resolve from
    # the worker environ.
    candidate_set = frozenset(candidate_keys)
    for secret in profile.secrets:
        if secret.kind != "env" or secret.target not in candidate_set:
            continue
        # The launcher only merges a ``kind="env"`` lease into the agent env when its
        # provider routes through ``LocalSecretLeaseMountResolver``'s env path —
        # ``provider: env`` (``node.secret_mounts._ENV_PROVIDERS``). A lease that omits
        # ``provider`` is skipped (``_normalized_provider`` returns ``None``) and a
        # non-env provider routes to a different handler (or is rejected), so neither
        # injects this env key into the agent. Gate the overlay on the same ``provider:
        # env`` semantics — otherwise admission would pass an ``openai``/... preflight on
        # a host credential the agent container never receives.
        if (secret.provider or "").strip().lower() != "env":
            continue
        if secret.target in runtime_declared:
            continue
        source_name = _env_secret_ref_name(secret.ref)
        if source_name is None:
            continue
        value = environ.get(source_name, "").strip()
        if value:
            result[secret.target] = value
    return result


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


def _http_post_stream(
    url: str,
    *,
    json: Mapping[str, Any],
    timeout: float,
) -> AbstractContextManager[HttpStreamResponseLike]:
    # Thin wrapper over httpx's streaming POST, mirroring ``_http_get``. The
    # body is exercised through the injectable seam in tests; the live default
    # is a trivial passthrough.
    return httpx.stream(  # pragma: no cover - thin httpx passthrough
        "POST",
        url,
        json=dict(json),
        timeout=timeout,
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
        # A docker-host signal grants broad daemon control, so it must dominate
        # the reported posture even when a more isolated registry source is also
        # present; otherwise the DOCKER_HOST_CONFIGURED reason is under-reported
        # as read-only host-path access.
        effective_scope = (
            "docker_host_control"
            if docker_host_signal is not None
            else _primary_credential_scope(credential_sources)
        )
        effective_isolation = (
            "host_daemon"
            if docker_host_signal is not None
            else _primary_isolation(credential_sources)
        )
        return _provider_result(
            ok=True,
            strict=strict,
            reason=reason,
            message="Docker credential and control-plane signals were observed without reading secret values.",
            signals=[source["signal"] for source in credential_sources],
            secrets=secrets,
            credential_sources=credential_sources,
            credential_scope=effective_scope,
            isolation=effective_isolation,
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


def _check_opencode(
    *,
    environ: Mapping[str, str],
    host_home: Path,
    strict: bool,
    http_get: HttpGet,
    secrets: frozenset[str],
    model: str | None = None,
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
        # Local-Ollama authless carve-out (symmetric to
        # OPENCODE_NON_OLLAMA_PROVIDER_SELECTED): a local ``ollama/``-prefixed
        # model (NOT a ``:cloud`` model) is served by the host Ollama daemon,
        # whose ``/api/tags`` and ``/api/pull`` endpoints need no OpenCode/Ollama
        # Cloud credential. When that daemon is reachable, waive the credential
        # requirement so ``_selected_launch_probe`` can report a pull-pending
        # disposition and the executor pre-agent step can auto-pull. The waiver
        # is conditional on daemon reachability: a ``:cloud`` model still needs
        # the cloud credential, and an unreachable daemon with no credential
        # still blocks with OPENCODE_OLLAMA_AUTH_MISSING below.
        if _opencode_model_is_local_ollama(model):
            version_urls = _ollama_version_urls(environ)
            local_probe = _probe_ollama(version_urls, http_get=http_get, secrets=secrets)
            if local_probe["ok"]:
                return _provider_result(
                    ok=True,
                    strict=strict,
                    reason="OPENCODE_OLLAMA_LOCAL_AUTHLESS",
                    message=(
                        "No OpenCode/Ollama Cloud credential is required: the selected "
                        "local Ollama model is served by the reachable host daemon."
                    ),
                    signals=["OLLAMA_HOST_REACHABLE"],
                    secrets=secrets,
                    credential_scope="not_observed",
                    isolation="none",
                    warnings=[],
                )
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


from awf.service.provider_readiness import (  # noqa: E402
    _CODEX_AUTH_FILES,
    _GITHUB_TOKEN_ENV_KEYS,
    _OLLAMA_AUTH_FILES,
    _OPENCODE_ENV_KEYS,
    _PROVIDER_PROBE_TIMEOUT_SECONDS,
    _XAI_ENV_KEYS,
    KNOWN_SECRET_ENV_KEYS,
    PROVIDER_NAMES,
    CompletedProcessLike,
    HttpGet,
    HttpResponseLike,
    HttpStreamResponseLike,
    ProviderName,
    SubprocessRun,
    _opencode_model_targets_non_ollama_provider,
)

# Re-exported so existing call sites (``_check_opencode`` below) and tests keep
# reaching the Ollama probe/pull helpers via the ``provider_readiness_helpers``
# namespace after they were extracted into ``provider_readiness_ollama`` to satisfy
# the maintainability line limit.
from awf.service.provider_readiness_ollama import (  # noqa: E402, F401
    _consume_pull_stream,
    _log_ollama_model_probe_exceptions,
    _ollama_model_candidates,
    _ollama_model_names,
    _ollama_probe_failure_debug,
    _probe_ollama,
    _probe_ollama_model,
    _pull_ollama_model,
    ensure_ollama_model_available,
)

# Re-exported so existing call sites (and tests) keep reaching the redaction
# helpers via the ``provider_readiness_helpers`` namespace after they were
# extracted into ``provider_readiness_redaction`` to satisfy the maintainability
# line limit.
from awf.service.provider_readiness_redaction import (  # noqa: E402, F401
    _log_redacted_exception,
    _log_redacted_terminal_failure,
    _merge_literal_redaction_segments,
    _redact,
    _redact_with_redaction_parts,
    _redaction_parts,
    _render_redaction_segments,
    _replace_literal_redaction_spans,
    _replace_rendered_redaction_spans,
    _replace_token_redaction_spans,
    _replace_url_credential_redaction_spans,
    _slice_redaction_segments,
    _truncate,
)
