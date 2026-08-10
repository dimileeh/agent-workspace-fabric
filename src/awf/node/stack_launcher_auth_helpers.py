"""Clarification-auth mount selection helpers for the stack launcher."""

from __future__ import annotations

import json
import posixpath
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from awf.node.compose_manager import AuthMount

_AGENT_HOME = "/home/agent"
_CLARIFICATION_AUTH_STAGING_ROOT = "/home/agent/.awf/clarification-auth"


def staged_provider_auth_mounts(
    provider_auth_mounts: Sequence[AuthMount],
    *,
    preserved_targets: frozenset[str] = frozenset(),
) -> tuple[AuthMount, ...]:
    """Stage provider auth while retaining external-account token paths."""

    return tuple(
        replace(
            mount,
            mode="ro",
            target=(
                mount.target
                if mount.target in preserved_targets
                else clarification_auth_target(mount.target, index=index)
            ),
        )
        for index, mount in enumerate(provider_auth_mounts)
    )


def staged_auth_value(value: str, staged_targets: Sequence[tuple[str, str]]) -> str:
    """Rewrite a staged mount target or a credential file below that target."""

    for source_target, staged_target in staged_targets:
        if value == source_target:
            return staged_target
    normalized_value = posixpath.normpath(value)
    containing_targets = tuple(
        (posixpath.normpath(source_target), staged_target)
        for source_target, staged_target in staged_targets
        if path_is_below(normalized_value, source_target)
    )
    if not containing_targets:
        return value
    source_target, staged_target = max(containing_targets, key=lambda target: len(target[0]))
    return posixpath.join(staged_target, posixpath.relpath(normalized_value, source_target))


def path_is_below(path: str, target: str) -> bool:
    """Return whether an absolute normalized path is a child of a mount target."""

    normalized_path = posixpath.normpath(path)
    normalized_target = posixpath.normpath(target)
    return (
        normalized_path.startswith("/")
        and normalized_target.startswith("/")
        and normalized_path != normalized_target
        and (normalized_target == "/" or normalized_path.startswith(f"{normalized_target}/"))
    )


def provider_auth_mounts(
    auth_mounts: Sequence[AuthMount],
    *,
    provider_mount_targets: frozenset[str],
    external_account_subject_token_mounts: Sequence[AuthMount],
    mirror_target: str,
) -> tuple[AuthMount, ...]:
    """Return provider mounts and declared external-account token sources."""

    return tuple(
        mount
        for mount in auth_mounts
        if mount.target != mirror_target
        and (
            mount in external_account_subject_token_mounts
            or any(
                mount.target == provider_target or path_is_below(provider_target, mount.target)
                for provider_target in provider_mount_targets
            )
        )
    )


def external_account_subject_token_mounts(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    provider_environment_names: frozenset[str],
    mirror_target: str,
) -> tuple[AuthMount, ...]:
    """Return declared mounts needed by a selected external-account ADC file."""

    google_credentials = dict(agent_environment).get("GOOGLE_APPLICATION_CREDENTIALS")
    if "GOOGLE_APPLICATION_CREDENTIALS" not in provider_environment_names or not google_credentials:
        return ()
    adc_mount = next(
        (
            mount
            for mount in auth_mounts
            if mount.target != mirror_target
            and (
                mount.target == google_credentials
                or path_is_below(google_credentials, mount.target)
            )
        ),
        None,
    )
    if adc_mount is None:
        return ()
    adc_source = mounted_file_source(adc_mount, google_credentials)
    if adc_source is None:
        return ()
    try:
        adc_configuration = json.loads(adc_source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ()
    if (
        not isinstance(adc_configuration, dict)
        or adc_configuration.get("type") != "external_account"
    ):
        return ()
    credential_source = adc_configuration.get("credential_source")
    if not isinstance(credential_source, dict):
        return ()
    subject_token_file = credential_source.get("file")
    if not isinstance(subject_token_file, str) or not subject_token_file.startswith("/"):
        return ()
    normalized_subject_token_file = posixpath.normpath(subject_token_file)
    return tuple(
        mount
        for mount in auth_mounts
        if mount.target != mirror_target
        and (
            mount.target == normalized_subject_token_file
            or path_is_below(normalized_subject_token_file, mount.target)
        )
    )


def mounted_file_source(mount: AuthMount, target: str) -> Path | None:
    """Return the host path corresponding to an absolute file mount target."""

    normalized_target = posixpath.normpath(target)
    normalized_mount_target = posixpath.normpath(mount.target)
    if normalized_target == normalized_mount_target:
        return Path(mount.source)
    if not path_is_below(normalized_target, normalized_mount_target):
        return None
    return Path(mount.source) / posixpath.relpath(normalized_target, normalized_mount_target)


def clarification_auth_target(target: str, *, index: int) -> str:
    """Keep agent-home targets and stage all other paths under the agent home."""

    if target == _AGENT_HOME or target.startswith(f"{_AGENT_HOME}/"):
        return target
    return f"{_CLARIFICATION_AUTH_STAGING_ROOT}/{index}"
