"""Hosted delegation request payload and profile sanitization helpers."""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import suppress
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
from awf.profiles.compose_postgres_env import compose_service_env_file_paths
from awf.profiles.models import WorkspaceProfile
from awf.runtime.hosted_delegation_payload_volumes import (
    _hosted_validation_compose_volume_name_translations,
    _hosted_validation_sanitize_compose_service_volumes,
    _hosted_validation_sanitize_compose_value,
    _hosted_validation_sanitize_rendered_stack_volumes,
)
from awf.service.environment import (
    ComposeEnvInterpolationError,
    compose_env_file_values,
    compose_expand_value,
)

_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COMPOSE_ENV_FILE_ASSIGNMENT_KEY_PATTERN = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(?P<value>.*)$"
)
_ENV_REFERENCE_PATTERN = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_ENV_EMPTY_DEFAULT_REFERENCE_PATTERN = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::-|-)\}$")
_COMPOSE_INTERPOLATION_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_COMPOSE_INTERPOLATION_OPERATORS = (":-", "-", ":+", "+", ":?", "?")
_SHELL_ENV_REFERENCE_PATTERN = re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_ENV_NAME_PATTERN = re.compile(
    rf"^(?:{TOKEN_ASSIGNMENT_KEY_PATTERN})$|"
    r"(?:^|[_-])(?:TOKEN|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|"
    r"PASSWORD|PASSWD|SECRET|CREDENTIALS?)(?:[_-]|$)",
    re.IGNORECASE,
)
_SAFE_NAMED_CONNECTION_CREDENTIAL_ENV_NAME_PATTERN = re.compile(
    r"(?:^|[_-])(?:DATABASE[_-]?(?:URL|URI)|POSTGRES[_-]?(?:URL|URI)|DB[_-]?(?:URL|URI))"
    r"(?:[_-]|$)|(?:^|[_-])DSN(?:[_-]|$)",
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
_log = get_logger(__name__)


def _agent_start_payload(request: AgentRuntimeExecRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "workspace_id": request.workspace_id,
        "agent_runtime": request.agent_runtime.value,
        "cli_args": [],
        "prompt_stdin_base64": base64.b64encode(request.prompt_stdin).decode("ascii"),
        "log_source": request.log_source,
        "model": request.model,
        "effort": request.effort,
        "env_passthrough_names": list(request.env_passthrough_names),
        "env_passthrough_aliases": [
            {"target": target, "source": source}
            for target, source in request.env_passthrough_aliases
        ],
        "file_auth_mount_targets": [],
        "profile_env": [{"name": name, "value": value} for name, value in request.profile_env],
        "timeouts": {
            "wall_seconds": request.wall_timeout_seconds,
            "idle_seconds": None,
        },
    }
    pr_identity = _agent_pr_identity_payload(request)
    if pr_identity:
        payload["pr_identity"] = pr_identity
    if request.git_preparation is not None:
        payload["git_preparation"] = {
            "mode": request.git_preparation.mode,
            "base_ref": request.git_preparation.base_ref,
            "expected_base_sha": request.git_preparation.expected_base_sha,
        }
    if request.profile is not None:
        agent_profile = request.profile.model_copy(deep=True)
        agent_profile.phases.setup = []
        agent_profile.phases.pre_agent = []
        agent_profile.phases.post_agent = []
        agent_profile.phases.cleanup = []
        agent_profile.database.generated_setup = []
        payload["profile"] = _hosted_validation_profile_payload(
            agent_profile,
            compose_dir=(request.compose_file.parent if request.compose_file is not None else None),
            profile_base_path=request.worktree_path,
        )
    if request.compose_project is not None and request.compose_file is not None:
        _hosted_validation_attach_rendered_stack(
            payload,
            compose_project=request.compose_project,
            compose_file=request.compose_file,
            omit_credential_env_keys=True,
            env_file_base_path=request.worktree_path,
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
    omit_credential_env_keys: bool = False,
    env_file_base_path: Path | None = None,
) -> dict[str, Any] | None:
    """Sanitized rendered compose stack; omit mode drops credential env for Cloud DTOs."""
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
            omit_credential_env_keys=omit_credential_env_keys,
            compose_dir=compose_file.parent,
            env_file_base_path=env_file_base_path,
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
    omit_credential_env_keys: bool = False,
    env_file_base_path: Path | None = None,
) -> None:
    rendered_stack = _hosted_validation_rendered_stack_payload(
        compose_project=compose_project,
        compose_file=compose_file,
        omit_credential_env_keys=omit_credential_env_keys,
        env_file_base_path=env_file_base_path,
    )
    if rendered_stack is not None:
        payload["rendered_stack"] = rendered_stack
        _hosted_validation_maybe_translate_docker_mode_none_to_compose(
            payload,
            rendered_stack=rendered_stack,
        )
    if include_agent_auth_context:
        agent_auth = _hosted_validation_agent_auth_payload(compose_file=compose_file)
        if agent_auth is not None:
            payload["agent_auth"] = agent_auth


