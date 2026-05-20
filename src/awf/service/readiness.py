"""Executable AWF Core release-readiness scorecard."""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Awaitable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, TypedDict, cast

from awf.db.session import make_engine, make_session_factory
from awf.profiles.onboarding import preview_project_onboarding
from awf.service.config import (
    COMPOSE_ENV_FILE_OMITTED,
    LOCAL_SERVICE_COMPOSE_FILE,
    ComposeEnvFileInput,
    ComposeEnvFileOmitted,
    ServiceSettings,
    resolve_local_service_provider_environ,
)
from awf.service.doctor import collect_doctor_report
from awf.service.doctor.models import DoctorReport
from awf.service.metrics import (
    DEFAULT_FAILURE_EXAMPLE_LIMIT,
    UNKNOWN_FAILURE_REASON,
    FailureAnalysisSummary,
    SloMetricsSummary,
    summarize_failure_analysis,
    summarize_slo_metrics,
)
from awf.service.status import collect_service_status

CoreReadinessStatus = Literal["ok", "warn", "fail"]

DEFAULT_DEMO_PATH = Path(__file__).resolve().parents[3] / "examples" / "awf-core-demo"
DEFAULT_FAILURE_WINDOW_HOURS = 24
DEFAULT_SLO_WINDOW_HOURS = 168
MIN_WORKSPACE_CREATION_SUCCESS_RATE = 0.98
MIN_CLEANUP_SUCCESS_RATE = 0.99
MAX_STUCK_STATE_RATE = 0.005
MIN_ACTIONABLE_FAILURE_REASON_RATE = 0.95
GENERIC_FAILURE_REASONS = frozenset({"", UNKNOWN_FAILURE_REASON, "agent_failure"})
GENERIC_REASON_CODES = frozenset({"", UNKNOWN_FAILURE_REASON, "agent_failure", "AGENT_FAILURE"})


class StatusCollector(Protocol):
    def __call__(  # pragma: no cover - Protocol declaration only.
        self,
        settings: ServiceSettings,
        *,
        strict_providers: Iterable[str] | None = None,
        provider_environ: Mapping[str, str] | None = None,
        environ: Mapping[str, str] | None = None,
        compose_file: Path | None = None,
        compose_env_file: ComposeEnvFileInput = COMPOSE_ENV_FILE_OMITTED,
    ) -> Awaitable[dict[str, object]]: ...


class DoctorCollector(Protocol):
    """Callable protocol for collecting doctor diagnostics during readiness."""

    def __call__(  # pragma: no cover - Protocol declaration only.
        self,
        settings: ServiceSettings,
        *,
        strict_providers: Iterable[str] | None = None,
        provider_environ: Mapping[str, str] | None = None,
        environ: Mapping[str, str] | None = None,
        status_collector: StatusCollector | None = None,
        compose_file: Path = LOCAL_SERVICE_COMPOSE_FILE,
        compose_env_file: ComposeEnvFileInput = COMPOSE_ENV_FILE_OMITTED,
    ) -> Awaitable[DoctorReport]:
        """Collect a doctor report using the readiness command context."""
        ...  # pragma: no cover


class FailureAnalysisCollector(Protocol):
    def __call__(  # pragma: no cover - Protocol declaration only.
        self,
        *,
        since_hours: int,
    ) -> Awaitable[FailureAnalysisSummary]: ...


class SloMetricsCollector(Protocol):
    def __call__(  # pragma: no cover - Protocol declaration only.
        self,
        *,
        since_hours: int,
    ) -> Awaitable[SloMetricsSummary | Mapping[str, object]]: ...


class _StatusCollectorKwargs(TypedDict, total=False):
    strict_providers: Iterable[str] | None
    provider_environ: Mapping[str, str] | None
    environ: Mapping[str, str] | None
    compose_file: Path | None
    compose_env_file: ComposeEnvFileInput


class _DoctorCollectorKwargs(TypedDict, total=False):
    strict_providers: Iterable[str] | None
    provider_environ: Mapping[str, str] | None
    environ: Mapping[str, str]
    status_collector: StatusCollector | None
    compose_file: Path
    compose_env_file: ComposeEnvFileInput


