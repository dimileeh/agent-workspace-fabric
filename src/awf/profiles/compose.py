"""Convert workspace profiles into compose-manager inputs."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
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
from awf.service.environment import compose_env_file_values

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
    for name in ("AWF_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        if source_env.get(name):
            return name
    return None


def hosted_github_token_passthrough_names(
    compose_file: Path,
    *,
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
    that common setup. The helper therefore also surfaces the chosen source
    *name* so the hosted executor can resolve the credential from the source
    name and mirror it into the gh-visible aliases (the same
    ``AWF_GITHUB_TOKEN`` -> ``GH_TOKEN`` / ``GITHUB_TOKEN`` mirroring
    ``_service_git_environment`` / ``_check_github`` / ``_gh_probe_environ``
    already apply). The source name is de-duplicated against the surfaced
    aliases (when the source is itself a gh-visible alias, e.g. ``GH_TOKEN``, it
    appears once).

    Names only — secret values are NEVER transported; the hosted executor
    resolves them out-of-band, mirroring ``env_passthrough_names``. The
    returned names carry no values, so this helper never embeds a worker
    secret (the placeholder string itself is never returned).

    A compose-declared GitHub token alias is surfaced only when its value
    equals the worker token placeholder (the same AWF source
    ``agent_environment_with_github_token`` would inject, or a GitHub secret
    lease that renders to the same source). When a profile owns a GitHub
    token alias with a *different* token (e.g. a generic ``env`` secret lease
    rendering ``GITHUB_TOKEN: ${MY_PROFILE_LEASE_TOKEN}``), NO worker alias or
    source name is surfaced: the local Compose path's group-precedence rule
    (``agent_environment_with_github_token``) ensures the profile-owned token
    wins, and surfacing a worker alias/source on the hosted path would let
    ``gh`` fall back to the worker credential (the profile-owned alias cannot
    be carried in ``env_passthrough_names`` — it is a worker-resolved secret
    slot the hosted path resolves via its own adapter contract, not a name
    the hosted executor re-resolves from the worker). Surfacing nothing
    preserves the existing no-profile-credential limitation for that edge case
    without introducing a worker-token shadow.

    Returns an empty tuple when no worker GitHub token source is present
    (mirroring the local path, which injects nothing) or when the compose
    file is unreadable (fail-closed: assume the profile owns a distinct
    GitHub token rather than surface a worker alias that could shadow a
    profile-owned token the unreadable parse could not see).

    A compose *pass-through* slot (``environment: [GH_TOKEN]`` with no ``=``,
    ``GH_TOKEN:`` / ``GH_TOKEN: null``) declares no value — Docker Compose
    takes it from the worker shell at stack launch — so it is worker-resolved
    and is treated as matching the corresponding worker source (the local
    Compose agent received the worker value for that name), NOT as a distinct
    profile-owned token. The pass-through alias name is surfaced so the hosted
    executor resolves it out-of-band, mirroring the local container (PR #751
    thread PRRT_kwDOSJAM6s6PZkRH).
    """
    source_env = os.environ if worker_env is None else worker_env
    worker_placeholder = _github_token_placeholder(source_env)
    if worker_placeholder is None:
        return ()
    compose_env = _try_agent_environment_from_compose_file(compose_file)
    if compose_env is None:
        return ()
    # If any compose-declared GitHub token alias points at a different token
    # than the worker source, the profile owns a distinct GitHub credential.
    # Surface nothing so a worker alias cannot shadow it on the hosted path
    # (the profile-owned alias itself is a worker-resolved secret slot the
    # hosted path does not re-resolve from the worker env). A pass-through slot
    # (raw value == :data:`_COMPOSE_PASSTHROUGH`) is worker-resolved, not
    # profile-owned — the local Compose container received the worker shell
    # value for that name — so it is NOT a distinct profile-owned token and
    # does not trigger the group-suppression branch (PR #751 thread
    # PRRT_kwDOSJAM6s6PZkRH).
    for alias in _GITHUB_TOKEN_ALIAS_PRECEDENCE:
        raw = compose_env.get(alias)
        if alias in compose_env and raw != worker_placeholder and raw != _COMPOSE_PASSTHROUGH:
            return ()
    # Surface every alias whose value matches the worker token placeholder
    # (AWF-injected, or a GitHub lease rendering to the same source), plus
    # pass-through slots (worker-resolved — the local Compose container received
    # the worker shell value for that name, so the hosted executor resolves the
    # same worker value out-of-band). These are exactly the aliases the local
    # Compose container receives the worker token in, so the hosted executor
    # resolving them out-of-band reproduces the same credential.
    aliases = tuple(
        alias
        for alias in _GITHUB_TOKEN_ALIAS_PRECEDENCE
        if compose_env.get(alias) == worker_placeholder
        or compose_env.get(alias) == _COMPOSE_PASSTHROUGH
    )
    # Surface the chosen source name first so a hosted executor can resolve the
    # credential from the source name when the worker only carries the AWF
    # source (local Compose substitutes the placeholder into the aliases at
    # stack launch; the hosted path has no equivalent substitution). De-duplicate
    # against the surfaced aliases so a source that is itself a gh-visible alias
    # (e.g. ``GH_TOKEN``) appears exactly once. Source first preserves the scan
    # order's precedence intent (``AWF_GITHUB_TOKEN`` before the aliases).
    source_name = _github_token_source_name(source_env)
    if source_name is not None and source_name not in aliases:
        return (source_name, *aliases)
    return aliases


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


