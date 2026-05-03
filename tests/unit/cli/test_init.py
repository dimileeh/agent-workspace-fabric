"""CLI coverage for first-run onboarding guidance."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from awf.cli.main import app

_runner = CliRunner()


def _docker_diagnostic(status: str = "ok") -> Any:
    from awf.service.doctor.models import DoctorDiagnostic

    return DoctorDiagnostic(
        id="docker",
        label="Docker",
        status=status,
        reason="DOCKER_OK" if status == "ok" else "DOCKER_DAEMON_UNREACHABLE",
        message=(
            "Docker daemon is reachable."
            if status == "ok"
            else "Docker is installed but the daemon is not reachable."
        ),
        action=(
            "No action required."
            if status == "ok"
            else "Start Docker Desktop or verify AWF_DOCKER_HOST."
        ),
        source="checks.docker",
    )


def _doctor_report(*diagnostics: Any) -> Any:
    from awf.service.doctor.models import DoctorReport

    overall = (
        "ok"
        if all(getattr(d, "status", "ok") == "ok" for d in diagnostics)
        else "fail"
    )
    return DoctorReport(
        service="awf",
        status=overall,
        diagnostics=tuple(diagnostics),
    )


def _stub_bootstrap_mode(
    monkeypatch: pytest.MonkeyPatch,
    *,
    docker_status: str = "ok",
    bootstrap_result: Any = None,
    bootstrap_error: Exception | None = None,
) -> dict[str, Any]:
    """Stub doctor + service bootstrap for ``awf init`` (no-path) tests."""

    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service import doctor as doctor_mod

    captured: dict[str, Any] = {"bootstrap_calls": []}
    settings = object()

    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda: settings)
    captured["settings"] = settings

    docker_diag = _docker_diagnostic(docker_status)
    report = _doctor_report(docker_diag)

    async def _collect_doctor_report(_settings: object, **kwargs: Any) -> Any:
        captured["doctor_kwargs"] = kwargs
        captured["doctor_settings"] = _settings
        return report

    monkeypatch.setattr(doctor_mod, "collect_doctor_report", _collect_doctor_report)

    if bootstrap_result is None:
        from awf.service.bootstrap import ServiceBootstrapResult

        bootstrap_result = ServiceBootstrapResult(
            stages=(),
            service_status={"service": "awf", "status": "ok", "checks": {}},
        )

    async def _bootstrap(received_settings: Any, **kwargs: Any) -> Any:
        captured["bootstrap_calls"].append({"settings": received_settings, **kwargs})
        if bootstrap_error is not None:
            raise bootstrap_error
        return bootstrap_result

    monkeypatch.setattr(bootstrap_mod, "run_service_bootstrap", _bootstrap)
    return captured


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


# ── Bootstrap-mode tests (awf init with no path) ────────────────────────


@pytest.mark.unit
def test_init_without_path_runs_service_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(state_dir))
    captured = _stub_bootstrap_mode(monkeypatch)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert "AWF init: local service bootstrap" in result.output
    assert str(state_dir.resolve()) in result.output
    assert "awf service status" in result.output
    assert "AWF_GITHUB_TOKEN" in result.output
    assert "awf init <path>" in result.output
    assert len(captured["bootstrap_calls"]) == 1


@pytest.mark.unit
def test_init_without_path_seeds_env_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    example = tmp_path / ".env.example"
    example.write_text("AWF_API_TOKEN=local\n", encoding="utf-8")
    _stub_bootstrap_mode(monkeypatch)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    env_file = tmp_path / ".env"
    assert env_file.exists()
    assert env_file.read_bytes() == example.read_bytes()
    assert "wrote .env" in result.output


@pytest.mark.unit
def test_init_without_path_does_not_overwrite_existing_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    example = tmp_path / ".env.example"
    example.write_text("AWF_API_TOKEN=example\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("AWF_API_TOKEN=already_set\n", encoding="utf-8")
    _stub_bootstrap_mode(monkeypatch)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert env_file.read_text(encoding="utf-8") == "AWF_API_TOKEN=already_set\n"
    assert "kept existing .env" in result.output


@pytest.mark.unit
def test_init_without_path_no_write_env_flag_skips_seeding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    (tmp_path / ".env.example").write_text("AWF_API_TOKEN=local\n", encoding="utf-8")
    _stub_bootstrap_mode(monkeypatch)

    result = _runner.invoke(app, ["init", "--no-write-env"])

    assert result.exit_code == 0, result.output
    assert not (tmp_path / ".env").exists()
    assert "wrote .env" not in result.output


@pytest.mark.unit
def test_init_without_path_warns_when_env_example_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    _stub_bootstrap_mode(monkeypatch)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert not (tmp_path / ".env").exists()
    assert "no .env.example found" in result.output
    assert "AWF repository root" in result.output


@pytest.mark.unit
def test_init_without_path_runs_docker_availability_check_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    captured = _stub_bootstrap_mode(monkeypatch, docker_status="fail")

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 1, result.output
    assert captured["bootstrap_calls"] == []
    assert "Docker is installed but the daemon is not reachable" in result.output
    assert not (tmp_path / "state").exists()


@pytest.mark.unit
def test_init_without_path_fails_when_docker_diagnostic_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service import doctor as doctor_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))

    bootstrap_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda: object())

    report = _doctor_report()  # No diagnostics: no docker entry.

    async def _collect_doctor_report(_settings: object, **_kwargs: Any) -> Any:
        return report

    async def _bootstrap(received_settings: Any, **kwargs: Any) -> Any:
        bootstrap_calls.append({"settings": received_settings, **kwargs})
        return None

    monkeypatch.setattr(doctor_mod, "collect_doctor_report", _collect_doctor_report)
    monkeypatch.setattr(bootstrap_mod, "run_service_bootstrap", _bootstrap)

    result = _runner.invoke(app, ["init", "--format", "json"])

    assert result.exit_code == 1, result.output
    assert bootstrap_calls == []
    payload = json.loads(result.output)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "DOCKER_DIAGNOSTIC_MISSING"
    assert not (tmp_path / "state").exists()


@pytest.mark.unit
def test_init_without_path_passes_strict_provider_options_to_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    captured = _stub_bootstrap_mode(monkeypatch)

    result = _runner.invoke(
        app,
        ["init", "--provider", "github", "--provider", "opencode"],
    )

    assert result.exit_code == 0, result.output
    options = captured["bootstrap_calls"][0]["options"]
    assert options.strict_providers == frozenset({"github", "opencode"})


@pytest.mark.unit
def test_init_without_path_passes_skip_agent_runtime_build_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    captured = _stub_bootstrap_mode(monkeypatch)

    result = _runner.invoke(app, ["init", "--skip-agent-runtime-build"])

    assert result.exit_code == 0, result.output
    options = captured["bootstrap_calls"][0]["options"]
    assert options.skip_agent_runtime_build is True


@pytest.mark.unit
def test_init_without_path_handles_bootstrap_failure_without_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from awf.service.bootstrap import ServiceBootstrapError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    error = ServiceBootstrapError(
        reason_code="SERVICE_BOOTSTRAP_TIMEOUT",
        message="timed out waiting for local service readiness",
        last_status={"status": "fail", "checks": {"api": {"reason": "API_UNREACHABLE"}}},
    )
    _stub_bootstrap_mode(monkeypatch, bootstrap_error=error)

    result = _runner.invoke(app, ["init", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "SERVICE_BOOTSTRAP_TIMEOUT"
    assert payload["last_status"]["status"] == "fail"
    combined = f"{result.stdout}{getattr(result, 'stderr', '')}"
    assert "Traceback" not in combined


@pytest.mark.unit
def test_init_without_path_rejects_unknown_provider_without_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))

    result = _runner.invoke(app, ["init", "--provider", "bogus"])

    output = f"{result.stdout}{getattr(result, 'stderr', '')}"
    assert result.exit_code == 2
    assert "error: unknown provider(s): bogus" in output
    assert "Traceback" not in output


@pytest.mark.unit
def test_init_without_path_ensures_state_directory_and_prints_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    state_dir = tmp_path / "state-fresh"
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(state_dir))
    _stub_bootstrap_mode(monkeypatch)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert state_dir.exists()
    assert state_dir.is_dir()
    assert str(state_dir.resolve()) in result.output


@pytest.mark.unit
def test_init_with_path_keeps_existing_project_onboarding_behavior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_local_prerequisites(monkeypatch)

    result = _runner.invoke(app, ["init", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "AWF init: local onboarding readiness check" in result.output
    assert "awf profile init" in result.output


@pytest.mark.unit
def test_init_with_path_does_not_invoke_service_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from awf.service import bootstrap as bootstrap_mod

    _stub_local_prerequisites(monkeypatch)

    async def _bootstrap(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("run_service_bootstrap should not be called in path mode")

    monkeypatch.setattr(bootstrap_mod, "run_service_bootstrap", _bootstrap)

    result = _runner.invoke(app, ["init", str(tmp_path)])

    assert result.exit_code == 0, result.output


@pytest.mark.unit
def test_init_with_path_rejects_bootstrap_only_flags_with_clear_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_local_prerequisites(monkeypatch)

    result = _runner.invoke(
        app,
        ["init", str(tmp_path), "--skip-agent-runtime-build"],
    )

    output = f"{result.stdout}{getattr(result, 'stderr', '')}"
    assert result.exit_code == 2
    assert "--skip-agent-runtime-build" in output
    assert "without a project path" in output or "no path" in output
    assert "Traceback" not in output


@pytest.mark.unit
def test_init_with_path_rejects_no_write_env_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_local_prerequisites(monkeypatch)

    result = _runner.invoke(
        app,
        ["init", str(tmp_path), "--no-write-env"],
    )

    output = f"{result.stdout}{getattr(result, 'stderr', '')}"
    assert result.exit_code == 2
    assert "--no-write-env" in output
    assert "without a project path" in output or "no path" in output
    assert "Traceback" not in output


@pytest.mark.unit
def test_init_with_path_rejects_format_json_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_local_prerequisites(monkeypatch)

    result = _runner.invoke(
        app,
        ["init", str(tmp_path), "--format", "json"],
    )

    output = f"{result.stdout}{getattr(result, 'stderr', '')}"
    assert result.exit_code == 2
    assert "--format" in output
    assert "without a project path" in output or "no path" in output
    assert "Traceback" not in output


@pytest.mark.unit
def test_init_with_path_rejects_explicit_default_bootstrap_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit-but-default bootstrap flags must be rejected, not silently ignored."""

    _stub_local_prerequisites(monkeypatch)

    cases = [
        (["--timeout-seconds", "180"], "--timeout-seconds"),
        (["--poll-interval-seconds", "2"], "--poll-interval-seconds"),
        (["--write-env"], "--write-env"),
        (["--format", "pretty"], "--format"),
    ]
    for extra, expected_flag in cases:
        result = _runner.invoke(app, ["init", str(tmp_path), *extra])

        output = f"{result.stdout}{getattr(result, 'stderr', '')}"
        assert result.exit_code == 2, f"expected exit 2 for {extra}: {output}"
        assert expected_flag in output, f"expected {expected_flag} in error for {extra}: {output}"
        assert "Traceback" not in output


