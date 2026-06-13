"""Provider readiness credential, redaction, and local probe helpers."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import ValidationError

from awf.adapters.opencode import DEFAULT_OLLAMA_OPENAI_BASE_URL
from awf.profiles.models import WorkspaceProfile
from awf.service.config import ServiceSettings
from awf.service.environment import compose_expand_value

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
    """
    pull_name = _ollama_pull_name(model)
    tag = pull_name.rpartition(":")[2] if ":" in pull_name else ""
    return tag == "cloud" or tag.endswith("-cloud")


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
    declared: dict[str, str] = {}
    for key in _OLLAMA_BASE_URL_ENV_KEYS:
        raw = profile_env.get(key)
        if not raw:
            continue
        expanded = compose_expand_value(raw, environ=environ).strip()
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
    allow_cloud: bool = False,
    pull_pending_ok: bool = False,
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
        redacted_detail = _truncate(_redact(detail, secrets))
        # The daemon answered but does not (yet) serve the model. A ``:cloud``
        # model is served remotely (never pulled), and an absent non-cloud model
        # is pullable — both are non-blocking launch dispositions when the caller
        # opts in. Otherwise this stays the historical hard "not available" fail.
        if allow_cloud and _is_cloud_model(model):
            return {
                "status": "ok",
                "reason_code": "OLLAMA_MODEL_CLOUD",
                "message": "Selected Ollama Cloud model is served remotely; no local pull required.",
                "detail": redacted_detail,
            }
        if pull_pending_ok:
            return {
                "status": "pending",
                "reason_code": "OLLAMA_MODEL_PULL_PENDING",
                "message": (
                    "Selected Ollama model is not present locally yet; "
                    "AWF will pull it before the agent runs."
                ),
                "detail": redacted_detail,
            }
        return {
            "status": "fail",
            "reason_code": "OLLAMA_MODEL_NOT_AVAILABLE",
            "message": "Selected OpenCode/Ollama model is not available from Ollama /api/tags.",
            "detail": redacted_detail,
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


def ensure_ollama_model_available(
    *,
    model: str | None,
    tags_urls: tuple[str, ...],
    pull_urls: tuple[str, ...],
    http_get: HttpGet,
    http_post_stream: HttpPostStream,
    secrets: frozenset[str],
    timeout: float | None = None,
    on_progress: Callable[[str], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Discover, classify, and (if needed) auto-pull the requested Ollama model.

    The host Ollama daemon is the source of truth. Dispositions:

    - already in ``/api/tags`` → ``OLLAMA_MODEL_AVAILABLE`` (no pull);
    - daemon reachable + ``:cloud`` model → ``OLLAMA_MODEL_CLOUD`` (served
      remotely, no pull; the daemon still proxies cloud requests, so it must be
      up);
    - daemon unreachable → ``OLLAMA_MODEL_PROBE_FAILED`` (no pull, no hang;
      applies to cloud models too);
    - absent non-cloud → ``POST /api/pull`` with bounded ``timeout`` and streamed
      (redacted) progress, then re-check ``/api/tags``: success →
      ``OLLAMA_MODEL_PULLED``; daemon error / timeout / still-missing →
      ``OLLAMA_MODEL_PULL_FAILED`` carrying the redacted daemon message.

    Returns a structured ``{"status", "reason_code", "message"[, "detail"]}``.
    """

    pull_timeout = _OLLAMA_PULL_TIMEOUT_SECONDS if timeout is None else timeout

    # An Ollama Cloud model is served remotely and is never pulled, but OpenCode
    # still reaches it *through the local host Ollama daemon* (the adapter points
    # ``provider.ollama`` at ``host.docker.internal:11434``). So the daemon must
    # still be reachable at agent-launch time even for a cloud model. Probe
    # ``/api/tags`` with ``allow_cloud`` — a daemon that answers resolves a cloud
    # tag (absent from the local catalog) to ``OLLAMA_MODEL_CLOUD``, while a
    # daemon that has gone down between create-time readiness and execution
    # surfaces the clear ``OLLAMA_MODEL_PROBE_FAILED`` reason this pre-agent step
    # exists to provide, instead of a confusing downstream ``AGENT_CLI_FAILED``.
    probe = _probe_ollama_model(
        tags_urls, model=model, http_get=http_get, secrets=secrets, allow_cloud=True
    )
    reason = probe.get("reason_code")
    if reason == "OLLAMA_MODEL_AVAILABLE":
        return {
            "status": "ok",
            "reason_code": "OLLAMA_MODEL_AVAILABLE",
            "message": "Selected Ollama model is already available from /api/tags.",
        }
    if reason == "OLLAMA_MODEL_CLOUD":
        # Daemon reachable; the selected cloud model is served remotely (no pull).
        return dict(probe)
    if reason in {"MODEL_NOT_SELECTED", "OLLAMA_MODEL_PROBE_FAILED"}:
        # No selectable model, or the daemon never answered: do not pull.
        return dict(probe)
    if reason != "OLLAMA_MODEL_NOT_AVAILABLE":
        # Unexpected probe disposition (e.g. a future non-pull reason code): do
        # not pull; surface the raw probe result rather than fall through.
        return dict(probe)

    # reason == OLLAMA_MODEL_NOT_AVAILABLE: the daemon answered but lacks it.
    pull_name = _ollama_pull_name(model)
    pull_result = _pull_ollama_model(
        pull_urls,
        name=pull_name,
        http_post_stream=http_post_stream,
        secrets=secrets,
        timeout=pull_timeout,
        on_progress=on_progress,
        monotonic=monotonic,
    )
    if not pull_result["ok"]:
        return {
            "status": "fail",
            "reason_code": "OLLAMA_MODEL_PULL_FAILED",
            "message": f"Ollama pull of {pull_name!r} did not complete successfully.",
            "detail": pull_result["detail"],
        }

    recheck = _probe_ollama_model(tags_urls, model=model, http_get=http_get, secrets=secrets)
    if recheck.get("reason_code") == "OLLAMA_MODEL_AVAILABLE":
        return {
            "status": "ok",
            "reason_code": "OLLAMA_MODEL_PULLED",
            "message": f"Ollama model {pull_name!r} was pulled and is now available.",
        }
    return {
        "status": "fail",
        "reason_code": "OLLAMA_MODEL_PULL_FAILED",
        "message": f"Ollama model {pull_name!r} is still unavailable after the pull completed.",
        "detail": recheck.get("detail"),
    }


def _pull_ollama_model(
    urls: tuple[str, ...],
    *,
    name: str,
    http_post_stream: HttpPostStream,
    secrets: frozenset[str],
    timeout: float,
    on_progress: Callable[[str], None] | None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    # The documented /api/pull body field is ``model``; ``name`` is the
    # deprecated alias still accepted by current daemons. Send both so newer
    # daemons (which prefer ``model``) and older ones (which only know ``name``)
    # both resolve the model to pull. See https://docs.ollama.com/api/pull.
    body: dict[str, Any] = {"model": name, "name": name, "stream": True}
    failures: list[str] = []
    # ``timeout`` reaches httpx as a per-read deadline that resets on every
    # NDJSON progress line, so it is not a total bound. Hold a single wall-clock
    # deadline across all URL attempts so a daemon that streams progress forever
    # without terminating still surfaces OLLAMA_MODEL_PULL_FAILED on time.
    deadline = monotonic() + timeout
    for url in urls:
        remaining = deadline - monotonic()
        if remaining <= 0:
            # The wall-clock budget is already spent — e.g. an earlier URL
            # streamed progress until the deadline. Opening this fallback with a
            # fresh full ``timeout`` would let the intended total bound be
            # exceeded substantially, so stop here and surface the timeout
            # instead of attempting more URLs.
            failure = "pull exceeded the bounded wall-clock timeout before fallback"
            failures.append(f"{url}: {failure}" if len(urls) > 1 else failure)
            break
        # Bound this attempt by the time left in the shared deadline rather than
        # the full ``timeout``. Otherwise a first attempt that returns just
        # *before* the deadline still lets a fallback open with a fresh full
        # connect/read timeout, so the executor thread could block for roughly
        # another whole ``timeout`` past the intended total pull budget.
        attempt_timeout = min(timeout, remaining)
        try:
            with http_post_stream(url, json=body, timeout=attempt_timeout) as response:
                status_code = response.status_code
                if not 200 <= status_code < 300:
                    failure = f"HTTP {status_code}"
                    failures.append(f"{url}: {failure}" if len(urls) > 1 else failure)
                    continue
                stream_error = _consume_pull_stream(
                    response,
                    secrets=secrets,
                    on_progress=on_progress,
                    deadline=deadline,
                    monotonic=monotonic,
                )
            if stream_error is not None:
                failures.append(f"{url}: {stream_error}" if len(urls) > 1 else stream_error)
                continue
            return {"ok": True, "detail": None}
        except httpx.HTTPError as exc:
            # Only transport failures are retried across URLs; non-transport
            # bugs (e.g. a faulty on_progress callback) must surface, not be
            # masked as OLLAMA_MODEL_PULL_FAILED.
            _log_redacted_exception(
                "provider_readiness.ollama_pull_exception",
                exc,
                secrets,
            )
            detail = f"{type(exc).__name__}: {exc}"
            failures.append(f"{url}: {detail}" if len(urls) > 1 else detail)
            continue
    return {
        "ok": False,
        "detail": _truncate(_redact("; ".join(failures), secrets)) or None,
    }


def _consume_pull_stream(
    response: HttpStreamResponseLike,
    *,
    secrets: frozenset[str],
    on_progress: Callable[[str], None] | None,
    deadline: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> str | None:
    """Drain a ``/api/pull`` NDJSON stream; return a redacted error if any.

    ``deadline`` is an absolute ``monotonic`` wall-clock bound on the total time
    spent draining the stream. httpx's per-read timeout resets on every progress
    line, so without this check a daemon that keeps streaming progress but never
    terminates would keep this loop running indefinitely; the between-lines check
    surfaces a bounded timeout error instead.
    """
    error_detail: str | None = None
    for line in response.iter_lines():
        if monotonic() >= deadline:
            return _truncate(
                _redact("pull stream exceeded the bounded wall-clock timeout", secrets)
            )
        text = line.strip() if isinstance(line, str) else ""
        if not text:
            continue
        redacted = _truncate(_redact(text, secrets))
        if on_progress is not None:
            on_progress(redacted)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            err = payload.get("error")
            if isinstance(err, str) and err:
                error_detail = _truncate(_redact(err, secrets))
            elif payload.get("status") == "success":
                # A terminal success supersedes any earlier recoverable error
                # line: the daemon finished the pull, so a stale error from a
                # retried-then-recovered step must not be reported as a failure.
                error_detail = None
    return error_detail


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
    _HTTP_TIMEOUT_SECONDS,
    _OLLAMA_AUTH_FILES,
    _OLLAMA_PULL_TIMEOUT_SECONDS,
    _OPENCODE_ENV_KEYS,
    _PROVIDER_PROBE_TIMEOUT_SECONDS,
    _XAI_ENV_KEYS,
    KNOWN_SECRET_ENV_KEYS,
    PROVIDER_NAMES,
    CompletedProcessLike,
    HttpGet,
    HttpPostStream,
    HttpResponseLike,
    HttpStreamResponseLike,
    ProviderName,
    SubprocessRun,
    _opencode_model_targets_non_ollama_provider,
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
