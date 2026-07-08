"""Convert workspace profiles into compose-manager inputs."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlunsplit

import yaml

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


# Ollama base-URL env keys in precedence order (highest first) — the OpenCode
# launcher prelude and the worker-side Ollama preflight both resolve the daemon
# from the first non-empty key here. Mirrors ``provider_readiness_helpers``'
# ``_OLLAMA_BASE_URL_ENV_KEYS``; kept local so ``profiles`` does not import the
# ``service`` layer.
_OLLAMA_BASE_URL_ENV_KEYS = ("AWF_OPENCODE_OLLAMA_BASE_URL", "OLLAMA_HOST")

# GitHub CLI token aliases in precedence order (highest first). ``gh`` reads
# ``GH_TOKEN`` before ``GITHUB_TOKEN`` (https://cli.github.com/manual/gh_help_environment),
# so injecting a worker token under an earlier alias shadows a profile-owned later one.
_GITHUB_TOKEN_ALIAS_PRECEDENCE = ("GH_TOKEN", "GITHUB_TOKEN")


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
    # Only the numerically-indexed protocol vars (``GIT_CONFIG_KEY_<n>`` /
    # ``GIT_CONFIG_VALUE_<n>``) and ``GIT_CONFIG_COUNT`` belong to the block that is
    # split out and re-emitted contiguously. A key that merely shares the prefix but
    # has a non-numeric suffix (e.g. ``GIT_CONFIG_KEY_THRESHOLD``) is not a protocol
    # entry: matching it here would strip it from ``others`` yet, lacking a numeric
    # index, it would never be re-emitted — silently dropping it from the merged env.
    if key == _GIT_CONFIG_COUNT_KEY:
        return True
    for prefix in (_GIT_CONFIG_KEY_PREFIX, _GIT_CONFIG_VALUE_PREFIX):
        if key.startswith(prefix):
            return key[len(prefix) :].isdigit()
    return False


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


def _try_compose_agent_env_and_postgres_password(
    compose_file: Path,
) -> tuple[dict[str, str] | None, str | None]:
    """Parse the compose file once, returning agent env and the postgres password.

    Returns ``(agent_env, postgres_password)`` where ``agent_env`` is ``None``
    when the compose is unreadable or has no agent service (mirrors
    ``_try_agent_environment_from_compose_file``) and ``postgres_password`` is
    ``None`` when no service declares ``POSTGRES_PASSWORD``). Parsing once avoids
    a second read/parse of the same file when ``literal_profile_env_from_compose``
    needs both the agent env and the rendered postgres password for redaction.

    ``POSTGRES_PASSWORD`` is collected from *every* compose service, not only a
    service literally named ``postgres``. A valid custom profile may name its
    database sidecar ``db`` / ``database`` (or anything else) while still setting
    ``POSTGRES_PASSWORD`` and expanding that same password into the agent env
    ``DATABASE_URL`` / ``AWF_DATABASE_URL``. Looking up only
    ``services["postgres"]`` would leave ``postgres_password`` ``None`` for such a
    profile and ``literal_profile_env_from_compose`` would carry the rendered DB
    URL in ``profile_env``, leaking the workspace credential to the hosted
    executor despite the secret-free contract. Scanning all services tracks the
    rendered secret source independent of the service name.
    """
    try:
        payload = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return None, None
    if not isinstance(payload, Mapping):
        return None, None
    services = payload.get("services")
    if not isinstance(services, Mapping):
        return None, None
    agent = services.get("agent")
    agent_env = (
        _compose_environment_mapping(agent.get("environment"))
        if isinstance(agent, Mapping)
        else None
    )
    postgres_password: str | None = None
    for service in services.values():
        if not isinstance(service, Mapping):
            continue
        service_env = _compose_environment_mapping(service.get("environment"))
        password = service_env.get("POSTGRES_PASSWORD")
        if password:
            postgres_password = password
            break
    return agent_env, postgres_password


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
    the broader set is unknown, so only the ``AGENT_AUTH_ENV_VARS``-territory /
    Ollama-shadowing exclusions apply (fail-closed the same way
    ``_compose_env_passthrough_exclusions`` does).

    The profile-owned *names* stay filtered out of ``env_passthrough_names`` so
    the hosted executor does not re-resolve them from the worker; their literal
    *values* reach the hosted job via ``profile_env`` instead (see
    ``literal_profile_env_from_compose``), mirroring the local container's
    stack-launch env.

    Worker-resolved defaulted forms (PR #751 thread PRRT_kwDOSJAM6s6PVH0t): a
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
    container received the worker value). Pure literals, unset defaults, and
    ``${NAME:+alt}`` / ``${NAME+alt}`` alternates stay excluded (their concrete
    value reaches the hosted job via ``profile_env``), and bare ``${NAME}`` /
    ``$NAME`` slots stay excluded (profile-owned secret slots resolved via the
    adapter contract). ``worker_env`` (default ``os.environ``) supplies the worker
    environment used to classify defaulted / required / alternate forms,
    mirroring ``literal_profile_env_from_compose``.
    """
    compose_env = _try_agent_environment_from_compose_file(compose_file)
    env = os.environ if worker_env is None else worker_env
    return _filter_hosted_env_passthrough_names_from_compose_env(names, compose_env, worker_env=env)


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

    A compose-declared name whose value resolves to
    ``WORKER_RESOLVED_DEFAULTED`` (a ``${NAME:-default}`` / ``${NAME-default}``
    form with ``NAME`` set, or a ``${NAME:?err}`` / ``${NAME?err}`` required form
    with ``NAME`` set) is NOT excluded: the local Compose container received the
    worker value at stack launch, so the hosted executor must resolve that value
    out-of-band rather than drop the name (which would leave the hosted job with
    neither the worker override nor the profile default). A name whose value
    resolves to ``LITERAL`` (a pure literal, a defaulted form with the variable
    unset, or an ``:+`` / ``+`` alternate form) IS excluded — its concrete value
    reaches the hosted job via ``profile_env`` instead. See
    ``filter_hosted_env_passthrough_names`` and PR #751 threads
    PRRT_kwDOSJAM6s6PVH0t / PRRT_kwDOSJAM6s6PVhhm.
    """
    excluded = _compose_env_passthrough_exclusions(compose_env)
    if compose_env is not None:
        # Exclude compose-declared names UNLESS their value is a worker-resolved
        # defaulted form (``:-`` / ``-`` with the variable set, or ``:?`` / ``?``
        # with the variable set) — those stay in passthrough for hosted out-of-band
        # resolution (the local container received the worker value at stack
        # launch; carrying it in ``profile_env`` would embed a secret, and
        # excluding the name would drop it entirely). Literal values (pure
        # literals, unset defaults, ``:+`` / ``+`` alternates) are excluded —
        # their concrete value reaches the hosted job via ``profile_env``.
        excluded = excluded | frozenset(
            name
            for name, raw in compose_env.items()
            if _compose_resolve_value(raw, worker_env=worker_env)[1]
            is not _ComposeEnvResolution.WORKER_RESOLVED_DEFAULTED
        )
    return tuple(name for name in names if name not in excluded)


def _profile_owned_auth_keys(compose_env: Mapping[str, str]) -> frozenset[str]:
    """Return agent auth env keys already declared in the compose environment block."""
    return frozenset(name for name in AGENT_AUTH_ENV_VARS if name in compose_env)


# Compose variable-interpolation resolver, mirroring the interpolation model in
# ``awf.service.environment`` (``${VAR}`` / ``${VAR:-...}`` / ``$VAR``). Kept
# local so ``profiles`` does not import the ``service`` layer. An escaped ``$$``
# is a literal dollar, not an interpolation reference.


class _ComposeEnvResolution(StrEnum):
    """How a compose env value resolves against the worker env.

    Drives both carry-to-``profile_env`` (``literal_profile_env_from_compose``)
    and hosted passthrough filtering
    (``_filter_hosted_env_passthrough_names_from_compose_env``):
    """

    LITERAL = "literal"
    # Worker-resolved via ``${NAME:-default}`` / ``${NAME-default}`` with ``NAME``
    # set in the worker env (non-empty for ``:-``). The local Compose container
    # receives the *worker* value at stack launch, so the hosted path must leave
    # the name available for out-of-band resolution (NOT carry the worker value in
    # ``profile_env`` — that would embed a secret — and NOT exclude the name from
    # ``env_passthrough_names`` — that would drop it entirely, diverging from the
    # local run). See PR #751 thread PRRT_kwDOSJAM6s6PVH0t.
    WORKER_RESOLVED_DEFAULTED = "worker_resolved_defaulted"
    # Worker-resolved via bare ``${NAME}`` / ``$NAME`` and ``${NAME:?...}`` /
    # ``${NAME?...}`` with the variable unset (the local stack would fail to
    # launch, so this is unreachable for a running container) — profile-owned
    # secret slots the local path keeps out of exec-time passthrough; the hosted
    # path resolves them via its own adapter contract, not by re-resolving
    # ``${NAME}`` from the worker. ``${NAME:?...}`` / ``${NAME?...}`` with the
    # variable set resolve to the worker value and are classified
    # ``WORKER_RESOLVED_DEFAULTED`` (kept in passthrough). ``${NAME:+...}`` /
    # ``${NAME+...}`` with the variable set carry the profile-owned alternate word
    # as ``LITERAL`` (the local container received the alternate, not a worker
    # value).
    WORKER_RESOLVED_SLOT = "worker_resolved_slot"


# Compose braced-expression operators, ordered longest-first so ``:-`` is
# matched before ``-`` (and ``:+`` before ``+``, ``:?`` before ``?``). Mirrors
# ``awf.service.environment._compose_expand_braced_expression``'s scan order.
_COMPOSE_BRACED_OPERATORS = (":-", "-", ":+", "+", ":?", "?")

# ``:-`` / ``-`` supply a concrete default when the referenced variable is unset
# (``:-`` tests non-empty, ``-`` tests set-ness). Only these forms carry a
# profile-owned concrete value to the hosted job when the variable is absent
# (or empty, for ``:-``) from the worker env; when the variable is set the slot
# is worker-resolved-defaulted. The non-empty vs set-ness distinction is handled
# in ``_compose_resolve_braced`` to mirror ``awf.service.environment``'s expander.
_COMPOSE_DEFAULT_OPERATORS = (":-", "-")

# ``:+`` / ``+`` supply an alternate word when the referenced variable is set
# (``:+`` tests non-empty, ``+`` tests set-ness). The alternate word is
# profile-owned config (literal text in the compose file), so it is carried as a
# literal when the test passes (the local container received the alternate word);
# when the test fails Compose resolves to "" and that empty literal is carried.
# An alternate word that references a worker secret propagates the worker-resolved
# classification so the secret never reaches ``profile_env``.
_COMPOSE_ALTERNATE_OPERATORS = (":+", "+")

# Sentinel used to mask ``$$`` escapes before interpolation scanning so an
# escaped dollar is never mistaken for a reference start.
_COMPOSE_ESCAPED_DOLLAR = "\0AWF_PROFILE_ESCAPED_DOLLAR\0"

_COMPOSE_ENV_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _compose_braced_expression_end(value: str, open_brace_index: int) -> int | None:
    """Return the index of the matching ``}`` for a ``${`` at ``open_brace_index``."""
    depth = 1
    index = open_brace_index + 1
    while index < len(value):
        char = value[index]
        if char == "$" and index + 1 < len(value) and value[index + 1] == "{":
            depth += 1
            index += 2
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _compose_resolve_value(
    value: str,
    *,
    worker_env: Mapping[str, str],
) -> tuple[str, _ComposeEnvResolution]:
    """Resolve a Compose env value against the worker env.

    Returns ``(expanded, resolution)`` where ``resolution`` classifies whether the
    value carries a concrete profile-owned literal (``LITERAL``) or pulls a
    worker-resolved value (``WORKER_RESOLVED_DEFAULTED`` for defaulted / required
    forms with the variable set; ``WORKER_RESOLVED_SLOT`` for bare ``${NAME}`` /
    ``$NAME`` and unset required forms). See :class:`_ComposeEnvResolution` for the
    carry vs passthrough rules.

    Carry rule (mirrors what the local agent container receives at stack
    launch, without embedding worker secrets in ``profile_env``):

    - A pure literal (no interpolation reference) is carried verbatim.
    - An escaped ``$$`` collapses to a single literal ``$`` and is carried.
    - ``${NAME:-default}`` / ``${NAME-default}`` with ``NAME`` unset in the
      worker env resolves to the concrete ``default`` and is carried — the
      local container receives that default, so the hosted job must too
      (dropping it leaves the hosted job missing the profile-owned value).
    - ``${NAME:-default}`` with ``NAME`` present-but-empty in the worker env
      resolves to the concrete ``default`` and is carried — ``:-`` tests
      non-empty, so Compose injects the default into the local container and
      the hosted job must receive it too.
    - ``${NAME:-default}`` with ``NAME`` set to a non-empty worker value is
      ``WORKER_RESOLVED_DEFAULTED``: skipped from ``profile_env`` (carrying it
      would embed a worker secret) but kept in ``env_passthrough_names`` so the
      hosted executor resolves the same worker value the local container received.
    - ``${NAME-default}`` with ``NAME`` set in the worker env (even empty) is
      ``WORKER_RESOLVED_DEFAULTED`` — ``-`` tests set-ness, so a present value is
      worker-resolved.
    - ``${NAME:+alternate}`` / ``${NAME+alternate}`` with ``NAME`` set (non-empty
      for ``:+``) resolves to the concrete ``alternate`` and is carried — the
      local container received the alternate word (profile-owned config, not a
      worker value), so the hosted job must too. When ``NAME`` is unset (or empty
      for ``:+``) Compose resolves to ``""`` and that empty literal is carried. An
      alternate word that references a worker secret propagates the worker-resolved
      classification so the secret never reaches ``profile_env``.
    - ``${NAME:?err}`` / ``${NAME?err}`` with ``NAME`` set (non-empty for ``:?``)
      resolves to the worker value and is ``WORKER_RESOLVED_DEFAULTED`` — kept in
      ``env_passthrough_names`` for hosted out-of-band resolution (the local
      container received the worker value) and skipped from ``profile_env`` (it
      would embed a secret). An unset required form would fail Compose at stack
      launch, so that branch is ``WORKER_RESOLVED_SLOT`` (unreachable for a
      running container).
    - A bare ``${NAME}`` / ``$NAME`` (no operator) is ``WORKER_RESOLVED_SLOT``: a
      worker-resolved slot the profile owns locally; the hosted path resolves
      credentials via its own adapter contract, not by re-resolving ``${NAME}``
      from the worker.

    The default / alternate word is itself recursively expanded against the
    worker env, mirroring ``awf.service.environment``'s env-file interpolator.
    """
    escaped = value.replace("$$", _COMPOSE_ESCAPED_DOLLAR)
    expanded: list[str] = []
    index = 0
    while index < len(escaped):
        char = escaped[index]
        if char != "$":
            expanded.append(char)
            index += 1
            continue
        if index + 1 < len(escaped) and escaped[index + 1] == "{":
            end = _compose_braced_expression_end(escaped, index + 1)
            if end is None:
                expanded.append(char)
                index += 1
                continue
            piece, piece_resolution = _compose_resolve_braced(
                escaped[index + 2 : end], worker_env=worker_env
            )
            if piece_resolution is not _ComposeEnvResolution.LITERAL:
                return "", piece_resolution
            expanded.append(piece)
            index = end + 1
            continue
        plain_match = _COMPOSE_ENV_NAME_PATTERN.match(escaped, index + 1)
        if plain_match is None:
            expanded.append(char)
            index += 1
            continue
        # Bare ``$NAME`` (no default operator) -> worker-resolved slot, skip.
        return "", _ComposeEnvResolution.WORKER_RESOLVED_SLOT
    return "".join(expanded).replace(_COMPOSE_ESCAPED_DOLLAR, "$"), _ComposeEnvResolution.LITERAL


def _compose_resolve_braced(
    expression: str,
    *,
    worker_env: Mapping[str, str],
) -> tuple[str, _ComposeEnvResolution]:
    """Resolve a Compose ``${...}`` braced expression.

    Returns ``(expanded, resolution)``; see :class:`_ComposeEnvResolution` and
    ``_compose_resolve_value``. Operator semantics mirror
    ``awf.service.environment._compose_expand_braced_expression`` so the hosted
    job receives the same value the local agent container gets at stack launch:

    - ``:-`` / ``-`` (default): when the variable is unset (or empty for ``:-``)
      the default word is recursively expanded and carried as ``LITERAL``
      (profile-owned concrete config); when the variable is set (non-empty for
      ``:-``) the worker value is used and the slot is ``WORKER_RESOLVED_DEFAULTED``
      (kept in passthrough, dropped from ``profile_env``).
    - ``:+`` / ``+`` (alternate): when the variable is set (non-empty for ``:+``)
      the alternate word is recursively expanded; if that expansion is a literal
      it is carried as ``LITERAL`` (the local container received the alternate
      word, which is profile-owned config, not a worker value). When the variable
      is unset (or empty for ``:+``) Compose resolves to ``""`` and that empty
      literal is carried so the hosted job matches the local container. An
      alternate word that itself references a worker secret (e.g.
      ``${FLAG:+${SECRET}}``) propagates the worker-resolved classification so the
      secret is never embedded in ``profile_env``.
    - ``:?`` / ``?`` (required): when the variable is set (non-empty for ``:?``)
      Compose resolves the worker value, so the slot is ``WORKER_RESOLVED_DEFAULTED``
      (kept in passthrough for hosted out-of-band resolution, dropped from
      ``profile_env``). When the variable is unset/empty the local stack would fail
      to launch, so that branch is unreachable for a running container and stays
      ``WORKER_RESOLVED_SLOT``.
    """
    name_match = _COMPOSE_ENV_NAME_PATTERN.match(expression)
    if name_match is None:
        # Unparseable braced text is carried through verbatim (no reference).
        return f"${{{expression}}}", _ComposeEnvResolution.LITERAL
    name = name_match.group(0)
    remainder = expression[name_match.end() :]
    if not remainder:
        # Bare ``${NAME}`` -> worker-resolved slot, skip.
        return "", _ComposeEnvResolution.WORKER_RESOLVED_SLOT
    operator = ""
    word = ""
    for candidate in _COMPOSE_BRACED_OPERATORS:
        if remainder.startswith(candidate):
            operator = candidate
            word = remainder[len(candidate) :]
            break
    if not operator:
        # Unknown operator -> carry verbatim (no reference).
        return f"${{{expression}}}", _ComposeEnvResolution.LITERAL
    worker_value = worker_env.get(name)
    is_set = name in worker_env
    is_non_empty = bool(worker_value)
    if operator in _COMPOSE_DEFAULT_OPERATORS:
        # ``:-`` tests non-empty; ``-`` tests set-ness.
        if (operator == ":-" and is_non_empty) or (operator == "-" and is_set):
            return "", _ComposeEnvResolution.WORKER_RESOLVED_DEFAULTED
        # Variable unset (or empty for :-) -> expand the default word and carry.
        default, default_resolution = _compose_resolve_value(word, worker_env=worker_env)
        if default_resolution is not _ComposeEnvResolution.LITERAL:
            return "", default_resolution
        return default, _ComposeEnvResolution.LITERAL
    if operator in _COMPOSE_ALTERNATE_OPERATORS:
        # ``:+`` tests non-empty; ``+`` tests set-ness. When the test passes the
        # alternate word is what the local container receives (profile-owned
        # config, not a worker value), so it is carried as a literal — unless the
        # word itself references a worker secret, in which case the worker-resolved
        # classification propagates. When the test fails Compose resolves to "".
        if (operator == ":+" and is_non_empty) or (operator == "+" and is_set):
            alternate, alternate_resolution = _compose_resolve_value(word, worker_env=worker_env)
            if alternate_resolution is not _ComposeEnvResolution.LITERAL:
                return "", alternate_resolution
            return alternate, _ComposeEnvResolution.LITERAL
        # Variable unset (or empty for :+) -> Compose resolves to "", carried.
        return "", _ComposeEnvResolution.LITERAL
    # ``:?`` / ``?`` (required): a set variable resolves to the worker value
    # (a secret), so the slot is worker-resolved-defaulted — kept in passthrough
    # for hosted out-of-band resolution and dropped from ``profile_env``. An
    # unset required form would fail Compose at stack launch, so that branch is
    # unreachable for a running container and stays a worker-resolved slot.
    if (operator == ":?" and is_non_empty) or (operator == "?" and is_set):
        return "", _ComposeEnvResolution.WORKER_RESOLVED_DEFAULTED
    return "", _ComposeEnvResolution.WORKER_RESOLVED_SLOT


def literal_profile_env_from_compose(
    compose_file: Path,
    *,
    compose_env: Mapping[str, str] | None = None,
    worker_env: Mapping[str, str] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return profile-owned env values the hosted executor must inject.

    The local ``docker compose exec`` path does not forward profile-owned env
    because the running agent container already has it (Docker Compose
    substitutes the compose env block at stack launch). The hosted (non-compose)
    path has no compose env block, so the hosted executor must inject the same
    values the local container received or it launches without them (e.g. a
    profile-owned ``OLLAMA_HOST`` daemon the OpenCode launcher then cannot
    resolve, falling back to the default daemon).

    Compose interpolation is rendered against ``worker_env`` (default
    ``os.environ``) so the hosted job receives the concrete value the local
    container gets at stack launch:

    - Pure literals are carried verbatim.
    - An escaped ``$$`` collapses to a single literal ``$`` and is carried
      (Compose models ``$$`` as a literal dollar, not a reference).
    - ``${NAME:-default}`` / ``${NAME-default}`` with ``NAME`` unset in the
      worker env resolves to the concrete ``default`` and is carried.
    - ``${NAME:-default}`` with ``NAME`` present-but-empty in the worker env
      resolves to the concrete ``default`` and is carried (``:-`` tests
      non-empty, matching ``awf.service.environment``'s expander so the hosted
      job receives the default the local container gets).
    - ``${NAME:-default}`` with ``NAME`` set to a non-empty worker value is
      skipped (worker-resolved; carrying the worker value would embed a
      secret in ``profile_env``). The name stays in ``env_passthrough_names``
      (see ``filter_hosted_env_passthrough_names``) so the hosted executor
      resolves the same worker value out-of-band — the local Compose container
      received it at stack launch.
    - ``${NAME-default}`` with ``NAME`` set in the worker env (even empty) is
      skipped (``-`` tests set-ness; a present value is worker-resolved). As
      above, the name stays in ``env_passthrough_names`` for hosted out-of-band
      resolution.
    - ``${NAME:+alternate}`` / ``${NAME+alternate}`` with ``NAME`` set (non-empty
      for ``:+``) resolves to the concrete ``alternate`` and is carried — the
      local container received the alternate word (profile-owned config), so the
      hosted job must too. With ``NAME`` unset (or empty for ``:+``) Compose
      resolves to ``""`` and that empty literal is carried. An alternate word that
      references a worker secret propagates the worker-resolved classification and
      is skipped (the secret never reaches ``profile_env``).
    - ``${NAME:?err}`` / ``${NAME?err}`` with ``NAME`` set (non-empty for ``:?``)
      is skipped (worker-resolved; the local container received the worker value,
      which is a secret). The name stays in ``env_passthrough_names`` for hosted
      out-of-band resolution. An unset required form would fail Compose at stack
      launch, so that branch is unreachable for a running container.
    - Bare ``${NAME}`` / ``$NAME`` forms are skipped (worker-resolved slots the
      profile owns locally; the hosted path resolves credentials via its own
      adapter contract, not by re-resolving ``${NAME}`` from the worker).

    Skipping worker-resolved values preserves the no-secret-values contract:
    ``profile_env`` never carries a ``${...}`` placeholder nor a worker secret.
    ComposeManager also expands ``${AWF_POSTGRES_PASSWORD}`` into profile DB URLs
    (e.g. ``DATABASE_URL`` / ``AWF_DATABASE_URL``) at render time so the local
    container can connect; those expanded literals embed the workspace DB
    password and are NOT carried — the hosted path resolves DB credentials via
    its own adapter contract, not from ``profile_env``. The rendered service env
    declaring ``POSTGRES_PASSWORD`` is the authoritative source of that secret;
    ``POSTGRES_PASSWORD`` is collected from any service (not only one named
    ``postgres``) so custom profiles that name their DB sidecar ``db`` /
    ``database`` are redacted too. Agent env values containing it are skipped so
    the password never reaches the hosted request object. When the compose file
    is unreadable the result is empty (fail-closed: no values), matching
    ``_compose_env_passthrough_exclusions``.

    ``compose_env`` lets a caller that already parsed the compose agent
    environment (e.g. ``_run_hosted`` computing the passthrough filter from the
    same parse) reuse the result and avoid a second read/parse of the file.
    ``worker_env`` lets a caller supply a deterministic worker environment for
    interpolation (default ``os.environ``); mirroring
    ``agent_environment_with_*`` helpers.
    """
    parsed_file = compose_env is None
    if compose_env is None:
        compose_env, file_postgres_password = _try_compose_agent_env_and_postgres_password(
            compose_file
        )
    else:
        file_postgres_password = None
    if compose_env is None:
        return ()
    # Redact agent env values that embed the rendered postgres password so the
    # generated workspace DB credential never reaches the hosted executor. Only
    # re-read the compose file when this call parsed the agent env itself; a
    # caller that supplied ``compose_env`` from a prior parse is responsible for
    # the same redaction context (it already has the file open). Failing to
    # locate a postgres password (no service declaring ``POSTGRES_PASSWORD``)
    # redacts nothing, preserving carry for profiles without a DB sidecar.
    postgres_password: str | None = file_postgres_password if parsed_file else None
    env = os.environ if worker_env is None else worker_env
    carried: list[tuple[str, str]] = []
    for key, raw in compose_env.items():
        expanded, resolution = _compose_resolve_value(raw, worker_env=env)
        if resolution is not _ComposeEnvResolution.LITERAL:
            continue
        if postgres_password and postgres_password in expanded:
            continue
        carried.append((key, expanded))
    return tuple(carried)


def _compose_environment_mapping(environment: object) -> dict[str, str]:
    """Normalize a compose ``environment`` scalar, list, or mapping into a string dict."""
    if isinstance(environment, Mapping):
        return {str(key): str(value) for key, value in environment.items()}
    if isinstance(environment, list):
        mapping: dict[str, str] = {}
        for item in environment:
            if isinstance(item, str):
                key, sep, value = item.partition("=")
                if key:
                    mapping[key] = value if sep else ""
            elif isinstance(item, Mapping):
                mapping.update({str(key): str(value) for key, value in item.items()})
        return mapping
    return {}
