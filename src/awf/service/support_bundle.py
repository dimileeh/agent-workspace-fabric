"""Telemetry-free redacted support bundle for first-time evaluators."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from awf import __version__
from awf.db.session import make_engine, make_session_factory
from awf.host_setup.config import (
    HostSetupConfig,
    HostSetupConfigError,
    ProviderConfig,
    read_host_setup_config,
)
from awf.service.config import (
    COMPOSE_ENV_FILE_OMITTED,
    ComposeEnvFileInput,
    ComposeEnvFileOmitted,
    ServiceSettings,
    resolve_local_service_provider_environ,
    service_config_payload,
)
from awf.service.doctor import _redact_text, _secret_values, collect_doctor_report
from awf.service.metrics import summarize_failure_analysis
from awf.service.status import collect_service_status

BUNDLE_FILENAME_PREFIX = "awf-support-bundle"
ISSUE_TEMPLATE_PATH = ".github/ISSUE_TEMPLATE/bug_report.yml"

_SAFE_EXAMPLE_KEYS = frozenset(
    {"workspace_id", "failure_reason", "reason_code", "status", "updated_at", "count"}
)
_SAFE_CLUSTER_KEYS = frozenset({"failure_reason", "reason_code", "count", "sample_workspace_ids"})


class _DoctorCollectorKwargs(TypedDict, total=False):
    """Optional path context forwarded to the doctor collector."""

    compose_file: Path
    compose_env_file: ComposeEnvFileInput


class _StatusCollectorKwargs(TypedDict, total=False):
    """Optional environment and path context forwarded to the status collector."""

    environ: Mapping[str, str]
    compose_file: Path
    compose_env_file: ComposeEnvFileInput


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


async def collect_support_bundle(
    settings: ServiceSettings,
    *,
    strict_providers: Iterable[str] | None = None,
    provider_environ: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
    compose_file: Path | None = None,
    compose_env_file: ComposeEnvFileInput = COMPOSE_ENV_FILE_OMITTED,
    failure_window_hours: int = 24,
    status_collector: Any = None,
    doctor_collector: Any = None,
    failure_analysis_collector: Any = None,
    setup_config_reader: Callable[[], HostSetupConfig] | None = None,
) -> dict[str, object]:
    """Collect a telemetry-free, redacted support bundle."""
    env = os.environ if environ is None else environ
    provider_env = resolve_local_service_provider_environ(
        provider_environ=provider_environ,
        environ=env,
        compose_file=compose_file,
        compose_env_file=compose_env_file,
    )
    secrets = _secret_values(settings, env, provider_env)

    _status_collector = status_collector or collect_service_status
    _doctor_collector = doctor_collector or collect_doctor_report

    service_status_result: dict[str, object] | BaseException
    doctor_result: dict[str, object] | BaseException | Any
    status_kwargs: _StatusCollectorKwargs = {"environ": env}
    doctor_kwargs: _DoctorCollectorKwargs = {}
    if compose_file is not None:
        status_kwargs["compose_file"] = compose_file
        doctor_kwargs["compose_file"] = compose_file
    if not isinstance(compose_env_file, ComposeEnvFileOmitted):
        status_kwargs["compose_env_file"] = compose_env_file
        doctor_kwargs["compose_env_file"] = compose_env_file
    doctor_task = _doctor_collector(
        settings,
        strict_providers=strict_providers,
        provider_environ=provider_env,
        environ=env,
        **doctor_kwargs,
    )

    service_status_result, doctor_result = await asyncio.gather(
        _status_collector(
            settings,
            strict_providers=strict_providers,
            provider_environ=provider_env,
            **status_kwargs,
        ),
        doctor_task,
        return_exceptions=True,
    )

    if isinstance(service_status_result, BaseException):
        service_status: dict[str, object] = {
            "service": settings.service_name,
            "status": "fail",
            "checks": {},
            "agent_readiness": {"status": "fail"},
            "detail": str(service_status_result),
        }
    else:
        service_status = service_status_result

    if isinstance(doctor_result, BaseException):
        doctor_report: dict[str, object] = {
            "service": settings.service_name,
            "status": "fail",
            "summary": {"ok": 0, "warn": 0, "fail": 1},
            "diagnostics": [],
            "detail": str(doctor_result),
        }
    elif hasattr(doctor_result, "to_dict"):
        doctor_report = doctor_result.to_dict()
    else:
        doctor_report = doctor_result

    agent_readiness = (
        service_status.get("agent_readiness", {}) if isinstance(service_status, Mapping) else {}
    )
    provider_readiness_summary = agent_readiness

    checks_raw = service_status.get("checks", {})
    checks: dict[str, object] = dict(checks_raw) if isinstance(checks_raw, Mapping) else {}
    orphan_cleanup_posture = {
        "workspace_cleanup": checks.get("workspace_cleanup", {}),
        "orphan_resources": checks.get("orphan_resources", {}),
        "stranded_workspaces": checks.get("stranded_workspaces", {}),
    }

    try:
        if failure_analysis_collector is not None:
            failure_summary = await failure_analysis_collector(since_hours=failure_window_hours)
        else:
            failure_summary = await _default_failure_analysis_collector(
                settings,
                since_hours=failure_window_hours,
            )
        recent_failure_summary = _sanitize_failure_summary(failure_summary, secrets)
    except Exception as exc:
        recent_failure_summary = {
            "degraded": True,
            "error": _redact_text(str(exc), secrets),
            "since_hours": failure_window_hours,
        }

    config_fingerprint = service_config_payload(settings)
    if setup_config_reader is None:
        setup_config_reader = _default_setup_config_reader
    setup_state = _setup_state(setup_config_reader, secrets=secrets)

    log_pointers = [
        "Service logs: run `awf service logs --tail 100`",
        "Worker logs: run `awf service logs --service worker --tail 100`",
        f"State directory: {settings.work_dir}",
    ]

    bundle: dict[str, object] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "version": __version__,
        "service_status": _redact_value(service_status, secrets),
        "doctor_report": _redact_value(doctor_report, secrets),
        "provider_readiness_summary": _redact_value(provider_readiness_summary, secrets),
        "orphan_cleanup_posture": _redact_value(orphan_cleanup_posture, secrets),
        "recent_failure_summary": recent_failure_summary,
        "config_fingerprint": _redact_value(config_fingerprint, secrets),
        "setup_state": setup_state,
        "log_pointers": [_redact_text(ptr, secrets) for ptr in log_pointers],
        "issue_template_pointer": ISSUE_TEMPLATE_PATH,
    }

    return bundle


def _default_setup_config_reader() -> HostSetupConfig:
    return read_host_setup_config()


def _setup_state(
    setup_config_reader: Callable[[], HostSetupConfig],
    *,
    secrets: frozenset[str],
) -> dict[str, object]:
    try:
        config = setup_config_reader()
    except HostSetupConfigError as exc:
        payload: dict[str, object] = {
            "status": "failed",
            "reason_code": exc.reason_code,
            "message": _redact_text(exc.message, secrets),
        }
        if exc.details:
            payload["details"] = _redact_value(exc.details, secrets)
        return payload
    except Exception as exc:
        unexpected_payload: dict[str, object] = {
            "status": "failed",
            "reason_code": str(getattr(exc, "reason_code", "HOST_SETUP_CONFIG_READ_FAILED")),
            "message": _redact_text(str(exc), secrets),
        }
        details = getattr(exc, "details", None)
        if details is not None:
            unexpected_payload["details"] = _redact_value(details, secrets)
        return unexpected_payload

    return {
        "status": "loaded",
        "config": {
            "version": config.version,
            "install_channel": _redact_text(config.install.channel, secrets),
            "api_host_port": config.api.host_port,
            "work_dir_configured": bool(config.work_dir),
        },
        "providers": {
            str(_redact_value(name, secrets)): _provider_setup_summary(provider, secrets=secrets)
            for name, provider in sorted(config.providers.items())
        },
        "clients": {
            str(_redact_value(name, secrets)): {
                "status": _redact_text(client.status, secrets),
                "updated_at": _isoformat(client.updated_at),
            }
            for name, client in sorted(config.clients.items())
        },
        "consent": {
            "plain_file_secrets": config.consent.plain_file_secrets,
            "source_checkout_assets": config.consent.source_checkout_assets,
        },
        "source_checkout": _source_checkout_summary(config),
    }


def _provider_setup_summary(
    provider: ProviderConfig,
    *,
    secrets: frozenset[str],
) -> dict[str, object]:
    credential_ref = provider.credential_ref
    return {
        "status": _redact_text(provider.status, secrets),
        "backend": _optional_redacted_text(provider.backend, secrets),
        "source": _optional_redacted_text(provider.source, secrets),
        "credential_ref_present": credential_ref is not None,
        "credential_ref_kind": _credential_ref_kind(credential_ref),
    }


def _source_checkout_summary(config: HostSetupConfig) -> dict[str, object]:
    source_checkout = config.source_checkout
    if source_checkout is None:
        return {"configured": False}
    return {
        "configured": True,
        "verified_at": _isoformat(source_checkout.verified_at),
        "marker_count": len(source_checkout.markers),
    }


def _credential_ref_kind(credential_ref: str | None) -> str | None:
    if credential_ref is None:
        return None
    if credential_ref.startswith("keyring://"):
        return "keyring"
    if credential_ref.startswith("env://"):
        return "env_ref"
    if credential_ref.startswith("plain-file://"):
        return "plain_file"
    return "unknown"


def _optional_redacted_text(value: str | None, secrets: frozenset[str]) -> str | None:
    return None if value is None else _redact_text(value, secrets)


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def write_support_bundle(
    bundle: Mapping[str, object],
    *,
    directory: Path | None = None,
) -> Path:
    """Write a support bundle to a deterministic JSON file."""
    target_dir = Path(directory or Path.cwd()).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    generated_at = bundle.get("generated_at", datetime.now(UTC).isoformat())
    safe_ts = str(generated_at).replace(":", "-").replace("+", "_")
    filename = f"{BUNDLE_FILENAME_PREFIX}-{safe_ts}.json"
    path = target_dir / filename
    path.write_text(
        json.dumps(bundle, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return path


async def _default_failure_analysis_collector(
    settings: ServiceSettings,
    *,
    since_hours: int,
) -> Any:
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    try:
        return await summarize_failure_analysis(session_factory, since_hours=since_hours)
    finally:
        await engine.dispose()


def _sanitize_failure_summary(summary: object, secrets: frozenset[str]) -> dict[str, object]:
    if dataclasses.is_dataclass(summary) and not isinstance(summary, type):
        payload: dict[str, object] = dataclasses.asdict(summary)
    elif isinstance(summary, Mapping):
        payload = dict(summary)
    else:
        payload = {"error": str(summary), "degraded": True}

    payload = _redact_value(payload, secrets)

    examples = payload.get("latest_examples")
    if isinstance(examples, list):
        payload["latest_examples"] = [_safe_example_item(item) for item in examples]

    clusters = payload.get("root_cause_clusters")
    if isinstance(clusters, list):
        payload["root_cause_clusters"] = [_safe_cluster_item(item) for item in clusters]

    return payload


def _safe_example_item(item: object) -> dict[str, object]:
    if not isinstance(item, Mapping):
        return {}
    return {k: v for k, v in item.items() if k in _SAFE_EXAMPLE_KEYS}


def _safe_cluster_item(item: object) -> dict[str, object]:
    if not isinstance(item, Mapping):
        return {}
    return {k: v for k, v in item.items() if k in _SAFE_CLUSTER_KEYS}
