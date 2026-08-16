"""Hosted render-only secret lease helpers for workspace stack launch."""

from __future__ import annotations

import re
from collections.abc import Sequence

from awf.common.git_auth import apply_bitbucket_agent_git_auth
from awf.node.compose_manager import AuthMount
from awf.node.secret_mounts import (
    SECRET_LEASE_PROVIDER_UNSUPPORTED,
    SECRET_LEASE_SOURCE_INVALID,
    SECRET_LEASE_TARGET_KIND_MISMATCH,
    SECRET_LEASE_TARGET_MISMATCH,
    LocalSecretLeaseResolution,
    SecretLeaseResolutionError,
)
from awf.node.stack_launcher_hosted_auth_helpers import (
    hosted_google_application_credentials_target as _hosted_google_application_credentials_target,
)
from awf.profiles.compose_env import hosted_env_secret_alias_placeholder
from awf.profiles.models import ProfileSecret, WorkspaceProfile

_HOSTED_RENDER_ENV_SECRET_PROVIDERS = frozenset(("env", "github", "bitbucket"))
_HOSTED_RENDER_MOUNT_SECRET_PROVIDERS = frozenset(("local-file", "host-file", "local-auth", "auth"))
_HOSTED_RENDER_SECRET_PROVIDERS = (
    _HOSTED_RENDER_ENV_SECRET_PROVIDERS | _HOSTED_RENDER_MOUNT_SECRET_PROVIDERS
)
_HOSTED_RENDER_ENV_REF_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HOSTED_GITHUB_ENV_SOURCE_NAME = "AWF_GITHUB_TOKEN"
_HOSTED_GITHUB_ENV_TARGET_NAMES = ("GH_TOKEN", "GITHUB_TOKEN")
_HOSTED_BITBUCKET_ENV_TARGET_SOURCE_NAMES = {
    "BITBUCKET_API_TOKEN": "BITBUCKET_API_TOKEN",
    "BITBUCKET_EMAIL": "BITBUCKET_EMAIL",
}
_HOSTED_BITBUCKET_ASKPASS_TARGET = "/run/awf/secrets/bb-askpass.sh"
_HOSTED_AUTH_PLACEHOLDER_SOURCE_ROOT = "/run/awf/hosted-auth-placeholders"


def _stack_secret_metadata(
    *,
    secret_lease_resolution: LocalSecretLeaseResolution | None,
    companion_secret_metadata: dict[str, object],
) -> dict[str, object]:
    """Merge profile secret lease metadata with companion env secret metadata."""
    metadata: dict[str, object] = {}
    if secret_lease_resolution is not None:
        metadata.update(dict(secret_lease_resolution.metadata))
    metadata.update(companion_secret_metadata)
    return metadata


def _hosted_secret_lease_placeholder_resolution(
    profile: WorkspaceProfile,
) -> LocalSecretLeaseResolution | None:
    """Return secret-free lease names/targets for hosted render-only stacks."""
    if not profile.secrets:
        return None

    env: dict[str, str] = {}
    lease_env_count = 0
    providers: list[str] = []
    targets: list[str] = []
    mounts: list[AuthMount] = []
    satisfied_legacy_targets: set[str] = set()
    satisfied_legacy_providers: set[str] = set()
    profile_presets_git_askpass = "GIT_ASKPASS" in profile.runtime.environment
    bitbucket_git_token_rendered = False
    mount_count = 0
    skipped_unresolved_count = 0

    for secret in profile.secrets:
        provider = _hosted_secret_provider(secret.provider)
        if provider is None:
            skipped_unresolved_count += 1
            continue
        if provider not in _HOSTED_RENDER_SECRET_PROVIDERS:
            skipped_unresolved_count += _skip_or_raise_unrenderable_hosted_secret(
                secret,
                provider=provider,
                reason_code=SECRET_LEASE_PROVIDER_UNSUPPORTED,
            )
            continue
        if provider in _HOSTED_RENDER_ENV_SECRET_PROVIDERS:
            if secret.kind != "env":
                skipped_unresolved_count += _skip_or_raise_unrenderable_hosted_secret(
                    secret,
                    provider=provider,
                    reason_code=SECRET_LEASE_TARGET_KIND_MISMATCH,
                )
                continue
            pairs = _hosted_env_secret_alias_pairs(secret, provider=provider)
            if pairs is None:
                skipped_unresolved_count += _skip_or_raise_unrenderable_hosted_secret(
                    secret,
                    provider=provider,
                    reason_code=_hosted_env_secret_unrenderable_reason(provider),
                )
                continue
            for target, source_name in pairs:
                injected = target not in env
                if injected:
                    env[target] = hosted_env_secret_alias_placeholder(source_name)
                    lease_env_count += 1
                    if provider == "bitbucket" and target == "BITBUCKET_API_TOKEN":
                        bitbucket_git_token_rendered = True
                _append_unique_hosted_secret_value(targets, target)
            _append_unique_hosted_secret_value(providers, provider)
            if provider == "github":
                satisfied_legacy_providers.add(provider)
            continue
        if secret.kind != "mount":
            skipped_unresolved_count += _skip_or_raise_unrenderable_hosted_secret(
                secret,
                provider=provider,
                reason_code=SECRET_LEASE_TARGET_KIND_MISMATCH,
            )
            continue
        if not secret.target.startswith("/"):
            skipped_unresolved_count += _skip_or_raise_unrenderable_hosted_secret(
                secret,
                provider=provider,
                reason_code=SECRET_LEASE_TARGET_MISMATCH,
            )
            continue
        if provider in _HOSTED_RENDER_MOUNT_SECRET_PROVIDERS:
            mount_count += 1
            _append_unique_hosted_secret_value(providers, provider)
            _append_unique_hosted_secret_value(targets, secret.target)
            _append_hosted_auth_placeholder_mounts(mounts, (secret.target,))
            satisfied_legacy_targets.add(secret.target)
            continue

    if (
        bitbucket_git_token_rendered
        and "GIT_ASKPASS" not in env
        and not profile_presets_git_askpass
        and not any(mount.target == _HOSTED_BITBUCKET_ASKPASS_TARGET for mount in mounts)
    ):
        mount_count += 1
        _append_hosted_auth_placeholder_mounts(mounts, (_HOSTED_BITBUCKET_ASKPASS_TARGET,))
        apply_bitbucket_agent_git_auth(env, askpass_path=_HOSTED_BITBUCKET_ASKPASS_TARGET)

    if not env and mount_count == 0 and not skipped_unresolved_count:
        return None

    metadata: dict[str, object] = {
        "schema": "secret_lease_mount_metadata.v1",
        "mount_plan": "profile_declared_secret_leases",
        "env_count": lease_env_count,
        "mount_count": mount_count,
        "providers": providers,
        "targets": targets,
    }
    if len(env) != lease_env_count:
        metadata["total_env_count"] = len(env)
    if skipped_unresolved_count:
        metadata["skipped_unresolved_count"] = skipped_unresolved_count
    return LocalSecretLeaseResolution(
        environment=tuple(env.items()),
        mounts=tuple(mounts),
        metadata=metadata,
        satisfied_legacy_targets=frozenset(satisfied_legacy_targets),
        satisfied_legacy_providers=frozenset(satisfied_legacy_providers),
    )


