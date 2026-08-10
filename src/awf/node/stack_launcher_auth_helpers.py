"""Clarification-auth mount selection helpers for the stack launcher."""

from __future__ import annotations

import configparser
import json
import posixpath
import shlex
from collections.abc import Sequence
from contextlib import suppress
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
    aws_profile_credential_mounts: Sequence[AuthMount],
    mirror_target: str,
) -> tuple[AuthMount, ...]:
    """Return provider mounts and declared transitive credential sources."""

    return tuple(
        mount
        for mount in auth_mounts
        if mount.target != mirror_target
        and (
            mount in external_account_subject_token_mounts
            or mount in aws_profile_credential_mounts
            or any(
                mount.target == provider_target or path_is_below(provider_target, mount.target)
                for provider_target in provider_mount_targets
            )
        )
    )


def aws_profile_credential_paths(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    provider_mount_targets: frozenset[str],
    mirror_target: str,
) -> tuple[str, ...]:
    """Return declared paths referenced by the active AWS profile files."""

    profile_name = dict(agent_environment).get("AWS_PROFILE") or "default"
    references: list[str] = []
    configurations: list[configparser.RawConfigParser] = []
    for profile_file in _aws_profile_file_targets(provider_mount_targets):
        source = _mounted_file_source(
            auth_mounts,
            target=profile_file,
            mirror_target=mirror_target,
        )
        if source is None:
            continue
        try:
            configuration = configparser.RawConfigParser(interpolation=None)
            with source.open(encoding="utf-8") as profile_source:
                configuration.read_file(profile_source)
        except (OSError, UnicodeDecodeError, configparser.Error):
            continue
        configurations.append(configuration)

    pending_profile_names = {profile_name}
    processed_profile_names: set[str] = set()
    while pending_profile_names:
        current_profile_name = pending_profile_names.pop()
        processed_profile_names.add(current_profile_name)
        for configuration in configurations:
            section = next(
                (
                    candidate
                    for candidate in (f"profile {current_profile_name}", current_profile_name)
                    if configuration.has_section(candidate)
                ),
                None,
            )
            if section is None:
                continue
            credential_process = configuration.get(section, "credential_process", fallback=None)
            if isinstance(credential_process, str):
                with suppress(ValueError):
                    references.extend(
                        value for value in shlex.split(credential_process) if value.startswith("/")
                    )
            web_identity_token_file = configuration.get(
                section, "web_identity_token_file", fallback=None
            )
            if isinstance(web_identity_token_file, str) and web_identity_token_file.startswith("/"):
                references.append(web_identity_token_file)
            source_profile = configuration.get(section, "source_profile", fallback=None)
            if source_profile:
                pending_profile_names.update({source_profile} - processed_profile_names)
    return tuple(
        reference
        for index, reference in enumerate(references)
        if reference not in references[:index]
        and any(
            mount.target != mirror_target
            and (
                mount.target == posixpath.normpath(reference)
                or path_is_below(reference, mount.target)
            )
            for mount in auth_mounts
        )
    )


def aws_profile_credential_mounts(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    provider_mount_targets: frozenset[str],
    mirror_target: str,
) -> tuple[AuthMount, ...]:
    """Return declared mounts named by the active AWS profile configuration."""

    credential_paths = aws_profile_credential_paths(
        auth_mounts,
        agent_environment=agent_environment,
        provider_mount_targets=provider_mount_targets,
        mirror_target=mirror_target,
    )
    return tuple(
        mount
        for mount in auth_mounts
        if mount.target != mirror_target
        and any(
            mount.target == posixpath.normpath(path) or path_is_below(path, mount.target)
            for path in credential_paths
        )
    )


def _aws_profile_file_targets(provider_mount_targets: frozenset[str]) -> tuple[str, ...]:
    """Return AWS configuration files selected from provider mount targets."""

    profile_files: list[str] = []
    for target in sorted(provider_mount_targets):
        normalized_target = posixpath.normpath(target)
        if not normalized_target.startswith("/"):
            continue
        if normalized_target == f"{_AGENT_HOME}/.aws":
            profile_files.extend(
                (f"{normalized_target}/config", f"{normalized_target}/credentials")
            )
        else:
            profile_files.append(normalized_target)
    return tuple(profile_files)


