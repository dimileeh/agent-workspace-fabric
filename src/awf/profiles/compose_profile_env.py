"""Hosted profile-env extraction from rendered Compose files."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from awf.profiles.compose_env import (
    _COMPOSE_PASSTHROUGH,
    _compose_resolve_value,
    _ComposeEnvResolution,
    _expanded_value_bears_postgres_password,
)
from awf.profiles.compose_postgres_env import try_compose_agent_env_and_postgres_passwords

_LOCAL_POSTGRES_DATABASE_URL_ENV_NAMES = frozenset(
    {"DATABASE_URL", "AWF_DATABASE_URL", "AWF_TEST_DATABASE_URL"}
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
        expanded, resolution = _compose_resolve_value(raw, worker_env=env)
        if resolution is not _ComposeEnvResolution.LITERAL:
            continue
        if _expanded_value_bears_postgres_password(expanded, postgres_passwords):
            continue
        if compose_module._value_has_url_userinfo(
            expanded
        ) and not _local_postgres_database_url_without_tracked_password(
            key,
            expanded,
            postgres_passwords=postgres_passwords,
        ):
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
        if key in compose_module._HOSTED_NAME_ONLY_CREDENTIAL_IDENTIFIER_ENV_VARS:
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
    postgres_passwords: frozenset[str],
) -> bool:
    """Return whether a local Postgres URL should survive generic userinfo redaction."""
    if (
        _has_concrete_postgres_password(postgres_passwords)
        or key.upper() not in _LOCAL_POSTGRES_DATABASE_URL_ENV_NAMES
    ):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        password = parsed.password
    except ValueError:
        return False
    return (
        (parsed.scheme == "postgresql" or parsed.scheme.startswith("postgresql+"))
        and hostname == "postgres"
        and password is None
    )


def _has_concrete_postgres_password(postgres_passwords: frozenset[str]) -> bool:
    """Return whether the redaction set contains a concrete password value."""
    return any(password and "$" not in password for password in postgres_passwords)
