"""Hosted delegation request payload and profile sanitization helpers."""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from awf.adapters.runtime_executor import AgentRuntimeExecRequest
from awf.common.logging import get_logger
from awf.common.redaction import redact_secrets
from awf.common.token_patterns import TOKEN_ASSIGNMENT_KEY_PATTERN, compile_known_token_re
from awf.profiles.models import WorkspaceProfile

_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_REFERENCE_PATTERN = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_SECRET_ENV_NAME_PATTERN = re.compile(
    rf"^(?:{TOKEN_ASSIGNMENT_KEY_PATTERN})$|"
    r"(?:^|[_-])(?:TOKEN|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|"
    r"PASSWORD|PASSWD|SECRET|CREDENTIALS?)(?:[_-]|$)",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERN = compile_known_token_re(match_truncated_provider_tokens=False)
_URL_WITH_USERINFO_PATTERN = re.compile(
    r"\b(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<userinfo>[^/?#\s@]+)@"
)
_HOSTED_PR_IDENTITY_URL_FIELDS = frozenset({"repo_url", "head_repo_url"})
_HOSTED_RENDERED_STACK_SCHEMA = "hosted_validation_rendered_stack.v1"
_HOSTED_COVERAGE_OMITTED_RUNTIME_ENV = frozenset({"PIP_EXTRA_INDEX_URL", "PIP_INDEX_URL"})
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
                value = _strip_url_userinfo(value)
            identity[key] = value
    if request.owned_paths:
        identity["owned_paths"] = list(request.owned_paths)
    return identity


def _hosted_pr_identity_payload(identity: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(identity)
    for key in _HOSTED_PR_IDENTITY_URL_FIELDS:
        value = payload.get(key)
        if isinstance(value, str):
            payload[key] = _strip_url_userinfo(value)
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

    rendered_stack: dict[str, Any] = {
        "schema": _HOSTED_RENDERED_STACK_SCHEMA,
        "compose_project": compose_project,
        "compose_file_path": str(compose_file),
        "services": _hosted_validation_rendered_stack_services(parsed.get("services")),
    }
    for key in ("volumes", "networks"):
        value = parsed.get(key)
        if isinstance(value, Mapping):
            rendered_stack[key] = _hosted_validation_sanitize_compose_value(value)
    return rendered_stack


def _hosted_validation_attach_rendered_stack(
    payload: dict[str, Any],
    *,
    compose_project: str,
    compose_file: Path,
) -> None:
    rendered_stack = _hosted_validation_rendered_stack_payload(
        compose_project=compose_project,
        compose_file=compose_file,
    )
    if rendered_stack is not None:
        payload["rendered_stack"] = rendered_stack


def _hosted_validation_rendered_stack_services(services: object) -> dict[str, Any]:
    """Return sanitized non-agent services from a rendered compose document."""
    if not isinstance(services, Mapping):
        return {}
    payload: dict[str, Any] = {}
    for name, service in services.items():
        service_name = str(name)
        if service_name == "agent" or not isinstance(service, Mapping):
            continue
        payload[service_name] = _hosted_validation_sanitize_compose_service(service)
    return payload


def _hosted_validation_sanitize_compose_service(service: Mapping[str, object]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in service.items():
        field = str(key)
        if field == "environment":
            payload[field] = _hosted_validation_sanitize_compose_environment(value)
            continue
        payload[field] = _hosted_validation_sanitize_compose_value(value)
    return payload


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


def _strip_url_userinfo(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or "@" not in parsed.netloc:
        return value
    userinfo, _, authority = parsed.netloc.rpartition("@")
    if parsed.scheme.lower() in {"ssh", "git+ssh"}:
        username, password_separator, _ = userinfo.partition(":")
        if not password_separator:
            return value
        if username:
            authority = f"{username}@{authority}"
    return urlunsplit(
        (
            parsed.scheme,
            authority,
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


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
    return payload


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
    for match in _URL_WITH_USERINFO_PATTERN.finditer(value):
        scheme = match.group("scheme").lower()
        userinfo = match.group("userinfo")
        if scheme in {"ssh", "git+ssh"} and userinfo == "git":
            continue
        return True
    return False
