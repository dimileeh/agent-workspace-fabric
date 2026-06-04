"""Resolved settings for the local AWF service runtime."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import cast

from sqlalchemy.engine import make_url

from awf.common.config import (
    DEFAULT_COMPLETED_WORKSPACE_RETENTION_HOURS,
    DEFAULT_LOCAL_DATABASE_URL,
    DEFAULT_MIN_FREE_DISK_BYTES,
    DEFAULT_ORPHAN_RECONCILE_LIMIT,
    DEFAULT_ORPHAN_RECONCILE_MIN_AGE_HOURS,
    DEFAULT_ORPHAN_RECONCILE_SCAN_INTERVAL_SECONDS,
    DEFAULT_WORKSPACE_CLEANUP_BATCH_LIMIT,
    DEFAULT_WORKSPACE_CLEANUP_SCAN_INTERVAL_SECONDS,
    Settings,
    settings_constructor_fields,
    validate_production_settings,
)
from awf.service.environment import compose_env_file_values

DEFAULT_LOCAL_SERVICE_DATABASE_URL = DEFAULT_LOCAL_DATABASE_URL
DEFAULT_LOCAL_SERVICE_API_BASE_URL = str(Settings.model_fields["api_base_url"].default)
DEFAULT_LOCAL_SERVICE_API_TOKEN = "local-dev-token"
DEFAULT_LOCAL_SERVICE_POSTGRES_PASSWORD = "awf_dev"
DEFAULT_LOCAL_SERVICE_WORK_DIR = "~/.awf/service"
DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID = "local"
_PROJECT_DEFAULT_WORK_DIR = str(Settings.model_fields["work_dir"].default)
LOCAL_SERVICE_COMPOSE_FILE = Path("compose.yaml")
LOCAL_SERVICE_INCLUDED_COMPOSE_FILE = Path("docker/compose/local-service.yml")
LOCAL_SERVICE_COMPOSE_ENV_FILE = Path(".env")
LEGACY_LOCAL_SERVICE_COMPOSE_ENV_FILE = Path("docker/compose/.env")
_AWF_SOURCE_ROOT_MARKERS = (
    "pyproject.toml",
    "src/awf/__init__.py",
    "compose.yaml",
    "docker/compose/local-service.yml",
)


__all__ = [
    "COMPOSE_ENV_FILE_OMITTED",
    "DEFAULT_LOCAL_SERVICE_API_BASE_URL",
    "DEFAULT_LOCAL_SERVICE_API_TOKEN",
    "DEFAULT_LOCAL_SERVICE_DATABASE_URL",
    "DEFAULT_LOCAL_SERVICE_POSTGRES_PASSWORD",
    "DEFAULT_LOCAL_SERVICE_WORK_DIR",
    "ComposeEnvFileInput",
    "ComposeEnvFileOmitted",
    "LEGACY_LOCAL_SERVICE_COMPOSE_ENV_FILE",
    "LOCAL_SERVICE_COMPOSE_ENV_FILE",
    "LOCAL_SERVICE_COMPOSE_FILE",
    "LOCAL_SERVICE_INCLUDED_COMPOSE_FILE",
    "ServiceSettings",
    "local_service_environ",
    "resolve_local_service_compose_env_file",
    "resolve_local_service_provider_environ",
    "resolve_service_settings",
    "service_config_payload",
]


class ComposeEnvFileOmitted:
    """Sentinel for distinguishing omitted env-file arguments from explicit null."""


COMPOSE_ENV_FILE_OMITTED = ComposeEnvFileOmitted()
type ComposeEnvFileInput = Path | None | ComposeEnvFileOmitted


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
    companion_image_cache_enabled: bool = True
    companion_image_retention_hours: int = 168
    work_dir: str
    api_token: str | None
    github_token: str | None
    worker_poll_interval_seconds: float
    worker_max_concurrent_provisions: int
    worker_max_concurrent_executions: int = 3
    workspace_steady_cpu: float = 3.0
    workspace_steady_memory_gb: float = 10.0
    workspace_peak_cpu: float = 6.0
    workspace_peak_memory_gb: float = 16.0
    agent_wall_timeout_seconds: float = 7200
    agent_idle_timeout_seconds: float = 3600
    planning_max_iterations_default: int = 3
    host_home: str = "~"
    node_id: str | None = None
    branch_prefix: str = "awf"
    service_startup_log_tail_lines: int = 200
    min_free_disk_bytes: int = DEFAULT_MIN_FREE_DISK_BYTES
    completed_workspace_retention_hours: float = DEFAULT_COMPLETED_WORKSPACE_RETENTION_HOURS
    workspace_cleanup_enabled: bool = True
    workspace_cleanup_scan_interval_seconds: float = DEFAULT_WORKSPACE_CLEANUP_SCAN_INTERVAL_SECONDS
    workspace_cleanup_batch_limit: int = DEFAULT_WORKSPACE_CLEANUP_BATCH_LIMIT
    auto_cleanup_orphans: bool = False
    orphan_reconcile_scan_interval_seconds: float = DEFAULT_ORPHAN_RECONCILE_SCAN_INTERVAL_SECONDS
    classified_orphan_reap_scan_interval_seconds: float = (
        DEFAULT_ORPHAN_RECONCILE_SCAN_INTERVAL_SECONDS
    )
    orphan_reconcile_max_per_scan: int = DEFAULT_ORPHAN_RECONCILE_LIMIT
    orphan_reconcile_min_age_hours: float = DEFAULT_ORPHAN_RECONCILE_MIN_AGE_HOURS
    network_posture_open_legacy_cutoff: datetime | None = None
    local_capacity_cpu_cores: float | None = None
    local_capacity_memory_gb: float | None = None
    local_capacity_dind_slots: int | None = None

    def __post_init__(self) -> None:
        """Catch a non-positive tail at settings resolution, not worker startup.

        ``Settings.worker_service_startup_log_tail_lines`` enforces ``gt=0`` and
        ``ProvisionerConfig.__post_init__`` re-checks, but a bad value assigned
        directly to this dataclass would otherwise only fail later when
        ``build_worker_runtime`` constructs the ``ProvisionerConfig``. Mirror the
        guard here so it surfaces at the layer that converts the env-var setting.
        """
        if self.service_startup_log_tail_lines <= 0:
            raise ValueError(
                "service_startup_log_tail_lines must be > 0, "
                f"got {self.service_startup_log_tail_lines}"
            )


@dataclass
class _ProjectDotenvLookup:
    """Cache project dotenv candidate discovery within one settings resolution."""

    _candidates: tuple[Path, ...] | None = None
    _values_by_file: dict[Path, dict[str, str]] = field(default_factory=dict)

    def value(self, key: str) -> str | None:
        for env_file in self._candidate_paths():
            if not env_file.exists():
                continue
            value = _env_value(self._values(env_file), key)
            if value is not None:
                return value
        return None

    def _candidate_paths(self) -> tuple[Path, ...]:
        if self._candidates is None:
            self._candidates = _project_dotenv_candidates()
        return self._candidates

    def _values(self, env_file: Path) -> dict[str, str]:
        cache_key = env_file.resolve()
        values = self._values_by_file.get(cache_key)
        if values is None:
            values = compose_env_file_values(env_file)
            self._values_by_file[cache_key] = values
        return values


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
    project_dotenv_lookup = _ProjectDotenvLookup()

    host_database_url = _env_value(env, "AWF_DATABASE_URL")
    database_url_explicit = host_database_url is not None and _database_url_env_is_explicit(
        env,
        service_env,
        project_dotenv_lookup=project_dotenv_lookup,
    )
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
        project_dotenv_lookup=project_dotenv_lookup,
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
        companion_image_cache_enabled=settings.companion_image_cache_enabled,
        companion_image_retention_hours=settings.companion_image_retention_hours,
        work_dir=work_dir,
        min_free_disk_bytes=settings.min_free_disk_bytes,
        host_home=settings.host_home or "~",
        api_token=_resolve_service_api_token(settings, service_env),
        github_token=_resolve_github_token(settings.github_token, env),
        worker_poll_interval_seconds=settings.worker_poll_interval_seconds,
        worker_max_concurrent_provisions=settings.worker_max_concurrent_provisions,
        worker_max_concurrent_executions=settings.worker_max_concurrent_executions,
        workspace_steady_cpu=settings.workspace_steady_cpu,
        workspace_steady_memory_gb=settings.workspace_steady_memory_gb,
        workspace_peak_cpu=settings.workspace_peak_cpu,
        workspace_peak_memory_gb=settings.workspace_peak_memory_gb,
        agent_wall_timeout_seconds=settings.agent_wall_timeout_seconds,
        agent_idle_timeout_seconds=settings.agent_idle_timeout_seconds,
        planning_max_iterations_default=settings.planning_max_iterations_default,
        node_id=_empty_to_none(settings.worker_node_id) or DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID,
        branch_prefix=settings.worker_branch_prefix,
        service_startup_log_tail_lines=settings.worker_service_startup_log_tail_lines,
        completed_workspace_retention_hours=settings.completed_workspace_retention_hours,
        workspace_cleanup_enabled=settings.workspace_cleanup_enabled,
        workspace_cleanup_scan_interval_seconds=settings.workspace_cleanup_scan_interval_seconds,
        workspace_cleanup_batch_limit=settings.workspace_cleanup_batch_limit,
        auto_cleanup_orphans=settings.auto_cleanup_orphans,
        orphan_reconcile_scan_interval_seconds=settings.orphan_reconcile_scan_interval_seconds,
        classified_orphan_reap_scan_interval_seconds=(
            settings.classified_orphan_reap_scan_interval_seconds
        ),
        orphan_reconcile_max_per_scan=settings.orphan_reconcile_max_per_scan,
        orphan_reconcile_min_age_hours=settings.orphan_reconcile_min_age_hours,
        network_posture_open_legacy_cutoff=settings.network_posture_open_legacy_cutoff,
        local_capacity_cpu_cores=settings.local_capacity_cpu_cores,
        local_capacity_memory_gb=settings.local_capacity_memory_gb,
        local_capacity_dind_slots=settings.local_capacity_dind_slots,
    )


def _resolve_service_api_token(
    settings: Settings, service_environ: Mapping[str, str]
) -> str | None:
    """Return the API token visible to local service containers and host CLI calls."""

    return _empty_to_none(settings.api_token) or _empty_to_none(
        _env_value(service_environ, "AWF_API_TOKEN")
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
    env_file: Path | None = LOCAL_SERVICE_COMPOSE_ENV_FILE,
) -> dict[str, str]:
    """Return the environment local Compose services actually receive.

    Docker Compose resolves variables from its env file and then lets the host
    shell override them. The CLI uses this merged view for readiness checks so a
    token present in root ``.env`` is not incorrectly reported as missing just
    because it is absent from the host shell.
    """

    merged: dict[str, str] = {}
    if (
        env_file is not None
        and (resolved_env_file := resolve_local_service_compose_env_file(env_file)) is not None
    ):
        caller_environ = os.environ if environ is None else environ
        merged.update(compose_env_file_values(resolved_env_file, environ=caller_environ))
    merged.update(os.environ if environ is None else dict(environ))
    _populate_compose_postgres_password(merged)
    _populate_local_compose_defaults(merged)
    return merged


def resolve_local_service_provider_environ(
    *,
    provider_environ: Mapping[str, str] | None,
    environ: Mapping[str, str],
    compose_file: Path | None,
    compose_env_file: ComposeEnvFileInput = COMPOSE_ENV_FILE_OMITTED,
) -> Mapping[str, str]:
    """Resolve provider auth inputs from explicit or adjacent Compose env files."""

    if provider_environ is not None:
        return provider_environ

    discover_adjacent_env_file = isinstance(compose_env_file, ComposeEnvFileOmitted)
    env_file = None if discover_adjacent_env_file else cast(Path | None, compose_env_file)
    if env_file is None and discover_adjacent_env_file and compose_file is not None:
        env_file = _provider_env_file_from_compose_file(
            compose_file,
            allow_custom_adjacent=True,
        )
    if env_file is None and compose_env_file is None and compose_file is not None:
        env_file = _provider_env_file_from_compose_file(
            compose_file,
            allow_custom_adjacent=False,
        )
    if env_file is None:
        return environ
    return local_service_environ(environ, env_file=env_file)


def _provider_env_file_from_compose_file(
    compose_file: Path,
    *,
    allow_custom_adjacent: bool,
) -> Path | None:
    """Return a trusted provider env file adjacent to compose_file, when available."""

    if _is_local_service_compose_file_path(compose_file):
        asset_env_file = _local_service_asset_path(LOCAL_SERVICE_COMPOSE_ENV_FILE)
        if asset_env_file is not None and asset_env_file.exists():
            return asset_env_file

    if not allow_custom_adjacent:
        return None

    candidate = compose_file.parent / ".env"
    if candidate.exists() and _can_use_adjacent_provider_env_file(candidate, compose_file):
        return candidate
    return None


def _can_use_adjacent_provider_env_file(candidate: Path, compose_file: Path) -> bool:
    """Return true when adjacent provider env fallback is trusted for compose_file."""

    if not _is_local_service_compose_file_path(compose_file):
        return True
    return _is_local_service_compose_env_path(candidate)


def _is_local_service_compose_file_path(path: Path) -> bool:
    """Return true for the default local-service compose file path."""

    if path in (LOCAL_SERVICE_COMPOSE_FILE, LOCAL_SERVICE_INCLUDED_COMPOSE_FILE):
        return True
    expected_paths = [
        candidate
        for candidate in (
            _local_service_asset_path(LOCAL_SERVICE_COMPOSE_FILE),
            _local_service_asset_path(LOCAL_SERVICE_INCLUDED_COMPOSE_FILE),
        )
        if candidate is not None
    ]
    if not expected_paths:
        expected_paths = [
            Path.cwd() / LOCAL_SERVICE_COMPOSE_FILE,
            Path.cwd() / LOCAL_SERVICE_INCLUDED_COMPOSE_FILE,
        ]
    resolved_path = path.resolve()
    return any(resolved_path == expected.resolve() for expected in expected_paths)


def _is_local_service_compose_env_path(path: Path) -> bool:
    """Return true for the canonical service env file under the verified asset root."""

    expected = _local_service_asset_path(LOCAL_SERVICE_COMPOSE_ENV_FILE)
    if expected is None:
        expected = Path.cwd() / LOCAL_SERVICE_COMPOSE_ENV_FILE
    return path.resolve() == expected.resolve()


def _local_service_asset_path(path: Path) -> Path | None:
    """Return path resolved under the verified AWF asset root, when available."""

    from awf.service import bootstrap as bootstrap_mod

    asset_root = bootstrap_mod.get_bootstrap_asset_root()
    if asset_root is None:
        return None
    expanded = path.expanduser()
    if not expanded.is_absolute():
        return asset_root / expanded
    resolved_asset_root = asset_root.expanduser().resolve()
    resolved_path = expanded.resolve()
    try:
        resolved_path.relative_to(resolved_asset_root)
    except ValueError:
        return None
    return resolved_path


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
        source_roots = _awf_source_search_roots(cwd)
        if source_roots:
            candidates.extend(root / expanded for root in source_roots)
        else:
            candidates.append(cwd / expanded)
        module_file = Path(__file__).resolve()
        candidates.extend(root / expanded for root in _awf_source_search_roots(module_file.parent))
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


def _awf_source_search_roots(start: Path) -> tuple[Path, ...]:
    """Return the nearest AWF source root for default env discovery."""

    roots = (start, *start.parents)
    for root in roots:
        if _is_awf_source_root(root):
            return (root,)
    return ()


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


def _populate_local_compose_defaults(environ: dict[str, str]) -> None:
    """Mirror root Compose's safe loopback-only local defaults."""

    if not environ.get("AWF_API_TOKEN"):
        environ["AWF_API_TOKEN"] = DEFAULT_LOCAL_SERVICE_API_TOKEN
    if not environ.get("AWF_POSTGRES_PASSWORD"):
        environ["AWF_POSTGRES_PASSWORD"] = DEFAULT_LOCAL_SERVICE_POSTGRES_PASSWORD


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
    project_dotenv_lookup: _ProjectDotenvLookup | None = None,
) -> str:
    """Return the service settings API self-reference URL.

    This preserves the existing ``AWF_API_BASE_URL``/Compose host-port
    resolution used by service doctor, smoke, and status flows. Host CLI calls
    resolve their operator-facing target separately from ``AWF_BASE_URL``.
    """

    host_api_base_url = _env_value(environ, "AWF_API_BASE_URL")
    if host_api_base_url is not None and _api_base_url_env_is_explicit(
        environ,
        service_environ,
        project_dotenv_lookup=project_dotenv_lookup,
    ):
        return host_api_base_url
    if _settings_api_base_url_is_explicit(
        settings,
        service_environ,
        require_init_field=require_init_field,
    ):
        return settings.api_base_url
    # This adds new information only for AWF_API_BASE_URL supplied by the
    # Compose .env while absent from the host env; host values were checked above.
    service_api_base_url = _env_value(service_environ, "AWF_API_BASE_URL")
    if service_api_base_url is not None and _api_base_url_is_explicit(
        service_api_base_url,
        service_environ,
    ):
        return service_api_base_url
    return _default_local_service_api_base_url(service_environ)


