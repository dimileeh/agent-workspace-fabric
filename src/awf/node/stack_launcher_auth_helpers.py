"""Clarification-auth mount selection helpers for the stack launcher."""

from __future__ import annotations

import base64
import binascii
import configparser
import json
import os
import posixpath
import re
import shlex
import stat
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Final

from awf.db.enums import AgentRuntime
from awf.node.compose_manager import AuthMount
from awf.service.environment import compose_expand_value

_AGENT_HOME = "/home/agent"
_CLARIFICATION_AUTH_STAGING_ROOT = "/home/agent/.awf/clarification-auth"
_CLARIFICATION_RUNTIME_AUTH_MOUNT_TARGETS: dict[AgentRuntime, frozenset[str]] = {
    AgentRuntime.codex: frozenset({"/home/agent/.codex"}),
    AgentRuntime.claude_code: frozenset({"/home/agent/.claude", "/home/agent/.claude.json"}),
    AgentRuntime.cursor: frozenset(),
    AgentRuntime.antigravity: frozenset(),
    AgentRuntime.gemini: frozenset({"/home/agent/.config/gcloud", "/home/agent/.gemini"}),
    AgentRuntime.opencode: frozenset(),
    AgentRuntime.grok: frozenset({"/home/agent/.grok"}),
}
_AWS_EXTERNAL_ACCOUNT_ENV_NAMES = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    }
)
_CREDENTIAL_PROCESS_ENVIRONMENT_REFERENCE_RE = re.compile(
    r"(?<!\\)\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?:(?::?[-=+?])(?P<fallback>[^}]*))?\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))"
)
_MAX_CLARIFICATION_AUTH_CREDENTIAL_BYTES: Final = 1024 * 1024


def staged_provider_auth_mounts(
    provider_auth_mounts: Sequence[AuthMount],
) -> tuple[AuthMount, ...]:
    """Stage provider auth so parent directories copy before nested files."""

    return tuple(
        replace(
            mount,
            mode="ro",
            target=clarification_auth_target(mount.target, index=index),
        )
        for index, mount in enumerate(_provider_auth_mount_staging_order(provider_auth_mounts))
    )


def _provider_auth_mount_staging_order(
    provider_auth_mounts: Sequence[AuthMount],
) -> tuple[AuthMount, ...]:
    """Order provider mounts so parent directories stage before nested files."""

    return tuple(
        sorted(
            provider_auth_mounts,
            key=lambda mount: posixpath.normpath(mount.target).count("/"),
        )
    )


def has_codex_file_auth(source_mounts: Sequence[AuthMount]) -> bool:
    """Return whether a staged Codex home supplies the file-auth credential."""

    for mount in source_mounts:
        if mount.target not in _CLARIFICATION_RUNTIME_AUTH_MOUNT_TARGETS[AgentRuntime.codex]:
            continue
        try:
            content = _read_bounded_clarification_auth_credential(mount.source, "auth.json")
            auth = json.loads(content) if content is not None else None
        except json.JSONDecodeError:
            continue
        if _has_codex_auth_credential(auth):
            return True
    return False


def _has_codex_auth_credential(auth: object) -> bool:
    """Return whether an auth.json payload contains a Codex credential."""

    if not isinstance(auth, dict):
        return False
    if isinstance(api_key := auth.get("OPENAI_API_KEY"), str) and api_key.strip():
        return True
    tokens = auth.get("tokens")
    return (
        isinstance(tokens, dict)
        and all(
            isinstance(value := tokens.get(name), str) and value.strip()
            for name in ("id_token", "access_token", "refresh_token")
        )
        and _has_codex_id_token(tokens["id_token"])
    )


def _has_codex_id_token(value: str) -> bool:
    """Return whether a token has the JWT payload Codex requires in auth.json."""

    parts = value.split(".")
    if len(parts) != 3 or not all(parts):
        return False
    try:
        encoded_payload = parts[1] + ("=" * (-len(parts[1]) % 4))
        payload = base64.b64decode(encoded_payload, altchars=b"-_", validate=True)
        return isinstance(json.loads(payload), dict)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return False


