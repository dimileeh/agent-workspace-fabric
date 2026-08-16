"""Helpers for collecting redacted Docker Compose diagnostics."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from awf.common.audit import redact_audit_value

_SERVICE_STARTUP_HEALTH_LOG_TAIL_ENTRIES = 5


def _redacted_diagnostics(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact every captured string in the diagnostics payload before persistence."""
    return cast("dict[str, Any]", redact_audit_value(payload))


def _capture_error_detail_raw(exc: Any) -> str:
    """Summarize an unredacted compose-capture failure for a diagnostics marker.

    Callers must pass the result through ``_redacted_diagnostics`` before
    persisting, returning, or logging it.
    """
    detail = exc.stderr.strip() or exc.stdout.strip() or "<no output>"
    return f"{exc.reason_code}: {detail}"


def _container_is_unhealthy(container: Any) -> bool:
    """Return whether an inspected container is worth capturing diagnostics for."""
    if not isinstance(container, Mapping):
        return False
    state = container.get("State")
    if not isinstance(state, Mapping):
        return False
    health = state.get("Health")
    if isinstance(health, Mapping):
        status = health.get("Status")
        if isinstance(status, str) and status != "none":
            return status != "healthy"
    exit_code = state.get("ExitCode") or 0
    return state.get("Status") == "exited" and exit_code != 0


def _compose_service_name(container: Mapping[str, Any]) -> str | None:
    """Return the compose service label for a container, if present."""
    config = container.get("Config")
    labels = config.get("Labels") if isinstance(config, Mapping) else None
    if not isinstance(labels, Mapping):
        return None
    name = labels.get("com.docker.compose.service")
    return name if isinstance(name, str) and name else None


def _container_health_summary(container: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize state and trailing healthcheck-log entries for a container."""
    state = container.get("State")
    state_map = state if isinstance(state, Mapping) else {}
    health = state_map.get("Health")
    health_map = health if isinstance(health, Mapping) else {}
    raw_log = health_map.get("Log")
    health_log: list[dict[str, Any]] = []
    if isinstance(raw_log, list):
        for entry in raw_log[-_SERVICE_STARTUP_HEALTH_LOG_TAIL_ENTRIES:]:
            if not isinstance(entry, Mapping):
                continue
            health_log.append({"ExitCode": entry.get("ExitCode"), "Output": entry.get("Output")})
    return {
        "status": state_map.get("Status"),
        "exit_code": state_map.get("ExitCode"),
        "health_status": health_map.get("Status"),
        "health_log": health_log,
    }


def _container_healthcheck_test(container: Mapping[str, Any]) -> list[Any] | None:
    """Return the rendered healthcheck ``Test`` array as parsed by compose, if any."""
    config = container.get("Config")
    healthcheck = config.get("Healthcheck") if isinstance(config, Mapping) else None
    test = healthcheck.get("Test") if isinstance(healthcheck, Mapping) else None
    return test if isinstance(test, list) else None