def _mounted_file_source(
    auth_mounts: Sequence[AuthMount],
    *,
    target: str,
    mirror_target: str,
) -> Path | None:
    """Return the source for a target from its most-specific declared mount."""

    matching_mounts = tuple(
        mount
        for mount in auth_mounts
        if mount.target != mirror_target
        and (mount.target == target or path_is_below(target, mount.target))
    )
    if not matching_mounts:
        return None
    mount = max(matching_mounts, key=lambda candidate: len(posixpath.normpath(candidate.target)))
    return mounted_file_source(mount, target)


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


def aws_profile_path_rewrites(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    mirror_target: str,
    agent_runtime: AgentRuntime,
    agent_model: str | None = None,
) -> tuple[tuple[str, str], ...]:
    """Map active AWS profile references to the corresponding staged copies."""

    # Import lazily because the stack launcher uses these helpers while it is
    # importing; by invocation time its shared selection helpers are defined.
    from awf.node.stack_launcher import (
        _clarification_aws_profile_mount_targets,
        _clarification_model_provider_auth_mount_targets,
        _clarification_model_provider_environment_names,
        _clarification_provider_auth_mounts,
        _clarification_resolve_aws_web_identity_token_file_placeholder,
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
    provider_mount_targets = _clarification_model_provider_auth_mount_targets(
        agent_environment,
        agent_runtime=agent_runtime,
        agent_model=agent_model,
        provider_environment_names=provider_environment_names,
    )
    credential_paths = aws_profile_credential_paths(
        auth_mounts,
        agent_environment=agent_environment,
        provider_mount_targets=_clarification_aws_profile_mount_targets(
            agent_environment,
            provider_mount_targets=provider_mount_targets,
        ),
        mirror_target=mirror_target,
    )
    if not credential_paths:
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
    staged_targets = tuple(
        (source.target, staged.target)
        for source, staged in zip(provider_auth_mounts, staged_mounts, strict=True)
    )
    return tuple(
        (path, staged_path)
        for path in credential_paths
        if (staged_path := staged_auth_value(path, staged_targets)) != path
    )


def legacy_clarification_entrypoint(
    mount_count: int,
    *,
    rewrite_external_account_subject_token_file: bool = False,
    rewrite_aws_profile_paths: bool = False,
) -> list[str]:
    """Copy staged auth and rewrite credential-file paths when required."""

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
                "import posixpath",
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
                "        if isinstance(subject_token_file, str):",
                "            normalized_subject_token_file = posixpath.normpath(subject_token_file)",
                "            if normalized_subject_token_file in rewrites:",
                '                credential_source["file"] = rewrites[normalized_subject_token_file]',
                '            credentials_path.write_text(json.dumps(configuration), encoding="utf-8")',
                "PY",
            )
        )
    if rewrite_aws_profile_paths:
        lines.extend(
            (
                "python - <<'PY'",
                "import json",
                "import os",
                "from pathlib import Path",
                "",
                "rewrites = dict(json.loads(os.environ['AWF_CLARIFICATION_AWS_PROFILE_PATH_REWRITES']))",
                "profile_paths = (",
                "    Path(os.environ.get('AWS_CONFIG_FILE', '/home/agent/.aws/config')),",
                "    Path(os.environ.get('AWS_SHARED_CREDENTIALS_FILE', '/home/agent/.aws/credentials')),",
                ")",
                "for profile_path in profile_paths:",
                "    try:",
                "        original_configuration = profile_path.read_text(encoding='utf-8')",
                "    except (OSError, UnicodeDecodeError):",
                "        continue",
                "    configuration = original_configuration",
                "    for source, staged in sorted(rewrites.items(), key=lambda item: len(item[0]), reverse=True):",
                "        configuration = configuration.replace(source, staged)",
                "    if configuration != original_configuration:",
                "        profile_path.chmod(profile_path.stat().st_mode | 0o200)",
                "        profile_path.write_text(configuration, encoding='utf-8')",
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
