"""Application settings — pydantic-settings backed.

Settings are read from environment variables (with the ``AWF_`` prefix) and from a
``.env`` file if one exists. The ``Settings`` class is immutable after construction
so later code can't accidentally mutate global state.

Tests override settings via ``Settings(_env_file=None, AWF_...=...)`` construction
rather than mutating a global object.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
RuntimeEnv = Literal["local", "ci", "staging", "prod"]
DEFAULT_MIN_FREE_DISK_BYTES = 10 * 1024 * 1024 * 1024


class Settings(BaseSettings):
    """Single source of truth for runtime configuration.

    All fields are either required or have defaults that are safe in local dev.
    Production deployments MUST set database_url and github_token explicitly.
    """

    model_config = SettingsConfigDict(
        env_prefix="AWF_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )

    # Identity
    service_name: str = Field(default="awf", description="Service identifier used in logs/metrics.")
    env: RuntimeEnv = Field(default="local", description="Runtime environment.")
    log_level: LogLevel = Field(default="INFO")

    # API
    api_host: str = Field(default="0.0.0.0")  # noqa: S104 (intentional in containers)
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_base_url: str = Field(default="http://localhost:8000")
    api_token: str | None = Field(
        default=None,
        description=(
            "Optional local bearer token for sensitive operator APIs. When set, "
            "log streams, WebSocket streams, and destructive workspace controls "
            "require Authorization: Bearer <token>."
        ),
    )

    # Database (control-plane)
    database_url: str = Field(
        default="sqlite+aiosqlite:///./awf.db",
        description=(
            "Control-plane database URL. SQLite default is for local dev only; "
            "production must set postgresql+asyncpg://..."
        ),
    )

    # GitHub
    github_token: str | None = Field(default=None)
    github_default_base_branch: str = Field(default="development")

    # Docker
    docker_host: str = Field(default="unix:///var/run/docker.sock")
    agent_runtime_image: str = Field(default="awf-agent-runtime:latest")
    work_dir: str = Field(
        default=".awf",
        description="Local AWF state root. Log streams live under <work_dir>/logs.",
    )
    min_free_disk_bytes: int = Field(
        default=DEFAULT_MIN_FREE_DISK_BYTES,
        ge=0,
        description=(
            "Minimum free bytes required on the AWF work directory filesystem before "
            "admitting new local workspaces."
        ),
    )
    host_home: str = Field(
        default="~",
        description=(
            "Host home directory used by local service mode to discover and copy "
            "agent/GitHub credentials for workspace container auth mounts."
        ),
    )

    # Worker
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0)
    worker_max_concurrent_provisions: int = Field(default=3, gt=0)
    worker_max_concurrent_executions: int = Field(default=3, gt=0)
    worker_node_id: str | None = Field(default=None)
    worker_branch_prefix: str = Field(default="awf")
    agent_wall_timeout_seconds: float = Field(
        default=7200,
        gt=0,
        description=(
            "Maximum wall-clock seconds for one agent CLI run before AWF terminates it. "
            "Default: 7200 seconds."
        ),
    )
    agent_idle_timeout_seconds: float = Field(
        default=900,
        gt=0,
        description=(
            "Maximum seconds with no agent stdout/stderr before AWF terminates it. "
            "Default: 900 seconds."
        ),
    )

    # Workspace resource defaults (overridable per-request)
    workspace_steady_cpu: float = Field(default=3.0, gt=0)
    workspace_steady_memory_gb: float = Field(default=10.0, gt=0)
    workspace_peak_cpu: float = Field(default=6.0, gt=0)
    workspace_peak_memory_gb: float = Field(default=16.0, gt=0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide Settings instance.

    Cached so repeated calls don't re-read the environment/file. Tests that need
    a different configuration should construct ``Settings(...)`` directly rather
    than mutate the cache.
    """
    return Settings()
