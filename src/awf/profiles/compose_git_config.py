"""Git config environment helpers for compose profiles."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from awf.profiles.compose_env import (
    _COMPOSE_PASSTHROUGH,
    _compose_bare_reference_name,
    _compose_empty_setness_reference_name,
    _compose_resolve_value,
    _compose_selected_worker_reference_name,
    _ComposeEnvResolution,
)

_GIT_CONFIG_COUNT_KEY = "GIT_CONFIG_COUNT"
_GIT_CONFIG_KEY_PREFIX = "GIT_CONFIG_KEY_"
_GIT_CONFIG_VALUE_PREFIX = "GIT_CONFIG_VALUE_"
_GIT_ASKPASS_KEY = "GIT_ASKPASS"
_GIT_TERMINAL_PROMPT_KEY = "GIT_TERMINAL_PROMPT"
_BITBUCKET_ASKPASS_TARGET = "/run/awf/secrets/bb-askpass.sh"
_BITBUCKET_AGENT_INSTEADOF_KEY = "url.https://x-bitbucket-api-token-auth@bitbucket.org/.insteadOf"
_GIT_CONFIG_URL_KEY_PREFIX = "url."
_GIT_CONFIG_INSTEADOF_KEY_SUFFIX = ".insteadOf"
_BITBUCKET_AGENT_SAFE_INSTEADOF_VALUES = frozenset(
    {
        "https://bitbucket.org/",
        "https://bitbucket.org:443/",
        "git@bitbucket.org:",
        "ssh://git@bitbucket.org/",
        "ssh://git@bitbucket.org:22/",
    }
)


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


def _is_safe_bitbucket_agent_insteadof_value(config_key: str, config_value: str) -> bool:
    return (
        config_key == _BITBUCKET_AGENT_INSTEADOF_KEY
        and config_value in _BITBUCKET_AGENT_SAFE_INSTEADOF_VALUES
    )


def _is_safe_ssh_git_config_insteadof_key(config_key: str) -> bool:
    if not (
        config_key.startswith(_GIT_CONFIG_URL_KEY_PREFIX)
        and config_key.endswith(_GIT_CONFIG_INSTEADOF_KEY_SUFFIX)
    ):
        return False
    config_url = config_key[
        len(_GIT_CONFIG_URL_KEY_PREFIX) : -len(_GIT_CONFIG_INSTEADOF_KEY_SUFFIX)
    ]
    try:
        parsed = urlsplit(config_url)
    except ValueError:
        return False
    if (
        parsed.scheme.lower() not in {"ssh", "git+ssh"}
        or parsed.username != "git"
        or parsed.password is not None
        or not parsed.hostname
    ):
        return False
    from awf.profiles import compose as compose_module

    return not any(
        compose_module._url_component_has_secret_credential_field(component)
        for component in (parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )


def _has_mount_backed_bitbucket_askpass(
    compose_env: Mapping[str, str],
    *,
    worker_env: Mapping[str, str],
) -> bool:
    raw = compose_env.get(_GIT_ASKPASS_KEY)
    if raw is None or raw == _COMPOSE_PASSTHROUGH:
        return False
    expanded, resolution = _compose_resolve_value(raw, worker_env=worker_env)
    return resolution is _ComposeEnvResolution.LITERAL and expanded == _BITBUCKET_ASKPASS_TARGET


def _hosted_git_config_profile_env(
    compose_env: Mapping[str, str],
    *,
    worker_env: Mapping[str, str],
    skip_bitbucket_agent_rewrites: bool,
) -> tuple[tuple[str, str], ...]:
    profile_env, _aliases = _hosted_git_config_env(
        compose_env,
        worker_env=worker_env,
        skip_bitbucket_agent_rewrites=skip_bitbucket_agent_rewrites,
    )
    return profile_env


def _hosted_git_config_passthrough_aliases(
    compose_env: Mapping[str, str],
    *,
    worker_env: Mapping[str, str],
    skip_bitbucket_agent_rewrites: bool,
) -> tuple[tuple[str, str], ...]:
    _profile_env, aliases = _hosted_git_config_env(
        compose_env,
        worker_env=worker_env,
        skip_bitbucket_agent_rewrites=skip_bitbucket_agent_rewrites,
    )
    return aliases


def _hosted_git_config_env(
    compose_env: Mapping[str, str],
    *,
    worker_env: Mapping[str, str],
    skip_bitbucket_agent_rewrites: bool,
) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    count_raw = compose_env.get(_GIT_CONFIG_COUNT_KEY)
    if count_raw is None or count_raw == _COMPOSE_PASSTHROUGH:
        return (), ()
    count_value, count_resolution = _compose_resolve_value(count_raw, worker_env=worker_env)
    if count_resolution is not _ComposeEnvResolution.LITERAL:
        return (), ()
    try:
        count = int(count_value)
    except ValueError:
        return (), ()

    from awf.profiles import compose as compose_module

    carried_entries: list[tuple[str, str | None, str | None]] = []
    for index in range(count):
        config_key_raw = compose_env.get(f"{_GIT_CONFIG_KEY_PREFIX}{index}")
        config_value_raw = compose_env.get(f"{_GIT_CONFIG_VALUE_PREFIX}{index}")
        if (
            config_key_raw is None
            or config_value_raw is None
            or config_key_raw == _COMPOSE_PASSTHROUGH
            or config_value_raw == _COMPOSE_PASSTHROUGH
        ):
            continue
        config_key, key_resolution = _compose_resolve_value(
            config_key_raw,
            worker_env=worker_env,
        )
        config_value, value_resolution = _compose_resolve_value(
            config_value_raw,
            worker_env=worker_env,
        )
        if key_resolution is not _ComposeEnvResolution.LITERAL:
            continue
        if (
            config_key != _BITBUCKET_AGENT_INSTEADOF_KEY
            and compose_module._value_has_url_userinfo(config_key)
            and not _is_safe_ssh_git_config_insteadof_key(config_key)
        ):
            continue
        if skip_bitbucket_agent_rewrites and config_key == _BITBUCKET_AGENT_INSTEADOF_KEY:
            continue
        if value_resolution is _ComposeEnvResolution.LITERAL:
            if config_value == "" and _compose_bare_reference_name(config_value_raw) is not None:
                continue
            if not _is_safe_bitbucket_agent_insteadof_value(
                config_key,
                config_value,
            ) and (
                compose_module._value_has_url_userinfo(config_value)
                or compose_module._is_auth_credential_like_profile_env_value(config_value)
            ):
                continue
            carried_entries.append((config_key, config_value, None))
            continue
        if (
            _compose_empty_setness_reference_name(config_value_raw, worker_env=worker_env)
            is not None
        ):
            carried_entries.append((config_key, "", None))
            continue
        value_source = _hosted_git_config_value_alias_source(
            config_value_raw,
            value_resolution=value_resolution,
            worker_env=worker_env,
        )
        if value_source is None:
            continue
        carried_entries.append((config_key, None, value_source))

    if not carried_entries:
        return (), ()

    pairs: list[tuple[str, str]] = []
    aliases: list[tuple[str, str]] = []
    for index, (config_key, entry_config_value, entry_value_source) in enumerate(carried_entries):
        pairs.append((f"{_GIT_CONFIG_KEY_PREFIX}{index}", config_key))
        value_key = f"{_GIT_CONFIG_VALUE_PREFIX}{index}"
        if entry_value_source is None:
            assert entry_config_value is not None
            pairs.append((value_key, entry_config_value))
        else:
            aliases.append((value_key, entry_value_source))
    pairs.append((_GIT_CONFIG_COUNT_KEY, str(len(carried_entries))))
    return tuple(pairs), tuple(aliases)


def _hosted_git_config_value_alias_source(
    raw: str,
    *,
    value_resolution: _ComposeEnvResolution,
    worker_env: Mapping[str, str],
) -> str | None:
    if value_resolution in (
        _ComposeEnvResolution.WORKER_RESOLVED_SLOT,
        _ComposeEnvResolution.WORKER_RESOLVED_DEFAULTED,
    ):
        source_name = _compose_selected_worker_reference_name(raw, worker_env=worker_env)
    else:
        return None
    if _compose_empty_setness_reference_name(raw, worker_env=worker_env) is not None:
        return None
    if source_name is None or source_name not in worker_env:
        return None
    return source_name
