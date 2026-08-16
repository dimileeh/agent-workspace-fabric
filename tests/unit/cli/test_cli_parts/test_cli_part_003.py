"""Typer CLI tests — connection/status/doctor/rebase command coverage.

Split out of ``test_cli_part_002`` to stay under the first-party 1500-line
maintainability guardrail. Shares the same CliRunner + httpx mock helpers.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import click
import httpx
import pytest
from typer.testing import CliRunner

import awf.cli.common as cli_common
from awf.cli.main import app

_runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_cli_local_service_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep developer root `.env` values out of mocked CLI HTTP tests."""
    monkeypatch.setattr(cli_common, "local_service_environ", lambda _environ: {})


def _mock_response(*, status_code: int = 202, payload: object = None, text: str = "") -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.content = b"ok" if payload is not None or text else b""
    response.text = text or (json.dumps(payload) if payload is not None else "")
    response.json.return_value = payload
    return response


def _assert_current_first_path_guidance(stdout: str) -> None:
    visible_help = " ".join(click.unstyle(stdout).split()).lower()
    stale_help = visible_help.replace("`", "")
    assert "current runnable first path" in visible_help
    assert "awf service bootstrap" in visible_help
    assert "awf init <path>" in visible_help
    assert "recommended first path is awf setup" not in stale_help
    assert "awf setup, then awf start" not in stale_help


def _assert_control_headers(
    headers: dict[str, str],
    *,
    idempotency_key: str,
    if_match: str,
) -> None:
    assert headers["Idempotency-Key"] == idempotency_key
    assert headers["If-Match"] == if_match


class TestConnectionErrors:
    @pytest.mark.unit
    def test_request_error_exits_with_code_2(self) -> None:
        with patch(
            "awf.cli.main.httpx.request",
            side_effect=httpx.ConnectError("refused"),
        ):
            result = _runner.invoke(app, ["workspace", "list"])

        assert result.exit_code == 2
        assert "could not reach" in result.stderr


class TestServiceStatusOrphanReporting:
    @pytest.mark.unit
    def test_pretty_output_surfaces_orphan_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from awf.service import config as config_mod
        from awf.service import status as status_mod

        settings = object()
        monkeypatch.setattr(
            config_mod, "resolve_service_settings", lambda *_args, **_kwargs: settings
        )

        async def _collect(received: object, **_kwargs: object) -> dict[str, object]:
            assert received is settings
            return {
                "service": "awf",
                "status": "fail",
                "checks": {
                    "orphan_workspaces": {
                        "ok": False,
                        "status": "fail",
                        "reason": "ORPHANS_PRESENT",
                        "orphan_count": 2,
                        "active_count": 1,
                        "examples": [
                            {
                                "workspace_id": "ws_dead",
                                "compose_project": "awf_ws_dead",
                                "classification": "terminal",
                                "reason": "WORKSPACE_TERMINAL",
                            },
                            {
                                "workspace_id": "ws_ghost",
                                "compose_project": "awf-ws_ghost",
                                "classification": "missing",
                                "reason": "WORKSPACE_MISSING",
                            },
                        ],
                        "action": ("Run docker compose -p <project> down -v --remove-orphans"),
                    }
                },
            }

        monkeypatch.setattr(status_mod, "collect_service_status", _collect)

        result = _runner.invoke(app, ["service", "status", "--format", "pretty"])

        assert result.exit_code == 1, result.output
        assert "checks.orphan_workspaces.orphan_count: 2" in result.stdout
        assert "checks.orphan_workspaces.active_count: 1" in result.stdout
        assert "ORPHANS_PRESENT" in result.stdout
        assert "ws_dead" in result.stdout
        assert "ws_ghost" in result.stdout


class TestCliHelp:
    @pytest.mark.unit
    def test_main_help_contains_dx_guidance(self) -> None:
        result = _runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "Mutates:" in result.stdout
        _assert_current_first_path_guidance(result.stdout)

    @pytest.mark.unit
    def test_init_help_contains_dx_guidance(self) -> None:
        result = _runner.invoke(app, ["init", "--help"])
        assert result.exit_code == 0
        _assert_current_first_path_guidance(result.stdout)

    @pytest.mark.unit
    def test_service_bootstrap_help_contains_dx_guidance(self) -> None:
        result = _runner.invoke(app, ["service", "bootstrap", "--help"])
        assert result.exit_code == 0
        _assert_current_first_path_guidance(result.stdout)

    @pytest.mark.unit
    def test_workspace_help_contains_dx_guidance(self) -> None:
        result = _runner.invoke(app, ["workspace", "--help"])
        assert result.exit_code == 0
        _assert_current_first_path_guidance(result.stdout)


class TestServiceDoctorBundle:
    @pytest.mark.unit
    def test_cli_service_doctor_bundle_flag_writes_file(
        self,
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
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from awf.service import config as config_mod
        from awf.service import doctor as doctor_mod

        monkeypatch.setattr(
            config_mod, "resolve_service_settings", lambda *_args, **_kwargs: object()
        )
        monkeypatch.setattr(config_mod, "local_service_environ", lambda **_kwargs: {})

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
        self,
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
        parsed = json.loads(result.stdout)
        assert parsed == {"support_bundle_path": str(bundle_path)}

    @pytest.mark.unit
    def test_cli_service_doctor_bundle_flag_ignores_failing_report_exit(
        self,
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


class TestWorkspaceRebase:
    """Workspace rebase command tests."""

    @pytest.mark.unit
    def test_posts_rebase_request_with_reason(self) -> None:
        """Post rebase requests with the requested reason."""
        response = _mock_response(
            status_code=202,
            payload={
                "operation_id": "op_rebase",
                "operation_status": "requested",
                "status": "rebasing",
                "workspace_id": "ws_rebase",
                "message": "workspace rebase requested",
            },
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "rebase",
                    "ws_rebase",
                    "--reason",
                    "recover merge conflicts",
                    "--idempotency-key",
                    "rebase-key",
                    "--if-match",
                    "11",
                ],
            )

        assert result.exit_code == 0
        assert "op_rebase" in result.stdout
        assert mock.call_args[0] == (
            "POST",
            "http://localhost:8000/v1/workspaces/ws_rebase/rebase",
        )
        assert mock.call_args.kwargs["json"] == {"reason": "recover merge conflicts"}
        _assert_control_headers(
            mock.call_args.kwargs["headers"],
            idempotency_key="rebase-key",
            if_match="11",
        )
