"""AWF Core release-readiness scorecard tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.service.doctor.models import DoctorReport
from awf.service.readiness import collect_core_readiness_report


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


@pytest.mark.unit
def test_ready_for_gke_criteria_reference_executable_release_gate() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    todo = (repo_root / "TODO" / "pre-gke-industrial-readiness.md").read_text(
        encoding="utf-8"
    )
    readme = (repo_root / "README.md").read_text(encoding="utf-8")

    assert "awf service readiness --format json" in todo
    assert "examples/awf-core-demo" in todo
    assert "AWF_CORE_TRUST_MODEL.md" in readme
    assert "awf service readiness --format json" in readme


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
        settings=SimpleNamespace(database_url="sqlite+aiosqlite:///:memory:"),  # type: ignore[arg-type]
        demo_path=demo_path,
        status_collector=_status_collector,
        doctor_collector=_doctor_collector,
        failure_analysis_collector=_failure_collector,  # type: ignore[arg-type]
        slo_metrics_collector=_ok_slo_collector,
    )

    assert report.status == "fail"
    taxonomy_check = next(check for check in report.checks if check.name == "recent_failure_taxonomy")
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
        settings=SimpleNamespace(database_url="sqlite+aiosqlite:///:memory:"),  # type: ignore[arg-type]
        demo_path=demo_path,
        status_collector=_status_collector,
        doctor_collector=_doctor_collector,
        failure_analysis_collector=_failure_collector,  # type: ignore[arg-type]
        slo_metrics_collector=_ok_slo_collector,
        allow_generic_failures=True,
    )

    assert report.status == "ok"
    taxonomy_check = next(check for check in report.checks if check.name == "recent_failure_taxonomy")
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
        settings=SimpleNamespace(database_url="sqlite+aiosqlite:///:memory:"),  # type: ignore[arg-type]
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
        settings=SimpleNamespace(database_url="sqlite+aiosqlite:///:memory:"),  # type: ignore[arg-type]
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
        settings=SimpleNamespace(database_url="sqlite+aiosqlite:///:memory:"),  # type: ignore[arg-type]
        demo_path=demo_path,
        status_collector=_status_collector,
        doctor_collector=_doctor_collector,  # type: ignore[arg-type]
        failure_analysis_collector=_failure_collector,  # type: ignore[arg-type]
        slo_metrics_collector=_slo_collector,  # type: ignore[arg-type]
    )

    assert report.status == "ok"
    assert calls["status"] == 1


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
