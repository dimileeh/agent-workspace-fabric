"""Trusted workspace runtime context for agent prompts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from awf.common.audit import redact_audit_text
from awf.profiles.compose import profile_app_endpoint_environment
from awf.profiles.models import DockerMode, ProfileService, WorkspaceProfile

_CONNECTION_ENV_KEY_PARTS = (
    "URL",
    "URI",
    "DSN",
    "DATABASE",
    "POSTGRES",
    "REDIS",
    "CACHE",
    "MONGO",
    "MYSQL",
)
_SENSITIVE_ENV_KEY_PARTS = ("PASSWORD", "PASSWD", "SECRET", "TOKEN", "API_KEY", "AUTH")


@dataclass(frozen=True)
class _EnvEndpoint:
    key: str
    value: str
    host: str
    port: int | None

    @property
    def endpoint(self) -> str:
        if self.port is None:
            return self.host
        return f"{self.host}:{self.port}"


def render_workspace_runtime_context(profile: WorkspaceProfile) -> str:
    """Render trusted profile service/env context for non-interactive agents.

    The generated text intentionally does not expose service secrets. Agents
    should use the live environment variables inside the workspace container,
    while the prompt only explains where sidecars are reachable.
    """

    services = tuple(profile.services)
    environment, generated_endpoint_keys = _prompt_environment(profile)
    env_endpoints = _env_endpoints(environment, services)
    env_lines = _environment_lines(environment, env_endpoints, generated_endpoint_keys)
    if not services and not env_lines:
        return ""

    lines = [
        "### Workspace runtime context",
        "Trusted AWF-provided context from the resolved workspace profile:",
    ]
    if services:
        lines.append("Services:")
        lines.extend(_service_lines(services, env_endpoints))
    if env_lines:
        lines.append("Agent environment:")
        lines.extend(env_lines)
        lines.append(
            "- Use these environment variables directly inside the workspace; "
            "do not copy redacted prompt values into code or tests."
        )
    if services:
        lines.append(
            "- Important: localhost is the agent container. Use Compose service "
            "DNS names such as `postgres:5432` for sidecars, not `localhost`."
        )
        if profile.docker.mode == DockerMode.none:
            lines.append(
                "- Docker may be unavailable in this workspace; AWF already started "
                "the declared sidecar services."
            )
        else:
            lines.append(
                "- AWF already started the declared sidecar services; do not start "
                "duplicate copies for ordinary validation."
            )
    return "\n".join(lines)


def render_workspace_runtime_context_section(workspace_runtime_context: str) -> str:
    """Render optional trusted runtime context as a prompt section."""

    context = workspace_runtime_context.strip()
    if not context:
        return ""
    return f"{context}\n\n"


def _service_lines(
    services: tuple[ProfileService, ...],
    env_endpoints: tuple[_EnvEndpoint, ...],
) -> list[str]:
    lines: list[str] = []
    for service in services:
        endpoints = _service_endpoints(service, env_endpoints)
        endpoint_text = (
            " internal endpoints: " + ", ".join(f"`{endpoint}`" for endpoint in endpoints)
            if endpoints
            else f" Compose DNS: `{service.name}`"
        )
        health_text = "; AWF waits for its healthcheck" if service.healthcheck_cmd else ""
        lines.append(
            f"- Service `{service.name}` is already started by AWF;{endpoint_text}{health_text}."
        )
    return lines


def _service_endpoints(
    service: ProfileService,
    env_endpoints: tuple[_EnvEndpoint, ...],
) -> tuple[str, ...]:
    endpoints = {endpoint.endpoint for endpoint in env_endpoints if endpoint.host == service.name}
    for container_port, _host_port in service.ports:
        endpoints.add(f"{service.name}:{container_port}")
    return tuple(sorted(endpoints))


def _environment_lines(
    environment: Mapping[str, str],
    env_endpoints: tuple[_EnvEndpoint, ...],
    generated_endpoint_keys: frozenset[str],
) -> list[str]:
    endpoint_keys = {endpoint.key for endpoint in env_endpoints}
    lines: list[str] = []
    for key, value in sorted(environment.items()):
        if key not in endpoint_keys and key not in generated_endpoint_keys:
            continue
        if _is_sensitive_env_key(key):
            lines.append(f"- `${key}` is set.")
            continue
        lines.append(f"- `${key}` = `{redact_audit_text(value, limit=500)}`")
    return lines


def _env_endpoints(
    environment: Mapping[str, str],
    services: tuple[ProfileService, ...],
) -> tuple[_EnvEndpoint, ...]:
    service_names = {service.name for service in services}
    endpoints: list[_EnvEndpoint] = []
    for key, value in sorted(environment.items()):
        endpoint = _env_endpoint(key=key, value=value)
        if endpoint is not None and endpoint.host in service_names:
            endpoints.append(endpoint)
    return tuple(endpoints)


def _prompt_environment(profile: WorkspaceProfile) -> tuple[dict[str, str], frozenset[str]]:
    generated_endpoint_env = profile_app_endpoint_environment(profile)
    environment = dict(profile.runtime.environment)
    environment.update(generated_endpoint_env)
    return environment, frozenset(key for key, _value in generated_endpoint_env)


def _env_endpoint(*, key: str, value: str) -> _EnvEndpoint | None:
    if not _is_connection_env_key(key) and "://" not in value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if not parsed.scheme or not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        port = None
    return _EnvEndpoint(key=key, value=value, host=parsed.hostname, port=port)


def _is_connection_env_key(key: str) -> bool:
    upper = key.upper()
    return any(part in upper for part in _CONNECTION_ENV_KEY_PARTS)


def _is_sensitive_env_key(key: str) -> bool:
    upper = key.upper()
    return any(part in upper for part in _SENSITIVE_ENV_KEY_PARTS)