@dataclass(frozen=True, kw_only=True)
class CoreReadinessCheck:
    """One stable release-gate check."""

    name: str
    status: CoreReadinessStatus
    reason_code: str
    message: str
    evidence: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "reason_code": self.reason_code,
            "message": self.message,
            "evidence": _jsonable(dict(self.evidence)),
        }


@dataclass(frozen=True, kw_only=True)
class CoreReadinessReport:
    """Top-level executable AWF Core release scorecard."""

    status: CoreReadinessStatus
    checks: tuple[CoreReadinessCheck, ...]
    next_actions: tuple[str, ...] = ()

    @property
    def summary(self) -> dict[str, int]:
        return {
            "ok": sum(1 for check in self.checks if check.status == "ok"),
            "warn": sum(1 for check in self.checks if check.status == "warn"),
            "fail": sum(1 for check in self.checks if check.status == "fail"),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "summary": self.summary,
            "checks": [check.to_dict() for check in self.checks],
            "next_actions": list(self.next_actions),
        }


def render_core_readiness_pretty(report: CoreReadinessReport) -> str:
    """Render the Core release gate as a compact operator terminal view."""

    lines = [
        f"AWF Core release readiness: {report.status}",
        (
            "This is the release gate for AWF Core. For local service health, "
            "use `awf service status --format pretty` or `awf service doctor`."
        ),
        (
            f"Summary: {report.summary['ok']} ok, {report.summary['warn']} warn, "
            f"{report.summary['fail']} fail"
        ),
        "",
        "Checks:",
    ]
    for check in report.checks:
        lines.append(f"  [{check.status}] {check.name}: {check.message}")
        if check.status != "ok":
            lines.append(f"        reason: {check.reason_code}")
        lines.extend(_readiness_evidence_lines(check))

    if report.next_actions:
        lines.append("")
        lines.append("Next actions:")
        for action in report.next_actions:
            lines.append(f"  - {action}")

    return "\n".join(lines) + "\n"


async def collect_core_readiness_report(
    *,
    settings: ServiceSettings,
    demo_path: Path = DEFAULT_DEMO_PATH,
    failure_window_hours: int = DEFAULT_FAILURE_WINDOW_HOURS,
    slo_window_hours: int = DEFAULT_SLO_WINDOW_HOURS,
    strict_providers: Iterable[str] | None = None,
    provider_environ: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
    compose_file: Path = LOCAL_SERVICE_COMPOSE_FILE,
    compose_env_file: ComposeEnvFileInput = COMPOSE_ENV_FILE_OMITTED,
    allow_generic_failures: bool = False,
    allow_slo_breach: bool = False,
    status_collector: StatusCollector = collect_service_status,
    doctor_collector: DoctorCollector = collect_doctor_report,
    failure_analysis_collector: FailureAnalysisCollector | None = None,
    slo_metrics_collector: SloMetricsCollector | None = None,
) -> CoreReadinessReport:
    """Collect the local open-source Core release gate from public service surfaces."""

    env = os.environ if environ is None else environ
    provider_env = resolve_local_service_provider_environ(
        provider_environ=provider_environ,
        environ=env,
        compose_file=compose_file,
        compose_env_file=compose_env_file,
    )
    strict = frozenset(strict_providers or ())

    checks: list[CoreReadinessCheck] = []
    status_payload: dict[str, object] | None = None
    status_kwargs: _StatusCollectorKwargs = {
        "strict_providers": strict,
        "provider_environ": provider_env,
        "environ": env,
        "compose_file": compose_file,
    }
    if not isinstance(compose_env_file, ComposeEnvFileOmitted):
        status_kwargs["compose_env_file"] = compose_env_file
    try:
        status_payload = await status_collector(settings, **status_kwargs)
        checks.append(_service_status_check(status_payload))
        checks.append(_provider_readiness_check(status_payload))
        checks.append(_cleanup_posture_check(status_payload))
    except Exception as exc:
        checks.append(
            CoreReadinessCheck(
                name="service_status",
                status="fail",
                reason_code="SERVICE_STATUS_COLLECTION_FAILED",
                message="service status collection failed",
                evidence={"error": str(exc)},
            )
        )

    cached_status_collector = _cached_status_collector(status_payload)
    doctor_kwargs: _DoctorCollectorKwargs = {
        "strict_providers": strict,
        "provider_environ": provider_env,
        "environ": env,
        "status_collector": cached_status_collector,
        "compose_file": compose_file,
    }
    if not isinstance(compose_env_file, ComposeEnvFileOmitted):
        doctor_kwargs["compose_env_file"] = compose_env_file
    try:
        doctor_report = await doctor_collector(settings, **doctor_kwargs)
        checks.append(_doctor_check(doctor_report))
    except Exception as exc:
        checks.append(
            CoreReadinessCheck(
                name="doctor",
                status="fail",
                reason_code="DOCTOR_COLLECTION_FAILED",
                message="doctor diagnostics failed to run",
                evidence={"error": str(exc)},
            )
        )

    checks.append(_demo_check(demo_path))
    try:
        slo_summary = await _collect_slo_summary(
            settings,
            slo_window_hours=slo_window_hours,
            collector=slo_metrics_collector,
        )
        checks.append(
            _slo_threshold_check(
                slo_summary,
                allow_slo_breach=allow_slo_breach,
            )
        )
    except Exception as exc:
        checks.append(
            CoreReadinessCheck(
                name="prd_slo_thresholds",
                status="warn" if allow_slo_breach else "fail",
                reason_code=(
                    "PRD_SLO_METRICS_UNAVAILABLE_ALLOWLISTED"
                    if allow_slo_breach
                    else "PRD_SLO_METRICS_UNAVAILABLE"
                ),
                message="rolling PRD SLO metrics are unavailable",
                evidence={"error": str(exc), "allow_slo_breach": allow_slo_breach},
            )
        )
    failure_summary = await _collect_failure_summary(
        settings,
        failure_window_hours=failure_window_hours,
        collector=failure_analysis_collector,
    )
    checks.append(
        _failure_taxonomy_check(
            failure_summary,
            allow_generic_failures=allow_generic_failures,
        )
    )

    status: CoreReadinessStatus = (
        "fail"
        if any(c.status == "fail" for c in checks)
        else ("warn" if any(c.status == "warn" for c in checks) else "ok")
    )
    return CoreReadinessReport(
        status=status,
        checks=tuple(checks),
        next_actions=tuple(_next_actions(checks)),
    )