def _try_compose_agent_env_and_postgres_passwords(
    compose_file: Path,
    *,
    worker_env: Mapping[str, str],
) -> tuple[dict[str, str] | None, frozenset[str]]:
    """Parse the compose file once, returning agent env and resolved DB passwords.

    Returns ``(agent_env, postgres_passwords)`` where ``agent_env`` is ``None``
    when the compose is unreadable or has no agent service (mirrors
    ``_try_agent_environment_from_compose_file``) and ``postgres_passwords`` is
    empty when no service declares ``POSTGRES_PASSWORD``). Parsing once avoids
    a second read/parse of the same file when ``literal_profile_env_from_compose``
    needs both the agent env and the rendered postgres password for redaction.

    ``POSTGRES_PASSWORD`` is collected from *every* compose service, not only a
    service literally named ``postgres``, and *all* distinct resolved values are
    returned (not only the first). A valid custom profile may name its database
    sidecar ``db`` / ``database`` (or anything else) while still setting
    ``POSTGRES_PASSWORD`` and expanding that same password into the agent env
    ``DATABASE_URL`` / ``AWF_DATABASE_URL``. Looking up only
    ``services["postgres"]`` would leave the password set empty for such a profile
    and ``literal_profile_env_from_compose`` would carry the rendered DB URL in
    ``profile_env``, leaking the workspace credential to the hosted executor
    despite the secret-free contract. Scanning all services tracks the rendered
    secret source independent of the service name.

    The collector also reads each service's ``env_file`` declarations. ComposeManager
    renders a profile service's ``env_file`` verbatim into the rendered compose file,
    and Docker Compose loads that file to populate the service's environment at
    stack launch — including ``POSTGRES_PASSWORD`` when a profile keeps its DB
    password in an ``env_file`` instead of the inline ``environment`` map. Inspecting
    only the inline map would leave ``postgres_passwords`` empty for such a profile,
    and ``literal_profile_env_from_compose`` would carry the rendered agent env
    ``DATABASE_URL`` / ``AWF_DATABASE_URL`` (which embed the same password) in
    ``profile_env``, leaking the workspace credential to a hosted executor despite
    the secret-free contract (PR #751 thread PRRT_kwDOSJAM6s6PZuE2). The env-file
    values are parsed with ``compose_env_file_values`` (mirroring Compose's
    interpolation rules) and ``POSTGRES_PASSWORD`` from each file is added to the
    redaction set, resolved against ``worker_env`` like an inline value. Relative
    ``env_file`` paths are resolved against the compose file's parent directory
    (matching Docker Compose's documented resolution rule) before parsing, so a
    profile keeping its DB password in a relative ``env_file`` (e.g. ``./db.env``)
    is found even though ComposeManager writes the rendered compose file under a
    per-workspace compose directory separate from the worker process cwd (PR #751
    thread PRRT_kwDOSJAM6s6PaMeK). A missing or unreadable env_file contributes
    nothing (best-effort: the inline env is the authoritative rendered source, so a
    missing env_file does not fail closed).

    Keeping only the first declared password (the previous behaviour) is also
    unsound when a profile runs several DB sidecars with *different* passwords:
    a rendered agent env value (e.g. a second service's ``WAREHOUSE_URL``) that
    embeds a later service's password would slip past a redaction that only
    compares against the first service's password. Collecting every declared
    value redacts each independently.

    Each declared ``POSTGRES_PASSWORD`` is resolved against ``worker_env`` with
    ``_compose_resolve_value`` before redaction so a service that expresses its
    password via Compose interpolation/defaults (e.g.
    ``${POSTGRES_PASSWORD:-fallback}`` or ``${POSTGRES_PASSWORD}``) redacts the
    same concrete value Docker Compose injects into the local agent container at
    stack launch. The raw ``${...}`` placeholder string is also tracked as a
    redaction target so a rendered agent env value that still carries the
    unexpanded placeholder (e.g. when ComposeManager did not expand that
    particular reference) is still redacted. Worker-resolved forms whose concrete
    value cannot be recovered here (``${NAME}`` with ``NAME`` set, or a defaulted
    form with ``NAME`` set) contribute only the raw placeholder string: the
    resolved secret is a worker value that must never be carried in
    ``profile_env``, and the rendered compose file ComposeManager writes already
    expands ``${AWF_POSTGRES_PASSWORD}`` into a literal for the common case.
    """
    try:
        payload = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return None, frozenset()
    if not isinstance(payload, Mapping):
        return None, frozenset()
    services = payload.get("services")
    if not isinstance(services, Mapping):
        return None, frozenset()
    agent = services.get("agent")
    agent_env = (
        _compose_environment_mapping(agent.get("environment"))
        if isinstance(agent, Mapping)
        else None
    )
    postgres_passwords: set[str] = set()
    for service in services.values():
        if not isinstance(service, Mapping):
            continue
        service_env = _compose_environment_mapping(service.get("environment"))
        # Collect ``POSTGRES_PASSWORD`` from the inline ``environment`` map and
        # from any ``env_file`` the service references. ComposeManager renders a
        # profile service's ``env_file`` verbatim into the rendered compose file,
        # and Docker Compose loads it to populate the service env at stack launch —
        # so an env-file-declared ``POSTGRES_PASSWORD`` is the same rendered secret
        # source as an inline one. Both are tracked for redaction (PR #751 thread
        # PRRT_kwDOSJAM6s6PZuE2).
        _collect_postgres_password(
            service_env.get("POSTGRES_PASSWORD"),
            postgres_passwords,
            worker_env=worker_env,
        )
        for env_file_path in _compose_service_env_file_paths(
            service.get("env_file"), compose_dir=compose_file.parent
        ):
            try:
                env_file_env = compose_env_file_values(env_file_path, environ=worker_env)
            except (OSError, UnicodeDecodeError):
                continue
            _collect_postgres_password(
                env_file_env.get("POSTGRES_PASSWORD"),
                postgres_passwords,
                worker_env=worker_env,
            )
    return agent_env, frozenset(postgres_passwords)


