"""CLI tests for the support bundle flag on awf service doctor."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from awf.cli.main import app

_runner = CliRunner()


@pytest.mark.unit
def test_cli_service_doctor_bundle_flag_writes_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import support_bundle as bundle_mod

    out_dir = tmp_path / "bundles"
    out_dir.mkdir(parents=True, exist_ok=True)

    async def _collect_bundle(*args: object, **kwargs: object) -> dict[str, object]:
        return {
            "generated_at": "2025-01-01T00:00:00+00:00",
            "version": "0.1.0",
            "service_status": {"status": "ok"},
            "doctor_report": {"status": "ok"},
            "provider_readiness_summary": {"status": "ok"},
            "orphan_cleanup_posture": {},
            "recent_failure_summary": {},
            "config_fingerprint": {},
            "log_pointers": [],
            "issue_template_pointer": ".github/ISSUE_TEMPLATE/bug_report.yml",
        }

    monkeypatch.setattr(bundle_mod, "collect_support_bundle", _collect_bundle)

    original_write = bundle_mod.write_support_bundle

    def _write(bundle: dict[str, object], *, directory: Path | None = None) -> Path:
        return original_write(bundle, directory=out_dir)

    monkeypatch.setattr(bundle_mod, "write_support_bundle", _write)

    result = _runner.invoke(app, ["service", "doctor", "--bundle"])

    assert result.exit_code == 0, result.output
    assert "Support bundle written" in result.stdout
    bundle_path = next(out_dir.glob("awf-support-bundle-*.json"), None)
    assert bundle_path is not None
    bundle = json.loads(bundle_path.read_text())
    assert "doctor_report" in bundle
    assert "service_status" in bundle


@pytest.mark.unit
def test_cli_service_doctor_fail_path_points_to_bundle_and_issue_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import config as config_mod
    from awf.service import doctor as doctor_mod

    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda: object())
    monkeypatch.setattr(config_mod, "local_service_environ", lambda: {})

    report = SimpleNamespace(
        status="fail",
        to_dict=lambda: {
            "service": "awf",
            "status": "fail",
            "summary": {"ok": 0, "warn": 0, "fail": 1},
            "diagnostics": [
                {
                    "id": "docker",
                    "label": "Docker",
                    "status": "fail",
                    "reason": "DOCKER_DAEMON_UNREACHABLE",
                    "message": "Docker is not available.",
                    "action": "Start Docker Desktop.",
                    "source": "checks.docker",
                    "metadata": {},
                }
            ],
        },
    )

    async def _collect(*args: object, **kwargs: object) -> SimpleNamespace:
        return report

    monkeypatch.setattr(doctor_mod, "collect_doctor_report", _collect)
    monkeypatch.setattr(
        doctor_mod,
        "render_doctor_pretty",
        lambda _report: (
            "AWF doctor: fail\n"
            "[fail] Docker: Docker is not available.\n"
            "       reason: DOCKER_DAEMON_UNREACHABLE\n"
            "       action: Start Docker Desktop.\n"
        ),
    )

    result = _runner.invoke(app, ["service", "doctor"])

    assert result.exit_code == 1
    output = result.stdout + result.stderr
    assert "awf service doctor --bundle" in output
    assert ".github/ISSUE_TEMPLATE/bug_report.yml" in output


@pytest.mark.unit
def test_cli_service_doctor_bundle_with_json_format(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import support_bundle as bundle_mod

    out_dir = tmp_path / "bundles"
    out_dir.mkdir(parents=True, exist_ok=True)

    async def _collect_bundle(*args: object, **kwargs: object) -> dict[str, object]:
        return {
            "generated_at": "2025-01-01T00:00:00+00:00",
            "version": "0.1.0",
            "service_status": {"status": "ok"},
            "doctor_report": {"status": "ok"},
            "provider_readiness_summary": {"status": "ok"},
            "orphan_cleanup_posture": {},
            "recent_failure_summary": {},
            "config_fingerprint": {},
            "log_pointers": [],
            "issue_template_pointer": ".github/ISSUE_TEMPLATE/bug_report.yml",
        }

    monkeypatch.setattr(bundle_mod, "collect_support_bundle", _collect_bundle)

    original_write = bundle_mod.write_support_bundle

    def _write(bundle: dict[str, object], *, directory: Path | None = None) -> Path:
        return original_write(bundle, directory=out_dir)

    monkeypatch.setattr(bundle_mod, "write_support_bundle", _write)

    result = _runner.invoke(app, ["service", "doctor", "--bundle", "--format", "json"])
    assert result.exit_code == 0, result.output
    bundle_path = next(out_dir.glob("awf-support-bundle-*.json"), None)
    assert bundle_path is not None


@pytest.mark.unit
def test_cli_service_doctor_bundle_flag_ignores_failing_report_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import support_bundle as bundle_mod

    out_dir = tmp_path / "bundles"
    out_dir.mkdir(parents=True, exist_ok=True)

    async def _collect_bundle(*args: object, **kwargs: object) -> dict[str, object]:
        return {
            "generated_at": "2025-01-01T00:00:00+00:00",
            "version": "0.1.0",
            "service_status": {"status": "fail"},
            "doctor_report": {"status": "fail"},
            "provider_readiness_summary": {"status": "fail"},
            "orphan_cleanup_posture": {},
            "recent_failure_summary": {},
            "config_fingerprint": {},
            "log_pointers": [],
            "issue_template_pointer": ".github/ISSUE_TEMPLATE/bug_report.yml",
        }

    monkeypatch.setattr(bundle_mod, "collect_support_bundle", _collect_bundle)

    original_write = bundle_mod.write_support_bundle

    def _write(bundle: dict[str, object], *, directory: Path | None = None) -> Path:
        return original_write(bundle, directory=out_dir)

    monkeypatch.setattr(bundle_mod, "write_support_bundle", _write)

    result = _runner.invoke(app, ["service", "doctor", "--bundle"])

    assert result.exit_code == 0, result.output
    assert "Support bundle written" in result.stdout
    bundle_path = next(out_dir.glob("awf-support-bundle-*.json"), None)
    assert bundle_path is not None
    bundle = json.loads(bundle_path.read_text())
    assert bundle.get("doctor_report", {}).get("status") == "fail"