def _readiness_evidence_lines(check: CoreReadinessCheck) -> list[str]:
    if check.name == "prd_slo_thresholds":
        evidence = _mapping(check.evidence)
        lines: list[str] = []
        since_hours = evidence.get("since_hours")
        if since_hours is not None:
            lines.append(f"        window: {since_hours}h")
        breaches = _mapping(evidence.get("breaches"))
        if breaches:
            lines.append("        breaches:")
            for name, breach in sorted(breaches.items()):
                if not isinstance(breach, Mapping):
                    lines.append(f"          - {name}: {breach}")
                    continue
                threshold = _mapping(breach.get("threshold"))
                operator = _string(threshold.get("operator")) or "?"
                threshold_value = threshold.get("value")
                actual = breach.get("actual")
                lines.append(
                    "          - "
                    f"{name}: {_format_readiness_rate(actual)} "
                    f"{operator} {_format_readiness_rate(threshold_value)}"
                )
        elif check.status != "ok":
            lines.append("        breaches: none reported")
        return lines

    if check.name in {"service_status", "provider_readiness", "cleanup_posture"}:
        status = check.evidence.get("status")
        if status is not None:
            return [f"        evidence status: {status}"]
    return []


def _format_readiness_rate(value: object) -> str:
    if isinstance(value, int | float):
        return f"{value * 100:.1f}%"
    if value is None:
        return "no data"
    return str(value)


def _cached_status_collector(payload: dict[str, object] | None) -> StatusCollector | None:
    if payload is None:
        return None

    async def _collect(
        _settings: ServiceSettings,
        *,
        strict_providers: Iterable[str] | None = None,
        provider_environ: Mapping[str, str] | None = None,
        environ: Mapping[str, str] | None = None,
        compose_file: Path | None = None,
        compose_env_file: ComposeEnvFileInput = COMPOSE_ENV_FILE_OMITTED,
    ) -> dict[str, object]:
        del strict_providers, provider_environ, environ, compose_file, compose_env_file
        return payload

    return cast(StatusCollector, _collect)


