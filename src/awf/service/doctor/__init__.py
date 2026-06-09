"""Operator-friendly local service diagnostics."""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url

from awf.common.redaction import redact_secrets
from awf.service import provider_readiness
from awf.service.config import (
    COMPOSE_ENV_FILE_OMITTED,
    LOCAL_SERVICE_COMPOSE_ENV_FILE,
    LOCAL_SERVICE_COMPOSE_FILE,
    ComposeEnvFileInput,
    ComposeEnvFileOmitted,
    ServiceSettings,
    local_service_environ,
)
from awf.service.doctor import reasons as _reasons
from awf.service.doctor.models import (
    CompletedProcessLike,
    DiagnosticStatus,
    DoctorDiagnostic,
    DoctorReport,
    PathPredicate,
    ReportStatus,
    SocketConnector,
    SocketLike,
    StatusCollector,
    SubprocessRun,
)
from awf.service.status import collect_service_status

_REASON_TEXT = _reasons._REASON_TEXT
_ReasonText = _reasons._ReasonText
_reason_text = _reasons._reason_text

_CHECK_TIMEOUT_SECONDS = 5.0
_PORT_TIMEOUT_SECONDS = 0.5
_SECRET_KEY_PARTS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "AUTH")

_PROVIDER_LABELS = {
    "codex": "Codex Credentials",
    "claude_code": "Claude Code Credentials",
    "cursor": "Cursor Credentials",
    "gemini": "Gemini Credentials",
    "opencode": "OpenCode Credentials",
    "grok": "Grok Build Credentials",
}


async def collect_doctor_report(
    settings: ServiceSettings,
    *,
    strict_providers: Iterable[str] | None = None,
    provider_environ: Mapping[str, str] | None = None,
    status_collector: StatusCollector | None = None,
    run_subprocess: SubprocessRun | None = None,
    socket_connector: SocketConnector | None = None,
    environ: Mapping[str, str] | None = None,
    path_exists: PathPredicate | None = None,
    path_is_dir: PathPredicate | None = None,
    compose_file: Path = LOCAL_SERVICE_COMPOSE_FILE,
    compose_env_file: ComposeEnvFileInput = COMPOSE_ENV_FILE_OMITTED,
) -> DoctorReport:
    """Collect read-only local diagnostics for operator troubleshooting."""

    env = os.environ if environ is None else environ
    service_env_file: Path | None
    if isinstance(compose_env_file, ComposeEnvFileOmitted):
        resolved_compose_env_file = _local_service_compose_env_file(compose_file)
        service_env_file = resolved_compose_env_file or LOCAL_SERVICE_COMPOSE_ENV_FILE
    else:
        resolved_compose_env_file = compose_env_file
        service_env_file = resolved_compose_env_file
    service_env = local_service_environ(
        env,
        env_file=service_env_file,
    )
    provider_env = service_env if provider_environ is None else provider_environ
    secrets = _secret_values(settings, service_env, provider_env)
    collector = status_collector or collect_service_status
    runner = run_subprocess or _run_subprocess
    connector = socket_connector or _socket_connect
    exists = path_exists or Path.exists
    is_dir = path_is_dir or Path.is_dir
    worker_compose_env_file = (
        resolved_compose_env_file
        if resolved_compose_env_file is not None
        and _safe_path_exists(resolved_compose_env_file, path_exists=exists)
        else None
    )

    try:
        service_status = await collector(
            settings,
            strict_providers=strict_providers,
            provider_environ=provider_env,
        )
    except Exception as exc:
        service_status = _collection_failed_status(settings, exc, secrets)

    diagnostics = _service_status_diagnostics(service_status, settings=settings, secrets=secrets)
    diagnostics.extend(
        (
            _worker_diagnostic(
                settings,
                run_subprocess=runner,
                environ=service_env,
                compose_file=compose_file,
                compose_env_file=worker_compose_env_file,
                secrets=secrets,
            ),
            *_port_diagnostics(settings, socket_connector=connector, secrets=secrets),
            _config_diagnostic(
                settings,
                path_exists=exists,
                path_is_dir=is_dir,
                secrets=secrets,
            ),
        )
    )
    status = _report_status(diagnostics)
    return DoctorReport(
        service=settings.service_name,
        status=status,
        diagnostics=tuple(diagnostics),
    )