def _default_local_service_api_base_url(environ: Mapping[str, str]) -> str:
    """Return the local service API URL matching existing Compose port overrides."""

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


def _api_base_url_env_is_explicit(
    environ: Mapping[str, str],
    service_environ: Mapping[str, str],
    *,
    project_dotenv_lookup: _ProjectDotenvLookup | None = None,
) -> bool:
    """Return true when the host environment carries a non-derivable API URL.

    A default-valued ``AWF_API_BASE_URL`` is treated as non-explicit when
    ``AWF_API_HOST_PORT`` is also present, allowing the port-derived URL to
    replace a stale default that was left unchanged. If a project ``.env`` was
    sourced into the host shell, a matching default URL is also non-explicit
    when the merged Compose environment carries the API host-port override.
    """

    api_base_url = _env_value(environ, "AWF_API_BASE_URL")
    if api_base_url is None:
        return False
    if api_base_url != DEFAULT_LOCAL_SERVICE_API_BASE_URL:
        return True
    if _env_value(environ, "AWF_API_HOST_PORT"):
        return False
    return not (
        _env_value(service_environ, "AWF_API_HOST_PORT")
        and _project_dotenv_value("AWF_API_BASE_URL", lookup=project_dotenv_lookup) == api_base_url
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


def _database_url_env_is_explicit(
    environ: Mapping[str, str],
    service_environ: Mapping[str, str],
    *,
    project_dotenv_lookup: _ProjectDotenvLookup | None = None,
) -> bool:
    """Return true when the host environment carries a non-derivable database URL.

    A default-valued ``AWF_DATABASE_URL`` is treated as non-explicit when
    ``AWF_POSTGRES_HOST_PORT`` is also present, allowing the port-derived URL to
    replace a stale default that was left unchanged. If a project ``.env`` was
    sourced into the host shell, a matching default URL is also non-explicit
    when the merged Compose environment carries the Postgres host-port override.
    """

    database_url = _env_value(environ, "AWF_DATABASE_URL")
    if database_url is None:
        return False
    if database_url != DEFAULT_LOCAL_SERVICE_DATABASE_URL:
        return True
    if _env_value(environ, "AWF_POSTGRES_HOST_PORT"):
        return False
    return not (
        _env_value(service_environ, "AWF_POSTGRES_HOST_PORT")
        and _project_dotenv_value("AWF_DATABASE_URL", lookup=project_dotenv_lookup) == database_url
    )


def _project_dotenv_value(
    key: str,
    *,
    lookup: _ProjectDotenvLookup | None = None,
) -> str | None:
    """Return a value from the project .env associated with local Compose."""

    return (lookup or _ProjectDotenvLookup()).value(key)


def _project_dotenv_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []
    cwd = Path.cwd().resolve()

    resolved_env_file = resolve_local_service_compose_env_file()
    if resolved_env_file is not None:
        project_dotenv = _project_dotenv_from_compose_env_file(resolved_env_file)
        project_root = project_dotenv.parent.resolve()
        if cwd == project_root or project_root in cwd.parents:
            candidates.extend(_project_dotenv_ancestor_candidates(cwd, project_root))
        candidates.append(project_dotenv)
    else:
        for root in _awf_source_search_roots(cwd):
            candidates.extend(_project_dotenv_ancestor_candidates(cwd, root))

    return _dedupe_paths(candidates)


def _project_dotenv_ancestor_candidates(start: Path, root: Path) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for directory in (start, *start.parents):
        candidates.append(directory / ".env")
        if directory == root:
            break
    return tuple(candidates)


def _dedupe_paths(paths: list[Path]) -> tuple[Path, ...]:
    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(path)
    return tuple(deduped)


def _project_dotenv_from_compose_env_file(env_file: Path) -> Path:
    project_root = env_file
    for _ in LOCAL_SERVICE_COMPOSE_ENV_FILE.parts:
        project_root = project_root.parent
    return project_root / ".env"


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
