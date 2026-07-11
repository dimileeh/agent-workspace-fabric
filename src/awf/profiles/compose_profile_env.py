"""Hosted profile-env extraction from rendered Compose files."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import unquote, urlsplit

from awf.profiles.compose_env import (
    _COMPOSE_PASSTHROUGH,
    _compose_empty_setness_reference_name,
    _compose_resolve_value,
    _compose_unselected_alternate_worker_reference_name,
    _ComposeEnvResolution,
    _expanded_value_bears_postgres_password,
)
from awf.profiles.compose_postgres_env import (
    compose_postgres_service_hostnames,
    try_compose_agent_env_and_postgres_passwords,
)

_LOCAL_POSTGRES_DATABASE_URL_ENV_NAMES = frozenset(
    {
        "DATABASE_URI",
        "DATABASE_URL",
        "POSTGRES_URI",
        "POSTGRES_URL",
        "SQLALCHEMY_DATABASE_URI",
        "AWF_DATABASE_URL",
        "AWF_TEST_DATABASE_URL",
    }
)
_LOCAL_POSTGRES_DATABASE_URL_ENV_NAME_SUFFIXES = (
    "_DATABASE_URI",
    "_DATABASE_URL",
    "_POSTGRES_URI",
    "_POSTGRES_URL",
)
_LIBPQ_KEYWORD_FIELD_PATTERN = re.compile(r"(?<!\S)([A-Za-z_][A-Za-z0-9_]*)\s*=")
_LIBPQ_KEYWORD_DSN_CONTEXT_FIELD_NAMES = frozenset(
    {
        "APPLICATION_NAME",
        "CONNECT_TIMEOUT",
        "DBNAME",
        "HOST",
        "HOSTADDR",
        "OPTIONS",
        "PORT",
        "SERVICE",
        "SSLMODE",
        "TARGET_SESSION_ATTRS",
        "USER",
    }
)


def literal_profile_env_from_compose(
    compose_file: Path,
    *,
    compose_env: Mapping[str, str] | None = None,
    worker_env: Mapping[str, str] | None = None,
    postgres_passwords: frozenset[str] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return profile-owned env values the hosted executor must inject.

    Compose-owned literals are carried to hosted runs so they match the local
    container environment. Worker-resolved values and secret-bearing literals are
    skipped; DB URLs are skipped only when they bear a recovered Postgres
    password or an unsafe generic URL credential.
    """
    from awf.profiles import compose as compose_module

    env = os.environ if worker_env is None else worker_env
    if compose_env is None:
        compose_env, file_postgres_passwords = try_compose_agent_env_and_postgres_passwords(
            compose_file,
            worker_env=env,
        )
    else:
        file_postgres_passwords = frozenset()
    if compose_env is None:
        return ()

    local_postgres_hostnames = frozenset({"postgres"}) | compose_postgres_service_hostnames(
        compose_file, worker_env=env
    )
    postgres_passwords = file_postgres_passwords | (postgres_passwords or frozenset())
    auth_secret_keys = compose_module._AGENT_AUTH_SECRET_ENV_VARS & compose_env.keys()
    mount_backed_bitbucket_askpass = compose_module._has_mount_backed_bitbucket_askpass(
        compose_env,
        worker_env=env,
    )
    hosted_git_config_profile_env = compose_module._hosted_git_config_profile_env(
        compose_env,
        worker_env=env,
        skip_bitbucket_agent_rewrites=mount_backed_bitbucket_askpass,
    )
    carried: list[tuple[str, str]] = []
    for key, raw in compose_env.items():
        if raw == _COMPOSE_PASSTHROUGH:
            continue
        if _compose_empty_setness_reference_name(raw, worker_env=env) == key:
            carried.append((key, ""))
            continue
        expanded, resolution = _compose_resolve_value(raw, worker_env=env)
        if resolution is not _ComposeEnvResolution.LITERAL:
            continue
        if key in compose_module._HOSTED_NAME_ONLY_CREDENTIAL_IDENTIFIER_ENV_VARS:
            continue
        if (
            expanded == ""
            and compose_module._is_secret_like_profile_env_name(key)
            and _compose_unselected_alternate_worker_reference_name(raw, worker_env=env) is not None
        ):
            continue
        if expanded == "" and (
            key in auth_secret_keys or compose_module._is_secret_like_profile_env_name(key)
        ):
            carried.append((key, expanded))
            continue
        if _expanded_value_bears_postgres_password(expanded, postgres_passwords):
            continue
        if compose_module._value_has_url_userinfo(
            expanded
        ) and not _local_postgres_database_url_without_tracked_password(
            key,
            expanded,
            local_postgres_hostnames=local_postgres_hostnames,
            postgres_passwords=postgres_passwords,
        ):
            continue
        if _expanded_value_has_libpq_keyword_dsn_secret_field(key, expanded):
            continue
        if (
            mount_backed_bitbucket_askpass
            and key == compose_module._GIT_ASKPASS_KEY
            and expanded == compose_module._BITBUCKET_ASKPASS_TARGET
        ):
            continue
        if compose_module._is_git_config_protocol_key(key):
            continue
        if key in auth_secret_keys:
            continue
        if compose_module._is_secret_like_profile_env_name(
            key
        ) or compose_module._is_auth_credential_like_profile_env_value(expanded):
            continue
        carried.append((key, expanded))
    carried.extend(hosted_git_config_profile_env)
    return tuple(carried)


def _local_postgres_database_url_without_tracked_password(
    key: str,
    value: str,
    *,
    local_postgres_hostnames: frozenset[str],
    postgres_passwords: frozenset[str],
) -> bool:
    """Return whether a local Postgres URL should survive generic userinfo redaction."""
    if not _is_local_postgres_database_url_env_name(key):
        return False
    if _expanded_value_bears_postgres_password(value, postgres_passwords):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        password = parsed.password
    except ValueError:
        return False
    from awf.profiles import compose as compose_module

    return (
        (parsed.scheme in {"postgres", "postgresql"} or parsed.scheme.startswith("postgresql+"))
        and hostname in local_postgres_hostnames
        and password is None
        and not any(
            compose_module._url_component_has_secret_credential_field(component)
            or compose_module._value_has_url_userinfo(component)
            for raw_component in (parsed.path, parsed.query, parsed.fragment)
            for component in _url_component_variants(raw_component)
        )
    )


def _url_component_variants(component: str) -> tuple[str, ...]:
    if not component:
        return ()
    decoded = unquote(component)
    if decoded == component:
        return (component,)
    return (component, decoded)


def _is_local_postgres_database_url_env_name(key: str) -> bool:
    normalized = key.upper().replace("-", "_")
    return normalized in _LOCAL_POSTGRES_DATABASE_URL_ENV_NAMES or normalized.endswith(
        _LOCAL_POSTGRES_DATABASE_URL_ENV_NAME_SUFFIXES
    )


def _expanded_value_has_libpq_keyword_dsn_secret_field(key: str, value: str) -> bool:
    fields = tuple(match.group(1) for match in _LIBPQ_KEYWORD_FIELD_PATTERN.finditer(value))
    if not fields:
        return False
    from awf.profiles import compose as compose_module

    has_secret_field = any(
        compose_module._url_field_name_has_secret_credential(field) for field in fields
    )
    if not has_secret_field:
        return False
    normalized_fields = {field.upper() for field in fields}
    return _is_local_postgres_database_url_env_name(key) or bool(
        normalized_fields & _LIBPQ_KEYWORD_DSN_CONTEXT_FIELD_NAMES
    )