def render_doctor_pretty(report: DoctorReport) -> str:
    """Render a concise terminal report."""

    lines = [f"AWF doctor: {report.status}"]
    for diagnostic in report.diagnostics:
        lines.append(f"[{diagnostic.status}] {diagnostic.label}: {diagnostic.message}")
        lines.append(f"       reason: {diagnostic.reason}")
        if diagnostic.detail:
            lines.append(f"       detail: {diagnostic.detail}")
        if diagnostic.action:
            lines.append(f"       action: {diagnostic.action}")
    return "\n".join(lines) + "\n"


def _service_status_diagnostics(
    service_status: Mapping[str, object],
    *,
    settings: ServiceSettings,
    secrets: frozenset[str],
) -> list[DoctorDiagnostic]:
    checks = _mapping(service_status.get("checks"))
    diagnostics = [
        _check_diagnostic(
            "docker",
            "Docker",
            checks.get("docker"),
            source="checks.docker",
            ok_reason="DOCKER_OK",
            secrets=secrets,
        ),
        _check_diagnostic(
            "api",
            "API",
            checks.get("api"),
            source="checks.api",
            ok_reason="API_OK",
            secrets=secrets,
            message_context={"url": f"{settings.api_base_url.rstrip('/')}/healthz"},
        ),
    ]
    diagnostics.extend(_provider_diagnostics(service_status, secrets=secrets))
    diagnostics.extend(
        [
            _check_diagnostic(
                "disk",
                "Disk",
                checks.get("disk"),
                source="checks.disk",
                ok_reason="SUFFICIENT_DISK",
                secrets=secrets,
            ),
            _check_diagnostic(
                "workspace_containers",
                "Workspace Containers",
                checks.get("stranded_workspaces"),
                source="checks.stranded_workspaces",
                ok_reason="NO_STRANDED_WORKSPACES",
                secrets=secrets,
            ),
            _check_diagnostic(
                "orphan_resources",
                "Orphan Resources",
                checks.get("orphan_resources"),
                source="checks.orphan_resources",
                ok_reason="NO_ORPHANS",
                secrets=secrets,
            ),
            _check_diagnostic(
                "network_posture",
                "Network Posture",
                checks.get("network_posture"),
                source="checks.network_posture",
                ok_reason="NETWORK_POSTURE_NO_ACTIVE_OPEN",
                secrets=secrets,
            ),
        ]
    )
    return diagnostics


def _provider_diagnostics(
    service_status: Mapping[str, object],
    *,
    secrets: frozenset[str],
) -> list[DoctorDiagnostic]:
    readiness = _mapping(service_status.get("agent_readiness"))
    providers = _mapping(readiness.get("providers"))
    diagnostics: list[DoctorDiagnostic] = []
    github = _mapping(providers.get("github"))
    diagnostics.append(
        _provider_diagnostic(
            "github",
            "GitHub",
            github,
            source="agent_readiness.providers.github",
            ok_reason="GITHUB_AUTH_OK",
            secrets=secrets,
        )
    )
    for provider_name, label in _PROVIDER_LABELS.items():
        diagnostics.append(
            _provider_diagnostic(
                f"provider.{provider_name}",
                label,
                _mapping(providers.get(provider_name)),
                source=f"agent_readiness.providers.{provider_name}",
                ok_reason=f"{provider_name.upper()}_AUTH_OK",
                secrets=secrets,
            )
        )
    return diagnostics


def _check_diagnostic(
    diagnostic_id: str,
    label: str,
    raw_check: object,
    *,
    source: str,
    ok_reason: str,
    secrets: frozenset[str],
    message_context: Mapping[str, str] | None = None,
) -> DoctorDiagnostic:
    check = _mapping(raw_check)
    status = _status_from_check(check)
    reason = str(check.get("reason") or (ok_reason if status == "ok" else "UNKNOWN"))
    text = _reason_text(reason, label=label, status=status, context=message_context)
    detail = _optional_text(check.get("detail"), secrets)
    metadata = _metadata_from_mapping(check, secrets=secrets)
    return DoctorDiagnostic(
        id=diagnostic_id,
        label=label,
        status=status,
        reason=reason,
        message=_redact_text(text.message, secrets),
        action=_redact_text(text.action, secrets),
        source=source,
        detail=detail,
        metadata=metadata,
    )