def _compose_service_env_file_paths(
    env_file: object, *, compose_dir: Path | None = None
) -> tuple[Path, ...]:
    """Return the ``env_file`` paths declared on a compose service.

    Compose accepts ``env_file`` as a single path string or a list of paths
    (and a mapping form with a ``path`` key). ComposeManager renders a profile
    service's ``env_file`` as a single-item list of the resolved workspace path
    (see ``workspace.base.yml.j2``), so both shapes are handled here. A missing
    or unreadable file is tolerated by the caller.

    Relative paths are resolved against the compose file's parent directory
    (matching Docker Compose's documented resolution rule) when ``compose_dir``
    is supplied, so a profile that keeps its DB password in a relative
    ``env_file`` (e.g. ``./db.env``) is looked up from the compose directory
    rather than the worker process cwd. Absolute paths are kept verbatim
    (PR #751 thread PRRT_kwDOSJAM6s6PaMeK).
    """
    paths: list[Path] = []
    raw_paths: list[str] = []
    if isinstance(env_file, str):
        raw_paths.append(env_file)
    elif isinstance(env_file, list):
        for item in env_file:
            if isinstance(item, str):
                raw_paths.append(item)
            elif isinstance(item, Mapping):
                raw = item.get("path")
                if isinstance(raw, str):
                    raw_paths.append(raw)
    for raw in raw_paths:
        path = Path(raw)
        if not path.is_absolute() and compose_dir is not None:
            path = compose_dir / path
        paths.append(path)
    return tuple(paths)


