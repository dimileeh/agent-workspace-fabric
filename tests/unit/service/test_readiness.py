"""AWF Core release-readiness scorecard tests."""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import awf.service.readiness as readiness
from awf.service.doctor.models import DoctorReport
from awf.service.readiness import CoreReadinessCheck, collect_core_readiness_report


def _write_demo_project(path: Path) -> None:
    path.mkdir()
    (path / "pyproject.toml").write_text(
        '[project]\nname = "awf-core-demo"\ndependencies = ["psycopg"]\n',
        encoding="utf-8",
    )
    (path / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")


async def _ok_slo_collector(*, since_hours: int) -> dict[str, object]:
    return {
        "since_hours": since_hours,
        "creation_total": 100,
        "creation_succeeded": 100,
        "cleanup_total": 100,
        "cleanup_succeeded": 100,
        "stuck_running_count": 0,
        "actionable_failure_count": 0,
        "unactionable_failure_count": 0,
    }


@dataclass(frozen=True)
class _EvidenceValue:
    path: Path
    when: datetime


async def _classified_failure_collector(*, since_hours: int) -> dict[str, object]:
    return {
        "since_hours": since_hours,
        "total_failed_workspaces": 0,
        "failure_groups": [],
        "latest_examples": [],
        "root_cause_clusters": [],
    }


@pytest.mark.unit
def test_release_docs_reference_executable_release_gate() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    readme = (repo_root / "docs/CONCEPTS.md").read_text(encoding="utf-8")
    releasing = (repo_root / "RELEASING.md").read_text(encoding="utf-8")

    assert "awf service readiness --format json" in releasing
    assert "Dependency License Audit" in releasing
    assert "awf service readiness --format json" in readme


@pytest.mark.unit
def test_readiness_evidence_lines_cover_slo_and_status_edge_shapes() -> None:
    slo_lines = readiness._readiness_evidence_lines(  # noqa: SLF001
        CoreReadinessCheck(
            name="prd_slo_thresholds",
            status="fail",
            reason_code="PRD_SLO_BREACH",
            message="SLOs breached.",
            evidence={
                "since_hours": 24,
                "breaches": {
                    "creation_success": "missing data",
                    "cleanup_success": {
                        "actual": None,
                        "threshold": {"operator": ">=", "value": 0.99},
                    },
                    "failure_rate": {
                        "actual": "n/a",
                        "threshold": {"operator": "<", "value": "1%"},
                    },
                },
            },
        )
    )
    assert slo_lines == [
        "        window: 24h",
        "        breaches:",
        "          - cleanup_success: no data >= 99.0%",
        "          - creation_success: missing data",
        "          - failure_rate: n/a < 1%",
    ]

    no_breach_lines = readiness._readiness_evidence_lines(  # noqa: SLF001
        CoreReadinessCheck(
            name="prd_slo_thresholds",
            status="warn",
            reason_code="PRD_SLO_METRICS_UNAVAILABLE",
            message="SLO metrics unavailable.",
            evidence={},
        )
    )
    assert no_breach_lines == ["        breaches: none reported"]

    assert readiness._readiness_evidence_lines(  # noqa: SLF001
        CoreReadinessCheck(
            name="service_status",
            status="ok",
            reason_code="SERVICE_READY",
            message="Service ready.",
            evidence={"status": "ok"},
        )
    ) == ["        evidence status: ok"]


@pytest.mark.unit
async def test_core_readiness_reports_collector_failures_and_missing_demo(
    tmp_path: Path,
) -> None:
    async def _status_collector(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("status unavailable")

    async def _doctor_collector(*_args: object, **kwargs: object) -> DoctorReport:
        assert kwargs["status_collector"] is None
        raise RuntimeError("doctor unavailable")

    async def _slo_collector(*, since_hours: int) -> dict[str, object]:
        raise RuntimeError(f"slo unavailable for {since_hours}h")

    report = await collect_core_readiness_report(
        settings=SimpleNamespace(
            database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
        ),  # type: ignore[arg-type]
        demo_path=tmp_path / "missing-demo",
        status_collector=_status_collector,
        doctor_collector=_doctor_collector,  # type: ignore[arg-type]
        failure_analysis_collector=_classified_failure_collector,  # type: ignore[arg-type]
        slo_metrics_collector=_slo_collector,  # type: ignore[arg-type]
    )

    reasons = {check.name: check.reason_code for check in report.checks}
    assert report.status == "fail"
    assert reasons["service_status"] == "SERVICE_STATUS_COLLECTION_FAILED"
    assert reasons["doctor"] == "DOCTOR_COLLECTION_FAILED"
    assert reasons["demo_project"] == "DEMO_PROJECT_MISSING"
    assert reasons["prd_slo_thresholds"] == "PRD_SLO_METRICS_UNAVAILABLE"
    assert "Add the in-repo AWF Core demo project under examples/." in report.next_actions
    assert (
        "Restore PRD SLO evidence or record an explicit release allowlist." in report.next_actions
    )
    assert "Resolve release gate check: service_status." in report.next_actions


@pytest.mark.unit
def test_core_demo_check_reports_preview_exceptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    demo_path = tmp_path / "demo"
    demo_path.mkdir()

    def _raise_preview(_path: Path, *, include_smoke_request: bool) -> object:
        assert include_smoke_request is True
        raise RuntimeError("preview failed")

    monkeypatch.setattr(readiness, "preview_project_onboarding", _raise_preview)

    check = readiness._demo_check(demo_path)

    assert check.status == "fail"
    assert check.reason_code == "DEMO_PROJECT_PREVIEW_FAILED"
    assert check.evidence["error"] == "preview failed"


@pytest.mark.unit
async def test_core_readiness_fails_on_generic_recent_failure_reason(tmp_path: Path) -> None:
    demo_path = tmp_path / "demo"
    _write_demo_project(demo_path)

    async def _status_collector(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "status": "ok",
            "checks": {
                "workspace_cleanup": {"ok": True},
                "orphan_resources": {"ok": True},
                "stranded_workspaces": {"ok": True},
            },
            "agent_readiness": {"status": "ok"},
        }

    async def _doctor_collector(*_args: object, **_kwargs: object) -> DoctorReport:
        return DoctorReport(service="awf-local", status="ok", diagnostics=())

    async def _failure_collector(*, since_hours: int) -> dict[str, object]:
        return {
            "since_hours": since_hours,
            "total_failed_workspaces": 1,
            "failure_groups": [
                {"failure_reason": "agent_failure", "count": 1},
            ],
            "latest_examples": [
                {
                    "workspace_id": "ws_generic",
                    "failure_reason": "agent_failure",
                    "reason_code": None,
                },
            ],
            "root_cause_clusters": [],
        }

    report = await collect_core_readiness_report(
        settings=SimpleNamespace(
            database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
        ),  # type: ignore[arg-type]
        demo_path=demo_path,
        status_collector=_status_collector,
        doctor_collector=_doctor_collector,
        failure_analysis_collector=_failure_collector,  # type: ignore[arg-type]
        slo_metrics_collector=_ok_slo_collector,
    )

    assert report.status == "fail"
    taxonomy_check = next(
        check for check in report.checks if check.name == "recent_failure_taxonomy"
    )
    assert taxonomy_check.reason_code == "GENERIC_FAILURE_REASON_BLOCKS_RELEASE"
    assert "Classify or reconcile" in report.next_actions[0]


@pytest.mark.unit
async def test_core_readiness_allowlist_can_downgrade_generic_failure_gate(
    tmp_path: Path,
) -> None:
    demo_path = tmp_path / "demo"
    _write_demo_project(demo_path)

    async def _status_collector(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "status": "ok",
            "checks": {
                "workspace_cleanup": {"ok": True},
                "orphan_resources": {"ok": True},
                "stranded_workspaces": {"ok": True},
            },
            "agent_readiness": {"status": "ok"},
        }

    async def _doctor_collector(*_args: object, **_kwargs: object) -> DoctorReport:
        return DoctorReport(service="awf-local", status="ok", diagnostics=())

    async def _failure_collector(*, since_hours: int) -> dict[str, object]:
        return {
            "since_hours": since_hours,
            "total_failed_workspaces": 1,
            "failure_groups": [{"failure_reason": "unknown", "count": 1}],
            "latest_examples": [],
            "root_cause_clusters": [],
        }

    report = await collect_core_readiness_report(
        settings=SimpleNamespace(
            database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
        ),  # type: ignore[arg-type]
        demo_path=demo_path,
        status_collector=_status_collector,
        doctor_collector=_doctor_collector,
        failure_analysis_collector=_failure_collector,  # type: ignore[arg-type]
        slo_metrics_collector=_ok_slo_collector,
        allow_generic_failures=True,
    )

    assert report.status == "ok"
    taxonomy_check = next(
        check for check in report.checks if check.name == "recent_failure_taxonomy"
    )
    assert taxonomy_check.reason_code == "FAILURE_TAXONOMY_CLASSIFIED"


@pytest.mark.unit
async def test_core_readiness_fails_when_prd_slo_thresholds_are_breached(
    tmp_path: Path,
) -> None:
    demo_path = tmp_path / "demo"
    _write_demo_project(demo_path)

    async def _status_collector(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "status": "ok",
            "checks": {
                "workspace_cleanup": {"ok": True},
                "orphan_resources": {"ok": True},
                "stranded_workspaces": {"ok": True},
            },
            "agent_readiness": {"status": "ok"},
        }

    async def _doctor_collector(*_args: object, **_kwargs: object) -> DoctorReport:
        return DoctorReport(service="awf-local", status="ok", diagnostics=())

    async def _failure_collector(*, since_hours: int) -> dict[str, object]:
        return {
            "since_hours": since_hours,
            "total_failed_workspaces": 0,
            "failure_groups": [],
            "latest_examples": [],
            "root_cause_clusters": [],
        }

    async def _slo_collector(*, since_hours: int) -> dict[str, object]:
        return {
            "since_hours": since_hours,
            "creation_total": 100,
            "creation_succeeded": 97,
            "creation_failed": 3,
            "creation_cancelled": 0,
            "cleanup_total": 100,
            "cleanup_succeeded": 98,
            "cleanup_failure_count": 2,
            "stuck_running_count": 1,
            "stuck_with_reason_count": 1,
            "actionable_failure_count": 18,
            "unactionable_failure_count": 2,
        }

    report = await collect_core_readiness_report(
        settings=SimpleNamespace(
            database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
        ),  # type: ignore[arg-type]
        demo_path=demo_path,
        status_collector=_status_collector,
        doctor_collector=_doctor_collector,
        failure_analysis_collector=_failure_collector,  # type: ignore[arg-type]
        slo_metrics_collector=_slo_collector,  # type: ignore[arg-type]
    )

    assert report.status == "fail"
    slo_check = next(check for check in report.checks if check.name == "prd_slo_thresholds")
    assert slo_check.reason_code == "PRD_SLO_THRESHOLDS_FAILED"
    assert {
        "workspace_creation_success_rate",
        "cleanup_success_rate",
        "stuck_state_rate",
        "actionable_failure_reason_rate",
    } <= set(slo_check.evidence["breaches"])  # type: ignore[arg-type]


@pytest.mark.unit
def test_slo_threshold_check_requires_creation_and_cleanup_evidence() -> None:
    check = readiness._slo_threshold_check(
        {
            "since_hours": 168,
            "creation_total": 0,
            "creation_succeeded": 0,
            "cleanup_total": 0,
            "cleanup_succeeded": 0,
            "stuck_running_count": 0,
            "actionable_failure_count": 0,
            "unactionable_failure_count": 0,
        },
        allow_slo_breach=False,
    )

    assert check.status == "fail"
    assert check.reason_code == "PRD_SLO_THRESHOLDS_FAILED"
    breaches = check.evidence["breaches"]
    assert isinstance(breaches, dict)
    assert breaches["workspace_creation_success_rate"]["reason"] == (
        "SLO_METRICS_INSUFFICIENT_EVIDENCE"
    )
    assert breaches["cleanup_success_rate"]["reason"] == "SLO_METRICS_INSUFFICIENT_EVIDENCE"


@pytest.mark.unit
def test_readiness_evidence_lines_render_slo_breaches_and_non_mapping_values() -> None:
    check = CoreReadinessCheck(
        name="prd_slo_thresholds",
        status="fail",
        reason_code="PRD_SLO_THRESHOLDS_FAILED",
        message="SLOs failed",
        evidence={
            "since_hours": 168,
            "breaches": {
                "cleanup_success_rate": {
                    "actual": 0.95,
                    "threshold": {"operator": ">=", "value": 0.99},
                },
                "raw_breach": "unstructured",
            },
        },
    )

    assert readiness._readiness_evidence_lines(check) == [
        "        window: 168h",
        "        breaches:",
        "          - cleanup_success_rate: 95.0% >= 99.0%",
        "          - raw_breach: unstructured",
    ]


@pytest.mark.unit
def test_readiness_evidence_lines_report_missing_breaches_for_non_ok_slo() -> None:
    check = CoreReadinessCheck(
        name="prd_slo_thresholds",
        status="warn",
        reason_code="PRD_SLO_THRESHOLDS_ALLOWLISTED",
        message="SLOs allowlisted",
        evidence={},
    )

    assert readiness._readiness_evidence_lines(check) == ["        breaches: none reported"]


@pytest.mark.unit
def test_readiness_evidence_lines_omit_empty_ok_slo_details() -> None:
    check = CoreReadinessCheck(
        name="prd_slo_thresholds",
        status="ok",
        reason_code="PRD_SLO_THRESHOLDS_OK",
        message="SLOs ok",
        evidence={},
    )

    assert readiness._readiness_evidence_lines(check) == []


@pytest.mark.unit
def test_readiness_evidence_lines_render_status_for_named_checks() -> None:
    check = CoreReadinessCheck(
        name="provider_readiness",
        status="warn",
        reason_code="PROVIDER_WARN",
        message="provider warning",
        evidence={"status": "warn"},
    )

    assert readiness._readiness_evidence_lines(check) == ["        evidence status: warn"]


@pytest.mark.unit
def test_readiness_evidence_lines_omit_status_when_named_check_has_no_status() -> None:
    check = CoreReadinessCheck(
        name="cleanup_posture",
        status="ok",
        reason_code="CLEANUP_OK",
        message="cleanup ok",
        evidence={},
    )

    assert readiness._readiness_evidence_lines(check) == []


@pytest.mark.unit
def test_readiness_evidence_lines_omit_unknown_check_details() -> None:
    check = CoreReadinessCheck(
        name="custom_check",
        status="ok",
        reason_code="CUSTOM_OK",
        message="custom ok",
        evidence={"status": "ok"},
    )

    assert readiness._readiness_evidence_lines(check) == []


@pytest.mark.unit
def test_format_readiness_rate_handles_missing_and_non_numeric_values() -> None:
    assert readiness._format_readiness_rate(None) == "no data"
    assert readiness._format_readiness_rate("raw") == "raw"


@pytest.mark.unit
async def test_core_readiness_allowlist_downgrades_prd_slo_breach(
    tmp_path: Path,
) -> None:
    demo_path = tmp_path / "demo"
    _write_demo_project(demo_path)

    async def _status_collector(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "status": "ok",
            "checks": {
                "workspace_cleanup": {"ok": True},
                "orphan_resources": {"ok": True},
                "stranded_workspaces": {"ok": True},
            },
            "agent_readiness": {"status": "ok"},
        }

    async def _doctor_collector(*_args: object, **_kwargs: object) -> DoctorReport:
        return DoctorReport(service="awf-local", status="ok", diagnostics=())

    async def _failure_collector(*, since_hours: int) -> dict[str, object]:
        return {
            "since_hours": since_hours,
            "total_failed_workspaces": 0,
            "failure_groups": [],
            "latest_examples": [],
            "root_cause_clusters": [],
        }

    async def _slo_collector(*, since_hours: int) -> dict[str, object]:
        return {
            "since_hours": since_hours,
            "creation_total": 10,
            "creation_succeeded": 8,
            "cleanup_total": 10,
            "cleanup_succeeded": 8,
            "stuck_running_count": 1,
            "actionable_failure_count": 1,
            "unactionable_failure_count": 1,
        }

    report = await collect_core_readiness_report(
        settings=SimpleNamespace(
            database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
        ),  # type: ignore[arg-type]
        demo_path=demo_path,
        status_collector=_status_collector,
        doctor_collector=_doctor_collector,
        failure_analysis_collector=_failure_collector,  # type: ignore[arg-type]
        slo_metrics_collector=_slo_collector,  # type: ignore[arg-type]
        allow_slo_breach=True,
    )

    assert report.status == "warn"
    slo_check = next(check for check in report.checks if check.name == "prd_slo_thresholds")
    assert slo_check.status == "warn"
    assert slo_check.reason_code == "PRD_SLO_THRESHOLDS_ALLOWLISTED"


@pytest.mark.unit
async def test_core_readiness_reuses_collected_service_status_for_doctor(
    tmp_path: Path,
) -> None:
    demo_path = tmp_path / "demo"
    _write_demo_project(demo_path)
    calls = {"status": 0}

    async def _status_collector(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls["status"] += 1
        return {
            "status": "ok",
            "checks": {
                "workspace_cleanup": {"ok": True},
                "orphan_resources": {"ok": True},
                "stranded_workspaces": {"ok": True},
            },
            "agent_readiness": {"status": "ok"},
        }

    async def _doctor_collector(*_args: object, **kwargs: object) -> DoctorReport:
        cached = kwargs["status_collector"]
        assert callable(cached)
        cached_payload = await cached(SimpleNamespace())  # type: ignore[misc]
        assert cached_payload["status"] == "ok"
        return DoctorReport(service="awf-local", status="ok", diagnostics=())

    async def _failure_collector(*, since_hours: int) -> dict[str, object]:
        return {
            "since_hours": since_hours,
            "total_failed_workspaces": 0,
            "failure_groups": [],
            "latest_examples": [],
            "root_cause_clusters": [],
        }

    async def _slo_collector(*, since_hours: int) -> dict[str, object]:
        return {
            "since_hours": since_hours,
            "creation_total": 100,
            "creation_succeeded": 100,
            "cleanup_total": 100,
            "cleanup_succeeded": 100,
            "stuck_running_count": 0,
            "actionable_failure_count": 0,
            "unactionable_failure_count": 0,
        }

    report = await collect_core_readiness_report(
        settings=SimpleNamespace(
            database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
        ),  # type: ignore[arg-type]
        demo_path=demo_path,
        status_collector=_status_collector,
        doctor_collector=_doctor_collector,  # type: ignore[arg-type]
        failure_analysis_collector=_failure_collector,  # type: ignore[arg-type]
        slo_metrics_collector=_slo_collector,  # type: ignore[arg-type]
    )

    assert report.status == "ok"
    assert calls["status"] == 1


@pytest.mark.unit
async def test_core_readiness_resolves_provider_environment_from_compose_env_file(
    tmp_path: Path,
) -> None:
    demo_path = tmp_path / "demo"
    _write_demo_project(demo_path)
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    compose_env_file = tmp_path / "compose.env"
    compose_token = "ghp_core_readiness_compose_token"
    compose_env_file.write_text(f"AWF_GITHUB_TOKEN={compose_token}\n", encoding="utf-8")
    base_environ = {"PATH": "/usr/bin"}
    captured_status: dict[str, object] = {}
    captured_doctor: dict[str, object] = {}

    async def _status_collector(*_args: object, **kwargs: object) -> dict[str, object]:
        captured_status.update(kwargs)
        return {
            "status": "ok",
            "checks": {
                "workspace_cleanup": {"ok": True},
                "orphan_resources": {"ok": True},
                "stranded_workspaces": {"ok": True},
            },
            "agent_readiness": {"status": "ok"},
        }

    async def _doctor_collector(*_args: object, **kwargs: object) -> DoctorReport:
        captured_doctor.update(kwargs)
        return DoctorReport(service="awf-local", status="ok", diagnostics=())

    report = await collect_core_readiness_report(
        settings=SimpleNamespace(
            database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
        ),  # type: ignore[arg-type]
        demo_path=demo_path,
        strict_providers=frozenset({"github"}),
        environ=base_environ,
        compose_file=compose_file,
        compose_env_file=compose_env_file,
        status_collector=_status_collector,
        doctor_collector=_doctor_collector,
        failure_analysis_collector=_classified_failure_collector,  # type: ignore[arg-type]
        slo_metrics_collector=_ok_slo_collector,
    )

    assert report.status == "ok"
    status_provider_env = captured_status["provider_environ"]
    doctor_provider_env = captured_doctor["provider_environ"]
    assert isinstance(status_provider_env, dict)
    assert isinstance(doctor_provider_env, dict)
    assert status_provider_env["AWF_GITHUB_TOKEN"] == compose_token
    assert doctor_provider_env["AWF_GITHUB_TOKEN"] == compose_token
    assert status_provider_env["PATH"] == "/usr/bin"
    assert doctor_provider_env["PATH"] == "/usr/bin"
    assert captured_status["environ"] is base_environ
    assert captured_status["compose_file"] == compose_file
    assert captured_status["compose_env_file"] == compose_env_file
    assert captured_doctor["environ"] is base_environ


@pytest.mark.unit
async def test_core_readiness_honors_explicit_null_compose_env_file(
    tmp_path: Path,
) -> None:
    demo_path = tmp_path / "demo"
    _write_demo_project(demo_path)
    compose_file = tmp_path / "docker" / "compose" / "local-service.yml"
    compose_file.parent.mkdir(parents=True)
    compose_file.write_text("services: {}\n", encoding="utf-8")
    (compose_file.parent / ".env").write_text(
        "AWF_GITHUB_TOKEN=ghp_untrusted_readiness_token\n",
        encoding="utf-8",
    )
    base_environ = {"PATH": "/usr/bin"}
    captured_status: dict[str, object] = {}
    captured_doctor: dict[str, object] = {}

    async def _status_collector(*_args: object, **kwargs: object) -> dict[str, object]:
        captured_status.update(kwargs)
        return {
            "status": "ok",
            "checks": {
                "workspace_cleanup": {"ok": True},
                "orphan_resources": {"ok": True},
                "stranded_workspaces": {"ok": True},
            },
            "agent_readiness": {"status": "ok"},
        }

    async def _doctor_collector(*_args: object, **kwargs: object) -> DoctorReport:
        captured_doctor.update(kwargs)
        return DoctorReport(service="awf-local", status="ok", diagnostics=())

    report = await collect_core_readiness_report(
        settings=SimpleNamespace(
            database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
        ),  # type: ignore[arg-type]
        demo_path=demo_path,
        environ=base_environ,
        compose_file=compose_file,
        compose_env_file=None,
        status_collector=_status_collector,
        doctor_collector=_doctor_collector,
        failure_analysis_collector=_classified_failure_collector,  # type: ignore[arg-type]
        slo_metrics_collector=_ok_slo_collector,
    )

    status_provider_env = captured_status["provider_environ"]
    doctor_provider_env = captured_doctor["provider_environ"]
    assert report.status == "ok"
    assert isinstance(status_provider_env, dict)
    assert isinstance(doctor_provider_env, dict)
    assert "AWF_GITHUB_TOKEN" not in status_provider_env
    assert "AWF_GITHUB_TOKEN" not in doctor_provider_env
    assert captured_status["compose_file"] == compose_file
    assert captured_status["compose_env_file"] is None
    assert captured_doctor["compose_env_file"] is None


@pytest.mark.unit
async def test_core_readiness_forwards_compose_file_to_status_collector_when_env_file_omitted(
    tmp_path: Path,
) -> None:
    demo_path = tmp_path / "demo"
    _write_demo_project(demo_path)
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    captured_status: dict[str, object] = {}

    async def _status_collector(*_args: object, **kwargs: object) -> dict[str, object]:
        captured_status.update(kwargs)
        return {
            "status": "ok",
            "checks": {
                "workspace_cleanup": {"ok": True},
                "orphan_resources": {"ok": True},
                "stranded_workspaces": {"ok": True},
            },
            "agent_readiness": {"status": "ok"},
        }

    async def _doctor_collector(*_args: object, **_kwargs: object) -> DoctorReport:
        return DoctorReport(service="awf-local", status="ok", diagnostics=())

    report = await collect_core_readiness_report(
        settings=SimpleNamespace(
            database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
        ),  # type: ignore[arg-type]
        demo_path=demo_path,
        compose_file=compose_file,
        status_collector=_status_collector,
        doctor_collector=_doctor_collector,
        failure_analysis_collector=_classified_failure_collector,  # type: ignore[arg-type]
        slo_metrics_collector=_ok_slo_collector,
    )

    assert report.status == "ok"
    assert captured_status["compose_file"] == compose_file
    assert "compose_env_file" not in captured_status


@pytest.mark.unit
async def test_readiness_collectors_use_database_fallbacks_when_not_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disposed: list[bool] = []

    class _Engine:
        async def dispose(self) -> None:
            disposed.append(True)

    def _make_engine(database_url: str) -> _Engine:
        assert database_url == "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
        return _Engine()

    def _make_session_factory(engine: _Engine) -> str:
        assert isinstance(engine, _Engine)
        return "session-factory"

    async def _summarize_failure_analysis(
        session_factory: str,
        *,
        since_hours: int,
        failure_example_limit: int,
    ) -> dict[str, object]:
        assert session_factory == "session-factory"
        assert since_hours == 24
        assert failure_example_limit == readiness.DEFAULT_FAILURE_EXAMPLE_LIMIT
        return {"kind": "failures"}

    async def _summarize_slo_metrics(
        session_factory: str,
        *,
        settings: object,
        since_hours: int,
    ) -> dict[str, object]:
        assert session_factory == "session-factory"
        assert settings.database_url == "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
        assert since_hours == 168
        return {"kind": "slo"}

    monkeypatch.setattr(readiness, "make_engine", _make_engine)
    monkeypatch.setattr(readiness, "make_session_factory", _make_session_factory)
    monkeypatch.setattr(readiness, "summarize_failure_analysis", _summarize_failure_analysis)
    monkeypatch.setattr(readiness, "summarize_slo_metrics", _summarize_slo_metrics)

    settings = SimpleNamespace(database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf")
    failure_summary = await readiness._collect_failure_summary(
        settings,  # type: ignore[arg-type]
        failure_window_hours=24,
        collector=None,
    )
    slo_summary = await readiness._collect_slo_summary(
        settings,  # type: ignore[arg-type]
        slo_window_hours=168,
        collector=None,
    )

    assert failure_summary == {"kind": "failures"}
    assert slo_summary == {"kind": "slo"}
    assert disposed == [True, True]


@pytest.mark.unit
def test_generic_failure_findings_accept_mapping_and_object_shapes() -> None:
    summary = SimpleNamespace(
        failure_groups=(
            {"failure_reason": "specific", "count": 2},
            SimpleNamespace(failure_reason="agent_failure", count=3),
        ),
        latest_examples=[
            SimpleNamespace(
                workspace_id="ws_specific",
                failure_reason="specific",
                reason_code="SPECIFIC_FAILURE",
            ),
            SimpleNamespace(
                workspace_id="ws_latest",
                failure_reason="specific",
                reason_code="AGENT_FAILURE",
            ),
        ],
        root_cause_clusters=(
            SimpleNamespace(
                sample_workspace_ids=("ws_specific",),
                failure_reason="specific",
                reason_code="SPECIFIC_FAILURE",
                count=1,
            ),
            SimpleNamespace(
                sample_workspace_ids=("ws_a", "ws_b"),
                failure_reason="specific",
                reason_code=readiness.UNKNOWN_FAILURE_REASON,
                count=4,
            ),
        ),
    )

    findings = readiness._generic_failure_findings(summary)

    assert findings == [
        {
            "source": "failure_group",
            "failure_reason": "agent_failure",
            "count": 3,
        },
        {
            "source": "latest_example",
            "workspace_id": "ws_latest",
            "failure_reason": "specific",
            "reason_code": "AGENT_FAILURE",
        },
        {
            "source": "root_cause_cluster",
            "sample_workspace_ids": ["ws_a", "ws_b"],
            "failure_reason": "specific",
            "reason_code": readiness.UNKNOWN_FAILURE_REASON,
            "count": 4,
        },
    ]
    assert readiness._items(None) == []
    assert readiness._items("bad-shape") == []
    assert readiness._int("not-an-int") == 0
    assert readiness._rate(1, 0) is None


@pytest.mark.unit
def test_core_readiness_jsonable_handles_nested_dataclass_values(tmp_path: Path) -> None:
    @dataclasses.dataclass(frozen=True)
    class _Evidence:
        path: Path
        checked_on: date
        checked_at: datetime
        tags: frozenset[str]

    check = readiness.CoreReadinessCheck(
        name="jsonable",
        status="ok",
        reason_code="JSONABLE_OK",
        message="json conversion works",
        evidence={
            "nested": _Evidence(
                path=tmp_path / "demo",
                checked_on=date(2026, 5, 3),
                checked_at=datetime(2026, 5, 3, 12, 30),
                tags=frozenset({"b", "a"}),
            )
        },
    )

    payload = check.to_dict()

    evidence = payload["evidence"]
    assert isinstance(evidence, dict)
    nested = evidence["nested"]
    assert isinstance(nested, dict)
    assert nested["path"] == str(tmp_path / "demo")
    assert nested["checked_on"] == "2026-05-03"
    assert nested["checked_at"] == "2026-05-03T12:30:00"
    assert set(nested["tags"]) == {"a", "b"}


@pytest.mark.unit
def test_core_readiness_pretty_omits_ok_reason_and_keeps_failure_reason() -> None:
    report = readiness.CoreReadinessReport(
        status="fail",
        checks=(
            readiness.CoreReadinessCheck(
                name="service_status",
                status="ok",
                reason_code="SERVICE_STATUS_OK",
                message="service dependencies are ready",
                evidence={"status": "ok"},
            ),
            readiness.CoreReadinessCheck(
                name="prd_slo_thresholds",
                status="fail",
                reason_code="PRD_SLO_THRESHOLDS_FAILED",
                message="rolling PRD SLO thresholds are below Core release criteria",
                evidence={
                    "since_hours": 168,
                    "breaches": {
                        "workspace_creation_success_rate": {
                            "actual": 0.9,
                            "threshold": {"operator": ">=", "value": 0.98},
                        }
                    },
                },
            ),
        ),
    )

    pretty = readiness.render_core_readiness_pretty(report)

    assert "[ok] service_status: service dependencies are ready" in pretty
    assert "reason: SERVICE_STATUS_OK" not in pretty
    assert "evidence status: ok" in pretty
    assert "[fail] prd_slo_thresholds" in pretty
    assert "reason: PRD_SLO_THRESHOLDS_FAILED" in pretty


@pytest.mark.unit
def test_core_demo_has_executable_offline_golden_path_smoke() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "examples" / "awf-core-demo" / "scripts" / "core_release_smoke.py"

    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert '"status": "ok"' in result.stdout
    assert '"profile_preview"' in result.stdout
    assert '"workspace_request"' in result.stdout
    assert '"pr_monitor"' in result.stdout
    assert '"cleanup"' in result.stdout


@pytest.mark.unit
async def test_core_readiness_reports_collector_failures_as_release_gates(
    tmp_path: Path,
) -> None:
    demo_path = tmp_path / "demo"
    _write_demo_project(demo_path)

    async def _status_collector(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("status exploded")

    async def _doctor_collector(*_args: object, **_kwargs: object) -> DoctorReport:
        raise RuntimeError("doctor exploded")

    async def _slo_collector(*, since_hours: int) -> dict[str, object]:
        raise RuntimeError(f"slo exploded after {since_hours}h")

    async def _failure_collector(*, since_hours: int) -> dict[str, object]:
        return {
            "since_hours": since_hours,
            "total_failed_workspaces": 0,
            "failure_groups": [],
            "latest_examples": [],
            "root_cause_clusters": [],
        }

    report = await collect_core_readiness_report(
        settings=SimpleNamespace(
            database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
        ),  # type: ignore[arg-type]
        demo_path=demo_path,
        status_collector=_status_collector,
        doctor_collector=_doctor_collector,  # type: ignore[arg-type]
        failure_analysis_collector=_failure_collector,  # type: ignore[arg-type]
        slo_metrics_collector=_slo_collector,  # type: ignore[arg-type]
    )

    checks = {check.name: check for check in report.checks}
    assert report.status == "fail"
    assert checks["service_status"].reason_code == "SERVICE_STATUS_COLLECTION_FAILED"
    assert checks["service_status"].evidence == {"error": "status exploded"}
    assert checks["doctor"].reason_code == "DOCTOR_COLLECTION_FAILED"
    assert checks["doctor"].evidence == {"error": "doctor exploded"}
    assert checks["prd_slo_thresholds"].reason_code == "PRD_SLO_METRICS_UNAVAILABLE"
    assert "Restore PRD SLO evidence" in report.next_actions[-1]


@pytest.mark.unit
async def test_core_readiness_allowlists_unavailable_slo_metrics(
    tmp_path: Path,
) -> None:
    demo_path = tmp_path / "demo"
    _write_demo_project(demo_path)

    async def _status_collector(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "status": "ok",
            "checks": {
                "workspace_cleanup": {"ok": True},
                "orphan_resources": {"ok": True},
                "stranded_workspaces": {"ok": True},
            },
            "agent_readiness": {"status": "ok"},
        }

    async def _doctor_collector(*_args: object, **_kwargs: object) -> DoctorReport:
        return DoctorReport(service="awf-local", status="ok", diagnostics=())

    async def _slo_collector(*, since_hours: int) -> dict[str, object]:
        raise RuntimeError(f"slo unavailable after {since_hours}h")

    async def _failure_collector(*, since_hours: int) -> dict[str, object]:
        return {
            "since_hours": since_hours,
            "total_failed_workspaces": 0,
            "failure_groups": [],
            "latest_examples": [],
            "root_cause_clusters": [],
        }

    report = await collect_core_readiness_report(
        settings=SimpleNamespace(
            database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
        ),  # type: ignore[arg-type]
        demo_path=demo_path,
        status_collector=_status_collector,
        doctor_collector=_doctor_collector,
        failure_analysis_collector=_failure_collector,  # type: ignore[arg-type]
        slo_metrics_collector=_slo_collector,  # type: ignore[arg-type]
        allow_slo_breach=True,
    )

    slo_check = next(check for check in report.checks if check.name == "prd_slo_thresholds")
    assert report.status == "warn"
    assert slo_check.status == "warn"
    assert slo_check.reason_code == "PRD_SLO_METRICS_UNAVAILABLE_ALLOWLISTED"
    assert slo_check.evidence["allow_slo_breach"] is True


@pytest.mark.unit
async def test_readiness_default_collectors_dispose_engines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disposed: list[str] = []
    calls: list[tuple[str, object, int]] = []

    class _FakeEngine:
        async def dispose(self) -> None:
            disposed.append("engine")

    engine = _FakeEngine()
    session_factory = object()

    def _make_engine(database_url: str) -> _FakeEngine:
        assert database_url == "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
        return engine

    def _make_session_factory(received_engine: _FakeEngine) -> object:
        assert received_engine is engine
        return session_factory

    async def _summarize_failure_analysis(
        received_factory: object,
        *,
        since_hours: int,
        failure_example_limit: int,
    ) -> dict[str, object]:
        calls.append(("failure", received_factory, failure_example_limit))
        return {"since_hours": since_hours, "total_failed_workspaces": 0}

    async def _summarize_slo_metrics(
        received_factory: object,
        *,
        settings: object,
        since_hours: int,
    ) -> dict[str, object]:
        calls.append(("slo", received_factory, since_hours))
        assert settings is service_settings
        return {"since_hours": since_hours, "creation_total": 0}

    service_settings = SimpleNamespace(
        database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
    )
    monkeypatch.setattr(readiness, "make_engine", _make_engine)
    monkeypatch.setattr(readiness, "make_session_factory", _make_session_factory)
    monkeypatch.setattr(readiness, "summarize_failure_analysis", _summarize_failure_analysis)
    monkeypatch.setattr(readiness, "summarize_slo_metrics", _summarize_slo_metrics)

    failure = await readiness._collect_failure_summary(  # noqa: SLF001
        service_settings,  # type: ignore[arg-type]
        failure_window_hours=6,
        collector=None,
    )
    slo = await readiness._collect_slo_summary(  # noqa: SLF001
        service_settings,  # type: ignore[arg-type]
        slo_window_hours=12,
        collector=None,
    )

    assert failure == {"since_hours": 6, "total_failed_workspaces": 0}
    assert slo == {"since_hours": 12, "creation_total": 0}
    assert disposed == ["engine", "engine"]
    assert calls[0][0] == "failure"
    assert calls[0][1] is session_factory
    assert calls[1] == ("slo", session_factory, 12)


@pytest.mark.unit
def test_readiness_private_helpers_cover_missing_and_unusable_demo_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert readiness._cached_status_collector(None) is None  # noqa: SLF001

    missing_demo = readiness._demo_check(tmp_path / "missing-demo")  # noqa: SLF001
    assert missing_demo.status == "fail"
    assert missing_demo.reason_code == "DEMO_PROJECT_MISSING"

    broken_demo = tmp_path / "broken-demo"
    broken_demo.mkdir()

    def _raise_preview(_path: Path, *, include_smoke_request: bool) -> object:
        assert include_smoke_request is True
        raise RuntimeError("preview failed")

    monkeypatch.setattr(readiness, "preview_project_onboarding", _raise_preview)
    preview_failed = readiness._demo_check(broken_demo)  # noqa: SLF001

    assert preview_failed.status == "fail"
    assert preview_failed.reason_code == "DEMO_PROJECT_PREVIEW_FAILED"
    assert preview_failed.evidence["error"] == "preview failed"


@pytest.mark.unit
def test_readiness_slo_check_requires_nonempty_creation_and_cleanup_samples() -> None:
    check = readiness._slo_threshold_check(  # noqa: SLF001
        {
            "since_hours": 168,
            "creation_total": 0,
            "creation_succeeded": 0,
            "cleanup_total": 0,
            "cleanup_succeeded": 0,
            "stuck_running_count": 0,
            "actionable_failure_count": 0,
            "unactionable_failure_count": 0,
        },
        allow_slo_breach=False,
    )

    breaches = check.evidence["breaches"]
    assert check.status == "fail"
    assert check.reason_code == "PRD_SLO_THRESHOLDS_FAILED"
    assert breaches == {
        "workspace_creation_success_rate": {
            "reason": "SLO_METRICS_INSUFFICIENT_EVIDENCE",
            "actual": None,
            "threshold": {
                "operator": ">=",
                "value": readiness.MIN_WORKSPACE_CREATION_SUCCESS_RATE,
            },
        },
        "cleanup_success_rate": {
            "reason": "SLO_METRICS_INSUFFICIENT_EVIDENCE",
            "actual": None,
            "threshold": {
                "operator": ">=",
                "value": readiness.MIN_CLEANUP_SUCCESS_RATE,
            },
        },
    }


@pytest.mark.unit
def test_readiness_failure_taxonomy_includes_generic_root_cause_clusters() -> None:
    check = readiness._failure_taxonomy_check(  # noqa: SLF001
        SimpleNamespace(
            since_hours=24,
            total_failed_workspaces=2,
            failure_groups=[],
            latest_examples=[],
            root_cause_clusters=[
                {
                    "failure_reason": "agent_failure",
                    "reason_code": "AGENT_FAILURE",
                    "sample_workspace_ids": ("ws_a", "ws_b"),
                    "count": 2,
                }
            ],
        ),
        allow_generic_failures=False,
    )

    assert check.status == "fail"
    assert check.reason_code == "GENERIC_FAILURE_REASON_BLOCKS_RELEASE"
    assert check.evidence["generic_failures"] == [
        {
            "source": "root_cause_cluster",
            "sample_workspace_ids": ["ws_a", "ws_b"],
            "failure_reason": "agent_failure",
            "reason_code": "AGENT_FAILURE",
            "count": 2,
        }
    ]


@pytest.mark.unit
def test_readiness_failure_taxonomy_ignores_classified_failures_and_reports_demo_action() -> None:
    assert (
        readiness._generic_failure_findings(  # noqa: SLF001
            {
                "failure_groups": [{"failure_reason": "validation_failure", "count": 1}],
                "latest_examples": [
                    {
                        "workspace_id": "ws_validation",
                        "failure_reason": "validation_failure",
                        "reason_code": "PYTEST_TEST_FAILURE",
                    }
                ],
                "root_cause_clusters": [
                    {
                        "failure_reason": "infrastructure_failure",
                        "reason_code": "GIT_PUSH_FAILED",
                        "sample_workspace_ids": ("ws_git",),
                        "count": 1,
                    }
                ],
            }
        )
        == []
    )

    assert readiness._next_actions(  # noqa: SLF001
        [
            CoreReadinessCheck(
                name="warn_only",
                status="warn",
                reason_code="WARN_ONLY",
                message="warn only",
            ),
            CoreReadinessCheck(
                name="demo_project",
                status="fail",
                reason_code="DEMO_PROJECT_MISSING",
                message="demo project is missing",
            ),
        ]
    ) == ["Add the in-repo AWF Core demo project under examples/."]


@pytest.mark.unit
def test_readiness_treats_preserved_validation_with_secondary_infra_as_classified() -> None:
    summary = {
        "since_hours": 24,
        "total_failed_workspaces": 1,
        "failure_groups": [{"failure_reason": "validation_failure", "count": 1}],
        "latest_examples": [
            {
                "workspace_id": "ws_validation",
                "failure_reason": "validation_failure",
                "reason_code": "PYTEST_TEST_FAILURE",
                "secondary_failure": {
                    "failure_reason": "infrastructure_failure",
                    "reason_code": "STALE_ACTIVE_EXECUTION",
                },
            }
        ],
        "root_cause_clusters": [
            {
                "failure_reason": "validation_failure",
                "reason_code": "PYTEST_TEST_FAILURE",
                "sample_workspace_ids": ("ws_validation",),
                "count": 1,
                "secondary_failures": [
                    {
                        "failure_reason": "cleanup_failure",
                        "reason_code": "CLEANUP_FAILED",
                    }
                ],
            }
        ],
    }

    check = readiness._failure_taxonomy_check(  # noqa: SLF001
        summary,
        allow_generic_failures=False,
    )

    assert check.status == "ok"
    assert check.reason_code == "FAILURE_TAXONOMY_CLASSIFIED"
    assert check.evidence["generic_failures"] == []
    assert readiness._generic_failure_findings(summary) == []  # noqa: SLF001


@pytest.mark.unit
def test_readiness_next_actions_and_jsonable_cover_non_mapping_inputs(
    tmp_path: Path,
) -> None:
    check = CoreReadinessCheck(
        name="custom_gate",
        status="fail",
        reason_code="CUSTOM_GATE_FAILED",
        message="custom gate failed",
        evidence={
            "payload": _EvidenceValue(
                path=tmp_path / "artifact.json",
                when=datetime(2026, 5, 3, 12, 0, tzinfo=UTC),
            ),
            "dates": {date(2026, 5, 3)},
        },
    )

    assert readiness._items(None) == []  # noqa: SLF001
    assert readiness._items("not-a-list") == []  # noqa: SLF001
    assert readiness._get(SimpleNamespace(value="from-attr"), "value") == "from-attr"  # noqa: SLF001
    assert readiness._rate(1, 0) is None  # noqa: SLF001
    assert readiness._next_actions([check]) == [  # noqa: SLF001
        "Resolve release gate check: custom_gate."
    ]
    assert check.to_dict()["evidence"] == {
        "payload": {
            "path": str(tmp_path / "artifact.json"),
            "when": "2026-05-03T12:00:00+00:00",
        },
        "dates": ["2026-05-03"],
    }
