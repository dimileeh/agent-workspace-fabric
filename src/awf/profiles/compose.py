"""Convert workspace profiles into compose-manager inputs."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

import yaml

from awf.node.compose_manager import ComposeService
from awf.profiles import compose_git_config as _compose_git_config
from awf.profiles.compose_auth_env import (
    _AGENT_AUTH_SECRET_ENV_VARS as _AGENT_AUTH_SECRET_ENV_VARS,
)
from awf.profiles.compose_auth_env import (
    _AUTH_CREDENTIAL_LIKE_VALUE_PATTERN,
    _CAMELCASE_API_KEY_CREDENTIAL_LIKE_VALUE_PATTERN,
    _CAMELCASE_COOKIE_CREDENTIAL_LIKE_VALUE_PATTERN,
    _CAMELCASE_ENCRYPTION_KEY_CREDENTIAL_LIKE_VALUE_PATTERN,
    _CAMELCASE_SECRET_CREDENTIAL_LIKE_VALUE_PATTERN,
    _CAMELCASE_SECRET_KEY_CREDENTIAL_LIKE_VALUE_PATTERN,
    _GITHUB_TOKEN_ALIAS_PRECEDENCE,
    _HOSTED_FILE_BACKED_ENV_ONLY_UNSUPPORTED_NAMES,
    _NETRC_AUTH_CREDENTIAL_LIKE_VALUE_PATTERN,
    _NON_SECRET_PROFILE_ENV_NAME_ENDPOINT_SUFFIX_TOKENS,
    _NON_SECRET_SECRET_LIKE_PROFILE_ENV_NAMES,
    _NPMRC_AUTH_CREDENTIAL_LIKE_VALUE_PATTERN,
    _OLLAMA_BASE_URL_ENV_KEYS,
    _PREFIXED_COOKIE_CREDENTIAL_LIKE_VALUE_PATTERN,
    _PREFIXED_PRIVATE_KEY_CREDENTIAL_LIKE_VALUE_PATTERN,
    _PREFIXED_SECRET_KEY_CREDENTIAL_LIKE_VALUE_PATTERN,
    _PUBLIC_PROFILE_ENV_NAME_KEY_QUALIFIERS,
    _PUBLIC_PROFILE_ENV_NAME_PREFIX_TOKEN_SEQUENCES,
    _PUBLIC_PROFILE_ENV_NAME_PREFIX_TOKENS,
    _SECRET_LIKE_PROFILE_ENV_EXACT_NAMES,
    _SECRET_LIKE_PROFILE_ENV_NAME_ABBREVIATION_TOKENS,
    _SECRET_LIKE_PROFILE_ENV_NAME_CONCATENATED_TOKENS,
    _SECRET_LIKE_PROFILE_ENV_NAME_TOKEN_PAIRS,
    _SECRET_LIKE_PROFILE_ENV_NAME_TOKENS,
    _URL_LIKE_SUBSTRING_PATTERN,
    _URL_SECRET_CREDENTIAL_FIELD_EXACT_NAMES,
    _URL_SECRET_CREDENTIAL_FIELD_NAME_TOKEN_PAIRS,
    _URL_SECRET_CREDENTIAL_FIELD_NAMES,
)
from awf.profiles.compose_auth_env import (
    _HOSTED_NAME_ONLY_CREDENTIAL_IDENTIFIER_ENV_VARS as _HOSTED_NAME_ONLY_CREDENTIAL_IDENTIFIER_ENV_VARS,
)
from awf.profiles.compose_auth_env import (
    AGENT_AUTH_ENV_VARS as AGENT_AUTH_ENV_VARS,
)
from awf.profiles.lint import profile_service_volume_lint_errors
from awf.profiles.models import (
    EndpointVisibility,
    ProfileAppEndpoint,
    ProfileLintFinding,
    WorkspaceProfile,
    _normalized_endpoint_env_name,
)

_BITBUCKET_AGENT_INSTEADOF_KEY = _compose_git_config._BITBUCKET_AGENT_INSTEADOF_KEY
_BITBUCKET_AGENT_SAFE_INSTEADOF_VALUES = _compose_git_config._BITBUCKET_AGENT_SAFE_INSTEADOF_VALUES
_BITBUCKET_ASKPASS_TARGET = _compose_git_config._BITBUCKET_ASKPASS_TARGET
_GIT_ASKPASS_KEY = _compose_git_config._GIT_ASKPASS_KEY
_GIT_CONFIG_COUNT_KEY = _compose_git_config._GIT_CONFIG_COUNT_KEY
_GIT_CONFIG_INSTEADOF_KEY_SUFFIX = _compose_git_config._GIT_CONFIG_INSTEADOF_KEY_SUFFIX
_GIT_CONFIG_KEY_PREFIX = _compose_git_config._GIT_CONFIG_KEY_PREFIX
_GIT_CONFIG_URL_KEY_PREFIX = _compose_git_config._GIT_CONFIG_URL_KEY_PREFIX
_GIT_CONFIG_VALUE_PREFIX = _compose_git_config._GIT_CONFIG_VALUE_PREFIX
_GIT_TERMINAL_PROMPT_KEY = _compose_git_config._GIT_TERMINAL_PROMPT_KEY
_git_config_count = _compose_git_config._git_config_count
_has_mount_backed_bitbucket_askpass = _compose_git_config._has_mount_backed_bitbucket_askpass
_hosted_git_config_env = _compose_git_config._hosted_git_config_env
_hosted_git_config_passthrough_aliases = _compose_git_config._hosted_git_config_passthrough_aliases
_hosted_git_config_profile_env = _compose_git_config._hosted_git_config_profile_env
_hosted_git_config_value_alias_source = _compose_git_config._hosted_git_config_value_alias_source
_is_git_config_protocol_key = _compose_git_config._is_git_config_protocol_key
_is_safe_bitbucket_agent_insteadof_value = (
    _compose_git_config._is_safe_bitbucket_agent_insteadof_value
)
_is_safe_ssh_git_config_insteadof_key = _compose_git_config._is_safe_ssh_git_config_insteadof_key
_split_git_config_entries = _compose_git_config._split_git_config_entries

_HOSTED_FILE_AUTH_MOUNT_TARGETS = frozenset(
    {
        "/home/agent/.claude",
        "/home/agent/.claude.json",
        "/home/agent/.codex",
        "/home/agent/.config/gh",
        "/home/agent/.config/gcloud",
        "/home/agent/.config/opencode",
        "/home/agent/.gemini",
        "/home/agent/.gitconfig",
        "/home/agent/.grok",
        "/home/agent/.ollama",
        "/home/agent/.ssh",
    }
)


def _is_secret_like_profile_env_name(name: str) -> bool:
    normalized = name.upper().replace("-", "_")
    if normalized in _NON_SECRET_SECRET_LIKE_PROFILE_ENV_NAMES:
        return False
    if normalized in _SECRET_LIKE_PROFILE_ENV_EXACT_NAMES:
        return True
    tokens = tuple(token for token in normalized.split("_") if token)
    if tokens and tokens[-1] in {"KEY", "APIKEY"}:
        if len(tokens) >= 2 and tokens[-2] in _PUBLIC_PROFILE_ENV_NAME_KEY_QUALIFIERS:
            return False
        if (tokens[-1] == "APIKEY" or (len(tokens) >= 3 and tokens[-2] == "API")) and (
            any(token in _PUBLIC_PROFILE_ENV_NAME_KEY_QUALIFIERS for token in tokens[:-1])
            or tokens[0] in _PUBLIC_PROFILE_ENV_NAME_PREFIX_TOKENS
            or any(
                tokens[: len(prefix)] == prefix
                for prefix in _PUBLIC_PROFILE_ENV_NAME_PREFIX_TOKEN_SEQUENCES
            )
        ):
            return False
    if tokens and tokens[-1] in _NON_SECRET_PROFILE_ENV_NAME_ENDPOINT_SUFFIX_TOKENS:
        endpoint_name_tokens = tokens[:-1]
        endpoint_secret_tokens = _SECRET_LIKE_PROFILE_ENV_NAME_TOKENS - frozenset({"TOKEN"})
        if "WEBHOOK" in endpoint_name_tokens:
            return True
        if any(token in endpoint_secret_tokens for token in endpoint_name_tokens):
            return True
        if any(token == "TOKEN" for token in endpoint_name_tokens) and not {"OAUTH", "OIDC"} & set(
            endpoint_name_tokens
        ):
            return True
        if any(
            token in _SECRET_LIKE_PROFILE_ENV_NAME_CONCATENATED_TOKENS
            for token in endpoint_name_tokens
        ):
            return True
        if (
            len(endpoint_name_tokens) >= 2
            and endpoint_name_tokens[-1] in _SECRET_LIKE_PROFILE_ENV_NAME_ABBREVIATION_TOKENS
        ):
            return True
        return any(
            (left, right) in _SECRET_LIKE_PROFILE_ENV_NAME_TOKEN_PAIRS
            for left, right in zip(endpoint_name_tokens, endpoint_name_tokens[1:], strict=False)
        )
    if any(token in _SECRET_LIKE_PROFILE_ENV_NAME_TOKENS for token in tokens):
        return True
    if any(token in _SECRET_LIKE_PROFILE_ENV_NAME_CONCATENATED_TOKENS for token in tokens):
        return True
    if len(tokens) >= 2 and tokens[-1] in _SECRET_LIKE_PROFILE_ENV_NAME_ABBREVIATION_TOKENS:
        return True
    if any(
        (left, right) in _SECRET_LIKE_PROFILE_ENV_NAME_TOKEN_PAIRS
        for left, right in zip(tokens, tokens[1:], strict=False)
    ):
        return True
    return bool(tokens and tokens[-1] == "KEY")


def _is_auth_credential_like_profile_env_value(value: str) -> bool:
    return bool(
        _AUTH_CREDENTIAL_LIKE_VALUE_PATTERN.search(value)
        or _CAMELCASE_API_KEY_CREDENTIAL_LIKE_VALUE_PATTERN.search(value)
        or _CAMELCASE_ENCRYPTION_KEY_CREDENTIAL_LIKE_VALUE_PATTERN.search(value)
        or _PREFIXED_SECRET_KEY_CREDENTIAL_LIKE_VALUE_PATTERN.search(value)
        or _CAMELCASE_SECRET_KEY_CREDENTIAL_LIKE_VALUE_PATTERN.search(value)
        or _CAMELCASE_SECRET_CREDENTIAL_LIKE_VALUE_PATTERN.search(value)
        or _PREFIXED_COOKIE_CREDENTIAL_LIKE_VALUE_PATTERN.search(value)
        or _CAMELCASE_COOKIE_CREDENTIAL_LIKE_VALUE_PATTERN.search(value)
        or _PREFIXED_PRIVATE_KEY_CREDENTIAL_LIKE_VALUE_PATTERN.search(value)
        or _NETRC_AUTH_CREDENTIAL_LIKE_VALUE_PATTERN.search(value)
        or _NPMRC_AUTH_CREDENTIAL_LIKE_VALUE_PATTERN.search(value)
    )


def _url_field_name_tokens(name: str) -> tuple[str, ...]:
    camel_split = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    camel_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", camel_split)
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", camel_split).upper()
    return tuple(token for token in normalized.split("_") if token)


def _url_field_name_has_secret_credential(name: str) -> bool:
    tokens = _url_field_name_tokens(name)
    normalized = "".join(tokens)
    if normalized in _URL_SECRET_CREDENTIAL_FIELD_EXACT_NAMES:
        return True
    return any(token in _URL_SECRET_CREDENTIAL_FIELD_NAMES for token in tokens) or any(
        (left, right) in _URL_SECRET_CREDENTIAL_FIELD_NAME_TOKEN_PAIRS
        for left, right in zip(tokens, tokens[1:], strict=False)
    )


def _url_component_has_secret_credential_field(component: str) -> bool:
    if not component:
        return False
    try:
        query_pairs = parse_qsl(component, keep_blank_values=False)
    except ValueError:
        query_pairs = []
    if any(_url_field_name_has_secret_credential(key) for key, _value in query_pairs):
        return True
    if any(
        ("://" in value or value.startswith("//")) and _value_has_url_userinfo(value)
        for _key, value in query_pairs
    ):
        return True
    if any(_url_query_value_has_secret_credential_field(value) for _key, value in query_pairs):
        return True
    if any(_relative_url_value_has_secret_credential_field(value) for _key, value in query_pairs):
        return True
    for raw_part in re.split(r"[&;]", component):
        key, separator, value = raw_part.partition("=")
        if separator and value and _url_field_name_has_secret_credential(key):
            return True
    return False


def _url_query_value_has_secret_credential_field(value: str) -> bool:
    if not value or not any(separator in value for separator in ("=", "&", ";")):
        return False
    return _url_component_has_secret_credential_field(value)


def _relative_url_value_has_secret_credential_field(value: str) -> bool:
    if not value or ("?" not in value and "#" not in value):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme or parsed.netloc:
        return False
    return any(
        _url_component_has_secret_credential_field(component)
        for raw_component in (parsed.query, parsed.fragment)
        for component in _url_component_variants(raw_component)
    )


def _url_component_variants(component: str) -> tuple[str, ...]:
    if not component:
        return ()
    decoded = unquote(component)
    if decoded == component:
        return (component,)
    return (component, decoded)


def _is_passwordless_git_ssh_url_userinfo(value: str, scheme: str) -> bool:
    return scheme.lower() in {"ssh", "git+ssh"} and value == "git"


def _value_has_url_userinfo(value: str) -> bool:
    """Return whether ``value`` is a URL containing credential material."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.netloc:
        if "@" in parsed.netloc:
            userinfo = parsed.netloc.rsplit("@", maxsplit=1)[0]
            if userinfo and not _is_passwordless_git_ssh_url_userinfo(
                userinfo,
                parsed.scheme,
            ):
                return True
        if any(
            _url_component_has_secret_credential_field(component)
            for raw_component in (parsed.netloc, parsed.path, parsed.query, parsed.fragment)
            for component in _url_component_variants(raw_component)
        ):
            return True
        return any(
            _value_has_url_userinfo(match.group(0))
            for raw_component in (parsed.path, parsed.query, parsed.fragment)
            for component in _url_component_variants(raw_component)
            for match in _URL_LIKE_SUBSTRING_PATTERN.finditer(component)
        )
    if not parsed.scheme:
        return any(
            _value_has_url_userinfo(match.group(0))
            for match in _URL_LIKE_SUBSTRING_PATTERN.finditer(value)
        )
    if any(
        _url_component_has_secret_credential_field(component)
        for raw_component in (parsed.path, parsed.query, parsed.fragment)
        for component in _url_component_variants(raw_component)
    ):
        return True
    return any(
        _value_has_url_userinfo(match.group(0))
        for raw_component in (parsed.path, parsed.query, parsed.fragment)
        for component in _url_component_variants(raw_component)
        for match in _URL_LIKE_SUBSTRING_PATTERN.finditer(component)
    )