def _collect_postgres_password(
    raw_password: str | None,
    postgres_passwords: set[str],
    *,
    worker_env: Mapping[str, str],
) -> None:
    """Resolve and track one declared ``POSTGRES_PASSWORD`` for redaction.

    Shared by the inline ``environment`` and ``env_file`` collection paths so
    both apply the same redaction rules: a pass-through slot / explicit empty is
    skipped, the raw declared value is always a redaction target, and a
    worker-resolvable form contributes the concrete worker value (when recoverable)
    so a rendered agent env DB URL embedding the resolved secret is redacted too.
    """
    # A pass-through slot (``POSTGRES_PASSWORD:`` / ``: null`` / list bare
    # name) declares no value — Docker Compose resolves it from the worker
    # shell — so it has no profile-declared literal/placeholder to redact.
    # An explicit empty (``POSTGRES_PASSWORD: ""`` / ``=``) likewise carries
    # no secret. Both are skipped (``not raw_password`` covers the explicit
    # empty ``""``; the sentinel covers the pass-through slot).
    if not raw_password or raw_password == _COMPOSE_PASSTHROUGH:
        return
    # The raw declared value (literal or ``${...}`` placeholder) is always a
    # redaction target so an agent env value carrying the unexpanded form is
    # still redacted.
    postgres_passwords.add(raw_password)
    resolved, resolution = _compose_resolve_value(raw_password, worker_env=worker_env)
    if resolution is _ComposeEnvResolution.LITERAL and resolved:
        postgres_passwords.add(resolved)
    elif resolution is _ComposeEnvResolution.WORKER_RESOLVED_DEFAULTED:
        # A defaulted/required form with the variable *set* in the worker env
        # resolves to the worker value at stack launch. The redaction set is
        # only ever used for substring matching to *skip* agent env values —
        # it is never carried in ``profile_env`` — so it is safe (and
        # necessary) to include the concrete worker value here. Without it a
        # rendered DB URL embedding the resolved worker password would slip
        # past redaction.
        concrete = _compose_concrete_worker_password(raw_password, worker_env=worker_env)
        if concrete:
            postgres_passwords.add(concrete)
    elif resolution is _ComposeEnvResolution.WORKER_RESOLVED_SLOT:
        # A bare ``${NAME}`` / ``$NAME`` slot (no default operator) resolves to
        # the worker value at stack launch too, so a rendered agent env DB URL
        # embedding the *resolved* worker password carries the workspace
        # credential. Tracking only the raw ``${...}`` placeholder string (added
        # above) misses the expanded secret, and the URL slips past substring
        # redaction into ``profile_env``. Recover the concrete worker value the
        # same way the defaulted branch does so a bare-slot DB URL is redacted
        # identically (PR #751 thread PRRT_kwDOSJAM6s6PaFeB). The redaction set is
        # never carried in ``profile_env`` (worker-resolved values are skipped
        # from carry), so recovering the worker secret here only marks which
        # agent env values to skip — it does not violate the no-secret-values
        # contract.
        concrete = _compose_concrete_worker_password(raw_password, worker_env=worker_env)
        if concrete:
            postgres_passwords.add(concrete)


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
    neither the worker override nor the profile default). A name whose value is
    a pass-through slot (``environment: [NAME]`` with no ``=``, ``NAME:`` /
    ``NAME: null``) is likewise NOT excluded: Docker Compose took its value from
    the worker shell at stack launch, so the hosted executor must resolve the same
    worker value out-of-band. A name whose value resolves to ``LITERAL`` (a pure
    literal, an *explicit* empty value ``NAME: ""`` / ``NAME=``, a defaulted form
    with the variable unset, or an ``:+`` / ``+`` alternate form) IS excluded —
    its concrete value reaches the hosted job via ``profile_env`` instead. See
    ``filter_hosted_env_passthrough_names`` and PR #751 threads
    PRRT_kwDOSJAM6s6PVH0t / PRRT_kwDOSJAM6s6PVhhm / PRRT_kwDOSJAM6s6PYnJJ /
    PRRT_kwDOSJAM6s6PY6Rn / PRRT_kwDOSJAM6s6PY8zB. A pass-through slot is removed
    from the baseline ``_compose_env_passthrough_exclusions`` set even when its
    name is in ``AGENT_AUTH_ENV_VARS`` (``_profile_owned_auth_keys`` treats any
    auth key declared on the agent service as profile-owned regardless of
    value); the local Compose container received the worker shell value, so the
    hosted executor must resolve it out-of-band (PRRT_kwDOSJAM6s6PY6Rn). An
    explicit empty value is NOT a pass-through slot (compose-go models it as a
    non-nil pointer to ``""`` that overrides the worker value), so it stays
    excluded and its literal ``""`` reaches the hosted job via ``profile_env``
    (PRRT_kwDOSJAM6s6PY8zB).
    """
    excluded = _compose_env_passthrough_exclusions(compose_env)
    if compose_env is not None:
        # Exclude compose-declared names UNLESS their value is worker-resolved
        # and the local container received the worker value at stack launch:
        # ``WORKER_RESOLVED_DEFAULTED`` (``:-`` / ``-`` / ``:?`` / ``?`` with the
        # variable set) and a pass-through slot (raw value ==
        # :data:`_COMPOSE_PASSTHROUGH` — ``environment: [NAME]`` with no ``=``,
        # ``NAME:`` / ``NAME: null``; Docker Compose took the value from the
        # worker shell) stay in passthrough for hosted out-of-band resolution.
        # Carrying the worker value in ``profile_env`` would embed a secret
        # (defaulted) or override the real worker value with an empty literal
        # (pass-through), and excluding the name would drop it entirely. Literal
        # values (pure literals, an *explicit* empty ``NAME: ""`` / ``NAME=``
        # which Compose sets as a non-nil empty literal overriding the worker
        # value, unset defaults, ``:+`` / ``+`` alternates) are excluded — their
        # defaults, ``:+`` / ``+`` alternates) are excluded — their concrete value
        # reaches the hosted job via ``profile_env``. Bare ``${NAME}`` / ``$NAME``
        # (``WORKER_RESOLVED_SLOT``) stay excluded — profile-owned secret slots
        # the hosted path resolves via its adapter contract.
        #
        # A pass-through slot (raw value == :data:`_COMPOSE_PASSTHROUGH`) is
        # removed from the baseline ``_compose_env_passthrough_exclusions`` set
        # first: that set treats any ``AGENT_AUTH_ENV_VARS`` key declared on the
        # agent service as profile-owned (``_profile_owned_auth_keys``)
        # regardless of value, so an auth pass-through slot would be excluded
        # before the worker-resolved exception below (which only prevents
        # *adding* a name) could keep it. The local Compose container received
        # the worker shell value for such a slot, so the hosted executor must
        # resolve it out-of-band too (PR #751 thread PRRT_kwDOSJAM6s6PY6Rn).
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
            name for name, raw in compose_env.items() if raw == _COMPOSE_PASSTHROUGH
        )
        excluded = (excluded - passthrough_slots) | frozenset(
            name
            for name, raw in compose_env.items()
            if raw != _COMPOSE_PASSTHROUGH
            and _compose_resolve_value(raw, worker_env=worker_env)[1]
            is not _ComposeEnvResolution.WORKER_RESOLVED_DEFAULTED
        )
    return tuple(name for name in names if name not in excluded)


def _profile_owned_auth_keys(compose_env: Mapping[str, str]) -> frozenset[str]:
    """Return agent auth env keys already declared in the compose environment block."""
    return frozenset(name for name in AGENT_AUTH_ENV_VARS if name in compose_env)


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
    _compose_braced_expression_end,
    _compose_concrete_worker_password,
    _compose_concrete_worker_password_braced,
    _compose_environment_mapping,
    _compose_resolve_braced,
    _compose_resolve_value,
    _ComposeEnvResolution,
    _expanded_value_bears_postgres_password,
)


def literal_profile_env_from_compose(
    compose_file: Path,
    *,
    compose_env: Mapping[str, str] | None = None,
    worker_env: Mapping[str, str] | None = None,
    postgres_passwords: frozenset[str] | None = None,
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
    - A Compose *pass-through* slot (``environment: [NAME]`` with no ``=``,
      ``NAME:`` / ``NAME: null``) is skipped (worker-resolved; Docker Compose
      took the value from the worker shell at stack launch) and the name stays
      in ``env_passthrough_names`` for hosted out-of-band resolution. An
      *explicit* empty value (``NAME: ""`` / ``NAME=``) is distinct: Docker
      Compose sets a non-nil empty literal that OVERRIDES the worker shell
      value, so it is carried here as a literal ``""`` (the local container
      received an explicit blank, not the worker value) and the name is excluded
      from passthrough (profile-owned). See ``_compose_environment_mapping`` /
      PR #751 thread PRRT_kwDOSJAM6s6PY8zB.

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
    ``database`` are redacted too, and *all* distinct declared values are tracked
    (not only the first) so a profile running several DB sidecars with different
    passwords redacts each one's rendered DB URL independently. Each declared
    value is resolved against the worker env (mirroring Compose interpolation)
    so a password expressed via ``${POSTGRES_PASSWORD:-fallback}`` /
    ``${POSTGRES_PASSWORD}`` redacts the same concrete value the local container
    receives at stack launch. Agent env values containing any tracked password
    are skipped so the credential never reaches the hosted request object. A
    rendered DB URL percent-encodes the userinfo password (per RFC 3986), so a
    password with URL-reserved characters (e.g. ``p@ss/word``) appears in the
    URL as its encoded form (``p%40ss%2Fword``); the raw substring test alone
    would miss it, so each tracked password is also compared against its
    URL-encoded variant (``quote(..., safe="")``) so an encoded secret-bearing
    URL is redacted too (PR #751 thread PRRT_kwDOSJAM6s6PZuE5). When
    the compose file is unreadable the result is empty (fail-closed: no values),
    matching ``_compose_env_passthrough_exclusions``.

    ``compose_env`` lets a caller that already parsed the compose agent
    environment (e.g. ``_run_hosted`` computing the passthrough filter from the
    same parse) reuse the result and avoid a second read/parse of the file. A
    caller that supplies ``compose_env`` is responsible for the same redaction
    context (it already has the file open): such a caller must perform its own
    postgres-password collection and pass the set via ``postgres_passwords``;
    otherwise no DB-credential redaction is applied for the pre-parsed path.
    ``worker_env`` lets a caller supply a deterministic worker environment for
    interpolation (default ``os.environ``); mirroring
    ``agent_environment_with_*`` helpers.
    """
    env = os.environ if worker_env is None else worker_env
    if compose_env is None:
        compose_env, file_postgres_passwords = _try_compose_agent_env_and_postgres_passwords(
            compose_file,
            worker_env=env,
        )
    else:
        file_postgres_passwords = frozenset()
    if compose_env is None:
        return ()
    # Redact agent env values that embed any rendered postgres password so the
    # generated workspace DB credential(s) never reach the hosted executor.
    # ``file_postgres_passwords`` is populated only when this call parsed the
    # compose file itself; a caller that supplied ``compose_env`` from a prior
    # parse must pass ``postgres_passwords`` explicitly for the same redaction.
    # Failing to locate a postgres password (no service declaring
    # ``POSTGRES_PASSWORD``) redacts nothing, preserving carry for profiles
    # without a DB sidecar.
    postgres_passwords = file_postgres_passwords | (postgres_passwords or frozenset())
    carried: list[tuple[str, str]] = []
    for key, raw in compose_env.items():
        # A Compose pass-through slot (``environment: [NAME]``, ``NAME:`` /
        # ``NAME: null``) is normalized to the :data:`_COMPOSE_PASSTHROUGH`
        # sentinel; Docker Compose takes its value from the worker shell at
        # stack launch, exactly like a bare ``${NAME}`` reference. Carrying an
        # empty literal would override the real worker value in the hosted
        # request, so the slot is skipped here and kept in
        # ``env_passthrough_names`` for hosted out-of-band resolution (see
        # ``filter_hosted_env_passthrough_names``).
        #
        # An *explicit* empty value (``NAME: ""`` / ``NAME=``) is normalized to
        # the plain string ``""`` (NOT the sentinel): Docker Compose sets it as a
        # non-nil empty literal that OVERRIDES the worker shell value, so the
        # local container received an explicit blank, not the worker value. It
        # flows through ``_compose_resolve_value("")`` -> ``("", LITERAL)`` and is
        # carried as a literal ``""`` so the hosted job mirrors the local
        # container instead of inheriting a worker value it never had. A literal
        # empty resolved from an interpolation default (e.g. ``${MISSING:-}`` with
        # ``MISSING`` unset) has a non-empty raw form and is likewise carried as
        # ``LITERAL``, matching the local container.
        if raw == _COMPOSE_PASSTHROUGH:
            continue
        expanded, resolution = _compose_resolve_value(raw, worker_env=env)
        if resolution is not _ComposeEnvResolution.LITERAL:
            continue
        if _expanded_value_bears_postgres_password(expanded, postgres_passwords):
            continue
        carried.append((key, expanded))
    return tuple(carried)
