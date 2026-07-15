"""Hosted delegation request payload and profile sanitization helpers."""

from __future__ import annotations

import base64
import hashlib
import re
from collections import Counter
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

import awf.profiles.compose as compose_helpers
import awf.profiles.compose_auth_env as compose_auth_env
from awf.adapters.runtime_executor import AgentRuntimeExecRequest
from awf.common.logging import get_logger
from awf.common.redaction import redact_secrets
from awf.common.token_patterns import (
    TOKEN_ASSIGNMENT_KEY_PATTERN,
    compile_known_token_re,
    compile_provider_ref_re,
)
from awf.profiles.models import WorkspaceProfile

_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_REFERENCE_PATTERN = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_ENV_EMPTY_DEFAULT_REFERENCE_PATTERN = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::-|-)\}$")
_SHELL_ENV_REFERENCE_PATTERN = re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_ENV_NAME_PATTERN = re.compile(
    rf"^(?:{TOKEN_ASSIGNMENT_KEY_PATTERN})$|"
    r"(?:^|[_-])(?:TOKEN|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|"
    r"PASSWORD|PASSWD|SECRET|CREDENTIALS?)(?:[_-]|$)",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERN = compile_known_token_re(match_truncated_provider_tokens=False)
_PROVIDER_REF_PATTERN = compile_provider_ref_re()
_HOSTED_COMMAND_ASSIGNMENT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?P<key>[A-Za-z_][A-Za-z0-9_-]*)"
    r"\s*[:=]\s*"
    r"(?P<quote>[\"'])?"
    r"(?P<value>[^\s\"'`,;)\]]+)"
    r"(?(quote)\s*(?P=quote)|)",
    re.IGNORECASE,
)
_HOSTED_COMMAND_BEARER_PATTERN = re.compile(
    r"(?:\bAuthorization\s*:\s*)?\bBearer\s+[A-Za-z0-9._~+/=-]{8,}",
    re.IGNORECASE,
)
_HOSTED_PR_IDENTITY_URL_FIELDS = frozenset({"repo_url", "head_repo_url"})
_HOSTED_AGENT_AUTH_SCHEMA = "hosted_validation_agent_auth.v1"
_HOSTED_RENDERED_STACK_SCHEMA = "hosted_validation_rendered_stack.v1"
_HOSTED_COVERAGE_OMITTED_RUNTIME_ENV = frozenset({"PIP_EXTRA_INDEX_URL", "PIP_INDEX_URL"})
_HOSTED_PHASE_COMMAND_FIELDS = ("setup", "pre_agent", "post_agent", "validate", "cleanup")
_HOSTED_DATABASE_COMMAND_FIELDS = ("generated_setup", "pre_validation_refresh")
_HOSTED_COMMAND_SECRET_ASSIGNMENT_KEYS = frozenset({"MYSQL_PWD", "PGPASSWORD"})
_HOSTED_DNS1123_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_HOSTED_VOLUME_INVALID_RUN_PATTERN = re.compile(r"[^a-z0-9-]+")
_HOSTED_VOLUME_HYPHEN_RUN_PATTERN = re.compile(r"-+")
_HOSTED_VOLUME_HASH_LENGTHS = (10, 12, 16, 20, 24, 32)
_HOSTED_KUBERNETES_LABEL_MAX_LENGTH = 63
_log = get_logger(__name__)


def _agent_start_payload(request: AgentRuntimeExecRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "workspace_id": request.workspace_id,
        "agent_runtime": request.agent_runtime.value,
        "cli_args": list(request.cli_args),
        "prompt_stdin_base64": base64.b64encode(request.prompt_stdin).decode("ascii"),
        "log_source": request.log_source,
        "model": request.model,
        "effort": request.effort,
        "env_passthrough_names": list(request.env_passthrough_names),
        "env_passthrough_aliases": [
            {"target": target, "source": source}
            for target, source in request.env_passthrough_aliases
        ],
        "file_auth_mount_targets": list(request.file_auth_mount_targets),
        "profile_env": [{"name": name, "value": value} for name, value in request.profile_env],
        "timeouts": {
            "wall_seconds": request.wall_timeout_seconds,
            "idle_seconds": request.idle_timeout_seconds,
        },
    }
    pr_identity = _agent_pr_identity_payload(request)
    if pr_identity:
        payload["pr_identity"] = pr_identity
    if request.profile is not None:
        payload["profile"] = _hosted_validation_profile_payload(request.profile)
    if request.compose_project is not None and request.compose_file is not None:
        _hosted_validation_attach_rendered_stack(
            payload,
            compose_project=request.compose_project,
            compose_file=request.compose_file,
        )
    return payload