@pytest.mark.unit
def test_init_without_path_rejects_include_smoke_request_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_to_resolve_service_settings() -> object:
        raise AssertionError("should not enter bootstrap mode")

    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        _fail_to_resolve_service_settings,
    )

    result = _runner.invoke(app, ["init", "--include-smoke-request"])

    output = f"{result.stdout}{getattr(result, 'stderr', '')}"
    assert result.exit_code == 2
    assert "--include-smoke-request" in output
    assert "project path" in output
    assert "Traceback" not in output


@pytest.mark.unit
def test_readme_recommends_awf_init_for_local_bootstrap() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "awf init" in readme
    assert "awf init <path>" in readme
    assert "awf service status --format pretty" in readme


@pytest.mark.unit
def test_project_onboarding_doc_distinguishes_init_modes() -> None:
    doc = Path("docs/PROJECT_ONBOARDING.md").read_text(encoding="utf-8")

    assert "awf init" in doc
    assert "awf init <path>" in doc
    assert "AWF-on-this-machine" in doc or "local service bootstrap" in doc


@pytest.mark.unit
def test_project_onboarding_doc_has_provider_prompts() -> None:
    """Regression: every supported provider has a copy-paste prompt block."""
    readme = Path("README.md").read_text(encoding="utf-8")
    doc = Path("docs/PROJECT_ONBOARDING.md").read_text(encoding="utf-8")

    # README links to the onboarding doc
    assert "docs/PROJECT_ONBOARDING.md" in readme

    providers = ["Codex", "Claude Code", "Gemini", "OpenCode", "OpenClaw"]
    for provider in providers:
        # Each provider must have a clear heading
        assert f"### {provider}" in doc, f"missing heading for {provider}"

    # Generic fallback prompt must still exist
    assert "## One-message prompt" in doc

    # Each provider block must contain the onboarding keyword set
    keyword_set = [".awf/workspace.yml", "awf profile preview", "smoke", "implement"]
    for provider in providers:
        start = doc.find(f"### {provider}")
        assert start != -1
        # Grab the block up to the next ### or end of file
        end = doc.find("###", start + 1)
        block = doc[start:end] if end != -1 else doc[start:]
        for keyword in keyword_set:
            assert keyword in block, f"{provider} prompt missing {keyword}"
