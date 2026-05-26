"""Focused coverage for service CLI edge branches."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from awf.cli import service_commands
from awf.cli.common import OutputFormat
from awf.common.commands import CommandResult
from awf.service.bootstrap import ServiceBootstrapError
from awf.service.logs import ServiceLogsError, ServiceLogsResult
from awf.service.target_branch_monitor import TargetBranchMonitorError


def _patch_compose_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    compose = tmp_path / "local-service.yml"
    env = tmp_path / ".env"
    compose.write_text("services: {}\n", encoding="utf-8")
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        service_commands,
        "_resolve_service_compose_paths",
        lambda: (compose, env, env),
    )
    monkeypatch.setattr(
        service_commands,
        "_resolve_service_runtime_env_files",
        lambda *_args, **_kwargs: (env, env),
    )
    monkeypatch.setattr("awf.service.config.local_service_environ", lambda **_kwargs: {})


@pytest.mark.unit
def test_service_logs_prints_stdout_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _patch_compose_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "awf.service.logs.run_service_logs",
        lambda **_kwargs: ServiceLogsResult(stdout="out\n", stderr="err\n"),
    )

    service_commands.service_logs(tail=0, service=[], follow=False)

    captured = capsys.readouterr()
    assert captured.out == "out\n"
    assert captured.err == "err\n"


@pytest.mark.unit
def test_service_logs_surfaces_service_log_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _patch_compose_env(monkeypatch, tmp_path)

    def _raise_logs_error(**_kwargs: object) -> ServiceLogsResult:
        raise ServiceLogsError(returncode=9, detail="compose failed")

    monkeypatch.setattr("awf.service.logs.run_service_logs", _raise_logs_error)

    with pytest.raises(typer.Exit) as excinfo:
        service_commands.service_logs(tail=0, service=[], follow=False)

    assert excinfo.value.exit_code == 1
    assert "compose failed" in capsys.readouterr().err


@pytest.mark.unit
def test_service_logs_handles_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_compose_env(monkeypatch, tmp_path)

    def _interrupt(**_kwargs: object) -> ServiceLogsResult:
        raise KeyboardInterrupt

    monkeypatch.setattr("awf.service.logs.run_service_logs", _interrupt)

    with pytest.raises(typer.Exit) as excinfo:
        service_commands.service_logs(tail=0, service=[], follow=False)

    assert excinfo.value.exit_code == 130


@pytest.mark.unit
def test_target_branch_monitor_error_is_emitted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payloads: list[object] = []

    async def _raise_target_branch_error(**_kwargs: object) -> object:
        result = CommandResult(returncode=2, stdout="out", stderr="bad")
        raise TargetBranchMonitorError(operation="fetch", result=result)

    monkeypatch.setattr(
        "awf.service.target_branch_monitor.run_target_branch_reconcile_once",
        _raise_target_branch_error,
    )
    monkeypatch.setattr(service_commands, "_emit", lambda payload, _fmt: payloads.append(payload))

    with pytest.raises(typer.Exit) as excinfo:
        service_commands.service_reconcile_target(
            repo_url="git@example.com:repo/app.git",
            branch="development",
            work_dir=tmp_path,
            dry_run=True,
            fmt=OutputFormat.json,
        )

    assert excinfo.value.exit_code == 1
    assert payloads == [
        {
            "status": "failed",
            "operation": "fetch",
            "returncode": 2,
            "stdout": "out",
            "stderr": "bad",
        }
    ]


@pytest.mark.unit
def test_service_status_invalid_provider_exits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _validate(_providers: list[str]) -> list[str]:
        from awf.service.provider_readiness import ProviderReadinessError

        raise ProviderReadinessError("bad provider")

    monkeypatch.setattr("awf.service.provider_readiness.validate_provider_names", _validate)

    with pytest.raises(typer.Exit) as excinfo:
        service_commands.service_status(fmt=OutputFormat.json, provider=["bad"])

    assert excinfo.value.exit_code == 2
    assert "bad provider" in capsys.readouterr().err


@pytest.mark.unit
def test_service_status_emits_payload_and_exits_on_failed_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_compose_env(monkeypatch, tmp_path)
    payloads: list[object] = []

    monkeypatch.setattr("awf.common.config.Settings", lambda **_kwargs: object())
    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(
        "awf.service.provider_readiness.validate_provider_names", lambda providers: providers
    )

    async def _collect_service_status(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"status": "fail", "reason_code": "NOT_READY"}

    monkeypatch.setattr("awf.service.status.collect_service_status", _collect_service_status)
    monkeypatch.setattr(service_commands, "_emit", lambda payload, _fmt: payloads.append(payload))

    with pytest.raises(typer.Exit) as excinfo:
        service_commands.service_status(fmt=OutputFormat.json, provider=["codex"])

    assert excinfo.value.exit_code == 1
    assert payloads == [{"status": "fail", "reason_code": "NOT_READY"}]


@pytest.mark.unit
def test_service_release_readiness_invalid_provider_exits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _validate(_providers: list[str]) -> list[str]:
        from awf.service.provider_readiness import ProviderReadinessError

        raise ProviderReadinessError("bad provider")

    monkeypatch.setattr("awf.service.provider_readiness.validate_provider_names", _validate)

    with pytest.raises(typer.Exit) as excinfo:
        service_commands.service_readiness(fmt=OutputFormat.json, provider=["bad"])

    assert excinfo.value.exit_code == 2
    assert "bad provider" in capsys.readouterr().err


@pytest.mark.unit
def test_service_bootstrap_handles_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_compose_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr("awf.common.config.Settings", lambda **_kwargs: object())
    monkeypatch.setattr(
        "awf.service.bootstrap.ServiceBootstrapOptions",
        lambda **_kwargs: SimpleNamespace(**_kwargs),
    )

    async def _interrupt(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr("awf.service.bootstrap.run_service_bootstrap", _interrupt)

    with pytest.raises(typer.Exit) as excinfo:
        service_commands.service_bootstrap(fmt=OutputFormat.json, provider=[])

    assert excinfo.value.exit_code == 130


@pytest.mark.unit
def test_service_bootstrap_emits_structured_error_and_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_compose_env(monkeypatch, tmp_path)
    payloads: list[object] = []
    results = iter(
        [
            ServiceBootstrapError(
                reason_code="BOOTSTRAP_FAILED",
                message="compose up failed",
                stage="compose_up",
                returncode=2,
                stderr="bad compose",
            ),
            SimpleNamespace(to_dict=lambda: {"status": "ok"}),
        ]
    )

    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr("awf.common.config.Settings", lambda **_kwargs: object())
    monkeypatch.setattr(
        "awf.service.bootstrap.ServiceBootstrapOptions",
        lambda **_kwargs: SimpleNamespace(**_kwargs),
    )
    monkeypatch.setattr(
        "awf.service.provider_readiness.validate_provider_names", lambda providers: providers
    )

    async def _run_bootstrap(*_args: object, **_kwargs: object) -> object:
        result = next(results)
        if isinstance(result, ServiceBootstrapError):
            raise result
        return result

    monkeypatch.setattr("awf.service.bootstrap.run_service_bootstrap", _run_bootstrap)
    monkeypatch.setattr(service_commands, "_emit", lambda payload, _fmt: payloads.append(payload))

    with pytest.raises(typer.Exit) as excinfo:
        service_commands.service_bootstrap(fmt=OutputFormat.json, provider=["codex"])

    assert excinfo.value.exit_code == 1
    assert payloads[-1]["reason_code"] == "BOOTSTRAP_FAILED"
    assert payloads[-1]["stderr"] == "bad compose"

    service_commands.service_bootstrap(fmt=OutputFormat.json, provider=["codex"])
    assert payloads[-1] == {"status": "ok"}