def _hosted_validation_maybe_translate_docker_mode_none_to_compose(
    payload: Mapping[str, Any],
    *,
    rendered_stack: Mapping[str, Any],
) -> None:
    """Hosted JSON: Core ``docker.mode=none`` + rendered sidecars → ``compose``.

    Base the translation on sanitized ``rendered_stack.services`` (non-agent
    only). Task-policy companions are rendered into compose but are not part of
    ``profile.services``; requiring profile services would leave mode ``none``
    and cause Cloud to skip those sidecars.
    """
    profile = payload.get("profile")
    if not isinstance(profile, dict):
        return
    docker = profile.get("docker")
    if not isinstance(docker, dict) or docker.get("mode") != "none":
        return
    stack_services = rendered_stack.get("services")
    if not isinstance(stack_services, Mapping) or not stack_services:
        return
    docker["mode"] = "compose"


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
    if not env_passthrough_names and not env_passthrough_aliases:
        return None
    return {
        "schema": _HOSTED_AGENT_AUTH_SCHEMA,
        "env_passthrough_names": list(env_passthrough_names),
        "env_passthrough_aliases": [
            {"target": target, "source": source} for target, source in env_passthrough_aliases
        ],
        "file_auth_mount_targets": [],
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
    omit_credential_env_keys: bool = False,
    compose_dir: Path | None = None,
    env_file_base_path: Path | None = None,
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
            omit_credential_env_keys=omit_credential_env_keys,
            compose_dir=compose_dir,
            env_file_base_path=env_file_base_path,
        )
    return payload


