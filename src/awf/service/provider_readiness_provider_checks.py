"""Per-provider credential check helpers for provider readiness.

Extracted from ``provider_readiness`` to keep each first-party module under the
maintainability line limit. Each ``_check_*`` helper inspects the credential and
runtime-CLI signals for a single non-OpenCode provider (GitHub, Codex, Claude,
Cursor, Gemini) and returns a ``_provider_result`` payload. They sit alongside the
``_check_docker_provider``/``_check_grok``/``_check_opencode`` helpers in
``provider_readiness_helpers`` conceptually, but depend on credential helpers,
constants, and redaction helpers reachable through ``provider_readiness`` — all
imported at module end to mirror the established late-binding import ordering.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from awf.node.auth_mounts import (
    claude_auth_isolation_label,
    force_copy_isolation_requested,
    overlay_path_has_reserved_chars,
)
from awf.service.config import ServiceSettings


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
                    "root .env."
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
    work_dir: Path,
    strict: bool,
    secrets: frozenset[str],
) -> dict[str, Any]:
    """Check whether Claude Code authentication signals are present.

    Probes file-based (``~/.claude`` directory + ``~/.claude.json``) and
    environment-based (``ANTHROPIC_API_KEY`` etc.) auth sources.  Reports
    the isolation posture (overlay vs per-workspace copy) determined by
    ``force_copy_isolation_requested`` and overlay path constraints.

    When the effective ``environ`` carries ``AWF_WORK_DIR_BIND_PROPAGATION``
    (set by bootstrap on non-propagating hosts or read from the compose
    env-file by status), the value is attached as ``mount_propagation`` so
    callers can correlate the readiness check with the bind-propagation
    posture.

    The force-copy and overlay-path-reserved-chars probes read the passed
    ``environ`` (not ``os.environ``) because bootstrap folds the operator
    override into the readiness environ dict; a default ``os.environ``
    probe would miss it and overstate overlay isolation.
    """
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
    #
    # The reserved-chars probe folds in the same deterministic, host-level copy
    # fallback the worker takes when ``work_dir`` (the overlay auth root, inherited
    # from ``AWF_WORK_DIR`` / ``AWF_HOST_WORK_DIR``) carries a ``,`` or ``:`` that
    # overlayfs's unescapable ``-o`` payload cannot encode. Every overlay mount
    # degrades to per-workspace copy there, so the label must report copy rather
    # than overstate overlay isolation.
    directory_isolation = claude_auth_isolation_label(
        force_copy_requested=lambda: force_copy_isolation_requested(environ),
        overlay_path_unsupported=lambda: overlay_path_has_reserved_chars(work_dir),
    )
    propagation_posture = environ.get("AWF_WORK_DIR_BIND_PROPAGATION")
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
        result = _provider_result(
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
    elif (signal := _first_present_env(environ, _CLAUDE_ENV_KEYS)) is not None:
        result = _provider_result(
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
    else:
        result = _provider_result(
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
    if propagation_posture is not None:
        result["mount_propagation"] = propagation_posture
    return result


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


# Imported at module end to mirror the established mutual-import ordering: the
# referenced constants, credential helpers, and redaction helpers are all bound on
# the ``provider_readiness`` module (its own top-level constants plus the helper /
# redaction names it re-exports at its own module end) before this leaf is pulled
# in, and these helpers reference them only at call time, so the late binding is
# safe.
from awf.service.provider_readiness import (  # noqa: E402
    _CLAUDE_ENV_KEYS,
    _CODEX_ENV_KEYS,
    _CURSOR_ENV_KEYS,
    _GEMINI_ENV_KEYS,
    _GITHUB_TIMEOUT_SECONDS,
    SubprocessRun,
    _credential_sources,
)
from awf.service.provider_readiness_helpers import (  # noqa: E402
    _codex_file_sources,
    _credential_source,
    _existing_credential_sources,
    _first_present_env,
    _github_token,
    _probe_agent_runtime_cli,
    _provider_result,
    _runtime_cli_probe_payload,
    _security_warning,
)
from awf.service.provider_readiness_redaction import (  # noqa: E402
    _log_redacted_exception,
    _redact,
)
