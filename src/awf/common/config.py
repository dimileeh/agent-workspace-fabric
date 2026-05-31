"""Application settings — pydantic-settings backed.

Settings are read from environment variables (with the ``AWF_`` prefix) and from a
``.env`` file if one exists. The ``Settings`` class is immutable after construction
so later code can't accidentally mutate global state.

Tests override settings via ``Settings(_env_file=None, AWF_...=...)`` construction
rather than mutating a global object.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
RuntimeEnv = Literal["local", "ci", "staging", "prod"]
DEFAULT_LOCAL_DATABASE_URL = "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
DEFAULT_MIN_FREE_DISK_BYTES = 10 * 1024 * 1024 * 1024
DEFAULT_COMPLETED_WORKSPACE_RETENTION_HOURS = 168
DEFAULT_WORKSPACE_CLEANUP_SCAN_INTERVAL_SECONDS = 3600
DEFAULT_WORKSPACE_CLEANUP_BATCH_LIMIT = 50
_MIN_PRODUCTION_API_TOKEN_LENGTH = 24
_WEAK_API_TOKEN_SEPARATORS = ("", "-", "_", ".")
_WEAK_API_TOKEN_VALUES = frozenset(
    {
        "admin",
        "api-key",
        "api_key",
        "apikey",
        "bearer",
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
        "secret",
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
        """Create an error carrying every unsafe production-setting diagnostic."""
        self.diagnostics = diagnostics
        super().__init__(self._message())

    def _message(self) -> str:
        """Render diagnostics without including secret or connection-string values."""
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
    env: RuntimeEnv,
    database_url: str,
    api_token: str | None,
) -> tuple[ProductionSettingsDiagnostic, ...]:
    """Return production-only settings diagnostics without side effects.

    Local and CI defaults intentionally remain usable. This helper treats only
    ``AWF_ENV=prod`` as production for the current local-first guardrail slice.
    Callback registration is safe in production once the API token guardrail
    passes because callback routes enforce bearer-token authentication.
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

    return tuple(diagnostics)


def _api_token_diagnostic(api_token: str | None) -> ProductionSettingsDiagnostic | None:
    """Return the production API-token diagnostic, if the token is missing or weak."""
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
    """Return true when a normalized token is short or matches known placeholders."""
    normalized = api_token.lower()
    return (
        len(normalized) < _MIN_PRODUCTION_API_TOKEN_LENGTH
        or normalized in _WEAK_API_TOKEN_VALUES
        or _is_repeated_weak_api_token_value(normalized)
    )


def _is_repeated_weak_api_token_value(api_token: str) -> bool:
    """Detect placeholder tokens repeated with or without common separators."""
    for weak_value in _WEAK_API_TOKEN_VALUES:
        if len(weak_value) >= len(api_token):
            continue
        for separator in _WEAK_API_TOKEN_SEPARATORS:
            if _is_repeated_token_value(api_token, weak_value, separator):
                return True
    return False


def _is_repeated_token_value(api_token: str, value: str, separator: str) -> bool:
    """Return true when ``api_token`` is composed only of repeated ``value`` parts."""
    count = 0
    remainder = api_token
    while remainder.startswith(value):
        count += 1
        remainder = remainder[len(value) :]
        if not remainder:
            return count > 1
        if not remainder.startswith(separator):
            return False
        remainder = remainder[len(separator) :]
        if not remainder:
            return count > 1
    return False


def _normalized_secret(value: str | None) -> str | None:
    """Trim a secret-like setting while preserving unset and blank as ``None``."""
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _is_default_local_database_url_or_credentials(database_url: str) -> bool:
    """Return true when a database URL uses bundled local development credentials."""
    normalized = database_url.strip()
    if not normalized:
        return True
    if normalized == DEFAULT_LOCAL_DATABASE_URL:
        return True
    parsed = urlsplit(normalized)
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    return username == "awf" and password == "awf_dev"