def _service_status_check(payload: Mapping[str, object]) -> CoreReadinessCheck:
    ok = payload.get("status") == "ok"
    return CoreReadinessCheck(
        name="service_status",
        status="ok" if ok else "fail",
        reason_code="SERVICE_STATUS_OK" if ok else "SERVICE_STATUS_NOT_READY",
        message=(
            "service dependencies are ready" if ok else "service dependencies are not all ready"
        ),
        evidence={
            "status": payload.get("status"),
            "checks": payload.get("checks", {}),
        },
    )


def _provider_readiness_check(payload: Mapping[str, object]) -> CoreReadinessCheck:
    readiness = _mapping(payload.get("agent_readiness"))
    ok = readiness.get("status") == "ok"
    return CoreReadinessCheck(
        name="provider_readiness",
        status="ok" if ok else "fail",
        reason_code="PROVIDERS_READY" if ok else "PROVIDER_READINESS_FAILED",
        message=(
            "configured agent providers are ready"
            if ok
            else "one or more configured agent providers are not ready"
        ),
        evidence=readiness,
    )


def _cleanup_posture_check(payload: Mapping[str, object]) -> CoreReadinessCheck:
    checks = _mapping(payload.get("checks"))
    names = ("workspace_cleanup", "orphan_resources", "stranded_workspaces")
    evidence = {name: checks.get(name, {}) for name in names}
    failed = [
        name
        for name, check in evidence.items()
        if isinstance(check, Mapping) and check.get("ok") is False
    ]
    return CoreReadinessCheck(
        name="cleanup_posture",
        status="fail" if failed else "ok",
        reason_code="CLEANUP_POSTURE_NOT_READY" if failed else "CLEANUP_POSTURE_OK",
        message=(
            "cleanup and orphan checks are release-blocking"
            if failed
            else "cleanup and orphan checks are ready"
        ),
        evidence=evidence,
    )


def _doctor_check(report: DoctorReport) -> CoreReadinessCheck:
    return CoreReadinessCheck(
        name="doctor",
        status=report.status,
        reason_code="DOCTOR_OK" if report.status == "ok" else "DOCTOR_NOT_CLEAN",
        message=(
            "doctor diagnostics are clean"
            if report.status == "ok"
            else "doctor diagnostics include warnings or failures"
        ),
        evidence=report.to_dict(),
    )


def _demo_check(path: Path) -> CoreReadinessCheck:
    demo_root = path.expanduser().resolve()
    if not demo_root.exists():
        return CoreReadinessCheck(
            name="demo_project",
            status="fail",
            reason_code="DEMO_PROJECT_MISSING",
            message="in-repo AWF Core demo project is missing",
            evidence={"path": str(demo_root)},
        )
    try:
        preview = preview_project_onboarding(demo_root, include_smoke_request=True)
    except Exception as exc:
        return CoreReadinessCheck(
            name="demo_project",
            status="fail",
            reason_code="DEMO_PROJECT_PREVIEW_FAILED",
            message="demo project onboarding preview failed",
            evidence={"path": str(demo_root), "error": str(exc)},
        )
    payload = preview.to_dict()
    smoke_request = payload.get("smoke_request")
    template = preview.draft.template
    ok = isinstance(smoke_request, dict) and template != "generic"
    return CoreReadinessCheck(
        name="demo_project",
        status="ok" if ok else "fail",
        reason_code="DEMO_PROJECT_READY" if ok else "DEMO_PROJECT_NOT_SMOKEABLE",
        message=(
            "demo project previews to a smoke-workspace request"
            if ok
            else "demo project does not produce a useful smoke-workspace request"
        ),
        evidence={
            "path": str(demo_root),
            "template": template,
            "confidence": preview.inspection.confidence,
            "signals": list(preview.inspection.signals),
            "has_smoke_request": isinstance(smoke_request, dict),
        },
    )


async def _collect_failure_summary(
    settings: ServiceSettings,
    *,
    failure_window_hours: int,
    collector: FailureAnalysisCollector | None,
) -> FailureAnalysisSummary | dict[str, object]:
    if collector is not None:
        return await collector(since_hours=failure_window_hours)

    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    try:
        return await summarize_failure_analysis(
            session_factory,
            since_hours=failure_window_hours,
            failure_example_limit=DEFAULT_FAILURE_EXAMPLE_LIMIT,
        )
    finally:
        await engine.dispose()


