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
    validate_production_settings,
)

DEFAULT_LOCAL_SERVICE_DATABASE_URL = DEFAULT_LOCAL_DATABASE_URL
DEFAULT_LOCAL_SERVICE_WORK_DIR = "~/.awf/service"
DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID = "local"
_PROJECT_DEFAULT_WORK_DIR = str(Settings.model_fields["work_dir"].default)
LOCAL_SERVICE_COMPOSE_FILE = Path("docker/compose/local-service.yml")
LOCAL_SERVICE_COMPOSE_ENV_FILE = Path("docker/compose/.env")


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
    pydantic-settings count as explicit.
    """

    settings = base or Settings()
    env = os.environ if environ is None else environ
    work_dir_env = local_service_environ(env) if environ is None else env
    database_url = settings.database_url

    database_url_explicit = _has_env_key(env, "AWF_DATABASE_URL")
    if environ is None:
        database_url_explicit = database_url_explicit or "database_url" in settings.model_fields_set

    if not database_url_explicit:
        database_url = DEFAULT_LOCAL_SERVICE_DATABASE_URL

    work_dir = _resolve_service_work_dir(settings, work_dir_env, host_environ=env)
    validate_production_settings(settings, database_url=database_url)

    return ServiceSettings(
        service_name=settings.service_name,
        env=settings.env,
        api_base_url=settings.api_base_url,
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
    if env_file.exists():
        merged.update(
            {key: value for key, value in dotenv_values(env_file).items() if value is not None}
        )
    merged.update(os.environ if environ is None else dict(environ))
    _populate_compose_postgres_password(merged)
    return merged


def resolve_local_service_provider_environ(
    *,
    provider_environ: Mapping[str, str] | None,
    environ: Mapping[str, str],
    compose_file: Path | None,
    compose_env_file: Path | None,
) -> Mapping[str, str]:
    """Resolve provider auth inputs from explicit or adjacent Compose env files."""

    if provider_environ is not None:
        return provider_environ

    env_file = compose_env_file
    if env_file is None and compose_file is not None:
        candidate = compose_file.parent / ".env"
        if candidate.exists():
            env_file = candidate
    if env_file is None:
        return environ
    return local_service_environ(environ, env_file=env_file)


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


def _has_env_key(environ: Mapping[str, str], key: str) -> bool:
    """Return true when ``environ`` contains ``key`` using case-insensitive matching."""
    wanted = key.upper()
    return any(existing.upper() == wanted for existing in environ)


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