def _provider_diagnostic(
    diagnostic_id: str,
    label: str,
    provider: Mapping[str, object],
    *,
    source: str,
    ok_reason: str,
    secrets: frozenset[str],
) -> DoctorDiagnostic:
    status = _status_from_provider(provider)
    reason = str(provider.get("reason") or (ok_reason if status == "ok" else "UNKNOWN"))
    text = _reason_text(reason, label=label, status=status)
    raw_message = provider.get("message")
    message = str(raw_message) if isinstance(raw_message, str) and raw_message else text.message
    raw_action = provider.get("action")
    action = str(raw_action) if isinstance(raw_action, str) and raw_action else text.action
    detail = _optional_text(provider.get("detail"), secrets)
    metadata = _metadata_from_mapping(provider, secrets=secrets)
    return DoctorDiagnostic(
        id=diagnostic_id,
        label=label,
        status=status,
        reason=reason,
        message=_redact_text(message, secrets),
        action=_redact_text(action, secrets),
        source=source,
        detail=detail,
        metadata=metadata,
    )


def _worker_diagnostic(
    settings: ServiceSettings,
    *,
    run_subprocess: SubprocessRun,
    environ: Mapping[str, str],
    compose_file: Path,
    compose_env_file: Path | None,
    secrets: frozenset[str],
) -> DoctorDiagnostic:
    args = [
        "docker",
        "compose",
    ]
    if compose_env_file is not None:
        args.extend(["--env-file", str(compose_env_file)])
    args.extend(["-f", str(compose_file), "ps", "worker", "--format", "json"])
    try:
        result = run_subprocess(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=_CHECK_TIMEOUT_SECONDS,
            env={**dict(environ), "DOCKER_HOST": settings.docker_host},
        )
    except FileNotFoundError:
        return _simple_diagnostic(
            "worker",
            "Worker",
            "fail",
            "DOCKER_CLI_NOT_FOUND",
            source="docker.compose.ps.worker",
            detail="docker binary not found on PATH",
            metadata={"compose_file": str(compose_file), "service": "worker"},
            secrets=secrets,
        )
    except subprocess.TimeoutExpired as exc:
        return _simple_diagnostic(
            "worker",
            "Worker",
            "fail",
            "WORKER_STATUS_UNAVAILABLE",
            source="docker.compose.ps.worker",
            detail=str(exc),
            metadata={"compose_file": str(compose_file), "service": "worker"},
            secrets=secrets,
        )
    except Exception as exc:
        return _simple_diagnostic(
            "worker",
            "Worker",
            "fail",
            "WORKER_STATUS_UNAVAILABLE",
            source="docker.compose.ps.worker",
            detail=f"{type(exc).__name__}: {exc}",
            metadata={"compose_file": str(compose_file), "service": "worker"},
            secrets=secrets,
        )

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if result.returncode != 0:
        return _simple_diagnostic(
            "worker",
            "Worker",
            "fail",
            "WORKER_STATUS_UNAVAILABLE",
            source="docker.compose.ps.worker",
            detail=stderr or stdout or f"docker compose ps exited {result.returncode}",
            metadata={"compose_file": str(compose_file), "service": "worker"},
            secrets=secrets,
        )

    records = _parse_compose_ps(stdout)
    if records is None:
        return _simple_diagnostic(
            "worker",
            "Worker",
            "fail",
            "WORKER_STATUS_UNPARSEABLE",
            source="docker.compose.ps.worker",
            detail=stdout,
            metadata={"compose_file": str(compose_file), "service": "worker"},
            secrets=secrets,
        )
    if not records:
        return _simple_diagnostic(
            "worker",
            "Worker",
            "fail",
            "WORKER_CONTAINER_MISSING",
            source="docker.compose.ps.worker",
            metadata={"compose_file": str(compose_file), "service": "worker"},
            secrets=secrets,
        )

    record = _worker_record(records)
    if record is None:
        return _simple_diagnostic(
            "worker",
            "Worker",
            "fail",
            "WORKER_CONTAINER_MISSING",
            source="docker.compose.ps.worker",
            metadata={"compose_file": str(compose_file), "service": "worker"},
            secrets=secrets,
        )

    state = _record_text(record, "State", "state")
    status_text = _record_text(record, "Status", "status")
    health = _record_text(record, "Health", "health")
    exit_code = record.get("ExitCode", record.get("exit_code"))
    metadata = {
        "compose_file": str(compose_file),
        "service": "worker",
        "state": state,
        "status": status_text,
        "health": health,
        "exit_code": exit_code,
    }
    state_lower = state.lower()
    status_lower = status_text.lower()
    health_lower = health.lower()
    if health_lower == "unhealthy":
        return _simple_diagnostic(
            "worker",
            "Worker",
            "fail",
            "WORKER_UNHEALTHY",
            source="docker.compose.ps.worker",
            metadata=metadata,
            secrets=secrets,
        )
    if "running" in state_lower or status_lower.startswith("up"):
        return _simple_diagnostic(
            "worker",
            "Worker",
            "ok",
            "WORKER_RUNNING",
            source="docker.compose.ps.worker",
            metadata=metadata,
            secrets=secrets,
        )
    if "exit" in state_lower or "exit" in status_lower or _exit_code_nonzero(exit_code):
        return _simple_diagnostic(
            "worker",
            "Worker",
            "fail",
            "WORKER_CONTAINER_EXITED",
            source="docker.compose.ps.worker",
            metadata=metadata,
            secrets=secrets,
        )
    return _simple_diagnostic(
        "worker",
        "Worker",
        "fail",
        "WORKER_CONTAINER_NOT_RUNNING",
        source="docker.compose.ps.worker",
        metadata=metadata,
        secrets=secrets,
    )