def _skip_or_raise_unrenderable_hosted_secret(
    secret: ProfileSecret,
    *,
    provider: str,
    reason_code: str,
) -> int:
    if secret.required:
        raise SecretLeaseResolutionError(
            reason_code=reason_code,
            secret_name=secret.name,
            provider=provider,
            target=secret.target,
            kind=secret.kind,
        )
    return 1


def _hosted_env_secret_unrenderable_reason(provider: str) -> str:
    if provider == "env":
        return SECRET_LEASE_SOURCE_INVALID
    return SECRET_LEASE_TARGET_MISMATCH


def _hosted_secret_provider(provider: str | None) -> str | None:
    if provider is None:
        return None
    normalized = provider.strip().lower()
    return normalized or None


def _hosted_env_secret_alias_pairs(
    secret: ProfileSecret,
    *,
    provider: str,
) -> tuple[tuple[str, str], ...] | None:
    """Return hosted target/source aliases matching local provider lease rules."""
    if provider == "env":
        source_name = _hosted_env_secret_source_name(secret.ref)
        if source_name is None:
            return None
        return ((secret.target, source_name),)
    if provider == "github":
        if secret.target not in _HOSTED_GITHUB_ENV_TARGET_NAMES:
            return None
        return tuple(
            (target, _HOSTED_GITHUB_ENV_SOURCE_NAME) for target in _HOSTED_GITHUB_ENV_TARGET_NAMES
        )
    if provider == "bitbucket":
        source_name = _HOSTED_BITBUCKET_ENV_TARGET_SOURCE_NAMES.get(secret.target)
        if source_name is None:
            return None
        return ((secret.target, source_name),)
    return None


def _hosted_env_secret_source_name(ref: str | None) -> str | None:
    raw = (ref or "").strip()
    if raw.startswith("env/"):
        raw = raw[len("env/") :]
    candidate = raw
    if not _HOSTED_RENDER_ENV_REF_RE.fullmatch(candidate):
        return None
    return candidate


def _append_unique_hosted_secret_value(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _append_hosted_auth_placeholder_mounts(
    mounts: list[AuthMount],
    targets: Sequence[str],
    *,
    suppressed_targets: frozenset[str] = frozenset(),
) -> None:
    seen = {mount.target for mount in mounts}
    for target in targets:
        if target in seen or target in suppressed_targets or not target.startswith("/"):
            continue
        mounts.append(
            AuthMount(
                source=_hosted_auth_placeholder_source(target),
                target=target,
                mode="ro",
            )
        )
        seen.add(target)


def _hosted_auth_placeholder_source(target: str) -> str:
    name = target.strip("/").replace("/", "__") or "root"
    return f"{_HOSTED_AUTH_PLACEHOLDER_SOURCE_ROOT}/{name}"


def _hosted_dynamic_file_auth_mount_targets(
    agent_environment: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    google_credentials_target = _hosted_google_application_credentials_target(agent_environment)
    if google_credentials_target is None:
        return ()
    return (google_credentials_target,)