def _agent_pr_identity_payload(request: AgentRuntimeExecRequest) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    for key in (
        "repo_url",
        "pr_url",
        "pr_number",
        "base_ref",
        "head_ref",
        "head_repo_url",
        "head_repo_slug",
        "expected_head_sha",
    ):
        value = getattr(request, key)
        if value is not None:
            if key in _HOSTED_PR_IDENTITY_URL_FIELDS and isinstance(value, str):
                value = _strip_pr_identity_url_credentials(value)
            identity[key] = value
    if request.owned_paths:
        identity["owned_paths"] = list(request.owned_paths)
    return identity


def _hosted_pr_identity_payload(identity: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(identity)
    for key in _HOSTED_PR_IDENTITY_URL_FIELDS:
        value = payload.get(key)
        if isinstance(value, str):
            payload[key] = _strip_pr_identity_url_credentials(value)
    return payload


def _hosted_validation_rendered_stack_payload(
    *,
    compose_project: str,
    compose_file: Path,
) -> dict[str, Any] | None:
    """Return sanitized rendered compose stack metadata for hosted validation."""
    try:
        if not compose_file.is_file():
            return None
        raw_compose = compose_file.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning(
            "hosted_validation.rendered_stack_unavailable",
            compose_file=str(compose_file),
            error=redact_secrets(str(exc))[:1000],
        )
        return None

    try:
        parsed = yaml.safe_load(raw_compose) or {}
    except yaml.YAMLError as exc:
        _log.warning(
            "hosted_validation.rendered_stack_malformed",
            compose_file=str(compose_file),
            error=redact_secrets(str(exc))[:1000],
        )
        return None
    if not isinstance(parsed, Mapping):
        return None

    volume_translations = _hosted_validation_compose_volume_name_translations(parsed)
    rendered_stack: dict[str, Any] = {
        "schema": _HOSTED_RENDERED_STACK_SCHEMA,
        "compose_project": compose_project,
        "compose_file_path": str(compose_file),
        "services": _hosted_validation_rendered_stack_services(
            parsed.get("services"),
            volume_translations=volume_translations,
        ),
    }
    volumes = parsed.get("volumes")
    if isinstance(volumes, Mapping):
        rendered_stack["volumes"] = _hosted_validation_sanitize_rendered_stack_volumes(
            volumes,
            volume_translations=volume_translations,
        )
    networks = parsed.get("networks")
    if isinstance(networks, Mapping):
        rendered_stack["networks"] = _hosted_validation_sanitize_compose_value(networks)
    return rendered_stack


def _hosted_validation_attach_rendered_stack(
    payload: dict[str, Any],
    *,
    compose_project: str,
    compose_file: Path,
    include_agent_auth_context: bool = False,
) -> None:
    rendered_stack = _hosted_validation_rendered_stack_payload(
        compose_project=compose_project,
        compose_file=compose_file,
    )
    if rendered_stack is not None:
        payload["rendered_stack"] = rendered_stack
    if include_agent_auth_context:
        agent_auth = _hosted_validation_agent_auth_payload(compose_file=compose_file)
        if agent_auth is not None:
            payload["agent_auth"] = agent_auth


def _hosted_validation_agent_auth_payload(
    *,
    compose_file: Path,
) -> dict[str, Any] | None:
    """Return secret-free agent auth context for hosted validation jobs."""
    compose_env = compose_helpers._try_agent_environment_from_compose_file(compose_file)
    if compose_env is None:
        return None

    env_passthrough_aliases = compose_helpers.hosted_profile_env_passthrough_aliases(
        compose_file,
        compose_env=compose_env,
    )
    env_passthrough_names = _hosted_validation_agent_auth_env_passthrough_names(
        compose_file=compose_file,
        compose_env=compose_env,
        env_passthrough_aliases=env_passthrough_aliases,
    )
    file_auth_mount_targets = compose_helpers.hosted_file_auth_mount_targets(
        compose_file,
        compose_env=compose_env,
    )
    if not env_passthrough_names and not env_passthrough_aliases and not file_auth_mount_targets:
        return None
    return {
        "schema": _HOSTED_AGENT_AUTH_SCHEMA,
        "env_passthrough_names": list(env_passthrough_names),
        "env_passthrough_aliases": [
            {"target": target, "source": source} for target, source in env_passthrough_aliases
        ],
        "file_auth_mount_targets": list(file_auth_mount_targets),
    }


def _hosted_validation_agent_auth_env_passthrough_names(
    *,
    compose_file: Path,
    compose_env: Mapping[str, str],
    env_passthrough_aliases: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    names = tuple(
        name
        for name in compose_helpers.hosted_profile_env_passthrough_names(
            compose_file,
            compose_env=compose_env,
        )
        if name not in compose_auth_env._HOSTED_FILE_BACKED_ENV_ONLY_UNSUPPORTED_NAMES
    )
    existing_names = set(names)
    alias_targets = {target for target, _source in env_passthrough_aliases}
    alias_sources = {source for _target, source in env_passthrough_aliases}
    github_token_names = compose_helpers.hosted_github_token_passthrough_names(
        compose_file,
        compose_env=compose_env,
    )
    return names + tuple(
        name
        for name in github_token_names
        if name not in existing_names and name not in alias_targets and name not in alias_sources
    )


def _hosted_validation_rendered_stack_services(
    services: object,
    *,
    volume_translations: Mapping[str, str],
) -> dict[str, Any]:
    """Return sanitized non-agent services from a rendered compose document."""
    if not isinstance(services, Mapping):
        return {}
    payload: dict[str, Any] = {}
    for name, service in services.items():
        service_name = str(name)
        if service_name == "agent" or not isinstance(service, Mapping):
            continue
        payload[service_name] = _hosted_validation_sanitize_compose_service(
            service,
            volume_translations=volume_translations,
        )
    return payload


def _hosted_validation_sanitize_compose_service(
    service: Mapping[str, object],
    *,
    volume_translations: Mapping[str, str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in service.items():
        field = str(key)
        if field == "environment":
            payload[field] = _hosted_validation_sanitize_compose_environment(value)
            continue
        if field == "volumes":
            payload[field] = _hosted_validation_sanitize_compose_service_volumes(
                value,
                volume_translations=volume_translations,
            )
            continue
        payload[field] = _hosted_validation_sanitize_compose_value(value)
    return payload


def _hosted_validation_compose_volume_name_translations(
    compose: Mapping[object, object],
) -> dict[str, str]:
    volume_names = set(_hosted_validation_compose_volume_names(compose))
    if not volume_names:
        return {}

    normalized_names = {
        name: _hosted_validation_normalized_compose_volume_name(name) for name in volume_names
    }
    candidates = {
        name: _hosted_validation_bounded_compose_volume_name(normalized_name)
        for name, normalized_name in normalized_names.items()
    }
    candidate_counts = Counter(candidates.values())
    translations: dict[str, str] = {}
    used_names: dict[str, str] = {}

    for name in sorted(volume_names):
        if _hosted_validation_dns1123_label_is_valid(name):
            translations[name] = name
            used_names[name] = name

    for name in sorted(volume_names):
        if name in translations:
            continue
        candidate = candidates[name]
        if candidate_counts[candidate] == 1 and candidate not in used_names:
            translated_name = candidate
        else:
            translated_name = _hosted_validation_disambiguated_compose_volume_name(
                normalized_base=normalized_names[name],
                original_name=name,
                used_names=used_names,
            )
        previous_original = used_names.get(translated_name)
        if previous_original is not None and previous_original != name:
            raise ValueError("hosted rendered stack volume name collision")
        translations[name] = translated_name
        used_names[translated_name] = name

    return translations


def _hosted_validation_compose_volume_names(
    compose: Mapping[object, object],
) -> Iterator[str]:
    volumes = compose.get("volumes")
    if isinstance(volumes, Mapping):
        for name, volume in volumes.items():
            yield str(name)
            explicit_name = _hosted_validation_compose_volume_declaration_explicit_name(volume)
            if explicit_name is not None:
                yield explicit_name

    services = compose.get("services")
    if not isinstance(services, Mapping):
        return
    for name, service in services.items():
        if str(name) == "agent" or not isinstance(service, Mapping):
            continue
        yield from _hosted_validation_compose_service_volume_names(service)


def _hosted_validation_compose_service_volume_names(
    service: Mapping[object, object],
) -> Iterator[str]:
    volumes = service.get("volumes")
    if not isinstance(volumes, list):
        return
    for volume in volumes:
        if isinstance(volume, str):
            source = _hosted_validation_compose_short_named_volume_source(volume)
        elif isinstance(volume, Mapping):
            source = _hosted_validation_compose_mapping_named_volume_source(volume)
        else:
            source = None
        if source is not None:
            yield source


def _hosted_validation_compose_volume_declaration_explicit_name(
    volume: object,
) -> str | None:
    if not isinstance(volume, Mapping):
        return None
    name = volume.get("name")
    if not isinstance(name, str) or not name:
        return None
    return name


def _hosted_validation_dns1123_label_is_valid(value: str) -> bool:
    return _HOSTED_DNS1123_LABEL_PATTERN.fullmatch(value) is not None


def _hosted_validation_normalized_compose_volume_name(value: str) -> str:
    normalized = _HOSTED_VOLUME_INVALID_RUN_PATTERN.sub("-", value.lower())
    normalized = _HOSTED_VOLUME_HYPHEN_RUN_PATTERN.sub("-", normalized).strip("-")
    return normalized or "volume"


def _hosted_validation_bounded_compose_volume_name(value: str) -> str:
    return value[:_HOSTED_KUBERNETES_LABEL_MAX_LENGTH].rstrip("-") or "volume"


def _hosted_validation_disambiguated_compose_volume_name(
    *,
    normalized_base: str,
    original_name: str,
    used_names: Mapping[str, str],
) -> str:
    digest = hashlib.sha256(original_name.encode("utf-8")).hexdigest()
    for hash_length in _HOSTED_VOLUME_HASH_LENGTHS:
        suffix = f"-{digest[:hash_length]}"
        max_prefix_length = _HOSTED_KUBERNETES_LABEL_MAX_LENGTH - len(suffix)
        if max_prefix_length <= 0:
            continue
        prefix = normalized_base[:max_prefix_length].rstrip("-") or "volume"
        candidate = f"{prefix}{suffix}"
        if _hosted_validation_dns1123_label_is_valid(candidate) and candidate not in used_names:
            return candidate
    raise ValueError("hosted rendered stack volume name collision")


def _hosted_validation_sanitize_rendered_stack_volumes(
    volumes: Mapping[object, object],
    *,
    volume_translations: Mapping[str, str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for name, value in volumes.items():
        volume_name = str(name)
        translated_name = volume_translations.get(volume_name, volume_name)
        if translated_name in payload:
            raise ValueError("hosted rendered stack volume declaration collision")
        payload[translated_name] = _hosted_validation_sanitize_rendered_stack_volume(
            value,
            volume_translations=volume_translations,
        )
    return payload


def _hosted_validation_sanitize_rendered_stack_volume(
    volume: object,
    *,
    volume_translations: Mapping[str, str],
) -> Any:
    if not isinstance(volume, Mapping):
        return _hosted_validation_sanitize_compose_value(volume)
    payload: dict[str, Any] = {}
    for key, value in volume.items():
        field = str(key)
        if field == "name" and isinstance(value, str):
            payload[field] = _hosted_validation_sanitize_compose_value(
                volume_translations.get(value, value)
            )
            continue
        payload[field] = _hosted_validation_sanitize_compose_value(value)
    return payload


def _hosted_validation_sanitize_compose_service_volumes(
    volumes: object,
    *,
    volume_translations: Mapping[str, str],
) -> Any:
    if not isinstance(volumes, list):
        return _hosted_validation_sanitize_compose_value(volumes)
    return [
        _hosted_validation_sanitize_compose_service_volume(
            volume,
            volume_translations=volume_translations,
        )
        for volume in volumes
    ]


def _hosted_validation_sanitize_compose_service_volume(
    volume: object,
    *,
    volume_translations: Mapping[str, str],
) -> Any:
    if isinstance(volume, str):
        return _hosted_validation_sanitize_compose_short_volume(
            volume,
            volume_translations=volume_translations,
        )
    if isinstance(volume, Mapping):
        return _hosted_validation_sanitize_compose_volume_mapping(
            volume,
            volume_translations=volume_translations,
        )
    return _hosted_validation_sanitize_compose_value(volume)


def _hosted_validation_sanitize_compose_short_volume(
    volume: str,
    *,
    volume_translations: Mapping[str, str],
) -> str:
    source = _hosted_validation_compose_short_named_volume_source(volume)
    if source is None:
        return redact_secrets(volume)
    translated_source = volume_translations.get(source, source)
    _source, _separator, remainder = volume.partition(":")
    return redact_secrets(f"{translated_source}:{remainder}")


def _hosted_validation_sanitize_compose_volume_mapping(
    volume: Mapping[object, object],
    *,
    volume_translations: Mapping[str, str],
) -> dict[str, Any]:
    source = _hosted_validation_compose_mapping_named_volume_source(volume)
    payload: dict[str, Any] = {}
    for key, value in volume.items():
        field = str(key)
        if field in {"source", "src"} and source is not None and value == source:
            payload[field] = _hosted_validation_sanitize_compose_value(
                volume_translations.get(source, source)
            )
            continue
        payload[field] = _hosted_validation_sanitize_compose_value(value)
    return payload


def _hosted_validation_compose_short_named_volume_source(volume: str) -> str | None:
    if _hosted_validation_compose_short_volume_is_windows_path(volume):
        return None
    source, separator, remainder = volume.partition(":")
    if not separator or not source or not remainder.startswith("/"):
        return None
    if _hosted_validation_compose_volume_source_is_host_path(source):
        return None
    return source


def _hosted_validation_compose_short_volume_is_windows_path(volume: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", volume))


def _hosted_validation_compose_mapping_named_volume_source(
    volume: Mapping[object, object],
) -> str | None:
    type_value = volume.get("type")
    if type_value is not None and (
        not isinstance(type_value, str) or type_value.lower() != "volume"
    ):
        return None
    source = volume.get("source")
    if not isinstance(source, str) or not source:
        source = volume.get("src")
    if not isinstance(source, str) or not source:
        return None
    if _hosted_validation_compose_volume_source_is_host_path(source):
        return None
    return source


def _hosted_validation_compose_volume_source_is_host_path(source: str) -> bool:
    return (
        source.startswith(("/", ".", "~", "$")) or "/" in source or "\\" in source or ":" in source
    )


def _hosted_validation_sanitize_compose_environment(environment: object) -> Any:
    if isinstance(environment, Mapping):
        return {
            str(name): _hosted_validation_env_value(str(name), value)
            for name, value in environment.items()
        }
    if isinstance(environment, list):
        sanitized: list[Any] = []
        for item in environment:
            if isinstance(item, str) and "=" in item:
                name, _, value = item.partition("=")
                sanitized.append(f"{name}={_hosted_validation_env_value(name, value)}")
                continue
            sanitized.append(_hosted_validation_sanitize_compose_value(item))
        return sanitized
    return _hosted_validation_sanitize_compose_value(environment)


def _hosted_validation_sanitize_compose_value(value: object) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _hosted_validation_sanitize_compose_value(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_hosted_validation_sanitize_compose_value(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value


def _strip_pr_identity_url_credentials(value: str) -> str:
    parsed = urlsplit(value)
    authority = parsed.netloc
    if parsed.scheme and "@" in authority:
        userinfo, _, host = authority.rpartition("@")
        if parsed.scheme.lower() in {"ssh", "git+ssh"}:
            username, password_separator, _ = userinfo.partition(":")
            if password_separator:
                authority = f"{username}@{host}" if username else host
        else:
            authority = host
    if authority == parsed.netloc and not (parsed.query or parsed.fragment):
        return value
    return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))


def _hosted_validation_profile_payload(
    profile: WorkspaceProfile,
    *,
    omit_runtime_environment: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    payload = profile.model_dump(mode="json", by_alias=True)
    _hosted_validation_sanitize_secret_refs(payload.get("secrets"))
    if omit_runtime_environment:
        _hosted_validation_omit_environment_entries(
            payload.get("runtime"),
            names=omit_runtime_environment,
        )
    _hosted_validation_sanitize_environment_container(payload.get("runtime"))
    services = payload.get("services")
    if isinstance(services, list):
        for service in services:
            _hosted_validation_sanitize_environment_container(service)
    _hosted_validation_reject_secret_bearing_fields(payload)
    return payload


def _hosted_validation_reject_secret_bearing_fields(payload: Mapping[str, Any]) -> None:
    secret_paths = [
        path
        for path, value in _hosted_validation_secret_checked_fields(payload)
        if _hosted_validation_command_value_is_secret(value)
    ]
    if secret_paths:
        joined_paths = ", ".join(secret_paths)
        raise ValueError(f"hosted profile payload contains secret-bearing fields: {joined_paths}")


def _hosted_validation_secret_checked_fields(
    payload: Mapping[str, Any],
) -> Iterator[tuple[str, str]]:
    phases = payload.get("phases")
    if isinstance(phases, Mapping):
        for field in _HOSTED_PHASE_COMMAND_FIELDS:
            yield from _hosted_validation_command_list_fields(
                phases.get(field),
                f"phases.{field}",
            )

    database = payload.get("database")
    if isinstance(database, Mapping):
        for field in _HOSTED_DATABASE_COMMAND_FIELDS:
            yield from _hosted_validation_command_list_fields(
                database.get(field),
                f"database.{field}",
            )

    validation = payload.get("validation")
    if isinstance(validation, Mapping):
        coverage = validation.get("coverage")
        if isinstance(coverage, Mapping):
            yield from _hosted_validation_command_object_fields(
                coverage.get("command"),
                "validation.coverage.command",
            )
        healthchecks = validation.get("healthchecks")
        if isinstance(healthchecks, list):
            for index, healthcheck in enumerate(healthchecks):
                yield from _hosted_validation_direct_command_field(
                    healthcheck,
                    f"validation.healthchecks[{index}].command",
                )
                yield from _hosted_validation_direct_command_field(
                    healthcheck,
                    f"validation.healthchecks[{index}].url",
                    field="url",
                )

    services = payload.get("services")
    if isinstance(services, list):
        for index, service in enumerate(services):
            if not isinstance(service, Mapping):
                continue
            for field in ("command", "healthcheck_cmd"):
                yield from _hosted_validation_direct_command_field(
                    service,
                    f"services[{index}].{field}",
                    field=field,
                )


def _hosted_validation_command_list_fields(
    value: object,
    path: str,
) -> Iterator[tuple[str, str]]:
    if not isinstance(value, list):
        return
    for index, item in enumerate(value):
        yield from _hosted_validation_command_object_fields(item, f"{path}[{index}]")


def _hosted_validation_command_object_fields(
    value: object,
    path: str,
) -> Iterator[tuple[str, str]]:
    yield from _hosted_validation_direct_command_field(
        value,
        f"{path}.command",
    )


def _hosted_validation_direct_command_field(
    value: object,
    path: str,
    *,
    field: str = "command",
) -> Iterator[tuple[str, str]]:
    if not isinstance(value, Mapping):
        return
    command = value.get(field)
    if isinstance(command, str):
        yield path, command


def _hosted_validation_command_value_is_secret(value: str) -> bool:
    return (
        bool(_SECRET_VALUE_PATTERN.search(value))
        or bool(_PROVIDER_REF_PATTERN.search(value))
        or bool(_HOSTED_COMMAND_BEARER_PATTERN.search(value))
        or _hosted_validation_value_has_url_credentials(value)
        or "-----BEGIN " in value
        or _hosted_validation_command_has_secret_assignment(value)
    )


def _hosted_validation_command_has_secret_assignment(value: str) -> bool:
    for match in _HOSTED_COMMAND_ASSIGNMENT_PATTERN.finditer(value):
        key = match.group("key")
        if not _hosted_validation_command_assignment_key_is_secret(key):
            continue
        assigned_value = match.group("value").strip()
        if _hosted_validation_command_assignment_value_is_reference(assigned_value):
            continue
        return True
    return False


def _hosted_validation_command_assignment_key_is_secret(key: str) -> bool:
    normalized = key.upper().replace("-", "_")
    return normalized in _HOSTED_COMMAND_SECRET_ASSIGNMENT_KEYS or bool(
        _SECRET_ENV_NAME_PATTERN.search(key)
    )


def _hosted_validation_command_assignment_value_is_reference(value: str) -> bool:
    return bool(
        _ENV_REFERENCE_PATTERN.fullmatch(value)
        or _ENV_EMPTY_DEFAULT_REFERENCE_PATTERN.fullmatch(value)
        or _SHELL_ENV_REFERENCE_PATTERN.fullmatch(value)
    )


def _hosted_validation_sanitize_secret_refs(secrets: object) -> None:
    if not isinstance(secrets, list):
        return
    for secret in secrets:
        if isinstance(secret, dict) and not _hosted_validation_preserves_secret_ref(secret):
            secret.pop("ref", None)


def _hosted_validation_preserves_secret_ref(secret: Mapping[str, object]) -> bool:
    kind = secret.get("kind")
    provider = secret.get("provider")
    ref = secret.get("ref")
    if kind != "env" or not isinstance(provider, str):
        return False
    if provider.strip().lower() != "env":
        return False
    return _hosted_validation_env_secret_ref_name(ref) is not None


def _hosted_validation_env_secret_ref_name(ref: object) -> str | None:
    if not isinstance(ref, str):
        return None
    stripped = ref.strip()
    if stripped.startswith("env/"):
        stripped = stripped[len("env/") :]
    if not _ENV_NAME_PATTERN.fullmatch(stripped):
        return None
    return stripped


def _hosted_validation_omit_environment_entries(
    container: object, *, names: frozenset[str]
) -> None:
    if not isinstance(container, dict):
        return
    environment = container.get("environment")
    if not isinstance(environment, dict):
        return
    for name in names:
        environment.pop(name, None)


def _hosted_validation_sanitize_environment_container(container: object) -> None:
    if not isinstance(container, dict):
        return
    environment = container.get("environment")
    if not isinstance(environment, dict):
        return
    container["environment"] = {
        str(name): _hosted_validation_env_value(str(name), value)
        for name, value in environment.items()
    }


def _hosted_validation_env_value(name: str, value: object) -> str:
    text = str(value)
    if _hosted_validation_env_value_is_secret(name, text):
        return f"${{{name}}}" if _ENV_NAME_PATTERN.fullmatch(name) else "<redacted>"
    return text


def _hosted_validation_env_value_is_secret(name: str, value: str) -> bool:
    stripped = value.strip()
    if not stripped or _ENV_REFERENCE_PATTERN.fullmatch(stripped):
        return False
    return (
        bool(_SECRET_ENV_NAME_PATTERN.search(name))
        or bool(_SECRET_VALUE_PATTERN.search(stripped))
        or _hosted_validation_value_has_url_credentials(stripped)
        or "-----BEGIN " in stripped
        or "\n" in stripped
    )


def _hosted_validation_value_has_url_credentials(value: str) -> bool:
    return compose_helpers._value_has_url_userinfo(value)
