"""CLI coverage for first-run onboarding guidance."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from awf.cli.main import app

_runner = CliRunner()


def _stub_local_prerequisites(
    monkeypatch: pytest.MonkeyPatch,
    *,
    service_status: str = "ok",
    doctor_status: str = "ok",
    preview_smoke_payload: bool = False,
) -> None:
    async def _collect_service_status(
        _settings: object, **kwargs: object
    ) -> dict[str, object]:
        return {
            "service": "awf",
            "status": service_status,
            "checks": {},
            "agent_readiness": {"status": service_status},
        }

    async def _collect_doctor_report(_settings: object, **_kwargs: object) -> object:
        return SimpleNamespace(status=doctor_status, diagnostics=())

    monkeypatch.setattr("awf.service.config.resolve_service_settings", lambda: object())
    monkeypatch.setattr("awf.service.status.collect_service_status", _collect_service_status)
    monkeypatch.setattr("awf.service.doctor.collect_doctor_report", _collect_doctor_report)
    monkeypatch.setattr(
        "awf.service.doctor.render_doctor_pretty",
        lambda report: f"AWF doctor: {getattr(report, 'status', 'unknown')}\n",
    )
    if preview_smoke_payload:
        def _preview_project_onboarding(
            _path: Path, **_kwargs: object
        ) -> object:
            return SimpleNamespace(
                draft=SimpleNamespace(template="generic"),
                smoke_request={"dummy": "payload"},
            )

        monkeypatch.setattr(
            "awf.profiles.onboarding.preview_project_onboarding",
            _preview_project_onboarding,
        )


@pytest.mark.unit
def test_init_command_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_local_prerequisites(monkeypatch)

    result = _runner.invoke(app, ["init", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "AWF init: local onboarding readiness check" in result.output


@pytest.mark.unit
def test_init_is_safe_by_default_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_local_prerequisites(monkeypatch)

    result_first = _runner.invoke(app, ["init", str(tmp_path)])
    result_second = _runner.invoke(app, ["init", str(tmp_path)])

    assert result_first.exit_code == 0, result_first.output
    assert result_second.exit_code == 0, result_second.output
    assert not (tmp_path / ".awf" / "workspace.yml").exists()


@pytest.mark.unit
def test_init_invalid_project_path_is_reported_without_service_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does-not-exist"

    def _fail_to_resolve_service_settings() -> object:
        raise AssertionError("should not resolve settings")

    async def _fail_to_collect_service_status(
        *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        raise AssertionError("should not collect service status")

    def _fail_to_collect_doctor_report(
        *_args: object, **_kwargs: object,
    ) -> object:
        raise AssertionError("should not collect doctor report")

    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        _fail_to_resolve_service_settings,
    )
    monkeypatch.setattr(
        "awf.service.status.collect_service_status",
        _fail_to_collect_service_status,
    )
    monkeypatch.setattr(
        "awf.service.doctor.collect_doctor_report",
        _fail_to_collect_doctor_report,
    )

    result = _runner.invoke(app, ["init", str(missing)])

    assert result.exit_code == 2, result.output
    assert f"error: project path does not exist: {missing}" in result.output


@pytest.mark.unit
def test_init_prints_clear_next_steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_local_prerequisites(monkeypatch)

    result = _runner.invoke(app, ["init", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "awf profile init" in result.output
    assert "awf profile preview" in result.output
    assert "--include-smoke-request" in result.output


@pytest.mark.unit
def test_init_runs_status_and_doctor_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"status": 0, "doctor": 0}

    async def _collect_service_status(
        _settings: object, **_kwargs: object
    ) -> dict[str, object]:
        calls["status"] += 1
        return {
            "service": "awf",
            "status": "ok",
            "checks": {},
            "agent_readiness": {"status": "ok"},
        }

    async def _collect_doctor_report(_settings: object, **_kwargs: object) -> object:
        calls["doctor"] += 1
        return SimpleNamespace(status="ok", diagnostics=())

    monkeypatch.setattr("awf.service.config.resolve_service_settings", lambda: object())
    monkeypatch.setattr("awf.service.status.collect_service_status", _collect_service_status)
    monkeypatch.setattr("awf.service.doctor.collect_doctor_report", _collect_doctor_report)
    monkeypatch.setattr(
        "awf.service.doctor.render_doctor_pretty",
        lambda report: f"AWF doctor: {getattr(report, 'status', 'ok')}\n",
    )

    result = _runner.invoke(app, ["init", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert calls["status"] == 1
    assert calls["doctor"] == 1


@pytest.mark.unit
def test_init_continues_when_service_status_probe_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"status": 0, "doctor": 0}

    async def _collect_service_status(
        _settings: object, **_kwargs: object
    ) -> dict[str, object]:
        calls["status"] += 1
        raise RuntimeError("service probe is unavailable")

    async def _collect_doctor_report(
        _settings: object,
        **kwargs: object,
    ) -> object:
        calls["doctor"] += 1
        status_collector = kwargs["status_collector"]
        status = await status_collector(_settings, strict_providers=frozenset(), provider_environ={})
        return SimpleNamespace(status=status.get("status", "fail"), diagnostics=())

    monkeypatch.setattr("awf.service.config.resolve_service_settings", lambda: object())
    monkeypatch.setattr("awf.service.status.collect_service_status", _collect_service_status)
    monkeypatch.setattr("awf.service.doctor.collect_doctor_report", _collect_doctor_report)
    monkeypatch.setattr(
        "awf.service.doctor.render_doctor_pretty",
        lambda report: f"AWF doctor: {getattr(report, 'status', 'unknown')}\n",
    )

    result = _runner.invoke(app, ["init", str(tmp_path)])

    assert result.exit_code == 1, result.output
    assert calls["status"] == 1
    assert calls["doctor"] == 1
    assert "service status: fail" in result.output
    assert "AWF doctor: fail" in result.output
    assert "Local prerequisites are not fully ready yet" in result.output


@pytest.mark.unit
def test_init_uses_cached_service_status_for_doctor_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"status": 0, "doctor": 0, "doctor_status": 0}

    status_payload = {
        "service": "awf",
        "status": "ok",
        "checks": {},
        "agent_readiness": {"status": "ok"},
    }

    async def _collect_service_status(
        _settings: object, **_kwargs: object
    ) -> dict[str, object]:
        calls["status"] += 1
        return status_payload

    async def _collect_doctor_report(
        _settings: object,
        **kwargs: object,
    ) -> object:
        calls["doctor"] += 1
        status_collector = kwargs["status_collector"]
        collected_status = await status_collector(_settings, strict_providers=frozenset(), provider_environ={})
        calls["doctor_status"] += 1
        assert collected_status is status_payload
        return SimpleNamespace(status="ok", diagnostics=())

    monkeypatch.setattr("awf.service.config.resolve_service_settings", lambda: object())
    monkeypatch.setattr("awf.service.status.collect_service_status", _collect_service_status)
    monkeypatch.setattr("awf.service.doctor.collect_doctor_report", _collect_doctor_report)
    monkeypatch.setattr(
        "awf.service.doctor.render_doctor_pretty",
        lambda report: f"AWF doctor: {getattr(report, 'status', 'ok')}\n",
    )

    result = _runner.invoke(app, ["init", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert calls["status"] == 1
    assert calls["doctor"] == 1
    assert calls["doctor_status"] == 1


@pytest.mark.unit
def test_init_reports_local_prerequisite_failures_without_api_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_local_prerequisites(
        monkeypatch,
        service_status="fail",
        doctor_status="fail",
    )

    result = _runner.invoke(app, ["init", str(tmp_path)])

    assert result.exit_code != 0
    assert "Local prerequisites are not fully ready" in result.output
    assert "AWF doctor: fail" in result.output


@pytest.mark.unit
def test_init_does_not_submit_workspace_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_local_prerequisites(monkeypatch)

    mocked_call = MagicMock()
    monkeypatch.setattr("awf.cli.main._call", mocked_call)

    result = _runner.invoke(app, ["init", str(tmp_path)])

    assert result.exit_code == 0, result.output
    mocked_call.assert_not_called()


@pytest.mark.unit
def test_init_includes_smoke_workspace_hints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_local_prerequisites(monkeypatch, preview_smoke_payload=True)

    result = _runner.invoke(
        app,
        ["init", str(tmp_path), "--include-smoke-request"],
    )

    assert result.exit_code == 0, result.output
    assert "Optional" in result.output
    assert "does not submit a workspace" in result.output
    assert "Smoke request payload (local-only, not submitted):" in result.output
