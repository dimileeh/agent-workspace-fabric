"""Credential readiness checks for local service agent providers."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from awf.adapters.opencode import DEFAULT_OLLAMA_OPENAI_BASE_URL
from awf.service.config import ServiceSettings

ProviderName = Literal["github", "claude_code", "gemini", "opencode"]

PROVIDER_NAMES: tuple[ProviderName, ...] = (
    "github",
    "claude_code",
    "gemini",
    "opencode",
)

_GITHUB_TIMEOUT_SECONDS = 5.0
_HTTP_TIMEOUT_SECONDS = 2.0
_REDACTION = "<redacted>"
_OLLAMA_AUTH_FILES = ("config.json", "id_ed25519", "id_ed25519.pub")

_GITHUB_TOKEN_ENV_KEYS = ("AWF_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
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
_KNOWN_SECRET_ENV_KEYS = frozenset(
    (
        *_GITHUB_TOKEN_ENV_KEYS,
        *_CLAUDE_ENV_KEYS,
        *_GEMINI_ENV_KEYS,
        *_OPENCODE_ENV_KEYS,
        "GOOGLE_APPLICATION_CREDENTIALS_JSON",
    )
)

_URL_CREDENTIAL_RE = re.compile(r"(https?://)([^/\s:@]+(?::[^/\s@]+)?@)")
_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])("
    r"gh[pousr]_[A-Za-z0-9_]{8,}|"
    r"github_pat_[A-Za-z0-9_]{8,}|"
    r"sk-ant-[A-Za-z0-9_-]{8,}|"
    r"AIza[A-Za-z0-9_-]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{8,}|"
    r"[A-Za-z0-9_-]{40,}"
    r")(?![A-Za-z0-9])"
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
    run_subprocess: SubprocessRun | None = None,
    http_get: HttpGet | None = None,
) -> dict[str, Any]:
    """Return redacted local-service readiness for agent provider credentials."""

    env = os.environ if environ is None else environ
    strict = validate_provider_names(strict_providers or ())
    host_home = Path(settings.host_home or "~").expanduser()
    secrets = _secret_values(settings, env)
    resolved_run = run_subprocess or _run_subprocess
    resolved_http_get = http_get or _http_get

    providers: dict[str, dict[str, Any]] = {
        "github": _check_github(
            settings,
            environ=env,
            host_home=host_home,
            strict="github" in strict,
            run_subprocess=resolved_run,
            secrets=secrets,
        ),
        "claude_code": _check_claude(
            environ=env,
            host_home=host_home,
            strict="claude_code" in strict,
            secrets=secrets,
        ),
        "gemini": _check_gemini(
            environ=env,
            host_home=host_home,
            strict="gemini" in strict,
            secrets=secrets,
        ),
        "opencode": _check_opencode(
            environ=env,
            host_home=host_home,
            strict="opencode" in strict,
            http_get=resolved_http_get,
            secrets=secrets,
        ),
    }
    return {
        "status": "fail"
        if any(provider["status"] == "fail" for provider in providers.values())
        else "ok",
        "strict_providers": _ordered_names(strict),
        "providers": providers,
    }


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
                    "Run `export AWF_GITHUB_TOKEN=\"$(gh auth token)\"` before "
                    "starting Compose, or put AWF_GITHUB_TOKEN/GH_TOKEN in "
                    "docker/compose/.env."
                ),
                signals=["~/.config/gh"],
                secrets=secrets,
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
        )

    gh_env = {**dict(environ), "AWF_GITHUB_TOKEN": token, "GH_TOKEN": token, "GITHUB_TOKEN": token}
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
        )
    except Exception as exc:
        return _provider_result(
            ok=False,
            strict=strict,
            reason="GITHUB_AUTH_UNUSABLE",
            message="GitHub CLI auth check failed before it could complete.",
            detail=f"{type(exc).__name__}: {exc}",
            signals=[token_signal, "gh auth status"],
            capabilities=["pr_create", "comment", "merge"],
            secrets=secrets,
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
        )

    return _provider_result(
        ok=True,
        strict=strict,
        reason="GITHUB_AUTH_OK",
        message="GitHub CLI auth is usable for PR creation, comments, and merges.",
        signals=[token_signal, "gh auth status"],
        capabilities=["pr_create", "comment", "merge"],
        secrets=secrets,
    )


def _check_claude(
    *,
    environ: Mapping[str, str],
    host_home: Path,
    strict: bool,
    secrets: frozenset[str],
) -> dict[str, Any]:
    signal = _first_present_env(environ, _CLAUDE_ENV_KEYS)
    if signal is not None:
        return _provider_result(
            ok=True,
            strict=strict,
            reason="CLAUDE_ENV_AUTH_PRESENT",
            message="Claude Code auth is visible through service environment variables.",
            signals=[signal],
            secrets=secrets,
        )

    signals = _existing_path_signals(
        (
            (host_home / ".claude", "~/.claude"),
            (host_home / ".claude.json", "~/.claude.json"),
        )
    )
    if signals:
        return _provider_result(
            ok=True,
            strict=strict,
            reason="CLAUDE_FILE_AUTH_PRESENT",
            message="Claude Code auth files are visible to the local service.",
            signals=signals,
            secrets=secrets,
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
    )


def _check_gemini(
    *,
    environ: Mapping[str, str],
    host_home: Path,
    strict: bool,
    secrets: frozenset[str],
) -> dict[str, Any]:
    signal = _first_present_env(environ, _GEMINI_ENV_KEYS)
    if signal is not None:
        return _provider_result(
            ok=True,
            strict=strict,
            reason="GEMINI_ENV_AUTH_PRESENT",
            message="Gemini auth is visible through service environment variables.",
            signals=[signal],
            secrets=secrets,
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
        )

    if (host_home / ".gemini").is_dir():
        return _provider_result(
            ok=True,
            strict=strict,
            reason="GEMINI_FILE_AUTH_PRESENT",
            message="Gemini auth files are visible to the local service.",
            signals=["~/.gemini"],
            secrets=secrets,
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
        filename
        for filename in _OLLAMA_AUTH_FILES
        if (host_home / ".ollama" / filename).is_file()
    ]
    env_signal = _first_present_env(environ, _OPENCODE_ENV_KEYS)
    signals: list[str] = []
    if opencode_config:
        signals.append("~/.config/opencode")
    if ollama_files:
        signals.append("~/.ollama auth files")
    if env_signal is not None:
        signals.append(env_signal)

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
        )

    version_url = _ollama_version_url(environ)
    probe = _probe_ollama(version_url, http_get=http_get, secrets=secrets)
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
        )

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
) -> dict[str, Any]:
    status_value = "ok" if ok else "fail" if strict else "warn"
    payload: dict[str, Any] = {
        "ok": ok,
        "status": status_value,
        "severity": "ok" if ok else "error" if strict else "warning",
        "reason": reason,
        "message": _redact(message, secrets),
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


def _existing_path_signals(candidates: Iterable[tuple[Path, str]]) -> list[str]:
    return [signal for path, signal in candidates if path.exists()]


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


def _truncate(value: str, *, limit: int = 240) -> str:
    stripped = value.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 1] + "…"


def _ollama_version_url(environ: Mapping[str, str]) -> str:
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
    path = f"{path}/api/version" if path else "/api/version"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _probe_ollama(
    url: str,
    *,
    http_get: HttpGet,
    secrets: frozenset[str],
) -> dict[str, Any]:
    try:
        response = http_get(url, timeout=_HTTP_TIMEOUT_SECONDS)
    except Exception as exc:
        return {
            "ok": False,
            "detail": _redact(f"{type(exc).__name__}: {exc}", secrets),
        }
    if 200 <= response.status_code < 400:
        return {"ok": True}
    detail = response.text or f"HTTP {response.status_code}"
    return {
        "ok": False,
        "detail": _redact(f"HTTP {response.status_code}: {detail}", secrets),
    }


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