async def _collect_slo_summary(
    settings: ServiceSettings,
    *,
    slo_window_hours: int,
    collector: SloMetricsCollector | None,
) -> SloMetricsSummary | Mapping[str, object]:
    if collector is not None:
        return await collector(since_hours=slo_window_hours)

    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    try:
        return await summarize_slo_metrics(
            session_factory,
            settings=settings,  # type: ignore[arg-type]
            since_hours=slo_window_hours,
        )
    finally:
        await engine.dispose()


def _slo_threshold_check(
    summary: SloMetricsSummary | Mapping[str, object],
    *,
    allow_slo_breach: bool,
) -> CoreReadinessCheck:
    creation_total = _int(_get(summary, "creation_total"))
    cleanup_total = _int(_get(summary, "cleanup_total"))
    failure_total = _int(_get(summary, "actionable_failure_count")) + _int(
        _get(summary, "unactionable_failure_count")
    )

    rates: dict[str, float | None] = {
        "workspace_creation_success_rate": _rate(
            _int(_get(summary, "creation_succeeded")),
            creation_total,
        ),
        "cleanup_success_rate": _rate(
            _int(_get(summary, "cleanup_succeeded")),
            cleanup_total,
        ),
        "stuck_state_rate": _rate(_int(_get(summary, "stuck_running_count")), creation_total),
        "actionable_failure_reason_rate": (
            1.0
            if failure_total == 0
            else _rate(_int(_get(summary, "actionable_failure_count")), failure_total)
        ),
    }
    thresholds = {
        "workspace_creation_success_rate": {
            "operator": ">=",
            "value": MIN_WORKSPACE_CREATION_SUCCESS_RATE,
        },
        "cleanup_success_rate": {
            "operator": ">=",
            "value": MIN_CLEANUP_SUCCESS_RATE,
        },
        "stuck_state_rate": {"operator": "<", "value": MAX_STUCK_STATE_RATE},
        "actionable_failure_reason_rate": {
            "operator": ">=",
            "value": MIN_ACTIONABLE_FAILURE_REASON_RATE,
        },
    }

    breaches: dict[str, dict[str, object]] = {}
    if creation_total <= 0:
        breaches["workspace_creation_success_rate"] = {
            "reason": "SLO_METRICS_INSUFFICIENT_EVIDENCE",
            "actual": None,
            "threshold": thresholds["workspace_creation_success_rate"],
        }
    if cleanup_total <= 0:
        breaches["cleanup_success_rate"] = {
            "reason": "SLO_METRICS_INSUFFICIENT_EVIDENCE",
            "actual": None,
            "threshold": thresholds["cleanup_success_rate"],
        }

    for name, actual in rates.items():
        if actual is None or name in breaches:
            continue
        threshold = cast(float, thresholds[name]["value"])
        operator = thresholds[name]["operator"]
        ok = actual < threshold if operator == "<" else actual >= threshold
        if not ok:
            breaches[name] = {
                "reason": "SLO_THRESHOLD_BREACHED",
                "actual": actual,
                "threshold": thresholds[name],
            }

    evidence: dict[str, object] = {
        "since_hours": _get(summary, "since_hours"),
        "rates": rates,
        "thresholds": thresholds,
        "breaches": breaches,
        "allow_slo_breach": allow_slo_breach,
        "counts": {
            "creation_total": creation_total,
            "creation_succeeded": _int(_get(summary, "creation_succeeded")),
            "cleanup_total": cleanup_total,
            "cleanup_succeeded": _int(_get(summary, "cleanup_succeeded")),
            "stuck_running_count": _int(_get(summary, "stuck_running_count")),
            "actionable_failure_count": _int(_get(summary, "actionable_failure_count")),
            "unactionable_failure_count": _int(_get(summary, "unactionable_failure_count")),
        },
    }
    if breaches and not allow_slo_breach:
        return CoreReadinessCheck(
            name="prd_slo_thresholds",
            status="fail",
            reason_code="PRD_SLO_THRESHOLDS_FAILED",
            message="rolling PRD SLO thresholds are below Core release criteria",
            evidence=evidence,
        )
    if breaches:
        return CoreReadinessCheck(
            name="prd_slo_thresholds",
            status="warn",
            reason_code="PRD_SLO_THRESHOLDS_ALLOWLISTED",
            message="rolling PRD SLO threshold breaches were explicitly allowlisted",
            evidence=evidence,
        )
    return CoreReadinessCheck(
        name="prd_slo_thresholds",
        status="ok",
        reason_code="PRD_SLO_THRESHOLDS_MET",
        message="rolling PRD SLO thresholds meet Core release criteria",
        evidence=evidence,
    )


