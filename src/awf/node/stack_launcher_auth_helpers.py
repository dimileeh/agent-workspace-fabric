"""Clarification-auth mount selection helpers for the stack launcher."""

from __future__ import annotations

import base64
import binascii
import configparser
import json
import os
import posixpath
import shlex
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from awf.db.enums import AgentRuntime
from awf.node.compose_manager import AuthMount
from awf.service.environment import compose_expand_value

_AGENT_HOME = "/home/agent"
_CLARIFICATION_AUTH_STAGING_ROOT = "/home/agent/.awf/clarification-auth"
_CLARIFICATION_RUNTIME_AUTH_MOUNT_TARGETS: dict[AgentRuntime, frozenset[str]] = {
    AgentRuntime.codex: frozenset({"/home/agent/.codex"}),
    AgentRuntime.claude_code: frozenset({"/home/agent/.claude", "/home/agent/.claude.json"}),
    AgentRuntime.cursor: frozenset(),
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
            auth = json.loads((Path(mount.source) / "auth.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
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


def has_claude_code_file_auth(source_mounts: Sequence[AuthMount]) -> bool:
    """Return whether a staged Claude home supplies its OAuth credential store."""

    for mount in source_mounts:
        if mount.target != "/home/agent/.claude":
            continue
        try:
            credentials = json.loads(
                (Path(mount.source) / ".credentials.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
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
) -> tuple[str, ...]:
    """Return declared paths referenced by the active AWS profile files."""

    profile_name = compose_expand_value(
        dict(agent_environment).get("AWS_PROFILE") or "default",
        environ=os.environ,
    )
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
                    for argument in shlex.split(credential_process):
                        if argument.startswith("/"):
                            references.append(argument)
                            continue
                        _, separator, option_value = argument.partition("=")
                        if separator and option_value.startswith("/"):
                            references.append(option_value)
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


def external_account_credential_source_paths(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    provider_environment_names: frozenset[str],
    mirror_target: str,
) -> tuple[str, ...]:
    """Return declared paths needed by a selected external-account ADC."""

    credential_source = _external_account_credential_source(
        auth_mounts,
        agent_environment=agent_environment,
        provider_environment_names=provider_environment_names,
        mirror_target=mirror_target,
    )
    if credential_source is None:
        return ()

    paths: list[str] = []
    subject_token_file = credential_source.get("file")
    if isinstance(subject_token_file, str) and subject_token_file.startswith("/"):
        paths.append(posixpath.normpath(subject_token_file))
    executable = credential_source.get("executable")
    if not isinstance(executable, dict):
        return tuple(dict.fromkeys(paths))
    command = executable.get("command")
    if isinstance(command, str):
        with suppress(ValueError):
            for argument in shlex.split(command):
                if argument.startswith("/"):
                    paths.append(posixpath.normpath(argument))
                    continue
                _, separator, option_value = argument.partition("=")
                if separator and option_value.startswith("/"):
                    paths.append(posixpath.normpath(option_value))
    output_file = executable.get("output_file")
    if isinstance(output_file, str) and output_file.startswith("/"):
        paths.append(posixpath.normpath(output_file))
    return tuple(dict.fromkeys(paths))


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
    return credential_source


def external_account_subject_token_mounts(
    auth_mounts: Sequence[AuthMount],
    *,
    agent_environment: tuple[tuple[str, str], ...],
    provider_environment_names: frozenset[str],
    mirror_target: str,
) -> tuple[AuthMount, ...]:
    """Return declared mounts needed by a selected external-account ADC file."""

    credential_source_paths = external_account_credential_source_paths(
        auth_mounts,
        agent_environment=agent_environment,
        provider_environment_names=provider_environment_names,
        mirror_target=mirror_target,
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
                '        subject_token_file = credential_source.get("file")',
                "        if isinstance(subject_token_file, str):",
                '            credential_source["file"] = rewrite_path(subject_token_file)',
                '        executable = credential_source.get("executable")',
                "        if isinstance(executable, dict):",
                '            command = executable.get("command")',
                "            if isinstance(command, str):",
                '                executable["command"] = executable_path_pattern.sub(',
                "                    rewrite_executable_path,",
                "                    command,",
                "                )",
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