def _read_bounded_clarification_auth_credential(directory: str, filename: str) -> str | None:
    """Read one regular credential file without following writable path replacements."""
    relative_path = Path(filename)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or any(part == ".." for part in relative_path.parts)
    ):
        return None
    directory_fds: list[int] = []
    credential_fd: int | None = None
    try:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        directory_fds.append(directory_fd)
        for component in relative_path.parts[:-1]:
            directory_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            directory_fds.append(directory_fd)
        credential_fd = os.open(
            relative_path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        credential_stat = os.fstat(credential_fd)
        if (
            not stat.S_ISREG(credential_stat.st_mode)
            or credential_stat.st_size > _MAX_CLARIFICATION_AUTH_CREDENTIAL_BYTES
        ):
            return None
        content = bytearray()
        while len(content) <= _MAX_CLARIFICATION_AUTH_CREDENTIAL_BYTES:
            chunk = os.read(
                credential_fd,
                min(64 * 1024, _MAX_CLARIFICATION_AUTH_CREDENTIAL_BYTES + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > _MAX_CLARIFICATION_AUTH_CREDENTIAL_BYTES:
            return None
        return bytes(content).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        for fd in (credential_fd, *reversed(directory_fds)):
            if fd is not None:
                with suppress(OSError):
                    os.close(fd)


def has_claude_code_file_auth(source_mounts: Sequence[AuthMount]) -> bool:
    """Return whether a staged Claude home supplies its OAuth credential store."""

    for mount in source_mounts:
        if mount.target != "/home/agent/.claude":
            continue
        try:
            content = _read_bounded_clarification_auth_credential(mount.source, ".credentials.json")
            credentials = json.loads(content) if content is not None else None
        except json.JSONDecodeError:
            continue
        if _has_claude_code_auth_credential(credentials):
            return True
    return False


def _has_claude_code_auth_credential(credentials: object) -> bool:
    """Return whether a Claude credential payload contains an OAuth access token."""

    if not isinstance(credentials, dict):
        return False
    oauth = credentials.get("claudeAiOauth")
    return (
        isinstance(oauth, dict)
        and isinstance(access_token := oauth.get("accessToken"), str)
        and bool(access_token.strip())
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
    allowed_credential_process_environment_names: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Return declared paths referenced by the active AWS profile files."""

    return _aws_profile_credential_references(
        auth_mounts,
        agent_environment=agent_environment,
        provider_mount_targets=provider_mount_targets,
        mirror_target=mirror_target,
        allowed_credential_process_environment_names=allowed_credential_process_environment_names,
    )[0]


def aws_profile_credential_environment_names(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    provider_mount_targets: frozenset[str],
    mirror_target: str,
) -> frozenset[str]:
    """Return environment inputs referenced by active AWS credential processes."""

    return _aws_profile_credential_references(
        auth_mounts,
        agent_environment=agent_environment,
        provider_mount_targets=provider_mount_targets,
        mirror_target=mirror_target,
        allowed_credential_process_environment_names=None,
    )[1]


def _aws_profile_credential_references(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    provider_mount_targets: frozenset[str],
    mirror_target: str,
    allowed_credential_process_environment_names: frozenset[str] | None,
) -> tuple[tuple[str, ...], frozenset[str]]:
    """Return declared paths and environment inputs for active AWS profiles."""

    profile_name = compose_expand_value(
        dict(agent_environment).get("AWS_PROFILE") or "default",
        environ=os.environ,
    )
    references: list[str] = []
    environment_names: set[str] = set()
    configurations: list[configparser.RawConfigParser] = []
    for profile_file in _aws_profile_file_targets(provider_mount_targets):
        mount = _mounted_file_mount(
            auth_mounts,
            target=profile_file,
            mirror_target=mirror_target,
        )
        if mount is None:
            continue
        content = _read_bounded_mounted_clarification_auth_credential(
            mount,
            target=profile_file,
        )
        if content is None:
            continue
        try:
            configuration = configparser.RawConfigParser(interpolation=None)
            configuration.read_string(content)
        except configparser.Error:
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
                credential_process_paths, credential_process_environment_names = (
                    _credential_process_references(credential_process)
                )
                references.extend(credential_process_paths)
                environment_names.update(credential_process_environment_names)
            web_identity_token_file = configuration.get(
                section, "web_identity_token_file", fallback=None
            )
            if isinstance(web_identity_token_file, str) and web_identity_token_file.startswith("/"):
                references.append(web_identity_token_file)
            source_profile = configuration.get(section, "source_profile", fallback=None)
            if source_profile:
                pending_profile_names.update({source_profile} - processed_profile_names)
    references.extend(
        _environment_value_paths(
            environment_names
            if allowed_credential_process_environment_names is None
            else environment_names & allowed_credential_process_environment_names,
            agent_environment,
        )
    )
    return (
        tuple(
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
        ),
        frozenset(environment_names),
    )


def aws_profile_credential_mounts(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    provider_mount_targets: frozenset[str],
    mirror_target: str,
    allowed_credential_process_environment_names: frozenset[str] | None = None,
) -> tuple[AuthMount, ...]:
    """Return declared mounts named by the active AWS profile configuration."""

    credential_paths = aws_profile_credential_paths(
        auth_mounts,
        agent_environment=agent_environment,
        provider_mount_targets=provider_mount_targets,
        mirror_target=mirror_target,
        allowed_credential_process_environment_names=allowed_credential_process_environment_names,
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


def _mounted_file_mount(
    auth_mounts: Sequence[AuthMount],
    *,
    target: str,
    mirror_target: str,
) -> AuthMount | None:
    """Return the most-specific declared mount containing a target file."""

    matching_mounts = tuple(
        mount
        for mount in auth_mounts
        if mount.target != mirror_target
        and (mount.target == target or path_is_below(target, mount.target))
    )
    if not matching_mounts:
        return None
    return max(matching_mounts, key=lambda candidate: len(posixpath.normpath(candidate.target)))


def _read_bounded_mounted_clarification_auth_credential(
    mount: AuthMount,
    *,
    target: str,
) -> str | None:
    """Read a bounded regular credential file through the selected declared mount."""

    normalized_target = posixpath.normpath(target)
    normalized_mount_target = posixpath.normpath(mount.target)
    if normalized_target == normalized_mount_target:
        source = Path(mount.source)
        return _read_bounded_clarification_auth_credential(str(source.parent), source.name)
    if not path_is_below(normalized_target, normalized_mount_target):
        return None
    return _read_bounded_clarification_auth_credential(
        mount.source,
        posixpath.relpath(normalized_target, normalized_mount_target),
    )


def external_account_credential_source_paths(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    provider_environment_names: frozenset[str],
    mirror_target: str,
    allowed_credential_process_environment_names: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Return declared paths needed by a selected external-account ADC."""

    return _external_account_credential_source_references(
        auth_mounts,
        agent_environment=agent_environment,
        provider_environment_names=provider_environment_names,
        mirror_target=mirror_target,
        allowed_credential_process_environment_names=allowed_credential_process_environment_names,
    )[0]


def external_account_credential_source_environment_names(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    provider_environment_names: frozenset[str],
    mirror_target: str,
) -> frozenset[str]:
    """Return environment inputs used by a selected external-account helper."""

    return _external_account_credential_source_references(
        auth_mounts,
        agent_environment=agent_environment,
        provider_environment_names=provider_environment_names,
        mirror_target=mirror_target,
        allowed_credential_process_environment_names=None,
    )[1]


def _external_account_credential_source_references(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    provider_environment_names: frozenset[str],
    mirror_target: str,
    allowed_credential_process_environment_names: frozenset[str] | None,
) -> tuple[tuple[str, ...], frozenset[str]]:
    """Return declared paths and environment inputs for an external-account helper."""

    credential_source = _external_account_credential_source(
        auth_mounts,
        agent_environment=agent_environment,
        provider_environment_names=provider_environment_names,
        mirror_target=mirror_target,
    )
    if credential_source is None:
        return (), frozenset()

    paths: list[str] = []
    environment_names: set[str] = set()
    subject_token_file = credential_source.get("file")
    if isinstance(subject_token_file, str) and subject_token_file.startswith("/"):
        paths.append(posixpath.normpath(subject_token_file))
    executable = credential_source.get("executable")
    if not isinstance(executable, dict):
        return tuple(dict.fromkeys(paths)), frozenset()
    command = executable.get("command")
    if isinstance(command, str):
        command_paths, command_environment_names = _credential_process_references(command)
        paths.extend(posixpath.normpath(path) for path in command_paths)
        environment_names.update(command_environment_names)
    output_file = executable.get("output_file")
    if isinstance(output_file, str) and output_file.startswith("/"):
        paths.append(posixpath.normpath(output_file))
    paths.extend(
        _environment_value_paths(
            environment_names
            if allowed_credential_process_environment_names is None
            else environment_names & allowed_credential_process_environment_names,
            agent_environment,
        )
    )
    return tuple(dict.fromkeys(paths)), frozenset(environment_names)


def _credential_process_references(command: str) -> tuple[tuple[str, ...], frozenset[str]]:
    """Return absolute path and environment references from a valid helper command."""

    with suppress(ValueError):
        arguments = shlex.split(command)
        environment_references = tuple(
            match
            for argument in arguments
            for match in _CREDENTIAL_PROCESS_ENVIRONMENT_REFERENCE_RE.finditer(argument)
        )
        paths = _credential_process_path_references(arguments) + tuple(
            fallback
            for match in environment_references
            if (fallback := match.group("fallback")) and fallback.startswith("/")
        )
        environment_names = frozenset(
            environment_name
            for match in environment_references
            if (environment_name := match.group("braced") or match.group("plain"))
        )
        return tuple(dict.fromkeys(paths)), environment_names
    return (), frozenset()


def _credential_process_path_references(arguments: Sequence[str]) -> tuple[str, ...]:
    """Return absolute paths from helper arguments, including shell-command bodies."""

    references: list[str] = []
    for argument in arguments:
        with suppress(ValueError):
            nested_arguments = shlex.split(argument)
            if nested_arguments != [argument]:
                for nested_argument in nested_arguments:
                    references.extend(_credential_process_argument_paths(nested_argument))
                continue
        references.extend(_credential_process_argument_paths(argument))
    return tuple(dict.fromkeys(references))


def _credential_process_argument_paths(argument: str) -> tuple[str, ...]:
    """Return absolute path values from one helper argument."""

    if argument.startswith("/"):
        return (argument,)
    if _CREDENTIAL_PROCESS_ENVIRONMENT_REFERENCE_RE.fullmatch(argument):
        return ()
    _, separator, option_value = argument.partition("=")
    if separator and option_value.startswith("/"):
        return (option_value,)
    return ()


def _environment_value_paths(
    environment_names: set[str],
    agent_environment: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    """Return absolute declared paths held by selected credential environment inputs."""

    environment_values = dict(agent_environment)
    return tuple(
        value
        for name in environment_names
        if (value := compose_expand_value(environment_values.get(name, ""), environ=os.environ))
        and value.startswith("/")
    )


def is_aws_external_account_credential_source(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    provider_environment_names: frozenset[str],
    mirror_target: str,
) -> bool:
    """Return whether the selected external-account ADC obtains credentials from AWS."""

    credential_source = _external_account_credential_source(
        auth_mounts,
        agent_environment=agent_environment,
        provider_environment_names=provider_environment_names,
        mirror_target=mirror_target,
    )
    return credential_source is not None and credential_source.get("environment_id") == "aws1"


def aws_external_account_environment_names(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    provider_environment_names: frozenset[str],
    mirror_target: str,
) -> frozenset[str]:
    """Return AWS inputs required by a selected Google AWS external-account ADC."""

    if not is_aws_external_account_credential_source(
        auth_mounts,
        agent_environment=agent_environment,
        provider_environment_names=provider_environment_names,
        mirror_target=mirror_target,
    ):
        return frozenset()
    return _AWS_EXTERNAL_ACCOUNT_ENV_NAMES


def _external_account_credential_source(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    provider_environment_names: frozenset[str],
    mirror_target: str,
) -> dict[str, object] | None:
    """Return the credential source from the selected external-account ADC."""

    google_credentials = compose_expand_value(
        dict(agent_environment).get("GOOGLE_APPLICATION_CREDENTIALS", ""),
        environ=os.environ,
    )
    if "GOOGLE_APPLICATION_CREDENTIALS" not in provider_environment_names or not google_credentials:
        return None
    adc_mount = _mounted_file_mount(
        auth_mounts,
        target=google_credentials,
        mirror_target=mirror_target,
    )
    if adc_mount is None:
        return None
    adc_configuration_content = _read_bounded_mounted_clarification_auth_credential(
        adc_mount,
        target=google_credentials,
    )
    if adc_configuration_content is None:
        return None
    try:
        adc_configuration = json.loads(adc_configuration_content)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(adc_configuration, dict)
        or adc_configuration.get("type") != "external_account"
    ):
        return None
    credential_source = adc_configuration.get("credential_source")
    if not isinstance(credential_source, dict):
        return None
    return credential_source


def external_account_subject_token_mounts(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    provider_environment_names: frozenset[str],
    mirror_target: str,
    allowed_credential_process_environment_names: frozenset[str] | None = None,
) -> tuple[AuthMount, ...]:
    """Return declared mounts needed by a selected external-account ADC file."""

    credential_source_paths = external_account_credential_source_paths(
        auth_mounts,
        agent_environment=agent_environment,
        provider_environment_names=provider_environment_names,
        mirror_target=mirror_target,
        allowed_credential_process_environment_names=allowed_credential_process_environment_names,
    )
    if not credential_source_paths:
        return ()
    return tuple(
        mount
        for mount in auth_mounts
        if mount.target != mirror_target
        and any(
            mount.target == path or path_is_below(path, mount.target)
            for path in credential_source_paths
        )
    )


def external_account_subject_token_file_rewrites(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    mirror_target: str,
    agent_runtime: AgentRuntime,
    agent_model: str | None = None,
) -> tuple[tuple[str, str], ...]:
    """Map selected external-account credential-source paths to staged copies."""

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
    credential_source_paths = external_account_credential_source_paths(
        auth_mounts,
        agent_environment=agent_environment,
        mirror_target=mirror_target,
        provider_environment_names=provider_environment_names,
    )
    if not credential_source_paths:
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
        for source, staged in zip(
            _provider_auth_mount_staging_order(provider_auth_mounts), staged_mounts, strict=True
        )
    )
    return tuple(
        (path, staged_path)
        for path in credential_source_paths
        if (staged_path := staged_auth_value(path, staged_targets)) != path
    )


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
        for source, staged in zip(
            _provider_auth_mount_staging_order(provider_auth_mounts), staged_mounts, strict=True
        )
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
                "import re",
                "import shlex",
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
                "        def rewrite_path(path):",
                "            return rewrites.get(posixpath.normpath(path), path)",
                "",
                "        executable_path_pattern = re.compile(",
                '            r"(?<![^\\s\'\\"|&;()<>=])(?:(?P<quote>[\'\\"])(?P<quoted>/[^\'\\"]+)(?P=quote)|(?P<unquoted>/(?:\\\\.|[^\\s\'\\"|&;()<>\\\\])+))(?![^\\s\'\\"|&;()<>])"',
                "        )",
                "        defaulted_environment_path_pattern = re.compile(",
                '            r"(?<!\\\\)(?P<prefix>\\$\\{[A-Za-z_][A-Za-z0-9_]*(?::?[-=+]))(?P<path>/[^}]+)(?P<suffix>\\})"',
                "        )",
                "",
                "        def rewrite_executable_path(match):",
                '            path = match.group("quoted") or match.group("unquoted")',
                '            quote = match.group("quote")',
                "            if not quote:",
                "                try:",
                "                    (path,) = shlex.split(path)",
                "                except ValueError:",
                "                    return match.group(0)",
                "                rewritten_path = rewrite_path(path)",
                "                return match.group(0) if rewritten_path == path else shlex.quote(rewritten_path)",
                '            return f"{quote}{rewrite_path(path)}{quote}"',
                "",
                "        def rewrite_defaulted_environment_path(match):",
                "            return f\"{match.group('prefix')}{rewrite_path(match.group('path'))}{match.group('suffix')}\"",
                "",
                '        subject_token_file = credential_source.get("file")',
                "        if isinstance(subject_token_file, str):",
                '            credential_source["file"] = rewrite_path(subject_token_file)',
                '        executable = credential_source.get("executable")',
                "        if isinstance(executable, dict):",
                '            command = executable.get("command")',
                "            if isinstance(command, str):",
                "                command = executable_path_pattern.sub(",
                "                    rewrite_executable_path,",
                "                    command,",
                "                )",
                "                command = defaulted_environment_path_pattern.sub(",
                "                    rewrite_defaulted_environment_path,",
                "                    command,",
                "                )",
                '                executable["command"] = command',
                '            output_file = executable.get("output_file")',
                "            if isinstance(output_file, str):",
                '                executable["output_file"] = rewrite_path(output_file)',
                '        credentials_path.write_text(json.dumps(configuration), encoding="utf-8")',
                "PY",
            )
        )
    if rewrite_aws_profile_paths:
        lines.extend(
            (
                "python - <<'PY'",
                "import json",
                "import os",
                "import re",
                "import shlex",
                "from pathlib import Path",
                "",
                "rewrites = dict(json.loads(os.environ['AWF_CLARIFICATION_AWS_PROFILE_PATH_REWRITES']))",
                "credential_process_path_pattern = re.compile(",
                '    r"(?<![^\\s\'\\"|&;()<>=])(?:(?P<quote>[\'\\"])(?P<quoted>/[^\'\\"]+)(?P=quote)|(?P<unquoted>/(?:\\\\.|[^\\s\'\\"|&;()<>\\\\])+))(?![^\\s\'\\"|&;()<>])"',
                ")",
                "",
                "def rewrite_credential_process_path(match):",
                '    path = match.group("quoted") or match.group("unquoted")',
                '    quote = match.group("quote")',
                "    if not quote:",
                "        try:",
                "            (path,) = shlex.split(path)",
                "        except ValueError:",
                "            return match.group(0)",
                "        rewritten_path = rewrites.get(path, path)",
                "        return match.group(0) if rewritten_path == path else shlex.quote(rewritten_path)",
                '    return f"{quote}{rewrites.get(path, path)}{quote}"',
                "",
                "def rewrite_credential_process(match):",
                '    return match.group("prefix") + credential_process_path_pattern.sub(',
                '        rewrite_credential_process_path, match.group("command")',
                "    )",
                "",
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
                "    configuration = re.sub(",
                '        r"(?im)^(?P<prefix>\\s*credential_process\\s*=\\s*)(?P<command>.*)$",',
                "        rewrite_credential_process,",
                "        configuration,",
                "    )",
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