def _hosted_validation_sanitize_compose_service(
    service: Mapping[str, object],
    *,
    volume_translations: Mapping[str, str],
    omit_credential_env_keys: bool = False,
    compose_dir: Path | None = None,
    env_file_base_path: Path | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    image = service.get("image")
    source_environment = service.get("environment")
    # Match profile payload: repo-relative env_file paths resolve from the
    # worktree when provided; compose_dir remains the Docker-relative fallback
    # and the base for compose ``.env`` image interpolation.
    env_file_base = env_file_base_path if env_file_base_path is not None else compose_dir
    inject_postgres_trust = (
        omit_credential_env_keys
        and (
            _hosted_validation_environment_declares_postgres_password(source_environment)
            or _hosted_validation_env_file_declares_postgres_password(
                service.get("env_file"),
                compose_dir=env_file_base,
            )
        )
        and _hosted_validation_compose_image_is_postgres_like(
            image,
            compose_dir=compose_dir,
        )
    )
    for key, value in service.items():
        field = str(key)
        if field == "environment":
            payload[field] = _hosted_validation_sanitize_compose_environment(
                value,
                inject_postgres_trust=inject_postgres_trust,
                omit_credential_env_keys=omit_credential_env_keys,
            )
            continue
        if field == "volumes":
            payload[field] = _hosted_validation_sanitize_compose_service_volumes(
                value,
                volume_translations=volume_translations,
            )
            continue
        payload[field] = _hosted_validation_sanitize_compose_value(value)
    if inject_postgres_trust and "environment" not in payload:
        payload["environment"] = {"POSTGRES_HOST_AUTH_METHOD": "trust"}
    return payload


def _hosted_validation_compose_image_is_postgres_like(
    image: object,
    *,
    compose_dir: Path | None = None,
) -> bool:
    if not isinstance(image, str) or not image.strip():
        return False
    for candidate in _hosted_validation_compose_image_candidates(
        image,
        compose_dir=compose_dir,
    ):
        repository = _hosted_validation_compose_image_repository_name(candidate)
        if (
            repository == "postgres"
            or repository.startswith("postgres-")
            or repository == "pgvector"
            or repository.startswith("pgvector-")
            or repository == "postgis"
            or repository.startswith("postgis-")
        ):
            return True
    return False


def _hosted_validation_compose_image_is_whole_interpolation(image: str) -> bool:
    """True when ``image`` is one full-value ``${...}`` (not a partial embedding)."""
    stripped = image.strip()
    if not stripped.startswith("${"):
        return False
    end = _hosted_validation_braced_expression_end(stripped, 1)
    return end is not None and end == len(stripped) - 1


def _hosted_validation_compose_image_candidates(
    image: str,
    *,
    compose_dir: Path | None = None,
) -> tuple[str, ...]:
    """Raw/expanded image strings for postgres detection.

    Expand compose ``.env`` then ``os.environ`` (shell wins); unset still yields
    ``:-``/``-`` defaults. When expansion succeeds, only the concrete expanded
    image is considered — the raw template can still parse as postgres-like via
    an inactive ``:-library/postgres`` (or similar) default path segment. Literal
    whole-interpolation operator arms are only considered when expansion cannot
    resolve the image — never inactive arms that expansion already deselected.
    """
    stripped = image.strip()
    candidates = [stripped]
    if "$" not in stripped:
        return tuple(candidates)
    environ: dict[str, str] = {}
    if compose_dir is not None:
        env_path = compose_dir / ".env"
        if env_path.is_file():
            with suppress(OSError, UnicodeDecodeError, ComposeEnvInterpolationError):
                environ.update(compose_env_file_values(env_path))
    environ.update(os.environ)
    try:
        expanded = compose_expand_value(stripped, environ=environ).strip()
    except ComposeEnvInterpolationError:
        if _hosted_validation_compose_image_is_whole_interpolation(stripped):
            for arm in _hosted_validation_compose_interpolation_operator_arms(stripped):
                arm_stripped = arm.strip()
                if arm_stripped and "$" not in arm_stripped and arm_stripped not in candidates:
                    candidates.append(arm_stripped)
        return tuple(candidates)
    if expanded:
        # Successful expansion: never keep the raw interpolated template, which
        # can still look postgres-like via inactive default path segments.
        return (expanded,)
    return tuple(candidates)


def _hosted_validation_compose_image_repository_name(image: str) -> str:
    """Repository leaf name; keep host:port (strip ``:tag`` only when no ``/`` in suffix)."""
    without_digest = image.split("@", 1)[0]
    colon = without_digest.rfind(":")
    if colon != -1 and "/" not in without_digest[colon + 1 :]:
        without_digest = without_digest[:colon]
    return without_digest.rsplit("/", 1)[-1].lower()


_POSTGRES_PASSWORD_DECLARATION_NAMES = frozenset(
    {
        # Official Docker Postgres entrypoint accepts either a literal password
        # or the Docker-secret *_FILE form as the password source.
        "POSTGRES_PASSWORD",
        "POSTGRES_PASSWORD_FILE",
    }
)


def _hosted_validation_environment_declares_postgres_password(environment: object) -> bool:
    if isinstance(environment, Mapping):
        return any(str(name) in _POSTGRES_PASSWORD_DECLARATION_NAMES for name in environment)
    if isinstance(environment, list):
        for item in environment:
            if not isinstance(item, str):
                continue
            if item in _POSTGRES_PASSWORD_DECLARATION_NAMES or any(
                item.startswith(f"{name}=") for name in _POSTGRES_PASSWORD_DECLARATION_NAMES
            ):
                return True
    return False


def _hosted_validation_env_file_declares_postgres_password(
    env_file: object,
    *,
    compose_dir: Path | None,
) -> bool:
    """Whether a Compose service env_file declares a Postgres password source.

    Detects ``POSTGRES_PASSWORD`` and ``POSTGRES_PASSWORD_FILE``, including bare
    pass-through lines without ``=/:`` (Compose env_file ``VAR`` form; same
    family as list ``environment: - POSTGRES_PASSWORD``). Scan keys only: full
    ``compose_env_file_values`` raises on unset required interpolations and
    would miss a sibling password declaration.
    """
    for env_file_path in compose_service_env_file_paths(env_file, compose_dir=compose_dir):
        try:
            text = env_file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            match = _COMPOSE_ENV_FILE_ASSIGNMENT_KEY_PATTERN.match(line)
            if match is not None and match.group("key") in _POSTGRES_PASSWORD_DECLARATION_NAMES:
                return True
            # Bare VAR (no =/:): Compose may pass through from the host env.
            bare_key = stripped.split("#", 1)[0].strip()
            if bare_key.startswith("export "):
                bare_key = bare_key[7:].strip()
            if bare_key in _POSTGRES_PASSWORD_DECLARATION_NAMES:
                return True
    return False


def _hosted_validation_env_key_is_credential(name: str) -> bool:
    # Exact client password names (PGPASSWORD, MYSQL_PWD) lack a separator before
    # PASSWORD/PWD, so the pattern alone misses them; mirror command-assignment keys.
    normalized = name.upper().replace("-", "_")
    return normalized in _HOSTED_COMMAND_SECRET_ASSIGNMENT_KEYS or bool(
        _SECRET_ENV_NAME_PATTERN.search(name)
    )


def _hosted_validation_env_key_is_safe_named_credential(name: str) -> bool:
    return bool(_SAFE_NAMED_CONNECTION_CREDENTIAL_ENV_NAME_PATTERN.search(name))


def _hosted_validation_braced_expression_end(value: str, open_brace_index: int) -> int | None:
    """Index of the ``}`` closing ``${...}`` at ``open_brace_index`` (nested-aware)."""
    depth = 1
    index = open_brace_index + 1
    while index < len(value):
        if value[index] == "$" and index + 1 < len(value) and value[index + 1] == "$":
            index += 2
            continue
        if value[index] == "$" and index + 1 < len(value) and value[index + 1] == "{":
            depth += 1
            index += 2
            continue
        if value[index] == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _hosted_validation_compose_interpolation_operator_arms(value: str) -> Iterator[str]:
    """Yield Compose ``:-``/``-``/``:+``/``+``/``:?``/``?`` arm texts (nested-aware)."""
    index = 0
    while index < len(value):
        dollar = value.find("$", index)
        if dollar < 0:
            return
        if dollar + 1 < len(value) and value[dollar + 1] == "$":
            index = dollar + 2
            continue
        if dollar + 1 < len(value) and value[dollar + 1] == "{":
            end = _hosted_validation_braced_expression_end(value, dollar + 1)
            if end is None:
                index = dollar + 1
                continue
            name_match = _COMPOSE_INTERPOLATION_NAME_PATTERN.match(value, dollar + 2)
            if name_match is not None and name_match.end() <= end:
                remainder = value[name_match.end() : end]
                for operator in _COMPOSE_INTERPOLATION_OPERATORS:
                    if remainder.startswith(operator):
                        arm = remainder[len(operator) :]
                        yield arm
                        yield from _hosted_validation_compose_interpolation_operator_arms(arm)
                        break
            index = end + 1
            continue
        index = dollar + 1


def _hosted_validation_env_reference_source_name(value: str) -> str | None:
    """Env var name when ``value`` is a single full-value Compose interpolation."""
    stripped = value.strip()
    if stripped.startswith("${"):
        end = _hosted_validation_braced_expression_end(stripped, 1)
        if end is None or end != len(stripped) - 1:
            return None
        name_match = _COMPOSE_INTERPOLATION_NAME_PATTERN.match(stripped, 2)
        if name_match is None or name_match.end() > end:
            return None
        return name_match.group(0)
    if stripped.startswith("$"):
        name_match = _COMPOSE_INTERPOLATION_NAME_PATTERN.match(stripped, 1)
        if name_match is None or name_match.end() != len(stripped):
            return None
        return name_match.group(0)
    return None


def _hosted_validation_env_reference_source_names(value: str) -> Iterator[str]:
    """Yield env names from Compose interpolations in ``value`` (nested arms too)."""
    index = 0
    while index < len(value):
        dollar = value.find("$", index)
        if dollar < 0:
            return
        if dollar + 1 < len(value) and value[dollar + 1] == "$":
            index = dollar + 2
            continue
        if dollar + 1 < len(value) and value[dollar + 1] == "{":
            end = _hosted_validation_braced_expression_end(value, dollar + 1)
            if end is None:
                index = dollar + 1
                continue
            name_match = _COMPOSE_INTERPOLATION_NAME_PATTERN.match(value, dollar + 2)
            if name_match is not None and name_match.end() <= end:
                yield name_match.group(0)
                remainder = value[name_match.end() : end]
                for operator in _COMPOSE_INTERPOLATION_OPERATORS:
                    if remainder.startswith(operator):
                        yield from _hosted_validation_env_reference_source_names(
                            remainder[len(operator) :]
                        )
                        break
            index = end + 1
            continue
        name_match = _COMPOSE_INTERPOLATION_NAME_PATTERN.match(value, dollar + 1)
        if name_match is not None:
            yield name_match.group(0)
            index = name_match.end()
            continue
        index = dollar + 1


def _hosted_validation_operator_arm_literal_is_secret(arm: str) -> bool:
    """True when a Compose operator arm holds a secret-valued literal (not a pure ref)."""

    stripped = arm.strip()
    if not stripped:
        return False
    if _ENV_REFERENCE_PATTERN.fullmatch(stripped) or _SHELL_ENV_REFERENCE_PATTERN.fullmatch(
        stripped
    ):
        return False
    if stripped.startswith("${"):
        end = _hosted_validation_braced_expression_end(stripped, 1)
        if end == len(stripped) - 1:
            name_match = _COMPOSE_INTERPOLATION_NAME_PATTERN.match(stripped, 2)
            if name_match is not None:
                remainder = stripped[name_match.end() : end]
                if not remainder or any(
                    remainder.startswith(operator) for operator in _COMPOSE_INTERPOLATION_OPERATORS
                ):
                    return False
    return _hosted_validation_value_has_encoded_secret_or_provider_ref(
        stripped
    ) or _hosted_validation_command_has_secret_assignment(stripped)


def _hosted_validation_should_omit_environment_entry(
    name: str,
    value: object,
    *,
    omit_credential_env_keys: bool,
) -> bool:
    """Whether omit mode should drop this Compose env entry.

    Drops credential-named keys, secret-valued entries, plain ``${NAME}`` URL/DSN
    refs, mapping pass-through URL/DSN slots (``NAME:`` / ``NAME: null`` →
    ``None``), credential-source refs under safe target names, and operator-arm
    credential literals (see regressions in hosted validation omit-mode tests).
    """
    if not omit_credential_env_keys:
        return False
    # Mapping pass-through slots deserialize as None; do not stringify to "None"
    # before the safe-name check, or Cloud keeps a broken credential slot.
    if value is None and _hosted_validation_env_key_is_safe_named_credential(name):
        return True
    text = str(value)
    if _hosted_validation_env_key_is_credential(name) or _hosted_validation_env_value_is_secret(
        name, text
    ):
        return True
    if any(
        _hosted_validation_env_key_is_credential(source_name)
        or _hosted_validation_env_key_is_safe_named_credential(source_name)
        for source_name in _hosted_validation_env_reference_source_names(text)
    ):
        return True
    if any(
        _hosted_validation_operator_arm_literal_is_secret(arm)
        for arm in _hosted_validation_compose_interpolation_operator_arms(text)
    ):
        return True
    source_name = _hosted_validation_env_reference_source_name(text)
    return _hosted_validation_env_key_is_safe_named_credential(name) and source_name is not None


def _hosted_validation_sanitize_compose_environment(
    environment: object,
    *,
    inject_postgres_trust: bool = False,
    omit_credential_env_keys: bool = False,
) -> Any:
    if isinstance(environment, Mapping):
        sanitized = {
            str(name): _hosted_validation_env_value(str(name), value)
            for name, value in environment.items()
            if not _hosted_validation_should_omit_environment_entry(
                str(name),
                value,
                omit_credential_env_keys=omit_credential_env_keys,
            )
        }
        if inject_postgres_trust:
            sanitized["POSTGRES_HOST_AUTH_METHOD"] = "trust"
        return sanitized
    if isinstance(environment, list):
        sanitized_list: list[Any] = []
        for item in environment:
            if isinstance(item, str) and "=" in item:
                name, _, value = item.partition("=")
                if _hosted_validation_should_omit_environment_entry(
                    name,
                    value,
                    omit_credential_env_keys=omit_credential_env_keys,
                ):
                    continue
                if inject_postgres_trust and name == "POSTGRES_HOST_AUTH_METHOD":
                    continue
                sanitized_list.append(f"{name}={_hosted_validation_env_value(name, value)}")
                continue
            if (
                isinstance(item, str)
                and omit_credential_env_keys
                and (
                    _hosted_validation_env_key_is_credential(item)
                    or _hosted_validation_env_key_is_safe_named_credential(item)
                )
            ):
                continue
            if (
                inject_postgres_trust
                and isinstance(item, str)
                and item == "POSTGRES_HOST_AUTH_METHOD"
            ):
                continue
            sanitized_list.append(_hosted_validation_sanitize_compose_value(item))
        if inject_postgres_trust:
            sanitized_list.append("POSTGRES_HOST_AUTH_METHOD=trust")
        return sanitized_list
    if inject_postgres_trust:
        return {"POSTGRES_HOST_AUTH_METHOD": "trust"}
    return _hosted_validation_sanitize_compose_value(environment)


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
    compose_dir: Path | None = None,
    profile_base_path: Path | None = None,
) -> dict[str, Any]:
    payload = profile.model_dump(mode="json", by_alias=True)
    # Cloud rejects non-empty profile.secrets (no Core-local secret resolution).
    payload["secrets"] = []
    if omit_runtime_environment:
        _hosted_validation_omit_environment_entries(
            payload.get("runtime"),
            names=omit_runtime_environment,
        )
    _hosted_validation_sanitize_environment_container(payload.get("runtime"))
    services = payload.get("services")
    env_file_base = profile_base_path if profile_base_path is not None else compose_dir
    if isinstance(services, list):
        for service in services:
            if not isinstance(service, dict):  # pragma: no cover
                continue
            inject_postgres_trust = (
                _hosted_validation_environment_declares_postgres_password(
                    service.get("environment")
                )
                or _hosted_validation_env_file_declares_postgres_password(
                    service.get("env_file"),
                    compose_dir=env_file_base,
                )
            ) and _hosted_validation_compose_image_is_postgres_like(
                service.get("image"),
                compose_dir=compose_dir,
            )
            _hosted_validation_sanitize_environment_container(
                service,
                inject_postgres_trust=inject_postgres_trust,
            )
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
    return _hosted_validation_env_key_is_credential(key)


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


