"""Application settings — pydantic-settings backed.

Settings are read from environment variables (with the ``AWF_`` prefix) and from a
``.env`` file if one exists. The ``Settings`` class is immutable after construction
so later code can't accidentally mutate global state.

Tests override settings via ``Settings(_env_file=None, AWF_...=...)`` construction
rather than mutating a global object.
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
RuntimeEnv = Literal["local", "ci", "staging", "prod"]
DEFAULT_MIN_FREE_DISK_BYTES = 10 * 1024 * 1024 * 1024
DEFAULT_COMPLETED_WORKSPACE_RETENTION_HOURS = 168
DEFAULT_WORKSPACE_CLEANUP_SCAN_INTERVAL_SECONDS = 3600
DEFAULT_WORKSPACE_CLEANUP_BATCH_LIMIT = 50


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
    console_url: str | None = Field(
        default=None,
        description="Optional console UI URL shown in smoke reports and operator views.",
    )
    api_token: str | None = Field(
        default=None,
        description=(
            "Optional local bearer token for sensitive operator APIs. When set, "
            "log streams, WebSocket streams, and destructive workspace controls "
            "require Authorization: Bearer <token>."
        ),
    )
    callbacks_enabled: bool = Field(
        default=True,
        description="Whether local external callback delivery registration is enabled.",
    )
    callback_delivery_timeout_seconds: int = Field(default=10, ge=1, le=120)
    callback_delivery_max_attempts: int = Field(default=3, ge=1, le=20)
    callback_delivery_initial_backoff_seconds: int = Field(default=5, ge=1, le=3600)

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

    # Retention cleanup
    completed_workspace_retention_hours: float = Field(
        default=DEFAULT_COMPLETED_WORKSPACE_RETENTION_HOURS,
        ge=0,
        description=(
            "Hours to retain completed PR workspace pressure directories before "
            "they become eligible for service GC."
        ),
    )
    workspace_cleanup_enabled: bool = Field(
        default=True,
        description="Whether retention-based workspace cleanup planning is enabled.",
    )
    workspace_cleanup_scan_interval_seconds: float = Field(
        default=DEFAULT_WORKSPACE_CLEANUP_SCAN_INTERVAL_SECONDS,
        gt=0,
        description="Interval for future automatic workspace cleanup sweeps.",
    )
    workspace_cleanup_batch_limit: int = Field(
        default=DEFAULT_WORKSPACE_CLEANUP_BATCH_LIMIT,
        gt=0,
        description="Maximum cleanup candidates to select in one retention cleanup batch.",
    )
    network_posture_open_legacy_cutoff: datetime | None = Field(
        default=None,
        description=(
            "Optional deployment-specific cutoff for treating persisted open "
            "network posture values as unknown legacy defaults."
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
    planning_max_iterations_default: int = Field(
        default=3,
        ge=0,
        le=5,
        description=(
            "Default plan-conformance remediation iterations when a workspace "
            "profile omits planning.max_iterations. Explicit profile values win."
        ),
    )

    # Workspace resource defaults (overridable per-request)
    workspace_steady_cpu: float = Field(default=3.0, gt=0)
    workspace_steady_memory_gb: float = Field(default=10.0, gt=0)
    workspace_peak_cpu: float = Field(default=6.0, gt=0)
    workspace_peak_memory_gb: float = Field(default=16.0, gt=0)
    local_capacity_cpu_cores: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Optional local node CPU capacity for reservation pressure reporting. "
            "When unset, CPU availability is reported as unknown."
        ),
    )
    local_capacity_memory_gb: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Optional local node memory capacity for reservation pressure reporting. "
            "When unset, memory availability is reported as unknown."
        ),
    )
    local_capacity_dind_slots: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Optional local node DinD workspace slot capacity for reservation "
            "pressure reporting. When unset, DinD availability is reported as unknown."
        ),
    )

    @field_validator(
        "local_capacity_cpu_cores",
        "local_capacity_memory_gb",
        "local_capacity_dind_slots",
        mode="before",
    )
    @classmethod
    def _empty_local_capacity_values_are_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("network_posture_open_legacy_cutoff", mode="before")
    @classmethod
    def _empty_network_posture_open_legacy_cutoff_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide Settings instance.

    Cached so repeated calls don't re-read the environment/file. Tests that need
    a different configuration should construct ``Settings(...)`` directly rather
    than mutate the cache.
    """
    return Settings()
