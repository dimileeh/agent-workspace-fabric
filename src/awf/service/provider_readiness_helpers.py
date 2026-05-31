"""Provider readiness credential, redaction, and local probe helpers."""

from __future__ import annotations

import json
import subprocess
import traceback
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx

from awf.adapters.opencode import DEFAULT_OLLAMA_OPENAI_BASE_URL
from awf.service.config import ServiceSettings


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


def _redact(value: str, secrets: frozenset[str]) -> str:
    redacted = value
    for secret in sorted(secrets, key=len, reverse=True):
        redacted = redacted.replace(secret, _REDACTION)
    redacted = URL_CREDENTIAL_RE.sub(r"\1<redacted>@", redacted)
    return TOKEN_RE.sub(_REDACTION, redacted)


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
    for match in URL_CREDENTIAL_RE.finditer(rendered):
        replacements.append((match.start(2), match.end(2), [("redaction", ""), ("literal", "@")]))
    return _replace_rendered_redaction_spans(segments, replacements)


def _replace_token_redaction_spans(
    segments: list[_RedactionSegment],
) -> list[_RedactionSegment]:
    rendered = _render_redaction_segments(segments)
    replacements: list[tuple[int, int, list[_RedactionSegment]]] = []
    for match in TOKEN_RE.finditer(rendered):
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


from awf.service.provider_readiness import (  # noqa: E402
    _CODEX_AUTH_FILES,
    _GITHUB_TOKEN_ENV_KEYS,
    _HTTP_TIMEOUT_SECONDS,
    _REDACTION,
    _TRACEBACK_LOG_LIMIT,
    KNOWN_SECRET_ENV_KEYS,
    PROVIDER_NAMES,
    TOKEN_RE,
    URL_CREDENTIAL_RE,
    CompletedProcessLike,
    HttpGet,
    HttpResponseLike,
    ProviderName,
    _credential_source,
    _log,
    _RedactionSegment,
)
