"""Clarification-auth mount selection helpers for the stack launcher."""

from __future__ import annotations

import json
import posixpath
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from awf.db.enums import AgentRuntime
from awf.node.compose_manager import AuthMount

_AGENT_HOME = "/home/agent"
_CLARIFICATION_AUTH_STAGING_ROOT = "/home/agent/.awf/clarification-auth"


def staged_provider_auth_mounts(
    provider_auth_mounts: Sequence[AuthMount],
) -> tuple[AuthMount, ...]:
    """Stage provider auth at destinations writable by the clarification agent."""

    return tuple(
        replace(
            mount,
            mode="ro",
            target=clarification_auth_target(mount.target, index=index),
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


def external_account_subject_token_file(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    provider_environment_names: frozenset[str],
    mirror_target: str,
) -> str | None:
    """Return the subject-token file named by a selected external-account ADC."""

    google_credentials = dict(agent_environment).get("GOOGLE_APPLICATION_CREDENTIALS")
    if "GOOGLE_APPLICATION_CREDENTIALS" not in provider_environment_names or not google_credentials:
        return None
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
        return None
    adc_source = mounted_file_source(adc_mount, google_credentials)
    if adc_source is None:
        return None
    try:
        adc_configuration = json.loads(adc_source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(adc_configuration, dict)
        or adc_configuration.get("type") != "external_account"
    ):
        return None
    credential_source = adc_configuration.get("credential_source")
    if not isinstance(credential_source, dict):
        return None
    subject_token_file = credential_source.get("file")
    if not isinstance(subject_token_file, str) or not subject_token_file.startswith("/"):
        return None
    return posixpath.normpath(subject_token_file)


def external_account_subject_token_mounts(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    provider_environment_names: frozenset[str],
    mirror_target: str,
) -> tuple[AuthMount, ...]:
    """Return declared mounts needed by a selected external-account ADC file."""

    subject_token_file = external_account_subject_token_file(
        auth_mounts,
        agent_environment=agent_environment,
        provider_environment_names=provider_environment_names,
        mirror_target=mirror_target,
    )
    if subject_token_file is None:
        return ()
    return tuple(
        mount
        for mount in auth_mounts
        if mount.target != mirror_target
        and (mount.target == subject_token_file or path_is_below(subject_token_file, mount.target))
    )


def external_account_subject_token_file_rewrites(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    mirror_target: str,
    agent_runtime: AgentRuntime,
    agent_model: str | None = None,
) -> tuple[tuple[str, str], ...]:
    """Map a selected external-account subject-token file to its staged copy."""

    # Import lazily because the stack launcher uses these helpers while it is
    # importing; by invocation time its shared selection helpers are defined.
    from awf.node.stack_launcher import (
        _clarification_model_provider_environment_names,
        _clarification_provider_auth_mounts,
        _clarification_resolve_aws_web_identity_token_file_placeholder,
        _clarification_resolve_google_credentials_placeholder,
    )

    agent_environment = _clarification_resolve_google_credentials_placeholder(
        agent_environment,
        auth_mounts=auth_mounts,
        agent_runtime=agent_runtime,
    )
    agent_environment = _clarification_resolve_aws_web_identity_token_file_placeholder(
        agent_environment,
        auth_mounts=auth_mounts,
        agent_runtime=agent_runtime,
    )
    provider_environment_names = _clarification_model_provider_environment_names(
        agent_environment,
        agent_runtime=agent_runtime,
        agent_model=agent_model,
    )
    subject_token_file = external_account_subject_token_file(
        auth_mounts,
        agent_environment=agent_environment,
        mirror_target=mirror_target,
        provider_environment_names=provider_environment_names,
    )
    if subject_token_file is None:
        return ()
    provider_auth_mounts = _clarification_provider_auth_mounts(
        auth_mounts,
        agent_environment=agent_environment,
        mirror_target=mirror_target,
        agent_runtime=agent_runtime,
        agent_model=agent_model,
        provider_environment_names=provider_environment_names,
    )
    staged_mounts = staged_provider_auth_mounts(provider_auth_mounts)
    staged_subject_token_file = staged_auth_value(
        subject_token_file,
        tuple(
            (source.target, staged.target)
            for source, staged in zip(provider_auth_mounts, staged_mounts, strict=True)
        ),
    )
    if staged_subject_token_file == subject_token_file:
        return ()
    return ((subject_token_file, staged_subject_token_file),)


def legacy_clarification_entrypoint(
    mount_count: int,
    *,
    rewrite_external_account_subject_token_file: bool = False,
) -> list[str]:
    """Copy staged auth and optionally rewrite an external-account ADC file."""

    lines: list[str] = []
    for index in range(mount_count):
        target = f"$AWF_CLARIFICATION_AUTH_TARGET_{index}"
        source = f"/run/awf/clarification-auth/{index}"
        lines.extend(
            (
                f'mkdir -p "$(dirname "{target}")"',
                f"if [ -d {source} ]; then",
                f'  mkdir -p "{target}"',
                f'  cp -a {source}/. "{target}/"',
                "else",
                f'  cp -a {source} "{target}"',
                "fi",
            )
        )
    if rewrite_external_account_subject_token_file:
        lines.extend(
            (
                'chmod u+w "$GOOGLE_APPLICATION_CREDENTIALS"',
                "python - <<'PY'",
                "import json",
                "import os",
                "from pathlib import Path",
                "",
                'credentials_path = Path(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])',
                "rewrites = dict(",
                "    json.loads(",
                '        os.environ["AWF_CLARIFICATION_EXTERNAL_ACCOUNT_SUBJECT_TOKEN_FILE_REWRITES"]',
                "    )",
                ")",
                'configuration = json.loads(credentials_path.read_text(encoding="utf-8"))',
                'if isinstance(configuration, dict) and configuration.get("type") == "external_account":',
                '    credential_source = configuration.get("credential_source")',
                "    if isinstance(credential_source, dict):",
                '        subject_token_file = credential_source.get("file")',
                "        if subject_token_file in rewrites:",
                '            credential_source["file"] = rewrites[subject_token_file]',
                '            credentials_path.write_text(json.dumps(configuration), encoding="utf-8")',
                "PY",
            )
        )
    lines.append('exec "$@"')
    return ["sh", "-ec", "\n".join(lines), "--"]


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