def _local_service_compose_env_file(compose_file: Path) -> Path | None:
    candidates: list[Path] = []
    if LOCAL_SERVICE_COMPOSE_ENV_FILE.is_absolute():
        candidates.append(LOCAL_SERVICE_COMPOSE_ENV_FILE)
    else:
        candidates.append(compose_file.parent / ".env")
        candidates.append(LOCAL_SERVICE_COMPOSE_ENV_FILE)

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.expanduser()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            return resolved
    return None


def _port_diagnostics(
    settings: ServiceSettings,
    *,
    socket_connector: SocketConnector,
    secrets: frozenset[str],
) -> tuple[DoctorDiagnostic, DoctorDiagnostic]:
    return (
        _port_diagnostic(
            "port.api",
            "API Port",
            _api_endpoint(settings.api_base_url),
            socket_connector=socket_connector,
            source="ports.api",
            secrets=secrets,
        ),
        _port_diagnostic(
            "port.db",
            "Postgres Port",
            _database_endpoint(settings.database_url),
            socket_connector=socket_connector,
            source="ports.db",
            secrets=secrets,
        ),
    )


def _port_diagnostic(
    diagnostic_id: str,
    label: str,
    endpoint: tuple[str, int] | str,
    *,
    socket_connector: SocketConnector,
    source: str,
    secrets: frozenset[str],
) -> DoctorDiagnostic:
    if isinstance(endpoint, str):
        return _simple_diagnostic(
            diagnostic_id,
            label,
            "skipped",
            "PORT_CONFIG_INVALID",
            source=source,
            detail=endpoint,
            secrets=secrets,
        )
    host, port = endpoint
    metadata = {"host": host, "port": port}
    try:
        connection = socket_connector((host, port), _PORT_TIMEOUT_SECONDS)
    except OSError as exc:
        return _simple_diagnostic(
            diagnostic_id,
            label,
            "fail",
            "PORT_CLOSED",
            source=source,
            detail=f"{host}:{port}: {exc}",
            metadata=metadata,
            secrets=secrets,
        )
    with contextlib.suppress(Exception):
        connection.close()
    return _simple_diagnostic(
        diagnostic_id,
        label,
        "ok",
        "PORT_OPEN",
        source=source,
        metadata=metadata,
        message_context={"endpoint": f"{host}:{port}"},
        secrets=secrets,
    )


