"""CLI tests for ``awf profile doctor``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from awf.cli.main import app

_runner = CliRunner()

pytestmark = pytest.mark.unit


def _ok_report(repo: str = "/repo") -> dict:
    return {
        "status": "ok",
        "repo": repo,
        "phases": [
            {
                "name": "profile_resolution",
                "status": "ok",
                "reason_code": "PROFILE_DOCTOR_PROFILE_RESOLVED",
                "message": "Resolved profile 'generic'.",
                "evidence": {},
                "action": "No action required.",
            },
        ],
        "next_actions": [],
    }


def _stub(monkeypatch: pytest.MonkeyPatch, report: dict) -> None:
    monkeypatch.setattr(
        "awf.service.profile_doctor.collect_profile_doctor_report",
        lambda *_a, **_k: report,
    )
    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _path: None,
    )


def test_doctor_json_exit_zero_on_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub(monkeypatch, _ok_report(str(tmp_path)))

    result = _runner.invoke(app, ["profile", "doctor", str(tmp_path)])

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert output["status"] == "ok"
    assert output["phases"][0]["name"] == "profile_resolution"


def test_doctor_json_exit_one_on_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report = _ok_report(str(tmp_path))
    report["status"] = "fail"
    report["phases"][0]["status"] = "fail"
    report["phases"][0]["reason_code"] = "SECRET_LEASE_SOURCE_MISSING"
    _stub(monkeypatch, report)

    result = _runner.invoke(app, ["profile", "doctor", str(tmp_path)])

    assert result.exit_code == 1
    output = json.loads(result.stdout)
    assert output["status"] == "fail"


def test_doctor_pretty_renders_human_lines(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report = _ok_report(str(tmp_path))
    report["phases"][0]["action"] = "No action required."
    report["phases"].append(
        {
            "name": "secret_leases",
            "status": "warn",
            "reason_code": "PROFILE_DOCTOR_SECRET_LEASES_OPTIONAL_MISSING",
            "message": "1 optional secret lease(s) have no source.",
            "evidence": {},
            "action": "Provide the optional secret sources.",
        }
    )
    report["status"] = "warn"
    report["next_actions"] = ["Provide the optional secret sources."]
    _stub(monkeypatch, report)

    result = _runner.invoke(app, ["profile", "doctor", str(tmp_path), "--format", "pretty"])

    assert result.exit_code == 0
    assert "AWF profile doctor: warn" in result.stdout
    assert f"Repo: {tmp_path}" in result.stdout
    assert "[ok] profile_resolution" in result.stdout
    assert "[warn] secret_leases" in result.stdout
    assert "reason: PROFILE_DOCTOR_SECRET_LEASES_OPTIONAL_MISSING" in result.stdout
    assert "action: Provide the optional secret sources." in result.stdout
    assert "Next actions:" in result.stdout


def test_doctor_rejects_missing_repo_path(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"

    result = _runner.invoke(app, ["profile", "doctor", str(missing)])

    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_doctor_rejects_file_repo_path(tmp_path: Path) -> None:
    a_file = tmp_path / "checkout.txt"
    a_file.write_text("not a directory")

    result = _runner.invoke(app, ["profile", "doctor", str(a_file)])

    assert result.exit_code != 0
    assert "is a file" in result.output


def test_doctor_appears_in_profile_help() -> None:
    result = _runner.invoke(app, ["profile", "--help"])
    assert result.exit_code == 0
    assert "doctor" in result.stdout