def _hosted_validation_sanitize_environment_container(
    container: object,
    *,
    inject_postgres_trust: bool = False,
) -> None:
    """Sanitize profile runtime/service env for Cloud; inject Postgres trust when asked."""
    if not isinstance(container, dict):
        return
    environment = container.get("environment")
    if not isinstance(environment, dict):
        return
    sanitized: dict[str, str] = {}
    for name, value in environment.items():
        name_str = str(name)
        text = str(value)
        if _hosted_validation_env_key_is_credential(name_str):
            continue
        passwordless = _hosted_validation_passwordless_postgres_url(text)
        if passwordless is not None:
            if (
                _hosted_validation_url_has_path_query_or_fragment_credentials(passwordless)
                or _hosted_validation_should_omit_profile_environment_entry(name_str, passwordless)
                # Username-only userinfo is intentional after password strip; do not
                # treat ``user@host`` as residual secret material.
                or _hosted_validation_value_has_encoded_secret_or_provider_ref(
                    passwordless,
                    include_url_userinfo=False,
                )
            ):
                continue
            sanitized[name_str] = passwordless
            continue
        if _hosted_validation_should_omit_profile_environment_entry(name_str, text):
            continue
        sanitized[name_str] = _hosted_validation_env_value(name_str, value)
    if inject_postgres_trust:
        sanitized["POSTGRES_HOST_AUTH_METHOD"] = "trust"
    container["environment"] = sanitized