def _config_diagnostic(
    settings: ServiceSettings,
    *,
    path_exists: PathPredicate,
    path_is_dir: PathPredicate,
    secrets: frozenset[str],
) -> DoctorDiagnostic:
    issues: list[dict[str, str]] = []
    api_issue = _api_endpoint(settings.api_base_url)
    if isinstance(api_issue, str):
        issues.append(
            {
                "reason": "CONFIG_API_BASE_URL_INVALID",
                "setting": "AWF_API_BASE_URL",
                "detail": api_issue,
            }
        )
    db_issue = _database_endpoint(settings.database_url)
    if isinstance(db_issue, str):
        issues.append(
            {
                "reason": "CONFIG_DATABASE_URL_INVALID",
                "setting": "AWF_DATABASE_URL",
                "detail": db_issue,
            }
        )

    host_home = Path(settings.host_home or "~").expanduser()
    if not _safe_path_is_dir(host_home, path_is_dir=path_is_dir):
        issues.append(
            {
                "reason": "HOST_HOME_MISSING",
                "setting": "AWF_HOST_HOME",
                "detail": f"{host_home} is not an accessible directory",
            }
        )

    work_dir_parent = Path(settings.work_dir).expanduser().parent
    if not (
        _safe_path_exists(work_dir_parent, path_exists=path_exists)
        and _safe_path_is_dir(work_dir_parent, path_is_dir=path_is_dir)
    ):
        issues.append(
            {
                "reason": "WORK_DIR_PARENT_INACCESSIBLE",
                "setting": "AWF_WORK_DIR",
                "detail": f"{work_dir_parent} is not an accessible directory",
            }
        )

    if not issues:
        return _simple_diagnostic(
            "local_config",
            "Local Config",
            "ok",
            "LOCAL_CONFIG_OK",
            source="config",
            metadata={
                "api_base_url": settings.api_base_url,
                "host_home": str(host_home),
                "work_dir_parent": str(work_dir_parent),
            },
            secrets=secrets,
        )

    return _simple_diagnostic(
        "local_config",
        "Local Config",
        "fail",
        "LOCAL_CONFIG_INVALID",
        source="config",
        metadata={"issue_count": len(issues), "issues": issues},
        secrets=secrets,
    )


def _simple_diagnostic(
    diagnostic_id: str,
    label: str,
    status: DiagnosticStatus,
    reason: str,
    *,
    source: str,
    secrets: frozenset[str],
    detail: str | None = None,
    metadata: Mapping[str, object] | None = None,
    message_context: Mapping[str, str] | None = None,
) -> DoctorDiagnostic:
    text = _reason_text(reason, label=label, status=status, context=message_context)
    return DoctorDiagnostic(
        id=diagnostic_id,
        label=label,
        status=status,
        reason=reason,
        message=_redact_text(text.message, secrets),
        action=_redact_text(text.action, secrets),
        source=source,
        detail=_optional_text(detail, secrets),
        metadata=_redact_mapping(metadata or {}, secrets),
    )


def _status_from_check(check: Mapping[str, object]) -> DiagnosticStatus:
    raw_status = str(check.get("status") or "").lower()
    ok = check.get("ok")
    if ok is False:
        return "fail"
    if raw_status in {"fail", "failed", "error"}:
        return "fail"
    if raw_status in {"warn", "warning", "unknown", "unavailable"}:
        return "warn"
    if ok is True or raw_status in {"ok", "ready", "disabled"}:
        return "ok"
    return "skipped"


def _status_from_provider(provider: Mapping[str, object]) -> DiagnosticStatus:
    raw_status = str(provider.get("status") or "").lower()
    if raw_status == "ok":
        return "ok"
    if raw_status == "warn":
        return "warn"
    if raw_status == "fail":
        return "fail"
    ok = provider.get("ok")
    if ok is True:
        return "ok"
    if ok is False:
        return "warn"
    return "skipped"


def _report_status(diagnostics: Iterable[DoctorDiagnostic]) -> ReportStatus:
    statuses = {diagnostic.status for diagnostic in diagnostics}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "ok"


def _metadata_from_mapping(
    payload: Mapping[str, object],
    *,
    secrets: frozenset[str],
) -> dict[str, object]:
    metadata = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "ok",
            "status",
            "reason",
            "message",
            "action",
            "detail",
            "warnings",
        }
    }
    return _redact_mapping(metadata, secrets)


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    return {}


def _optional_text(value: object, secrets: frozenset[str]) -> str | None:
    if value is None:
        return None
    return _redact_text(str(value), secrets)


def _parse_compose_ps(stdout: str) -> list[Mapping[str, object]] | None:
    text = stdout.strip()
    if not text:
        return []
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        line_records: list[Mapping[str, object]] = []
        for line in text.splitlines():
            try:
                loaded_line = json.loads(line)
            except json.JSONDecodeError:
                return None
            if not isinstance(loaded_line, Mapping):
                return None
            line_records.append(loaded_line)
        return line_records
    if isinstance(loaded, Mapping):
        return [loaded]
    if isinstance(loaded, list):
        records: list[Mapping[str, object]] = []
        for item in loaded:
            if not isinstance(item, Mapping):
                return None
            records.append(item)
        return records
    return None


