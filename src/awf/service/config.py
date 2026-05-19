"""Resolved settings for the local AWF service runtime."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import make_url

from awf.common.config import (
    DEFAULT_COMPLETED_WORKSPACE_RETENTION_HOURS,
    DEFAULT_LOCAL_DATABASE_URL,
    DEFAULT_MIN_FREE_DISK_BYTES,
    DEFAULT_WORKSPACE_CLEANUP_BATCH_LIMIT,
    DEFAULT_WORKSPACE_CLEANUP_SCAN_INTERVAL_SECONDS,
    Settings,
    settings_constructor_fields,
    validate_production_settings,
)

DEFAULT_LOCAL_SERVICE_DATABASE_URL = DEFAULT_LOCAL_DATABASE_URL
DEFAULT_LOCAL_SERVICE_API_BASE_URL = str(Settings.model_fields["api_base_url"].default)
DEFAULT_LOCAL_SERVICE_WORK_DIR = "~/.awf/service"
DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID = "local"
_PROJECT_DEFAULT_WORK_DIR = str(Settings.model_fields["work_dir"].default)
LOCAL_SERVICE_COMPOSE_ENV_FILE = Path("docker/compose/.env")
_PROJECT_ROOT_MARKER = ".git"
_AWF_SOURCE_ROOT_MARKERS = (
    "pyproject.toml",
    "src/awf/__init__.py",
    "docker/compose/local-service.yml",
)


@dataclass(frozen=True, kw_only=True)
class ServiceSettings:
    """Settings used by local service commands and containers."""

    service_name: str
    env: str
    api_base_url: str
    console_url: str | None = None
    database_url: str
    docker_host: str
    agent_runtime_image: str
    work_dir: str
    api_token: str | None
    github_token: str | None
    worker_poll_interval_seconds: float
    worker_max_concurrent_provisions: int
    worker_max_concurrent_executions: int = 3
    agent_wall_timeout_seconds: float = 7200
    agent_idle_timeout_seconds: float = 3600
    planning_max_iterations_default: int = 3
    host_home: str = "~"
    node_id: str | None = None
    branch_prefix: str = "awf"
    min_free_disk_bytes: int = DEFAULT_MIN_FREE_DISK_BYTES
    completed_workspace_retention_hours: float = DEFAULT_COMPLETED_WORKSPACE_RETENTION_HOURS
    workspace_cleanup_enabled: bool = True
    workspace_cleanup_scan_interval_seconds: float = DEFAULT_WORKSPACE_CLEANUP_SCAN_INTERVAL_SECONDS
    workspace_cleanup_batch_limit: int = DEFAULT_WORKSPACE_CLEANUP_BATCH_LIMIT
    network_posture_open_legacy_cutoff: datetime | None = None
    local_capacity_cpu_cores: float | None = None
    local_capacity_memory_gb: float | None = None
    local_capacity_dind_slots: int | None = None


def resolve_service_settings(
    base: Settings | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ServiceSettings:
    """Resolve settings for service mode.

    AWF is PostgreSQL-only. Service mode uses the configured database URL, with
    the local Postgres URL as the default unless ``AWF_DATABASE_URL`` is
    explicitly set. When ``environ`` is ``None``, values loaded from ``.env`` by
    pydantic-settings count as explicit. When ``environ`` is provided, only the
    provided environment or direct ``base`` constructor overrides count.
    """

    settings = base or Settings()
    env = os.environ if environ is None else environ
    service_env = local_service_environ(env) if environ is None else env
    database_url = settings.database_url

    database_url_explicit = _database_url_env_is_explicit(env)
    if not database_url_explicit:
        database_url_explicit = _settings_database_url_is_explicit(
            settings,
            service_env,
            require_init_field=environ is not None,
        )

    if not database_url_explicit:
        database_url = _default_local_service_database_url(service_env)

    api_base_url = _resolve_service_api_base_url(
        settings,
        env,
        service_env,
        require_init_field=environ is not None,
    )
    work_dir = _resolve_service_work_dir(settings, service_env, host_environ=env)
    validate_production_settings(settings, database_url=database_url)

    return ServiceSettings(
        service_name=settings.service_name,
        env=settings.env,
        api_base_url=api_base_url,
        console_url=settings.console_url,
        database_url=database_url,
        docker_host=settings.docker_host,
        agent_runtime_image=settings.agent_runtime_image,
        work_dir=work_dir,
        min_free_disk_bytes=settings.min_free_disk_bytes,
        host_home=settings.host_home or "~",
        api_token=_empty_to_none(settings.api_token),
        github_token=_resolve_github_token(settings.github_token, env),
        worker_poll_interval_seconds=settings.worker_poll_interval_seconds,
        worker_max_concurrent_provisions=settings.worker_max_concurrent_provisions,
        worker_max_concurrent_executions=settings.worker_max_concurrent_executions,
        agent_wall_timeout_seconds=settings.agent_wall_timeout_seconds,
        agent_idle_timeout_seconds=settings.agent_idle_timeout_seconds,
        planning_max_iterations_default=settings.planning_max_iterations_default,
        node_id=_empty_to_none(settings.worker_node_id) or DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID,
        branch_prefix=settings.worker_branch_prefix,
        completed_workspace_retention_hours=settings.completed_workspace_retention_hours,
        workspace_cleanup_enabled=settings.workspace_cleanup_enabled,
        workspace_cleanup_scan_interval_seconds=settings.workspace_cleanup_scan_interval_seconds,
        workspace_cleanup_batch_limit=settings.workspace_cleanup_batch_limit,
        network_posture_open_legacy_cutoff=settings.network_posture_open_legacy_cutoff,
        local_capacity_cpu_cores=settings.local_capacity_cpu_cores,
        local_capacity_memory_gb=settings.local_capacity_memory_gb,
        local_capacity_dind_slots=settings.local_capacity_dind_slots,
    )


def service_config_payload(settings: ServiceSettings) -> dict[str, object]:
    """Return JSON-serializable service settings with secrets redacted."""

    payload: dict[str, object] = asdict(settings)
    payload["database_url"] = _redact_database_url(settings.database_url)
    if settings.network_posture_open_legacy_cutoff is not None:
        payload["network_posture_open_legacy_cutoff"] = (
            settings.network_posture_open_legacy_cutoff.isoformat()
        )
    for key in ("api_token", "github_token"):
        if payload.get(key):
            payload[key] = "<redacted>"
    return payload


def local_service_environ(
    environ: Mapping[str, str] | None = None,
    *,
    env_file: Path = LOCAL_SERVICE_COMPOSE_ENV_FILE,
) -> dict[str, str]:
    """Return the environment local Compose services actually receive.

    Docker Compose resolves variables from its env file and then lets the host
    shell override them. The CLI uses this merged view for readiness checks so a
    token present in ``docker/compose/.env`` is not incorrectly reported as
    missing just because it is absent from the host shell.
    """

    merged: dict[str, str] = {}
    resolved_env_file = resolve_local_service_compose_env_file(env_file)
    if resolved_env_file is not None:
        merged.update(
            {
                key: value
                for key, value in dotenv_values(resolved_env_file).items()
                if value is not None
            }
        )
    merged.update(os.environ if environ is None else dict(environ))
    _populate_compose_postgres_password(merged)
    return merged


def resolve_local_service_compose_env_file(
    env_file: Path = LOCAL_SERVICE_COMPOSE_ENV_FILE,
) -> Path | None:
    """Resolve the default local service Compose env file from nested commands."""

    expanded = env_file.expanduser()
    if expanded.is_absolute():
        return expanded if expanded.exists() else None

    candidates: list[Path] = []
    if expanded == LOCAL_SERVICE_COMPOSE_ENV_FILE:
        cwd = Path.cwd().resolve()
        candidates.extend(root / expanded for root in _bounded_env_search_roots(cwd))
        module_file = Path(__file__).resolve()
        candidates.extend(
            root / expanded
            for root in _bounded_env_search_roots(
                module_file.parent,
                require_awf_source_root=True,
            )
        )
    else:
        candidates.append(expanded.resolve())

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            return resolved
    return None


def _bounded_env_search_roots(
    start: Path,
    *,
    require_awf_source_root: bool = False,
) -> tuple[Path, ...]:
    """Return ancestor roots bounded by the first recognizable project root."""

    roots = (start, *start.parents)
    predicate = _is_awf_source_root if require_awf_source_root else _is_project_root
    for index, root in enumerate(roots):
        if predicate(root):
            return roots[: index + 1]
    return (start,)


def _is_project_root(candidate: Path) -> bool:
    return (candidate / _PROJECT_ROOT_MARKER).exists()


def _is_awf_source_root(candidate: Path) -> bool:
    return all((candidate / marker).exists() for marker in _AWF_SOURCE_ROOT_MARKERS)


def _populate_compose_postgres_password(environ: dict[str, str]) -> None:
    """Expose the local Postgres password as the variable Compose interpolates."""

    if _env_value(environ, "AWF_POSTGRES_PASSWORD"):
        return
    database_url = _env_value(environ, "AWF_DATABASE_URL")
    if not database_url:
        return
    try:
        password = make_url(database_url).password
    except Exception:
        return
    if password:
        environ["AWF_POSTGRES_PASSWORD"] = password


def _default_local_service_database_url(environ: Mapping[str, str]) -> str:
    """Return the host-side local Postgres URL matching Compose port overrides."""

    host_port = _env_value(environ, "AWF_POSTGRES_HOST_PORT")
    if not host_port:
        return DEFAULT_LOCAL_SERVICE_DATABASE_URL
    parsed_port = _parse_host_port("AWF_POSTGRES_HOST_PORT", host_port)
    url = make_url(DEFAULT_LOCAL_SERVICE_DATABASE_URL).set(port=parsed_port)
    return url.render_as_string(hide_password=False)


def _resolve_service_api_base_url(
    settings: Settings,
    environ: Mapping[str, str],
    service_environ: Mapping[str, str],
    *,
    require_init_field: bool = False,
) -> str:
    """Return the host-side API base URL matching Compose port overrides."""

    host_api_base_url = _env_value(environ, "AWF_API_BASE_URL")
    if host_api_base_url is not None and _api_base_url_is_explicit(
        host_api_base_url,
        environ,
    ):
        return host_api_base_url
    if _settings_api_base_url_is_explicit(
        settings,
        service_environ,
        require_init_field=require_init_field,
    ):
        return settings.api_base_url
    service_api_base_url = _env_value(service_environ, "AWF_API_BASE_URL")
    if service_api_base_url is not None and _api_base_url_is_explicit(
        service_api_base_url,
        service_environ,
    ):
        return service_api_base_url
    return _default_local_service_api_base_url(service_environ)


def _default_local_service_api_base_url(environ: Mapping[str, str]) -> str:
    """Return the host-side local API URL matching Compose port overrides."""

    host_port = _env_value(environ, "AWF_API_HOST_PORT")
    if not host_port:
        return DEFAULT_LOCAL_SERVICE_API_BASE_URL
    parsed_port = _parse_host_port("AWF_API_HOST_PORT", host_port)
    return f"http://localhost:{parsed_port}"


def _parse_host_port(env_key: str, value: str) -> int:
    """Parse a Compose host port override into a TCP port number."""

    invalid_port_message = f"{env_key} must be an integer between 1 and 65535; got {value!r}"
    try:
        parsed_port = int(value)
    except ValueError as exc:
        raise ValueError(invalid_port_message) from exc
    if not 1 <= parsed_port <= 65535:
        raise ValueError(invalid_port_message)
    return parsed_port


def _settings_api_base_url_is_explicit(
    settings: Settings,
    environ: Mapping[str, str],
    *,
    require_init_field: bool = False,
) -> bool:
    """Return true when settings carries a non-derivable API base URL."""

    if "api_base_url" in _settings_init_fields(settings):
        return True
    if require_init_field or "api_base_url" not in settings.model_fields_set:
        return False
    return _api_base_url_is_explicit(settings.api_base_url, environ)


def _api_base_url_is_explicit(api_base_url: str, environ: Mapping[str, str]) -> bool:
    """Return true when the API base URL should not be derived from Compose ports.

    A default-valued ``AWF_API_BASE_URL`` is treated as non-explicit when
    ``AWF_API_HOST_PORT`` is also present, allowing the port-derived URL to
    replace a stale default that was left unchanged.
    """

    return not (
        api_base_url == DEFAULT_LOCAL_SERVICE_API_BASE_URL
        and _env_value(environ, "AWF_API_HOST_PORT")
    )


def _settings_database_url_is_explicit(
    settings: Settings,
    environ: Mapping[str, str],
    *,
    require_init_field: bool = False,
) -> bool:
    """Return true when settings carries a non-derivable database URL.

    A default-valued database URL is treated as derivable when
    ``AWF_POSTGRES_HOST_PORT`` is present, unless the value came directly from a
    ``Settings(...)`` constructor override.
    """

    if "database_url" in _settings_init_fields(settings):
        return True
    if require_init_field or "database_url" not in settings.model_fields_set:
        return False
    return not (
        settings.database_url == DEFAULT_LOCAL_SERVICE_DATABASE_URL
        and _env_value(environ, "AWF_POSTGRES_HOST_PORT")
    )


def _database_url_env_is_explicit(environ: Mapping[str, str]) -> bool:
    """Return true when the host environment carries a non-derivable database URL.

    A default-valued ``AWF_DATABASE_URL`` is treated as non-explicit when
    ``AWF_POSTGRES_HOST_PORT`` is also present, allowing the port-derived URL to
    replace a stale default that was left unchanged.
    """

    database_url = _env_value(environ, "AWF_DATABASE_URL")
    if database_url is None:
        return False
    return not (
        database_url == DEFAULT_LOCAL_SERVICE_DATABASE_URL
        and _env_value(environ, "AWF_POSTGRES_HOST_PORT")
    )


def _settings_init_fields(settings: Settings) -> frozenset[str]:
    """Return direct constructor-provided settings fields."""

    return settings_constructor_fields(settings)


def _env_value(environ: Mapping[str, str], key: str) -> str | None:
    """Return an environment value using case-insensitive key matching."""
    wanted = key.upper()
    for existing, value in environ.items():
        if existing.upper() == wanted:
            return value
    return None


def _resolve_service_work_dir(
    settings: Settings,
    environ: Mapping[str, str],
    *,
    host_environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve the host state root used by local service Compose.

    Docker Compose maps ``AWF_HOST_WORK_DIR`` into the service container as
    ``AWF_WORK_DIR``. Local service CLI commands need to honor the same host
    root so dry-run and cleanup actions inspect the resources that readiness
    reports from the running service.
    """

    host_env = environ if host_environ is None else host_environ
    host_work_dir = _env_value(host_env, "AWF_HOST_WORK_DIR")
    if host_work_dir:
        return host_work_dir
    awf_work_dir = _env_value(host_env, "AWF_WORK_DIR")
    if awf_work_dir and not _is_project_default_work_dir(awf_work_dir):
        return awf_work_dir
    if "work_dir" in settings.model_fields_set and not _is_project_default_work_dir(
        settings.work_dir
    ):
        return settings.work_dir
    host_work_dir = _env_value(environ, "AWF_HOST_WORK_DIR")
    if host_work_dir:
        return host_work_dir
    awf_work_dir = _env_value(environ, "AWF_WORK_DIR")
    if awf_work_dir and not _is_project_default_work_dir(awf_work_dir):
        return awf_work_dir
    home = _env_value(environ, "HOME") or settings.host_home or "~"
    return str(Path(home).expanduser() / ".awf" / "service")


def _is_project_default_work_dir(value: str) -> bool:
    """Return true when ``value`` is the generic project-local AWF work directory."""
    return value.strip() == _PROJECT_DEFAULT_WORK_DIR


def _empty_to_none(value: str | None) -> str | None:
    """Normalize optional environment values by treating empty strings as unset."""
    return value or None


def _resolve_github_token(settings_value: str | None, environ: Mapping[str, str]) -> str | None:
    """Resolve the GitHub token accepted by local service mode.

    ``AWF_GITHUB_TOKEN`` is the documented setting, but local shells and CI often
    already expose ``GH_TOKEN`` or ``GITHUB_TOKEN``. Accepting those fallbacks
    keeps the service worker authenticated after restarts without requiring a
    separate token export.
    """

    return (
        _empty_to_none(settings_value)
        or _empty_to_none(environ.get("AWF_GITHUB_TOKEN"))
        or _empty_to_none(environ.get("GH_TOKEN"))
        or _empty_to_none(environ.get("GITHUB_TOKEN"))
    )


def _redact_database_url(value: str) -> str:
    """Return a database URL string with credentials hidden from operator payloads."""
    try:
        return make_url(value).render_as_string(hide_password=True)
    except Exception:
        return "<redacted>" if value else value
