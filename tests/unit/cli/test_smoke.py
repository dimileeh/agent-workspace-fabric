"""CLI tests for ``awf smoke run`` command."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from awf.cli.main import app

_runner = CliRunner()

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Remove ANSI SGR color codes from help output.

    Rich auto-enables color under CI (it detects GitHub Actions), and it styles
    each segment of an option flag separately — so ``--mocked-local`` renders as
    ``-`` ``-mocked`` ``-local`` with escape codes wedged between the pieces. A
    raw substring check then fails in CI while passing locally (no color). Strip
    the styling so assertions see the literal flag text the help advertises.
    """
    return _ANSI_ESCAPE.sub("", text)


_MOCK_SETTINGS = SimpleNamespace(
    api_base_url="http://localhost:8000",
    database_url="postgresql://localhost/",
    github_token=None,
    service_name="test",
    env="dev",
    docker_host="unix:///var/run/docker.sock",
    agent_runtime_image="test",
    work_dir="/tmp",
    api_token=None,
    worker_poll_interval_seconds=5.0,
    worker_max_concurrent_provisions=3,
)


def _stub_smoke_collector(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str = "ok",
    mode: str = "live",
    phases: list[dict] | None = None,
    raise_exception: BaseException | None = None,
):
    async def _collect(*args, **kwargs):
        if raise_exception is not None:
            raise raise_exception
        return {
            "status": status,
            "project": str(kwargs.get("project", "/tmp/test")),
            "mode": mode,
            "phases": phases or [],
            "console_links": {
                "ui": "http://localhost:3000",
                "api_docs": "http://localhost:8000/docs",
            },
            "next_actions": [],
        }

    monkeypatch.setattr("awf.service.smoke.collect_smoke_report", _collect)
    monkeypatch.setattr("awf.service.config.resolve_service_settings", lambda: _MOCK_SETTINGS)


@pytest.mark.unit
class TestSmokeRunCommand:
    def test_emits_json_output_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_smoke_collector(monkeypatch)
        result = _runner.invoke(app, ["smoke", "run"])
        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["status"] == "ok"
        assert "phases" in output

    def test_pretty_format_human_readable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_smoke_collector(
            monkeypatch,
            phases=[
                {
                    "name": "service_readiness",
                    "status": "ok",
                    "reason_code": "SMOKE_SERVICE_READY",
                    "message": "ready",
                    "evidence": {},
                    "action": "none",
                },
                {
                    "name": "auth_readiness",
                    "status": "ok",
                    "reason_code": "SMOKE_AUTH_READY",
                    "message": "ready",
                    "evidence": {},
                    "action": "none",
                },
            ],
        )
        result = _runner.invoke(app, ["smoke", "run", "--format", "pretty"])
        assert result.exit_code == 0
        assert "AWF smoke: ok" in result.stdout
        assert "[ok] service_readiness" in result.stdout
        assert "SMOKE_SERVICE_READY" in result.stdout
        assert "SMOKE_AUTH_READY" in result.stdout
        assert "phases[0]." not in result.stdout

    def test_pretty_format_does_not_duplicate_reason_when_message_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_smoke_collector(
            monkeypatch,
            phases=[
                {
                    "name": "service_readiness",
                    "status": "ok",
                    "reason_code": "SMOKE_SERVICE_READY",
                    "evidence": {},
                    "action": "none",
                },
            ],
        )

        result = _runner.invoke(app, ["smoke", "run", "--format", "pretty"])

        assert result.exit_code == 0
        assert "  [ok] service_readiness\n" in result.stdout
        assert "  [ok] service_readiness: SMOKE_SERVICE_READY" not in result.stdout
        assert "        reason: SMOKE_SERVICE_READY" in result.stdout
        assert result.stdout.count("SMOKE_SERVICE_READY") == 1

    def test_project_flag_is_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _collect(*args, **kwargs):
            return {
                "status": "ok",
                "project": str(kwargs.get("project", "")),
                "mode": "live",
                "phases": [],
                "console_links": {},
                "next_actions": [],
            }

        monkeypatch.setattr("awf.service.smoke.collect_smoke_report", _collect)
        monkeypatch.setattr("awf.service.config.resolve_service_settings", lambda: _MOCK_SETTINGS)
        result = _runner.invoke(app, ["smoke", "run", "--project", "/some/other/path"])
        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert "/some/other/path" in output["project"]

    def test_mocked_local_flag_is_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _collect(*args, **kwargs):
            return {
                "status": "ok",
                "project": "/proj",
                "mode": "live",
                "mocked_local_value": kwargs.get("mocked_local", "not-set"),
                "phases": [],
                "console_links": {},
                "next_actions": [],
            }

        monkeypatch.setattr("awf.service.smoke.collect_smoke_report", _collect)
        monkeypatch.setattr("awf.service.config.resolve_service_settings", lambda: _MOCK_SETTINGS)
        result = _runner.invoke(app, ["smoke", "run", "--mocked-local"])
        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["mocked_local_value"] is True

    def test_service_unreachable_exits_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_smoke_collector(monkeypatch, status="fail")
        result = _runner.invoke(app, ["smoke", "run"])
        assert result.exit_code != 0
        output = json.loads(result.stdout)
        assert output["status"] == "fail"

    def test_demo_path_flag_is_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _collect(*args, **kwargs):
            return {
                "status": "ok",
                "project": str(kwargs.get("project", "")),
                "mode": "live",
                "demo_path_value": str(kwargs.get("demo_path", "not-set")),
                "phases": [],
                "console_links": {},
                "next_actions": [],
            }

        monkeypatch.setattr("awf.service.smoke.collect_smoke_report", _collect)
        monkeypatch.setattr("awf.service.config.resolve_service_settings", lambda: _MOCK_SETTINGS)
        result = _runner.invoke(app, ["smoke", "run", "--demo-path", "/tmp/custom-demo"])
        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output["demo_path_value"] == str(Path("/tmp/custom-demo").resolve())

    def test_help_shows_description(self) -> None:
        result = _runner.invoke(app, ["smoke", "run", "--help"])
        assert result.exit_code == 0
        assert "smoke" in result.stdout.lower()

    def test_help_describes_provider_free_local_proof(self) -> None:
        """`smoke run --help` advertises the no-token local proof (T10).

        The first-run chain points evaluators here, so the help text must make
        clear the proof needs no provider token or GitHub access.
        """
        result = _runner.invoke(app, ["smoke", "run", "--help"])
        assert result.exit_code == 0
        plain = _strip_ansi(result.stdout)
        lowered = plain.lower()
        assert "--mocked-local" in plain
        assert "without" in lowered
        # No provider token or GitHub authority is required for the local proof.
        assert "token" in lowered or "github" in lowered


@pytest.mark.unit
class TestSmokeGroupRegistration:
    def test_smoke_appears_in_top_level_help(self) -> None:
        result = _runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "smoke" in result.stdout

    def test_smoke_group_help_shows_run_subcommand(self) -> None:
        result = _runner.invoke(app, ["smoke", "--help"])
        assert result.exit_code == 0
        assert "run" in result.stdout