def _hosted_validation_should_omit_profile_environment_entry(name: str, text: str) -> bool:
    """Omit profile env with credential-source refs or secret operator arms."""
    if any(
        _hosted_validation_env_key_is_credential(source_name)
        or _hosted_validation_env_key_is_safe_named_credential(source_name)
        for source_name in _hosted_validation_env_reference_source_names(text)
    ):
        return True
    if any(
        _hosted_validation_operator_arm_literal_is_secret(arm)
        for arm in _hosted_validation_compose_interpolation_operator_arms(text)
    ):
        return True
    source_name = _hosted_validation_env_reference_source_name(text)
    return _hosted_validation_env_key_is_safe_named_credential(name) and source_name is not None


def _hosted_validation_passwordless_postgres_url(value: str) -> str | None:
    """Postgres URL with password removed; strip credential usernames from userinfo.

    Host-only stays unchanged. Non-Postgres returns ``None`` for ordinary omit/redact.
    """
    stripped = value.strip()
    try:
        parsed = urlsplit(stripped)
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if not (scheme in {"postgres", "postgresql"} or scheme.startswith("postgresql+")):
        return None
    authority = parsed.netloc
    if "@" not in authority:
        return stripped
    userinfo, _, host = authority.rpartition("@")
    username, password_separator, _password = userinfo.partition(":")
    if not password_separator:
        if _hosted_validation_postgres_userinfo_has_credentials(userinfo):
            return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
        return stripped
    # Password stripped; still drop credential usernames (e.g. ${POSTGRES_PASSWORD}:x)
    # so callers that accept the rewrite never ship secret refs/tokens in userinfo.
    if username and _hosted_validation_postgres_userinfo_has_credentials(username):
        return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
    new_authority = f"{username}@{host}" if username else host
    return urlunsplit((parsed.scheme, new_authority, parsed.path, parsed.query, parsed.fragment))