def _failure_taxonomy_check(
    summary: FailureAnalysisSummary | Mapping[str, object],
    *,
    allow_generic_failures: bool,
) -> CoreReadinessCheck:
    findings = _generic_failure_findings(summary)
    evidence = {
        "since_hours": _get(summary, "since_hours"),
        "total_failed_workspaces": _get(summary, "total_failed_workspaces"),
        "generic_failures": findings,
        "allow_generic_failures": allow_generic_failures,
    }
    if findings and not allow_generic_failures:
        return CoreReadinessCheck(
            name="recent_failure_taxonomy",
            status="fail",
            reason_code="GENERIC_FAILURE_REASON_BLOCKS_RELEASE",
            message="recent failed workspaces include generic or unknown failure reasons",
            evidence=evidence,
        )
    return CoreReadinessCheck(
        name="recent_failure_taxonomy",
        status="ok",
        reason_code="FAILURE_TAXONOMY_CLASSIFIED",
        message="recent failed workspaces have classified failure reasons",
        evidence=evidence,
    )


def _generic_failure_findings(
    summary: FailureAnalysisSummary | Mapping[str, object],
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for group in _items(_get(summary, "failure_groups")):
        reason = _string(_get(group, "failure_reason")).lower()
        if reason in GENERIC_FAILURE_REASONS:
            findings.append(
                {
                    "source": "failure_group",
                    "failure_reason": reason or UNKNOWN_FAILURE_REASON,
                    "count": _get(group, "count") or 0,
                }
            )
    for example in _items(_get(summary, "latest_examples")):
        reason = _string(_get(example, "failure_reason")).lower()
        reason_code = _string(_get(example, "reason_code"))
        if reason in GENERIC_FAILURE_REASONS or reason_code in GENERIC_REASON_CODES:
            findings.append(
                {
                    "source": "latest_example",
                    "workspace_id": _get(example, "workspace_id"),
                    "failure_reason": reason or UNKNOWN_FAILURE_REASON,
                    "reason_code": reason_code or UNKNOWN_FAILURE_REASON,
                }
            )
    for cluster in _items(_get(summary, "root_cause_clusters")):
        reason = _string(_get(cluster, "failure_reason")).lower()
        reason_code = _string(_get(cluster, "reason_code"))
        if reason in GENERIC_FAILURE_REASONS or reason_code in GENERIC_REASON_CODES:
            findings.append(
                {
                    "source": "root_cause_cluster",
                    "sample_workspace_ids": list(_items(_get(cluster, "sample_workspace_ids"))),
                    "failure_reason": reason or UNKNOWN_FAILURE_REASON,
                    "reason_code": reason_code or UNKNOWN_FAILURE_REASON,
                    "count": _get(cluster, "count") or 0,
                }
            )
    return findings


def _next_actions(checks: Iterable[CoreReadinessCheck]) -> list[str]:
    actions: list[str] = []
    for check in checks:
        if check.status != "fail":
            continue
        if check.reason_code == "GENERIC_FAILURE_REASON_BLOCKS_RELEASE":
            actions.append("Classify or reconcile recent generic workspace failures.")
        elif check.reason_code in {
            "PRD_SLO_THRESHOLDS_FAILED",
            "PRD_SLO_METRICS_UNAVAILABLE",
        }:
            actions.append("Restore PRD SLO evidence or record an explicit release allowlist.")
        elif check.reason_code == "DEMO_PROJECT_MISSING":
            actions.append("Add the in-repo AWF Core demo project under examples/.")
        else:
            actions.append(f"Resolve release gate check: {check.name}.")
    return actions


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _items(value: object) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _get(obj: object, key: str) -> object:
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _jsonable(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value
