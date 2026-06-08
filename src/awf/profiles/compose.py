"""Convert workspace profiles into compose-manager inputs."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlunsplit

from awf.node.compose_manager import ComposeService
from awf.profiles.lint import profile_service_volume_lint_errors
from awf.profiles.models import (
    EndpointVisibility,
    ProfileAppEndpoint,
    ProfileLintFinding,
    WorkspaceProfile,
    _normalized_endpoint_env_name,
)

AGENT_AUTH_ENV_VARS = (
    # Codex/OpenAI static-token fallback auth. Prefer isolated ~/.codex copies
    # when present; these keep local shells compatible without writing values.
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "CODEX_API_KEY",
    "CODEX_AUTH_TOKEN",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_ORGANIZATION",
    "OPENAI_PROJECT",
    "OPENAI_PROJECT_ID",
    # Claude Code portable/API-key auth. Host claude.ai OAuth can live in
    # macOS Keychain, which is not available inside a Linux container.
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    # Cursor CLI headless auth.
    "CURSOR_API_KEY",
    # Gemini CLI headless auth.
    "GEMINI_API_KEY",
    "GEMINI_API_KEY_AUTH_MECHANISM",
    "GOOGLE_API_KEY",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_GENAI_USE_GCA",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_ACCESS_TOKEN",
    # OpenCode via Ollama/Ollama Cloud.
    "AWF_OPENCODE_OLLAMA_BASE_URL",
    "OLLAMA_HOST",
    "OLLAMA_API_KEY",
    # xAI Grok Build headless auth.
    "XAI_API_KEY",
    # OpenCode shell-tool runtime tuning. This is not auth, but it must follow
    # the same service -> workspace placeholder path to affect agent containers.
    "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS",
)


class ProfileServiceValidationError(ValueError):
    """Raised when profile-declared services fail security validation."""

    def __init__(self, findings: tuple[ProfileLintFinding, ...]) -> None:
        self.findings = findings
        self.reason_code = findings[0].reason_code if findings else "PROFILE_SERVICE_INVALID"
        message = findings[0].message if findings else "profile service validation failed"
        super().__init__(f"{self.reason_code}: {message}")


def profile_services(
    profile: WorkspaceProfile,
    *,
    base_path: Path | None = None,
) -> tuple[ComposeService, ...]:
    lint_errors = profile_service_volume_lint_errors(profile)
    if lint_errors:
        raise ProfileServiceValidationError(lint_errors)

    return tuple(
        ComposeService(
            name=s.name,
            image=s.image,
            build_context=_resolve_repo_path(s.build_context, base_path=base_path),
            dockerfile=s.dockerfile,
            env_file=_resolve_repo_path(s.env_file, base_path=base_path),
            environment=tuple(s.environment.items()),
            depends_on=tuple(s.depends_on),
            healthcheck_cmd=s.healthcheck_cmd,
            ports=tuple(s.ports),
            command=s.command,
            volumes=tuple(
                (_resolve_volume_source(source, base_path=base_path), target)
                for source, target in s.volumes
            ),
            privileged=s.privileged,
            required=s.required,
        )
        for s in profile.services
    )


def _resolve_repo_path(value: str | None, *, base_path: Path | None) -> str | None:
    if value is None:
        return value
    return _resolve_workspace_path(value, base_path=base_path)


def _resolve_volume_source(source: str, *, base_path: Path | None) -> str:
    path = Path(source)
    if path.is_absolute():
        raise ValueError(f"profile service path must be workspace-relative: {source!r}")
    if source.startswith(".") or "/" in source:
        return _resolve_workspace_path(source, base_path=base_path)
    return source


def _resolve_workspace_path(value: str, *, base_path: Path | None) -> str:
    path = Path(value)
    if path.is_absolute():
        raise ValueError(f"profile service path must be workspace-relative: {value!r}")
    if base_path is None:
        return value

    root = base_path.resolve()
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"profile service path escapes workspace root: {value!r}")
    return str(resolved)


def profile_agent_environment(profile: WorkspaceProfile) -> tuple[tuple[str, str], ...]:
    return (
        *profile.runtime.environment.items(),
        *profile_app_endpoint_environment(profile),
    )


def resolve_app_endpoints(
    profile: WorkspaceProfile,
    *,
    include_internal: bool = True,
) -> tuple[dict[str, Any], ...]:
    """Resolve profile endpoints into deterministic internal service URLs."""

    return resolve_profile_app_endpoints(
        profile.app_endpoints,
        include_internal=include_internal,
    )


def resolve_profile_app_endpoints(
    app_endpoints: Iterable[ProfileAppEndpoint],
    *,
    include_internal: bool = True,
) -> tuple[dict[str, Any], ...]:
    """Resolve profile endpoint definitions into deterministic internal service URLs."""

    endpoints: list[dict[str, Any]] = []
    for endpoint in app_endpoints:
        if not include_internal and endpoint.visibility is EndpointVisibility.internal:
            continue
        endpoints.append(_resolved_app_endpoint(endpoint))
    return tuple(endpoints)


def profile_app_endpoint_environment(
    profile: WorkspaceProfile,
    *,
    resolved_endpoints: Iterable[dict[str, Any]] | None = None,
) -> tuple[tuple[str, str], ...]:
    if resolved_endpoints is None:
        resolved_endpoints = resolve_app_endpoints(profile)

    endpoints = [
        endpoint
        for endpoint in resolved_endpoints
        if endpoint["visibility"] in {"agent", "validation"}
    ]
    if not endpoints:
        return ()

    env: list[tuple[str, str]] = [
        (
            "AWF_APP_ENDPOINTS_JSON",
            json.dumps(
                endpoints,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ),
        )
    ]
    for endpoint in endpoints:
        env.append(
            (
                f"AWF_APP_ENDPOINT_{_normalized_endpoint_env_name(str(endpoint['name']))}_URL",
                str(endpoint["internal_url"]),
            )
        )
    return tuple(env)


def _resolved_app_endpoint(endpoint: ProfileAppEndpoint) -> dict[str, Any]:
    internal_url = _endpoint_url(endpoint, endpoint.path)
    return {
        "name": endpoint.name,
        "service": endpoint.service,
        "scheme": endpoint.scheme,
        "port": endpoint.port,
        "path": endpoint.path,
        "internal_url": internal_url,
        "visibility": endpoint.visibility.value,
        "health": (
            {
                "path": endpoint.health.path,
                "method": endpoint.health.method,
                "expected_status": endpoint.health.expected_status,
                "internal_url": _endpoint_url(endpoint, endpoint.health.path),
            }
            if endpoint.health is not None
            else None
        ),
    }


def _endpoint_url(endpoint: ProfileAppEndpoint, path: str) -> str:
    return urlunsplit((endpoint.scheme, f"{endpoint.service}:{endpoint.port}", path, "", ""))


_GIT_CONFIG_COUNT_KEY = "GIT_CONFIG_COUNT"
_GIT_CONFIG_KEY_PREFIX = "GIT_CONFIG_KEY_"
_GIT_CONFIG_VALUE_PREFIX = "GIT_CONFIG_VALUE_"


def _is_git_config_protocol_key(key: str) -> bool:
    return (
        key == _GIT_CONFIG_COUNT_KEY
        or key.startswith(_GIT_CONFIG_KEY_PREFIX)
        or key.startswith(_GIT_CONFIG_VALUE_PREFIX)
    )


def _git_config_count(pairs: tuple[tuple[str, str], ...]) -> int:
    for key, value in pairs:
        if key == _GIT_CONFIG_COUNT_KEY:
            try:
                return int(value)
            except ValueError:
                return 0
    return 0


def _split_git_config_entries(
    pairs: tuple[tuple[str, str], ...],
) -> tuple[list[tuple[str, str]], tuple[tuple[str, str], ...]]:
    """Split env pairs into ordered git-config (key, value) entries and the rest.

    The git-config entries are returned in index order (``0..GIT_CONFIG_COUNT-1``);
    every ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_n``/``GIT_CONFIG_VALUE_n`` var is
    stripped from the second tuple so the protocol can be re-emitted contiguously
    by the caller without leaking stray indices.
    """
    mapping = dict(pairs)
    entries: list[tuple[str, str]] = []
    for index in range(_git_config_count(pairs)):
        config_key = mapping.get(f"{_GIT_CONFIG_KEY_PREFIX}{index}")
        config_value = mapping.get(f"{_GIT_CONFIG_VALUE_PREFIX}{index}")
        if config_key is None or config_value is None:
            continue
        entries.append((config_key, config_value))
    others = tuple((k, v) for k, v in pairs if not _is_git_config_protocol_key(k))
    return entries, others


def merge_agent_environment(
    base_environment: tuple[tuple[str, str], ...],
    additions: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """Merge agent environment pairs without overwriting existing keys.

    Non-git-config keys keep "first writer wins" semantics (the base value is
    preserved). The ``GIT_CONFIG_KEY_n/VALUE_n/COUNT`` protocol is merged
    specially: the additions' git-config entries are re-indexed to append onto
    the base's ``GIT_CONFIG_COUNT`` and the counts are summed. The lease resolver
    always emits its bitbucket ``insteadOf`` entries starting at index 0, so a
    profile that already declares indexed ``GIT_CONFIG_*`` in
    ``runtime.environment`` would otherwise either collide on index 0 (dropped by
    the skip-existing rule) or sit above the effective count and never reach git,
    breaking private Bitbucket HTTPS in the agent. Re-indexing keeps both blocks
    reachable.
    """

    addition_entries, addition_others = _split_git_config_entries(additions)
    merged: list[tuple[str, str]] = list(base_environment)
    existing = {key for key, _ in merged}
    for key, value in addition_others:
        if key not in existing:
            merged.append((key, value))
            existing.add(key)
    if not addition_entries:
        return tuple(merged)

    base_count = _git_config_count(base_environment)
    for offset, (config_key, config_value) in enumerate(addition_entries):
        index = base_count + offset
        merged.append((f"{_GIT_CONFIG_KEY_PREFIX}{index}", config_key))
        merged.append((f"{_GIT_CONFIG_VALUE_PREFIX}{index}", config_value))
    total = base_count + len(addition_entries)
    counted: list[tuple[str, str]] = []
    count_emitted = False
    for key, value in merged:
        if key == _GIT_CONFIG_COUNT_KEY:
            counted.append((key, str(total)))
            count_emitted = True
        else:
            counted.append((key, value))
    if not count_emitted:
        counted.append((_GIT_CONFIG_COUNT_KEY, str(total)))
    return tuple(counted)


def agent_environment_with_declared_secret_leases(
    base_environment: tuple[tuple[str, str], ...],
    lease_environment: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """Add profile-declared secret lease placeholders before legacy fallbacks."""

    return merge_agent_environment(base_environment, lease_environment)


def agent_environment_with_github_token(
    base_environment: tuple[tuple[str, str], ...],
    *,
    host_env: Mapping[str, str] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Expose AWF's GitHub token to agent containers via Compose placeholders."""
    source_env = os.environ if host_env is None else host_env
    token_placeholder = _github_token_placeholder(source_env)
    if token_placeholder is None:
        return base_environment

    merged: list[tuple[str, str]] = list(base_environment)
    existing = {key for key, _ in merged}
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if name not in existing:
            merged.append((name, token_placeholder))
            existing.add(name)
    return tuple(merged)


def _github_token_placeholder(source_env: Mapping[str, str]) -> str | None:
    for name in ("AWF_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        if source_env.get(name):
            return "${" + name + "}"
    return None


def agent_environment_with_host_auth(
    base_environment: tuple[tuple[str, str], ...],
    *,
    host_env: Mapping[str, str] | None = None,
) -> tuple[tuple[str, str], ...]:
    return agent_environment_with_legacy_host_auth(base_environment, host_env=host_env)


def agent_environment_with_legacy_host_auth(
    base_environment: tuple[tuple[str, str], ...],
    *,
    host_env: Mapping[str, str] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Expose selected host auth env vars to the agent via Compose placeholders.

    Values are intentionally rendered as ``${NAME}`` placeholders. The generated
    per-workspace compose.yml records variable names only; Docker Compose
    substitutes the actual secret values from the worker environment at launch.
    """
    source_env = os.environ if host_env is None else host_env
    merged: list[tuple[str, str]] = list(base_environment)
    existing = {key for key, _ in merged}
    for name in AGENT_AUTH_ENV_VARS:
        if name not in existing and source_env.get(name):
            merged.append((name, f"${{{name}}}"))
            existing.add(name)
    return agent_environment_with_github_token(tuple(merged), host_env=source_env)