def _hosted_validation_postgres_userinfo_has_credentials(userinfo: str) -> bool:
    return any(
        bool(_SECRET_VALUE_PATTERN.search(component))
        or bool(_PROVIDER_REF_PATTERN.search(component))
        or any(
            _hosted_validation_env_key_is_credential(source_name)
            or _hosted_validation_env_key_is_safe_named_credential(source_name)
            for source_name in _hosted_validation_env_reference_source_names(component)
        )
        for component in compose_helpers._url_component_variants(userinfo)
    )


def _hosted_validation_url_has_path_query_or_fragment_credentials(value: str) -> bool:
    """Return whether URL path, query, or fragment carries secret credential fields.

    Used after passwordless Postgres rewrite where ``user@host`` userinfo must
    stay allowed: scan non-userinfo components (including decoded variants) so
    path fields like ``/db;password=...`` and nested credentialed URLs cannot
    bypass omit. Authority userinfo is intentionally out of scope here.
    """
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return any(
        compose_helpers._url_component_has_secret_credential_field(component)
        or compose_helpers._value_has_url_userinfo(component)
        for raw_component in (parsed.path, parsed.query, parsed.fragment)
        for component in compose_helpers._url_component_variants(raw_component)
    )


def _hosted_validation_env_value(name: str, value: object) -> str:
    text = str(value)
    if _hosted_validation_env_value_is_secret(name, text):
        return f"${{{name}}}" if _ENV_NAME_PATTERN.fullmatch(name) else "<redacted>"
    return text


