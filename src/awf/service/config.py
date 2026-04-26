"""Resolved settings for the local AWF service runtime."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from sqlalchemy.engine import make_url

from awf.common.config import DEFAULT_MIN_FREE_DISK_BYTES, Settings

DEFAULT_LOCAL_SERVICE_DATABASE_URL = "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID = "local"


@dataclass(frozen=True, kw_only=True)
class ServiceSettings:
    """Settings used by local service commands and containers."""

    service_name: str
    env: str
    api_base_url: str
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
    agent_idle_timeout_seconds: float = 900
    host_home: str = "~"
    node_id: str | None = None
    branch_prefix: str = "awf"
    min_free_disk_bytes: int = DEFAULT_MIN_FREE_DISK_BYTES


def resolve_service_settings(
    base: Settings | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ServiceSettings:
    """Resolve settings for service mode.

    ``Settings`` intentionally keeps SQLite as its default so tests and
    compatibility scripts can run without a service stack. Service mode promotes
    the default to local Postgres unless ``AWF_DATABASE_URL`` is explicitly set.
    When ``environ`` is ``None``, values loaded from ``.env`` by pydantic-settings
    count as explicit.
    """

    settings = base or Settings()
    env = os.environ if environ is None else environ
    database_url = settings.database_url

    database_url_explicit = _has_env_key(env, "AWF_DATABASE_URL")
    if environ is None:
        database_url_explicit = database_url_explicit or "database_url" in settings.model_fields_set

    if not database_url_explicit:
        database_url = DEFAULT_LOCAL_SERVICE_DATABASE_URL

    return ServiceSettings(
        service_name=settings.service_name,
        env=settings.env,
        api_base_url=settings.api_base_url,
        database_url=database_url,
        docker_host=settings.docker_host,
        agent_runtime_image=settings.agent_runtime_image,
        work_dir=settings.work_dir,
        min_free_disk_bytes=settings.min_free_disk_bytes,
        host_home=settings.host_home or "~",
        api_token=_empty_to_none(settings.api_token),
        github_token=_resolve_github_token(settings.github_token, env),
        worker_poll_interval_seconds=settings.worker_poll_interval_seconds,
        worker_max_concurrent_provisions=settings.worker_max_concurrent_provisions,
        worker_max_concurrent_executions=settings.worker_max_concurrent_executions,
        agent_wall_timeout_seconds=settings.agent_wall_timeout_seconds,
        agent_idle_timeout_seconds=settings.agent_idle_timeout_seconds,
        node_id=_empty_to_none(settings.worker_node_id) or DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID,
        branch_prefix=settings.worker_branch_prefix,
    )


def service_config_payload(settings: ServiceSettings) -> dict[str, object]:
    """Return JSON-serializable service settings with secrets redacted."""

    payload: dict[str, object] = asdict(settings)
    payload["database_url"] = _redact_database_url(settings.database_url)
    for key in ("api_token", "github_token"):
        if payload.get(key):
            payload[key] = "<redacted>"
    return payload


def _has_env_key(environ: Mapping[str, str], key: str) -> bool:
    wanted = key.upper()
    return any(existing.upper() == wanted for existing in environ)


def _empty_to_none(value: str | None) -> str | None:
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
        or _empty_to_none(environ.get("GH_TOKEN"))
        or _empty_to_none(environ.get("GITHUB_TOKEN"))
    )


def _redact_database_url(value: str) -> str:
    try:
        return make_url(value).render_as_string(hide_password=True)
    except Exception:
        return "<redacted>" if value else value