def _normalize_callback_allowed_host(value: Any) -> str:
    host = str(value).strip().rstrip(".").lower()
    if not host:
        return ""
    if host.startswith("["):
        bracketed_host, bracket, _suffix = host[1:].partition("]")
        if bracket:
            return bracketed_host.rstrip(".")
    if host.count(":") == 1:
        host_without_port, _separator, _port = host.partition(":")
        return host_without_port.rstrip(".")
    return host


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

    def __init__(self, **values: Any) -> None:
        """Initialize settings while remembering direct constructor overrides."""
        init_fields = _settings_constructor_fields_from_values(type(self), values)
        super().__init__(**values)
        _record_settings_constructor_fields(self, init_fields)

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
    callbacks_require_https: bool = Field(
        default=False,
        description="Require callback targets to use https:// scheme.",
    )
    callbacks_allowed_hosts: Annotated[tuple[str, ...], NoDecode] = Field(
        default_factory=tuple,
        description=(
            "Optional allowlist of callback target hostnames (normalized to lowercase "
            "without trailing dots or port suffixes). Empty means no callback host "
            "restriction."
        ),
    )
    callback_delivery_timeout_seconds: int = Field(default=10, ge=1, le=120)
    callback_delivery_max_attempts: int = Field(default=3, ge=1, le=20)
    callback_delivery_initial_backoff_seconds: int = Field(default=5, ge=1, le=3600)
    request_admission_window_seconds: int = Field(
        default=60,
        ge=1,
        le=3600,
        description="Fixed-window size for request-level admission limits.",
    )
    workspace_create_rate_limit_count: int = Field(
        default=120,
        ge=1,
        description=(
            "Maximum fresh workspace creation requests admitted per request identity "
            "within one request_admission_window_seconds window."
        ),
    )
    callback_register_rate_limit_count: int = Field(
        default=240,
        ge=1,
        description=(
            "Maximum fresh callback registration requests admitted per request identity "
            "within one request_admission_window_seconds window."
        ),
    )

    # Database (control-plane)
    database_url: str = Field(
        default=DEFAULT_LOCAL_DATABASE_URL,
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
    companion_image_cache_enabled: bool = Field(
        default=True,
        description=(
            "Pre-build each companion image once per (name, commit) on the host "
            "daemon and reference it via image: so parallel dispatches reuse it."
        ),
    )
    companion_image_retention_hours: int = Field(
        default=168,
        ge=1,
        description=(
            "Creation age after which cached companion images are pruned by GC. "
            "Keyed on image creation time, not last use (Docker has no last-used "
            "filter), so a still-current image built earlier may be evicted and "
            "rebuilt cold on the next dispatch."
        ),
    )
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

    @field_validator("callbacks_allowed_hosts", mode="before")
    @classmethod
    def _normalize_callback_allowed_hosts(cls, value: Any) -> tuple[str, ...]:
        if value is None or value == "":
            return ()
        if isinstance(value, str):
            values = value.split(",")
        elif isinstance(value, (list, tuple)):
            values = list(value)
        else:
            raise ValueError(
                "callbacks_allowed_hosts must be a comma-separated string, list, or tuple"
            )

        normalized_hosts = (_normalize_callback_allowed_host(host) for host in values)
        return tuple(host for host in normalized_hosts if host)

    @field_validator(
        "local_capacity_cpu_cores",
        "local_capacity_memory_gb",
        "local_capacity_dind_slots",
        mode="before",
    )
    @classmethod
    def _empty_local_capacity_values_are_unset(cls, value: Any) -> Any:
        """Treat blank optional capacity values from environment variables as unset."""
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("network_posture_open_legacy_cutoff", mode="before")
    @classmethod
    def _empty_network_posture_open_legacy_cutoff_is_unset(cls, value: Any) -> Any:
        """Treat a blank legacy-network-posture cutoff as an omitted value."""
        if isinstance(value, str) and value.strip() == "":
            return None
        return value


def _settings_constructor_fields_from_values(
    settings_type: type[Settings],
    values: Mapping[str, Any],
) -> frozenset[str]:
    return frozenset(
        name
        for name, field in settings_type.model_fields.items()
        if name in values
        or _settings_alias_is_present(field.alias, values)
        or _settings_alias_is_present(field.validation_alias, values)
    )


def _settings_alias_is_present(alias: Any, values: Mapping[str, Any]) -> bool:
    if alias is None:
        return False
    if isinstance(alias, str):
        return alias in values

    choices = getattr(alias, "choices", None)
    if isinstance(choices, (list, tuple)):
        return any(_settings_alias_is_present(choice, values) for choice in choices)

    path = getattr(alias, "path", None)
    if isinstance(path, (list, tuple)) and path:
        first = path[0]
        return isinstance(first, str) and first in values

    return False


class _SettingsIdentityRef(weakref.ref[Settings]):
    """Weak reference that keys settings by object identity, not model value."""

    __slots__ = ("_hash",)

    _hash: int

    def __new__(
        cls,
        settings: Settings,
        callback: Callable[[weakref.ReferenceType[Settings]], object] | None = None,
    ) -> _SettingsIdentityRef:
        reference = super().__new__(cls, settings, callback)
        reference._hash = id(settings)
        return reference

    def __hash__(self) -> int:
        return self._hash

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _SettingsIdentityRef):
            return False
        if self is other:
            return True
        settings = self()
        return settings is not None and settings is other()


_SETTINGS_INIT_FIELDS_BY_SETTINGS: dict[_SettingsIdentityRef, frozenset[str]] = {}
_SETTINGS_INIT_FIELDS_LOCK = threading.RLock()


def _discard_settings_constructor_fields(reference: weakref.ReferenceType[Settings]) -> None:
    if isinstance(reference, _SettingsIdentityRef):
        with _SETTINGS_INIT_FIELDS_LOCK:
            _SETTINGS_INIT_FIELDS_BY_SETTINGS.pop(reference, None)


def _record_settings_constructor_fields(settings: Settings, fields: frozenset[str]) -> None:
    reference = _SettingsIdentityRef(settings, _discard_settings_constructor_fields)
    with _SETTINGS_INIT_FIELDS_LOCK:
        _SETTINGS_INIT_FIELDS_BY_SETTINGS[reference] = fields


def settings_constructor_fields(settings: Settings) -> frozenset[str]:
    """Return fields passed directly to ``Settings(...)`` construction."""

    with _SETTINGS_INIT_FIELDS_LOCK:
        return _SETTINGS_INIT_FIELDS_BY_SETTINGS.get(_SettingsIdentityRef(settings), frozenset())


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
