"""Application settings — pydantic-settings backed.

Settings are read from environment variables (with the ``AWF_`` prefix) and from a
``.env`` file if one exists. The ``Settings`` class is immutable after construction
so later code can't accidentally mutate global state.

Tests override settings via ``Settings(_env_file=None, AWF_...=...)`` construction
rather than mutating a global object.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
RuntimeEnv = Literal["local", "ci", "staging", "prod"]
DEFAULT_LOCAL_DATABASE_URL = "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
DEFAULT_MIN_FREE_DISK_BYTES = 10 * 1024 * 1024 * 1024
DEFAULT_COMPLETED_WORKSPACE_RETENTION_HOURS = 168
DEFAULT_WORKSPACE_CLEANUP_SCAN_INTERVAL_SECONDS = 3600
DEFAULT_WORKSPACE_CLEANUP_BATCH_LIMIT = 50
_MIN_PRODUCTION_API_TOKEN_LENGTH = 24
_WEAK_API_TOKEN_VALUES = frozenset(
    {
        "change-me",
        "changeme",
        "default",
        "dev",
        "dev-token",
        "example",
        "local",
        "local-dev-token",
        "password",
        "placeholder",
        "replace-me",
        "replace_me",
        "test",
        "token",
    }
)


@dataclass(frozen=True, slots=True)
class ProductionSettingsDiagnostic:
    """Structured production settings validation diagnostic."""

    code: str
    field: str
    message: str
    remediation: str


class ProductionSettingsError(RuntimeError):
    """Raised when production settings use unsafe local-development defaults."""

    diagnostics: tuple[ProductionSettingsDiagnostic, ...]

    def __init__(self, diagnostics: tuple[ProductionSettingsDiagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        super().__init__(self._message())

    def _message(self) -> str:
        if not self.diagnostics:
            return "Production settings validation failed."
        rendered = "; ".join(
            (
                f"{diagnostic.code} ({diagnostic.field}): {diagnostic.message} "
                f"Remediation: {diagnostic.remediation}"
            )
            for diagnostic in self.diagnostics
        )
        return f"Production settings validation failed: {rendered}"


def settings_guardrails(
    *,
    env: str,
    database_url: str,
    api_token: str | None,
    callbacks_enabled: bool,
) -> tuple[ProductionSettingsDiagnostic, ...]:
    """Return production-only settings diagnostics without side effects.

    Local and CI defaults intentionally remain usable. This helper treats only
    ``AWF_ENV=prod`` as production for the current local-first guardrail slice.
    """

    if env != "prod":
        return ()

    diagnostics: list[ProductionSettingsDiagnostic] = []
    if _is_default_local_database_url_or_credentials(database_url):
        diagnostics.append(
            ProductionSettingsDiagnostic(
                code="production_default_database_url",
                field="AWF_DATABASE_URL",
                message=(
                    "Production must not use AWF's bundled local development "
                    "database URL or credentials."
                ),
                remediation=(
                    "Set AWF_DATABASE_URL to a production PostgreSQL database "
                    "with deployment-specific credentials."
                ),
            )
        )

    token_diagnostic = _api_token_diagnostic(api_token)
    if token_diagnostic is not None:
        diagnostics.append(token_diagnostic)

    if callbacks_enabled:
        diagnostics.append(
            ProductionSettingsDiagnostic(
                code="production_callbacks_disabled_until_auth",
                field="AWF_CALLBACKS_ENABLED",
                message=(
                    "Callback registration is enabled, but callback routes do not "
                    "yet enforce AWF API bearer token authentication."
                ),
                remediation=(
                    "Disable AWF_CALLBACKS_ENABLED in production until callback "
                    "route authentication is implemented."
                ),
            )
        )

    return tuple(diagnostics)


def _api_token_diagnostic(api_token: str | None) -> ProductionSettingsDiagnostic | None:
    normalized = _normalized_secret(api_token)
    if normalized is None:
        return ProductionSettingsDiagnostic(
            code="production_api_token_missing",
            field="AWF_API_TOKEN",
            message="Production requires an AWF API bearer token for operator APIs.",
            remediation="Set AWF_API_TOKEN to a deployment-specific high-entropy secret.",
        )
    if _is_weak_api_token(normalized):
        return ProductionSettingsDiagnostic(
            code="production_api_token_weak",
            field="AWF_API_TOKEN",
            message="Production AWF_API_TOKEN must not be a local placeholder or short value.",
            remediation="Generate and set a deployment-specific high-entropy AWF_API_TOKEN.",
        )
    return None


def _is_weak_api_token(api_token: str) -> bool:
    return (
        len(api_token) < _MIN_PRODUCTION_API_TOKEN_LENGTH
        or api_token.lower() in _WEAK_API_TOKEN_VALUES
    )


def _normalized_secret(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _is_default_local_database_url_or_credentials(database_url: str) -> bool:
    if database_url.strip() == DEFAULT_LOCAL_DATABASE_URL:
        return True
    parsed = urlsplit(database_url)
    port = parsed.port
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    hostname = parsed.hostname or ""
    database = parsed.path.lstrip("/")
    uses_default_local_url = (
        parsed.scheme == "postgresql+asyncpg"
        and username == "awf"
        and password == "awf_dev"
        and hostname in {"localhost", "127.0.0.1"}
        and port == 5433
        and database == "awf"
    )
    uses_default_local_credentials = username == "awf" and password == "awf_dev"
    return uses_default_local_url or uses_default_local_credentials


class Settings(BaseSettings):
    """Single source of truth for runtime configuration.

    All fields are either required or have defaults that are safe in local dev.
    Production deployments MUST override local defaults and set a strong
    ``api_token`` explicitly.
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
        default="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf",
        description=(
            "Control-plane PostgreSQL database URL. AWF requires postgresql+asyncpg://..."
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
        default=3600,
        gt=0,
        description=(
            "Maximum seconds with no agent stdout/stderr before AWF terminates it. "
            "Default: 3600 seconds."
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


def validate_production_settings(
    settings: Settings,
    *,
    database_url: str | None = None,
) -> None:
    """Raise structured diagnostics when production settings are unsafe."""

    diagnostics = settings_guardrails(
        env=settings.env,
        database_url=database_url if database_url is not None else settings.database_url,
        api_token=settings.api_token,
        callbacks_enabled=settings.callbacks_enabled,
    )
    if diagnostics:
        raise ProductionSettingsError(diagnostics)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide Settings instance.

    Cached so repeated calls don't re-read the environment/file. Tests that need
    a different configuration should construct ``Settings(...)`` directly rather
    than mutate the cache.
    """
    return Settings()