def _worker_record(records: list[Mapping[str, object]]) -> Mapping[str, object] | None:
    for record in records:
        service = _record_text(record, "Service", "service")
        name = _record_text(record, "Name", "name")
        if service == "worker" or name.endswith("worker"):
            return record
    return None


def _record_text(record: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        if value is not None:
            return str(value)
    return ""


def _exit_code_nonzero(value: object) -> bool:
    if value is None or value == "":
        return False
    try:
        return int(str(value)) != 0
    except ValueError:
        return True


def _api_endpoint(api_base_url: str) -> tuple[str, int] | str:
    try:
        parsed = urlsplit(api_base_url)
    except ValueError as exc:
        return f"{type(exc).__name__}: {exc}"
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return f"AWF_API_BASE_URL must be an http(s) URL with a host: {_redact_uri(api_base_url)}"
    try:
        port = parsed.port
    except ValueError as exc:
        return f"{type(exc).__name__}: {exc}"
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.hostname, port


def _database_endpoint(database_url: str) -> tuple[str, int] | str:
    try:
        url = make_url(database_url)
    except Exception as exc:
        return f"{type(exc).__name__}: {_redact_uri(str(exc))}"
    if not url.host:
        return "AWF_DATABASE_URL must include a host for local service mode."
    try:
        port = url.port or 5432
    except Exception as exc:
        return f"{type(exc).__name__}: {_redact_uri(str(exc))}"
    return url.host, port


def _safe_path_exists(path: Path, *, path_exists: PathPredicate) -> bool:
    try:
        return path_exists(path)
    except OSError:
        return False


def _safe_path_is_dir(path: Path, *, path_is_dir: PathPredicate) -> bool:
    try:
        return path_is_dir(path)
    except OSError:
        return False


def _redact_value(value: object, secrets: frozenset[str]) -> Any:
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, Mapping):
        return {
            str(_redact_value(key, secrets)): _redact_value(nested, secrets)
            for key, nested in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact_value(item, secrets) for item in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    return _redact_text(str(value), secrets)


def _redact_mapping(value: Mapping[str, object], secrets: frozenset[str]) -> dict[str, object]:
    return cast(dict[str, object], _redact_value(value, secrets))


def _secret_values(
    settings: ServiceSettings,
    *environments: Mapping[str, str],
) -> frozenset[str]:
    values: set[str] = set()
    for env in environments:
        values.update(provider_readiness._secret_values(settings, env))
        for key, value in env.items():
            if len(value) >= 4 and any(part in key.upper() for part in _SECRET_KEY_PARTS):
                values.add(value)
    if settings.api_token and len(settings.api_token) >= 4:
        values.add(settings.api_token)
    if settings.github_token and len(settings.github_token) >= 4:
        values.add(settings.github_token)
    try:
        password = make_url(settings.database_url).password
    except Exception:
        password = None
    if password and len(password) >= 4:
        values.add(password)
    return frozenset(values)


def _redact_text(value: str, secrets: frozenset[str]) -> str:
    redacted = provider_readiness._redact(value, secrets)
    return redact_secrets(_redact_uri(redacted))


def _redact_uri(value: str) -> str:
    return re.sub(
        r"([A-Za-z][A-Za-z0-9+.-]*://[^:/\s@]+:)([^@\s]+)(@)",
        r"\1<redacted>\3",
        value,
    )


def _collection_failed_status(
    settings: ServiceSettings,
    exc: Exception,
    secrets: frozenset[str],
) -> dict[str, object]:
    return {
        "service": settings.service_name,
        "status": "fail",
        "checks": {
            "api": {
                "ok": False,
                "status": "fail",
                "reason": "SERVICE_STATUS_COLLECTION_FAILED",
                "detail": _redact_text(f"{type(exc).__name__}: {exc}", secrets),
            }
        },
        "agent_readiness": {"status": "fail", "providers": {}},
    }


def _socket_connect(address: tuple[str, int], timeout: float) -> SocketLike:
    return socket.create_connection(address, timeout=timeout)


def _run_subprocess(
    args: list[str],
    *,
    check: bool,
    capture_output: bool,
    text: Literal[True],
    timeout: float,
    env: Mapping[str, str],
) -> CompletedProcessLike:
    return subprocess.run(
        args,
        check=check,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        env=env,
    )