_GITHUB_TOKEN_SOURCE_PRECEDENCE = ("AWF_GITHUB_TOKEN", *_GITHUB_TOKEN_ALIAS_PRECEDENCE)


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
    """Resolve profile service declarations into Compose service records."""
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
    """Return runtime and endpoint environment entries for the agent service."""
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
    """Render profile app endpoints into agent-visible environment entries."""
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


def merge_agent_environment(
    base_environment: tuple[tuple[str, str], ...],
    additions: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """Merge agent environment pairs without overwriting existing keys.

    Non-git-config keys keep "first writer wins" semantics (the base value is
    preserved). The ``GIT_CONFIG_KEY_n/VALUE_n/COUNT`` protocol is merged
    specially: both the base and the additions are split into their *present*
    git-config entries, concatenated in order, and re-emitted as a single
    contiguous ``0..N-1`` block with a matching ``GIT_CONFIG_COUNT``. The lease
    resolver always emits its bitbucket ``insteadOf`` entries starting at index
    0, so a profile that already declares indexed ``GIT_CONFIG_*`` in
    ``runtime.environment`` would otherwise either collide on index 0 (dropped by
    the skip-existing rule) or sit above the effective count and never reach git,
    breaking private Bitbucket HTTPS in the agent. Re-emitting a fresh contiguous
    block keeps both blocks reachable and tolerates a malformed base whose count
    overstates the present entries (holes) or whose indexed keys lack a count —
    git rejects any block with holes or a mismatched count, so the base is
    normalized rather than passed through verbatim.
    """

    base_entries, base_others = _split_git_config_entries(base_environment)
    addition_entries, addition_others = _split_git_config_entries(additions)
    merged: list[tuple[str, str]] = list(base_others)
    existing = {key for key, _ in merged}
    for key, value in addition_others:
        if key not in existing:
            merged.append((key, value))
            existing.add(key)
    entries = base_entries + addition_entries
    if not entries:
        return tuple(merged)

    for index, (config_key, config_value) in enumerate(entries):
        merged.append((f"{_GIT_CONFIG_KEY_PREFIX}{index}", config_key))
        merged.append((f"{_GIT_CONFIG_VALUE_PREFIX}{index}", config_value))
    merged.append((_GIT_CONFIG_COUNT_KEY, str(len(entries))))
    return tuple(merged)


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
    # ``GH_TOKEN`` and ``GITHUB_TOKEN`` are read by the GitHub CLI "in order of
    # precedence" (GH_TOKEN first — https://cli.github.com/manual/gh_help_environment).
    # Treat the aliases as one group: when a profile already owns a lower-precedence
    # alias (e.g. a secret lease that renders only ``GITHUB_TOKEN``), do NOT inject
    # the worker token under a higher-precedence alias, or ``gh`` would use the
    # worker credential instead of the profile-owned token. Injecting a lower-
    # precedence worker alias alongside a profile-owned higher-precedence one stays
    # harmless because the profile alias still wins.
    for index, name in enumerate(_GITHUB_TOKEN_ALIAS_PRECEDENCE):
        if name in existing:
            continue
        if any(lower in existing for lower in _GITHUB_TOKEN_ALIAS_PRECEDENCE[index + 1 :]):
            continue
        merged.append((name, token_placeholder))
        existing.add(name)
    return tuple(merged)


def _github_token_placeholder(source_env: Mapping[str, str]) -> str | None:
    for name in _GITHUB_TOKEN_SOURCE_PRECEDENCE:
        if source_env.get(name):
            return "${" + name + "}"
    return None


def _github_token_source_name(source_env: Mapping[str, str]) -> str | None:
    """Return the env name of the first present GitHub token source.

    Mirrors :func:`_github_token_placeholder`'s scan order
    (``AWF_GITHUB_TOKEN`` first), but returns the bare *name* (e.g.
    ``AWF_GITHUB_TOKEN``) rather than the ``${NAME}`` placeholder. Used by the
    hosted path to surface the chosen source *name* so a hosted executor can
    resolve the credential out-of-band when the worker only carries the AWF
    source and not the ``gh``-visible aliases (see
    :func:`hosted_github_token_passthrough_names`).
    """
    for name in _GITHUB_TOKEN_SOURCE_PRECEDENCE:
        if source_env.get(name):
            return name
    # Unreachable through the production call path: this helper mirrors
    # ``_github_token_placeholder``'s scan order, and
    # ``hosted_github_token_passthrough_names`` returns ``()`` early when the
    # placeholder is ``None`` (no token source present), so this fallback is
    # never reached for a running caller. Kept for completeness; excluding it
    # avoids a hollow test that calls the private helper solely to mark the
    # line executed.
    return None  # pragma: no cover


def hosted_github_token_passthrough_names(
    compose_file: Path,
    *,
    compose_env: Mapping[str, str] | None = None,
    worker_env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return GitHub token *names* a hosted executor should resolve.

    Mirrors ``agent_environment_with_github_token``'s alias injection for the
    local Compose path: when the worker env carries a GitHub token source
    (``AWF_GITHUB_TOKEN`` / ``GH_TOKEN`` / ``GITHUB_TOKEN``), the local path
    injects ``GH_TOKEN`` / ``GITHUB_TOKEN`` placeholders into the compose agent
    env block so the local agent container can run ``gh``. The hosted
    (non-compose) path has no compose env block substitution, so without these
    names the hosted executor cannot resolve the credential and the hosted
    monitor-repair agent loses GitHub CLI access even though the same
    workspace has it under Compose (PR #751 thread PRRT_kwDOSJAM6s6PXFPz).

    Source-name surfacing (PR #751 thread PRRT_kwDOSJAM6s6PYNGv):
    ``_github_token_placeholder`` orders ``AWF_GITHUB_TOKEN`` first, so a worker
    that only has ``AWF_GITHUB_TOKEN`` set (the documented service token) yields
    the ``${AWF_GITHUB_TOKEN}`` placeholder. Local Compose substitutes that
    placeholder into the ``GH_TOKEN`` / ``GITHUB_TOKEN`` aliases at stack launch,
    so the local agent container can run ``gh``; the hosted path has no
    equivalent substitution — it resolves ``env_passthrough_names`` by name
    out-of-band, so resolving ``GH_TOKEN`` / ``GITHUB_TOKEN`` finds nothing in
    that common setup. When at least one gh-visible alias was rendered in the
    Compose environment, the helper therefore also surfaces the chosen source
    *name* so the hosted executor can resolve the credential from the source
    name and mirror it into the gh-visible aliases (the same
    ``AWF_GITHUB_TOKEN`` -> ``GH_TOKEN`` / ``GITHUB_TOKEN`` mirroring
    ``_service_git_environment`` / ``_check_github`` / ``_gh_probe_environ``
    already apply). The source name is de-duplicated against the surfaced
    aliases (when the source is itself a gh-visible alias, e.g. ``GH_TOKEN``, it
    appears once). If no gh-visible alias was rendered, no source name is
    surfaced because the local Compose agent did not receive a GitHub CLI token
    alias to mirror.

    Names only — secret values are NEVER transported; the hosted executor
    resolves them out-of-band, mirroring ``env_passthrough_names``. The
    returned names carry no values, so this helper never embeds a worker
    secret (the placeholder string itself is never returned).

    A compose-declared GitHub token source or alias allows worker-token
    surfacing only when its value equals the worker token placeholder (the same
    AWF source ``agent_environment_with_github_token`` would inject, or a
    GitHub secret lease that renders to the same source). When a profile owns a
    GitHub token source or alias with a *different* token (e.g. a literal
    ``AWF_GITHUB_TOKEN`` or a generic ``env`` secret lease rendering
    ``GITHUB_TOKEN: ${MY_PROFILE_LEASE_TOKEN}``), NO worker alias or source name
    is surfaced: the local Compose path's group-precedence rule
    (``agent_environment_with_github_token``) ensures the profile-owned token
    wins, and surfacing a worker alias/source on the hosted path would let
    ``gh`` fall back to the worker credential (the profile-owned name cannot be
    carried in ``env_passthrough_names`` — it is a worker-resolved secret slot
    the hosted path resolves via its own adapter contract, not a name the hosted
    executor re-resolves from the worker). Surfacing nothing preserves the
    existing no-profile-credential limitation for that edge case without
    introducing a worker-token shadow.

    Returns an empty tuple when no worker GitHub token source is present
    (mirroring the local path, which injects nothing) or when the compose
    file is unreadable (fail-closed: assume the profile owns a distinct
    GitHub token rather than surface a worker alias that could shadow a
    profile-owned token the unreadable parse could not see).

    A compose *pass-through* slot (``environment: [GH_TOKEN]`` with no ``=``,
    ``GH_TOKEN:`` / ``GH_TOKEN: null``) declares no value — Docker Compose
    takes it from the worker shell at stack launch. It is worker-resolved only
    when that same name exists in the worker environment; if the worker has only
    ``AWF_GITHUB_TOKEN``, local Compose does not give the agent ``GH_TOKEN`` and
    the hosted path must not surface ``GH_TOKEN`` as a pass-through name (PR
    #754 thread PRRT_kwDOSJAM6s6P6an-).
    """
    source_env = os.environ if worker_env is None else worker_env
    worker_placeholder = _github_token_placeholder(source_env)
    if worker_placeholder is None:
        return ()
    if compose_env is None:
        compose_env = _try_agent_environment_from_compose_file(compose_file)
    if compose_env is None:
        return ()
    # If any compose-declared GitHub token source or alias points at a different
    # token than the worker source, the profile owns a distinct GitHub
    # credential. Surface nothing so a worker source/alias cannot shadow it on
    # the hosted path (the profile-owned name itself is a worker-resolved secret
    # slot the hosted path does not re-resolve from the worker env). A
    # pass-through slot (raw value == :data:`_COMPOSE_PASSTHROUGH`) is not
    # profile-owned; it is either worker-resolved for that same name or absent
    # from the local container. An absent pass-through slot does not suppress
    # other explicit aliases, but it is not surfaced as its own hosted
    # pass-through name.
    for token_name in _GITHUB_TOKEN_SOURCE_PRECEDENCE:
        raw = compose_env.get(token_name)
        if raw == _COMPOSE_PASSTHROUGH and not source_env.get(token_name):
            continue
        if token_name in compose_env and not _github_token_slot_matches_worker(
            token_name,
            raw,
            worker_placeholder=worker_placeholder,
            worker_env=source_env,
        ):
            return ()
    # Surface every alias whose value matches the worker token placeholder
    # (AWF-injected, or a GitHub lease rendering to the same source), plus
    # pass-through slots when the same name exists in the worker env
    # (worker-resolved — the local Compose container received the worker shell
    # value for that name, so the hosted executor resolves the same worker value
    # out-of-band). These are exactly the aliases the local Compose container
    # receives the worker token in, so the hosted executor resolving them
    # out-of-band reproduces the same credential.
    aliases = tuple(
        alias
        for alias in _GITHUB_TOKEN_ALIAS_PRECEDENCE
        if _github_token_slot_matches_worker(
            alias,
            compose_env.get(alias),
            worker_placeholder=worker_placeholder,
            worker_env=source_env,
        )
    )
    if not aliases:
        return aliases
    # Surface the chosen source name first so a hosted executor can resolve the
    # credential from the source name when Compose rendered that source into at
    # least one gh-visible alias at stack launch (the hosted path has no
    # equivalent substitution). Do not prepend a higher-precedence source merely
    # because a lower-precedence same-name pass-through alias matched; local
    # Compose did not render the higher source into that alias.
    # De-duplicate against the surfaced aliases so a source that is itself a
    # gh-visible alias (e.g. ``GH_TOKEN``) appears exactly once. Source first
    # preserves the scan order's precedence intent (``AWF_GITHUB_TOKEN`` before
    # the aliases).
    source_name = _github_token_source_name(source_env)
    if (
        source_name is not None
        and source_name not in aliases
        and any(
            _github_token_alias_selects_source(
                compose_env.get(alias),
                source_name,
                worker_env=source_env,
            )
            for alias in aliases
        )
    ):
        return (source_name, *aliases)
    return aliases


def _github_token_alias_selects_source(
    raw: str | None,
    source_name: str,
    *,
    worker_env: Mapping[str, str],
) -> bool:
    """Return whether a rendered alias selected the chosen worker source."""
    return (
        raw is not None
        and raw != _COMPOSE_PASSTHROUGH
        and _compose_selected_worker_reference_name(raw, worker_env=worker_env) == source_name
    )


def _github_token_slot_matches_worker(
    token_name: str,
    raw: str | None,
    *,
    worker_placeholder: str,
    worker_env: Mapping[str, str],
) -> bool:
    """Return whether a compose GitHub token slot resolves to the worker token."""
    if (
        raw is not None
        and _compose_empty_setness_reference_name(raw, worker_env=worker_env) is not None
    ):
        return False
    return (
        raw == worker_placeholder
        or (raw == _COMPOSE_PASSTHROUGH and bool(worker_env.get(token_name)))
        or (
            raw is not None
            and _compose_defaulted_reference_name(raw, worker_env=worker_env) == token_name
        )
        or (
            raw is not None
            and _compose_selected_worker_reference_name(raw, worker_env=worker_env)
            == _github_token_source_name(worker_env)
        )
    )


def agent_environment_with_host_auth(
    base_environment: tuple[tuple[str, str], ...],
    *,
    host_env: Mapping[str, str] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Expose legacy host auth placeholders for the agent environment."""
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
    shadowing_ollama_keys = _shadowing_worker_ollama_keys(existing)
    for name in AGENT_AUTH_ENV_VARS:
        if name in shadowing_ollama_keys:
            continue
        if name not in existing and source_env.get(name):
            merged.append((name, f"${{{name}}}"))
            existing.add(name)
    return agent_environment_with_github_token(tuple(merged), host_env=source_env)


def _shadowing_worker_ollama_keys(profile_keys: set[str]) -> frozenset[str]:
    """Worker Ollama base-URL keys that would shadow the profile's own selection.

    When a profile declares only a lower-precedence Ollama key (e.g. ``OLLAMA_HOST``)
    the worker env may still carry a higher-precedence ``AWF_OPENCODE_OLLAMA_BASE_URL``.
    Injecting that worker placeholder would let the agent's OpenCode launcher resolve
    a different daemon than AWF's profile-aware preflight just readied (see
    ``provider_readiness_helpers.overlay_profile_ollama_base_url``). Return the
    higher-precedence keys to skip so the profile-declared daemon wins.
    """

    declared = [i for i, key in enumerate(_OLLAMA_BASE_URL_ENV_KEYS) if key in profile_keys]
    if not declared:
        return frozenset()
    top = min(declared)
    return frozenset(_OLLAMA_BASE_URL_ENV_KEYS[:top])


def _try_agent_environment_from_compose_file(
    compose_file: Path,
) -> dict[str, str] | None:
    """Return agent env pairs from compose, or ``None`` when the file is unreadable."""

    try:
        payload = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    services = payload.get("services")
    if not isinstance(services, Mapping):
        return None
    agent = services.get("agent")
    if not isinstance(agent, Mapping):
        return None
    return _compose_environment_mapping(agent.get("environment"))


def hosted_file_auth_mount_targets(
    compose_file: Path,
    *,
    compose_env: Mapping[str, str] | None = None,
    worker_env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return secret-free provider auth mount targets for hosted agent runs.

    The rendered Compose file includes host source paths for local auth mounts;
    hosted requests must not carry those host paths or credential contents.
    Recognized container targets are returned so a hosted executor can resolve
    equivalent file-backed credentials out-of-band. ADC files are mounted at
    the same path named by ``GOOGLE_APPLICATION_CREDENTIALS`` rather than a
    fixed provider directory, so accept that dynamic target only when the
    Compose agent environment resolves the matching credential path.
    """

    try:
        payload = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return ()
    if not isinstance(payload, Mapping):
        return ()
    services = payload.get("services")
    if not isinstance(services, Mapping):
        return ()
    agent = services.get("agent")
    if not isinstance(agent, Mapping):
        return ()
    volumes = agent.get("volumes")
    if not isinstance(volumes, list):
        return ()

    if compose_env is None:
        compose_env = _try_agent_environment_from_compose_file(compose_file)
    adc_targets = _hosted_google_application_credentials_mount_targets(
        compose_env,
        worker_env=os.environ if worker_env is None else worker_env,
    )
    targets: list[str] = []
    seen: set[str] = set()
    for volume in volumes:
        target = _compose_volume_target(volume)
        if (
            target not in _HOSTED_FILE_AUTH_MOUNT_TARGETS and target not in adc_targets
        ) or target in seen:
            continue
        targets.append(target)
        seen.add(target)
    return tuple(targets)


def _hosted_google_application_credentials_mount_targets(
    compose_env: Mapping[str, str] | None,
    *,
    worker_env: Mapping[str, str],
) -> frozenset[str]:
    if compose_env is None:
        return frozenset()
    raw = compose_env.get("GOOGLE_APPLICATION_CREDENTIALS")
    if raw is None:
        return frozenset()
    if raw == _COMPOSE_PASSTHROUGH:
        target = worker_env.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    else:
        target, resolution = _compose_resolve_value(raw, worker_env=worker_env)
        if resolution in (
            _ComposeEnvResolution.WORKER_RESOLVED_SLOT,
            _ComposeEnvResolution.WORKER_RESOLVED_DEFAULTED,
        ):
            source_name = _compose_selected_worker_reference_name(raw, worker_env=worker_env)
            target = worker_env.get(source_name, "") if source_name is not None else ""
        elif resolution is not _ComposeEnvResolution.LITERAL:
            return frozenset()
    if not target or not Path(target).is_absolute():
        return frozenset()
    return frozenset({target})


def _compose_volume_target(volume: object) -> str | None:
    if isinstance(volume, str):
        parts = volume.split(":")
        if len(parts) < 2:
            return None
        return parts[1]
    if isinstance(volume, Mapping):
        target = volume.get("target") or volume.get("dst") or volume.get("destination")
        if isinstance(target, str):
            return target
    return None


def _try_agent_environment_keys_from_compose_file(
    compose_file: Path,
) -> frozenset[str] | None:
    """Return agent env keys from compose, or ``None`` when the file is unreadable."""

    compose_env = _try_agent_environment_from_compose_file(compose_file)
    if compose_env is None:
        return None
    return frozenset(compose_env)


def agent_environment_keys_from_compose_file(compose_file: Path) -> frozenset[str]:
    """Return env var names declared on the agent service in a rendered compose file."""

    return _try_agent_environment_keys_from_compose_file(compose_file) or frozenset()


def _compose_env_passthrough_exclusions(
    compose_env: Mapping[str, str] | None,
) -> frozenset[str]:
    """Return auth env var names a compose exec/passthrough must not re-inject.

    Shared by the local ``docker compose exec -e`` path and the hosted (non-
    compose) execution path so both suppress the same profile-owned auth/env
    slots and the same shadowing worker Ollama base-URL keys. An unreadable
    compose fails closed the same way ``agent_exec_env_passthrough`` does:
    assume a profile-owned ``OLLAMA_HOST`` would shadow the worker base URL
    rather than treat a parse failure as "no profile Ollama keys".

    Takes the already-parsed compose agent environment (``None`` when the
    compose file was unreadable) so callers can read/parse the file once and
    reuse the result for both this exclusion set and the compose-env union,
    avoiding a TOCTOU window between two reads of the same file.
    """
    if compose_env is None:
        shadowing = _shadowing_worker_ollama_keys({"OLLAMA_HOST"})
        profile_owned = frozenset[str]()
    else:
        shadowing = _shadowing_worker_ollama_keys(set(compose_env))
        profile_owned = _profile_owned_auth_keys(compose_env)
    return shadowing | profile_owned


def agent_exec_env_passthrough(
    *,
    compose_file: Path,
    host_env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return auth env var names safe to pass through on ``compose exec -e``.

    Mirrors ``agent_environment_with_legacy_host_auth`` shadowing: when compose
    generation omitted a higher-precedence worker Ollama key so a profile-owned
    daemon wins, do not re-inject that key via exec-time ``-e`` passthrough.

    Also skip keys already rendered into the agent service environment block.
    ``docker compose exec -e NAME`` (no value) re-reads ``NAME`` from the worker
    shell and overrides the running container's env, clobbering profile-owned or
    lease-resolved credentials/endpoints — including declared env leases that
    render as same-name ``${NAME}`` placeholders. Only auth keys absent from the
    compose environment **and** configured in the worker env remain eligible for
    exec-time passthrough.
    """

    source_env = os.environ if host_env is None else host_env
    compose_env = _try_agent_environment_from_compose_file(compose_file)
    excluded = _compose_env_passthrough_exclusions(compose_env)
    return tuple(
        name for name in AGENT_AUTH_ENV_VARS if name not in excluded and source_env.get(name)
    )


def filter_hosted_env_passthrough_names(
    names: tuple[str, ...],
    *,
    compose_file: Path,
    compose_env: Mapping[str, str] | None = None,
    worker_env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Apply the same compose/profile-owned exclusions to hosted passthrough names.

    The hosted (non-compose) execution path resolves secret values out-of-band
    from adapter-declared passthrough *names*. Without this filter the hosted
    request would unconditionally surface every name an adapter declares, even
    when the workspace's profile already owns that auth/env slot (e.g.
    ``OPENAI_API_KEY: ""`` placeholder, a required placeholder, or a
    lease-rendered value) and the local ``docker compose exec`` path deliberately
    suppresses it. That suppression lives in ``agent_exec_env_passthrough`` /
    ``_compose_env_passthrough_exclusions``: profile-owned keys already resolved
    at stack launch, and higher-precedence worker Ollama base-URL keys that would
    shadow a profile-owned daemon. Reintroducing such a name on the hosted path
    lets the hosted executor resolve an inherited worker credential/endpoint the
    local path and readiness overlay keep out of the agent environment.

    The local ``docker compose exec`` path only forwards ``AGENT_AUTH_ENV_VARS``
    and skips compose-declared ones, so *any* env key declared on the agent
    service's environment block is profile-owned at stack launch and never
    re-injected from the worker — including adapter backend-credential
    supplements (e.g. Claude Code ``AWS_*`` / Vertex project / region) that are
    not in ``AGENT_AUTH_ENV_VARS``. The hosted path must apply the same
    broader exclusion or a profile-owned backend credential/endpoint declared in
    the compose env block would be re-resolved from the worker by the hosted
    executor, diverging from the local run. When the compose file is unreadable
    the broader set is unknown, so hosted passthrough names are suppressed
    entirely rather than risking ambient worker credential injection.

    The profile-owned *names* stay filtered out of ``env_passthrough_names`` so
    the hosted executor does not re-resolve them from the worker; their literal
    *values* reach the hosted job via ``profile_env`` instead (see
    ``literal_profile_env_from_compose``), mirroring the local container's
    stack-launch env.

    Worker-resolved same-name defaulted forms (PR #751 thread
    PRRT_kwDOSJAM6s6PVH0t): a
    profile env value declared with a Compose default/override such as
    ``AWS_REGION: ${AWS_REGION:-us-west-2}`` is interpolated by Docker Compose
    against the *worker* env at stack launch, so the local agent container
    receives the worker's ``AWS_REGION`` when it is set (not the profile
    default). Carrying the worker value in ``profile_env`` would embed a secret;
    excluding the name from passthrough would drop it entirely — so hosted runs
    received neither, diverging from the local run. Such a name is therefore
    kept in ``env_passthrough_names`` (classified
    ``WORKER_RESOLVED_DEFAULTED``) so the hosted executor resolves the same
    worker value out-of-band, mirroring the local Compose container. The same
    applies to ``${NAME:?err}`` / ``${NAME?err}`` with ``NAME`` set (the local
    container received the worker value). Cross-name defaulted/required aliases
    such as ``AWS_REGION: ${AWS_DEFAULT_REGION:-us-west-2}`` stay excluded: the
    hosted executor resolves by the target key, not the referenced source name,
    so a target-name passthrough cannot reconstruct the local value. Pure
    literals, unset defaults, and ``${NAME:+alt}`` / ``${NAME+alt}`` alternates
    stay excluded (their concrete value reaches the hosted job via
    ``profile_env``). A bare ``${NAME}`` / ``$NAME`` single reference whose
    variable is worker-set stays in ``env_passthrough_names`` for hosted
    out-of-band resolution (the local container received the worker value at
    stack launch; carrying it would embed the endpoint/secret), mirroring the
    pass-through slot fix — PR #751 thread PRRT_kwDOSJAM6s6Pi7sN. A bare slot
    whose variable is unset stays excluded (Compose substitutes ``""``; out of
    scope), as do nested/mixed worker-resolved slot forms (e.g.
    ``${X:-${SECRET}}`` with ``X`` unset / ``prefix-${NAME}``) whose value is a
    profile-owned literal interpolating a worker value the hosted executor
    cannot reconstruct from the name alone.
    ``worker_env`` (default ``os.environ``) supplies the worker environment used
    to classify defaulted / required / alternate / bare forms, mirroring
    ``literal_profile_env_from_compose``.
    """
    if compose_env is None:
        compose_env = _try_agent_environment_from_compose_file(compose_file)
    env = os.environ if worker_env is None else worker_env
    return _filter_hosted_env_passthrough_names_from_compose_env(names, compose_env, worker_env=env)


def hosted_profile_env_passthrough_names(
    compose_file: Path,
    *,
    compose_env: Mapping[str, str] | None = None,
    worker_env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return compose-declared env names the hosted executor must resolve by name.

    Adapter-declared passthrough names cover runtime auth contracts, but a
    profile can also declare arbitrary same-name worker-resolved env secrets
    such as ``NPM_TOKEN: ${NPM_TOKEN}``. The local Compose container receives
    those values at stack launch. Hosted runs skip the worker-resolved values
    from ``profile_env`` for secret safety, so they must carry the resolvable
    names out-of-band even when no adapter advertises them.
    """
    if compose_env is None:
        compose_env = _try_agent_environment_from_compose_file(compose_file)
    if compose_env is None:
        return ()
    env = os.environ if worker_env is None else worker_env
    names = _filter_hosted_env_passthrough_names_from_compose_env(
        tuple(compose_env),
        compose_env,
        worker_env=env,
    )
    return tuple(
        name for name in names if compose_env.get(name) != _COMPOSE_PASSTHROUGH or name in env
    )


def hosted_profile_env_passthrough_aliases(
    compose_file: Path,
    *,
    compose_env: Mapping[str, str] | None = None,
    worker_env: Mapping[str, str] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return cross-name env aliases the hosted executor must resolve by source.

    Profile-declared env secret leases can render Compose env as
    ``TARGET: ${SOURCE}`` or ``TARGET: ${SOURCE:-}``. Local Compose substitutes
    ``SOURCE`` into ``TARGET`` at stack launch, but hosted target-name
    passthrough cannot reconstruct that relationship. Return names only so
    hosted executors can resolve ``source_name`` out-of-band and inject it as
    ``target_name`` without transporting the secret value in the request.
    """
    if compose_env is None:
        compose_env = _try_agent_environment_from_compose_file(compose_file)
    if compose_env is None:
        return ()
    env = os.environ if worker_env is None else worker_env
    aliases: list[tuple[str, str]] = []
    for name, raw in compose_env.items():
        if _is_git_config_protocol_key(name):
            continue
        hosted_secret_source = _hosted_env_secret_alias_source_name(raw)
        if hosted_secret_source is not None:
            if (
                name not in _HOSTED_FILE_BACKED_ENV_ONLY_UNSUPPORTED_NAMES
                and hosted_secret_source not in _HOSTED_FILE_BACKED_ENV_ONLY_UNSUPPORTED_NAMES
            ):
                aliases.append((name, hosted_secret_source))
            continue
        if raw == _COMPOSE_PASSTHROUGH:
            continue
        resolution = _compose_resolve_value(raw, worker_env=env)[1]
        if resolution in (
            _ComposeEnvResolution.WORKER_RESOLVED_SLOT,
            _ComposeEnvResolution.WORKER_RESOLVED_DEFAULTED,
        ):
            source_name = _compose_selected_worker_reference_name(raw, worker_env=env)
        else:
            continue
        if _compose_empty_setness_reference_name(raw, worker_env=env) is not None:
            continue
        if source_name is None or source_name == name or source_name not in env:
            continue
        if (
            name in _HOSTED_FILE_BACKED_ENV_ONLY_UNSUPPORTED_NAMES
            or source_name in _HOSTED_FILE_BACKED_ENV_ONLY_UNSUPPORTED_NAMES
        ):
            continue
        aliases.append((name, source_name))
    aliases.extend(
        _hosted_git_config_passthrough_aliases(
            compose_env,
            worker_env=env,
            skip_bitbucket_agent_rewrites=_has_mount_backed_bitbucket_askpass(
                compose_env,
                worker_env=env,
            ),
        )
    )
    return tuple(aliases)


def _filter_hosted_env_passthrough_names_from_compose_env(
    names: tuple[str, ...],
    compose_env: Mapping[str, str] | None,
    *,
    worker_env: Mapping[str, str],
) -> tuple[str, ...]:
    """Apply compose/profile-owned exclusions given a pre-parsed compose env.

    Shared shape so a caller that already parsed the compose agent environment
    (e.g. ``_run_hosted`` computing ``profile_env`` from the same parse) can
    reuse the result and avoid a second read/parse of the file.

    A compose-declared name whose value resolves to a same-name
    ``WORKER_RESOLVED_DEFAULTED`` (a ``${NAME:-default}`` / ``${NAME-default}``
    form with ``NAME`` set, or a ``${NAME:?err}`` / ``${NAME?err}`` required form
    with ``NAME`` set) is NOT excluded: the local Compose container received the
    worker value at stack launch, so the hosted executor must resolve that value
    out-of-band rather than drop the name (which would leave the hosted job with
    neither the worker override nor the profile default). A name whose value is
    a pass-through slot (``environment: [NAME]`` with no ``=``, ``NAME:`` /
    ``NAME: null``) whose name exists in the worker environment is likewise NOT
    excluded: the hosted executor must keep the name available for out-of-band
    resolution instead of replacing it with an empty literal. An unset
    pass-through slot is excluded because there is no local value for hosted
    execution to mirror. A name whose value resolves to ``LITERAL`` (a pure
    literal, an *explicit* empty value ``NAME: ""`` / ``NAME=``, a defaulted form
    with the variable unset, or an ``:+`` / ``+`` alternate form) IS excluded —
    its concrete value reaches the hosted job via ``profile_env`` instead. A
    bare ``${NAME}`` / ``$NAME`` single reference whose variable is worker-set
    is NOT excluded either: the local Compose container received the worker
    value at stack launch, so the hosted executor must resolve it out-of-band
    rather than drop the name (PR #751 thread PRRT_kwDOSJAM6s6Pi7sN). Cross-name
    worker-resolved aliases stay excluded because target-name-only passthrough
    cannot recover the source-name value. A bare slot whose variable is unset IS
    excluded because Compose substitutes ``""`` and the empty value reaches
    hosted through ``profile_env``, as are nested/mixed worker-resolved forms
    (e.g. ``${X:-${SECRET}}`` / ``prefix-${NAME}``) — the hosted executor cannot
    reconstruct a profile-owned literal interpolating a worker value from the
    name alone. See ``filter_hosted_env_passthrough_names`` and PR #751 threads
    PRRT_kwDOSJAM6s6PVH0t / PRRT_kwDOSJAM6s6PVhhm /
    PRRT_kwDOSJAM6s6PYnJJ / PRRT_kwDOSJAM6s6PY6Rn / PRRT_kwDOSJAM6s6PY8zB /
    PRRT_kwDOSJAM6s6Pi7sN. A pass-through slot is removed from the baseline
    ``_compose_env_passthrough_exclusions`` set even when its name is in
    ``AGENT_AUTH_ENV_VARS`` (``_profile_owned_auth_keys`` treats any auth key
    declared on the agent service as profile-owned regardless of value); the
    hosted executor keeps the name in the out-of-band passthrough contract
    (PRRT_kwDOSJAM6s6PY6Rn). An explicit empty value is NOT a pass-through slot
    (compose-go models it as a non-nil
    pointer to ``""`` that overrides the worker value), so it stays excluded
    and its literal ``""`` reaches the hosted job via ``profile_env``
    (PRRT_kwDOSJAM6s6PY8zB).
    """
    if compose_env is None:
        return ()
    excluded = _compose_env_passthrough_exclusions(compose_env)
    if compose_env is not None:
        # Exclude compose-declared names UNLESS their value is worker-resolved
        # and the local container received the worker value at stack launch:
        # ``WORKER_RESOLVED_DEFAULTED`` (``:-`` / ``-`` / ``:?`` / ``?`` with a
        # selected non-empty worker value) and a worker-present, non-empty
        # pass-through slot (raw value == :data:`_COMPOSE_PASSTHROUGH` —
        # ``environment: [NAME]`` with no ``=``, ``NAME:`` / ``NAME: null``) stay
        # in passthrough for hosted out-of-band resolution.
        # Carrying the worker value in ``profile_env`` would embed a secret
        # (defaulted) or override the real worker value with an empty literal
        # (pass-through), and excluding the name would drop it entirely. Literal
        # values (pure literals, an *explicit* empty ``NAME: ""`` / ``NAME=``
        # which Compose sets as a non-nil empty literal overriding the worker
        # value, unset defaults, ``:+`` / ``+`` alternates) are excluded — their
        # concrete value reaches the hosted job via ``profile_env``. A bare
        # ``${NAME}`` / ``$NAME`` slot (``WORKER_RESOLVED_SLOT``) whose variable
        # IS set in the worker env stays in passthrough too — see below.
        #
        # A worker-present pass-through slot (raw value ==
        # :data:`_COMPOSE_PASSTHROUGH`) is removed from the baseline
        # ``_compose_env_passthrough_exclusions`` set first: that set treats any
        # ``AGENT_AUTH_ENV_VARS`` key declared on the agent service as
        # profile-owned (``_profile_owned_auth_keys``) regardless of value, so
        # an auth pass-through slot would be excluded before the worker-resolved
        # exception below (which only prevents *adding* a name) could keep it.
        # The hosted executor must keep the name available for out-of-band
        # resolution rather than carry an empty literal (PR #751 thread
        # PRRT_kwDOSJAM6s6PY6Rn).
        #
        # An *explicit* empty value (``NAME: ""`` / ``NAME=``) is normalized to
        # the plain string ``""`` (NOT the sentinel): Docker Compose sets a
        # non-nil empty literal that OVERRIDES the worker shell value, so it is a
        # profile-owned LITERAL — excluded from passthrough (its concrete ``""``
        # reaches the hosted job via ``profile_env``), NOT a worker-resolved slot
        # (PR #751 thread PRRT_kwDOSJAM6s6PY8zB). Only the sentinel marks a true
        # pass-through slot; ``_compose_resolve_value("")`` -> ``("", LITERAL)``
        # so an explicit empty is excluded by the LITERAL branch below.
        passthrough_slots = frozenset(
            name
            for name, raw in compose_env.items()
            if raw == _COMPOSE_PASSTHROUGH and bool(worker_env.get(name))
        )
        # Worker-resolved same-name defaulted/required forms resolve to a worker
        # value at stack launch, exactly like pass-through slots. Keep those
        # target names in hosted passthrough only when the outer selected
        # variable matches the target key and the selected worker value is
        # non-empty; an explicitly empty set-ness override is carried in
        # ``profile_env``. Cross-name aliases and unused nested default words
        # cannot be reconstructed by the hosted executor's target-name-only
        # resolution.
        worker_resolved_defaulted = frozenset(
            name
            for name, raw in compose_env.items()
            if raw != _COMPOSE_PASSTHROUGH
            and _compose_resolve_value(raw, worker_env=worker_env)[1]
            is _ComposeEnvResolution.WORKER_RESOLVED_DEFAULTED
            and _compose_defaulted_reference_name(raw, worker_env=worker_env) == name
            and _compose_empty_setness_reference_name(raw, worker_env=worker_env) != name
        )
        # A bare ``${NAME}`` / ``$NAME`` slot (``WORKER_RESOLVED_SLOT``) whose
        # variable has a non-empty worker value resolves to the worker value at
        # stack launch, exactly like a pass-through slot and a worker-resolved
        # defaulted form. Core injects this exact form via
        # ``agent_environment_with_legacy_host_auth`` (it appends
        # ``NAME: ${NAME}`` for worker-present ``AGENT_AUTH_ENV_VARS`` keys the
        # profile does not already declare), so a profile that owns only the
        # lower-precedence Ollama key still surfaces a bare ``${OLLAMA_HOST}``
        # slot the local container received the worker value for.
        # ``literal_profile_env_from_compose`` skips ``WORKER_RESOLVED_SLOT``
        # (carrying the worker value would embed the endpoint/secret in
        # ``profile_env``), and the baseline excluded set treats any
        # ``AGENT_AUTH_ENV_VARS`` key declared on the agent service as
        # profile-owned (``_profile_owned_auth_keys``), so — just like the
        # worker-resolved-defaulted auth case — the name would be excluded and
        # the hosted executor would carry neither the value nor the name, even
        # though adapters like OpenCode advertise ``OLLAMA_HOST`` in
        # ``hosted_env_passthrough_names``. A hosted monitor-repair run would
        # then launch without the daemon endpoint the same workspace has under
        # Compose. Remove such names from the baseline excluded set first,
        # mirroring the pass-through slot (PRRT_kwDOSJAM6s6PY6Rn) and
        # worker-resolved-defaulted (PRRT_kwDOSJAM6s6PiGHK) fixes.
        #
        # Only a *bare single reference* (``${NAME}`` / ``$NAME`` — the exact
        # form Core injects) qualifies, AND the referenced variable must match
        # the target key name. A bare reference to a *different* worker variable
        # (e.g. a declared env secret lease rendering
        # ``ANTHROPIC_API_KEY: ${MY_ANTHROPIC_TOKEN}`` or
        # ``AWS_REGION: ${AWS_DEFAULT_REGION}``) classifies
        # ``WORKER_RESOLVED_SLOT`` and the source name has a non-empty worker value,
        # but the hosted executor resolves by the *target* name (absent from the
        # worker env), so keeping it in ``env_passthrough_names`` surfaces a
        # name that resolves to nothing — the hosted request carries neither the
        # source-to-target mapping nor the resolved value, and the credential is
        # silently dropped (``literal_profile_env_from_compose`` skips the slot,
        # so ``profile_env`` has no alias either). The source-to-target aliasing
        # for such leases is handled by ``env_passthrough_aliases``, not by
        # target-name passthrough, so a cross-name slot stays excluded (PR #751
        # thread PRRT_kwDOSJAM6s6PjYmf). A selected default/alternate word that
        # is exactly a same-name worker reference (e.g. ``${FLAG:+${NAME}}``)
        # is safe to keep because the hosted executor resolves the same target
        # name local Compose selected. Mixed forms (e.g. ``prefix-${NAME}``)
        # still cannot be reconstructed from the name alone. A bare slot whose
        # variable is UNSET or present-but-empty stays excluded too: Compose
        # substitutes "" for a bare reference without a non-empty worker value,
        # and ``literal_profile_env_from_compose`` carries that empty literal into
        # ``profile_env``. Core only injects the bare form when the worker value is
        # present (``source_env.get(name)`` is truthy), and the unset
        # ``${NAME:?err}`` / ``${NAME?err}`` form would fail Compose at stack
        # launch (unreachable for a running container). The
        # ``worker_env.get(source_name)`` test gates on the local container
        # actually receiving a worker value (PR #751 thread PRRT_kwDOSJAM6s6Pi7sN).
        worker_resolved_slots = frozenset(
            name
            for name, raw in compose_env.items()
            if raw != _COMPOSE_PASSTHROUGH
            and _compose_resolve_value(raw, worker_env=worker_env)[1]
            is _ComposeEnvResolution.WORKER_RESOLVED_SLOT
            and (source_name := _compose_selected_worker_reference_name(raw, worker_env=worker_env))
            is not None
            and source_name == name
            and bool(worker_env.get(source_name))
        )
        name_only_credential_identifiers = _hosted_name_only_credential_identifier_keys(
            compose_env,
            worker_env=worker_env,
        )
        keep = (
            passthrough_slots
            | worker_resolved_defaulted
            | worker_resolved_slots
            | name_only_credential_identifiers
        )
        excluded = (excluded - keep) | frozenset(name for name in compose_env if name not in keep)
    return tuple(name for name in names if name not in excluded)


def _profile_owned_auth_keys(compose_env: Mapping[str, str]) -> frozenset[str]:
    """Return agent auth env keys already declared in the compose environment block."""
    return frozenset(name for name in AGENT_AUTH_ENV_VARS if name in compose_env)


def _hosted_name_only_credential_identifier_keys(
    compose_env: Mapping[str, str],
    *,
    worker_env: Mapping[str, str],
) -> frozenset[str]:
    """Return credential identifiers that hosted should resolve by name."""
    keys: set[str] = set()
    for name, raw in compose_env.items():
        if name not in _HOSTED_NAME_ONLY_CREDENTIAL_IDENTIFIER_ENV_VARS:
            continue
        if raw == _COMPOSE_PASSTHROUGH:
            continue
        expanded, resolution = _compose_resolve_value(raw, worker_env=worker_env)
        if (
            resolution is _ComposeEnvResolution.LITERAL
            and expanded != ""
            and worker_env.get(name) == expanded
        ) or (
            resolution
            in (
                _ComposeEnvResolution.WORKER_RESOLVED_SLOT,
                _ComposeEnvResolution.WORKER_RESOLVED_DEFAULTED,
            )
            and _compose_selected_worker_reference_name(raw, worker_env=worker_env) == name
            and _compose_empty_setness_reference_name(raw, worker_env=worker_env) != name
        ):
            keys.add(name)
    return frozenset(keys)


# Compose env-value interpolation / resolution machinery lives in
# ``awf.profiles.compose_env`` (extracted to keep this file under the
# maintainability line limit). The names are re-imported here so every existing
# caller and test that imports them from ``awf.profiles.compose`` — including
# module-private names such as ``_COMPOSE_PASSTHROUGH`` and attribute access
# via ``compose_module.<name>`` — keeps working unchanged. This is a pure
# relocation; the logic is byte-for-byte identical.
from awf.profiles.compose_env import (  # noqa: E402, F401  (re-export)
    _COMPOSE_ALTERNATE_OPERATORS,
    _COMPOSE_BRACED_OPERATORS,
    _COMPOSE_DEFAULT_OPERATORS,
    _COMPOSE_ENV_NAME_PATTERN,
    _COMPOSE_ESCAPED_DOLLAR,
    _COMPOSE_PASSTHROUGH,
    _compose_bare_reference_name,
    _compose_braced_expression_end,
    _compose_concrete_worker_password,
    _compose_concrete_worker_password_braced,
    _compose_default_word_is_worker_resolved,
    _compose_defaulted_reference_name,
    _compose_empty_setness_reference_name,
    _compose_environment_mapping,
    _compose_resolve_braced,
    _compose_resolve_value,
    _compose_selected_worker_reference_name,
    _ComposeEnvResolution,
    _expanded_value_bears_postgres_password,
    _hosted_env_secret_alias_source_name,
)

# Re-exported for the long-standing ``awf.profiles.compose`` public surface.
from awf.profiles.compose_profile_env import (  # noqa: E402
    literal_profile_env_from_compose as literal_profile_env_from_compose,
)