def _hosted_validation_env_value_is_secret(name: str, value: str) -> bool:
    stripped = value.strip()
    if not stripped or _ENV_REFERENCE_PATTERN.fullmatch(stripped):
        return False
    return _hosted_validation_env_key_is_credential(
        name
    ) or _hosted_validation_value_has_encoded_secret_or_provider_ref(stripped)


def _hosted_validation_value_has_encoded_secret_or_provider_ref(
    value: str,
    *,
    include_url_userinfo: bool = True,
) -> bool:
    """True when raw or URL-decoded variants look like secret material.

    Scans known tokens, provider refs, bearer headers, credentialed URLs, PEM
    headers, and multiline payloads so percent-encoded forms cannot bypass omit
    / redaction checks that already catch the decoded equivalents.

    Pass ``include_url_userinfo=False`` after a passwordless Postgres rewrite so
    intentional ``user@host`` authority is kept while tokens/refs/bearers/PEMs
    still omit. Nested credentialed URLs in path/query/fragment are handled by
    ``_hosted_validation_url_has_path_query_or_fragment_credentials``.
    """
    for variant in compose_helpers._url_component_variants(value):
        if (
            bool(_SECRET_VALUE_PATTERN.search(variant))
            or bool(_PROVIDER_REF_PATTERN.search(variant))
            or bool(_HOSTED_COMMAND_BEARER_PATTERN.search(variant))
            or "-----BEGIN " in variant
            or "\n" in variant
        ):
            return True
        if include_url_userinfo and _hosted_validation_value_has_url_credentials(variant):
            return True
    return False


def _hosted_validation_value_has_url_credentials(value: str) -> bool:
    return compose_helpers._value_has_url_userinfo(value)
