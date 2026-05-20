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


@pytest.mark.unit
def test_init_profile_marker_paths_are_shared_with_smoke_service() -> None:
    from awf.cli import main as cli_main
    from awf.service import smoke

    assert cli_main._PROJECT_PROFILE_MARKER_PATHS is smoke._PROFILE_MARKER_PATHS


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

    overall = "ok" if all(getattr(d, "status", "ok") == "ok" for d in diagnostics) else "fail"
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
    asset_root: Path | None = None,
) -> dict[str, Any]:
    """Stub doctor + service bootstrap for ``awf init`` (no-path) tests."""

    from awf.common import config as common_config
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service import doctor as doctor_mod

    captured: dict[str, Any] = {"bootstrap_calls": [], "settings_instances": []}
    settings = object()

    class StubSettings:
        """Minimal Settings double for helper-backed bootstrap tests."""

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            captured["settings_instances"].append(self)

    monkeypatch.setattr(common_config, "Settings", StubSettings)

    def _resolve_service_settings(*_args: object, **_kwargs: object) -> object:
        return settings

    monkeypatch.setattr(config_mod, "resolve_service_settings", _resolve_service_settings)
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
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: asset_root)
    return captured


def _fail_path_write(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failing_path: str,
    message: str = "permission denied",
) -> None:
    """Patch Path.open to fail writes for one expected path."""
    original_open = Path.open
    failing_path_resolved = Path(failing_path).resolve()

    def _open(
        self: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> Any:
        """Raise a synthetic write failure only for the configured path."""
        if self.resolve() == failing_path_resolved and {"w", "a", "x", "+"}.intersection(mode):
            raise OSError(message)
        return original_open(
            self,
            mode=mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    monkeypatch.setattr(Path, "open", _open)


def _create_path_before_exclusive_open(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target_path: str,
    contents: bytes,
) -> None:
    """Create a path just before an exclusive open attempts to seed it."""
    original_open = Path.open
    target_path_resolved = Path(target_path).resolve()

    def _open(
        self: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> Any:
        if self.resolve() == target_path_resolved and "x" in mode and not self.exists():
            with original_open(self, "wb") as handle:
                handle.write(contents)
        return original_open(
            self,
            mode=mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    monkeypatch.setattr(Path, "open", _open)


def _fail_path_read_bytes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failing_path: str,
    message: str = "permission denied",
) -> None:
    """Patch Path.read_bytes to fail for one expected path."""
    original_read_bytes = Path.read_bytes
    failing_path_resolved = Path(failing_path).resolve()

    def _read_bytes(self: Path) -> bytes:
        """Raise a synthetic read failure only for the configured path."""
        if self.resolve() == failing_path_resolved:
            raise OSError(message)
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _read_bytes)


def _fail_path_mkdir(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failing_path: str,
    message: str = "permission denied",
) -> None:
    """Patch Path.mkdir to fail for one expected path."""
    original_mkdir = Path.mkdir
    failing_path_resolved = Path(failing_path).resolve()

    def _mkdir(
        self: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        """Raise a synthetic mkdir failure only for the configured path."""
        if self.resolve() == failing_path_resolved:
            raise OSError(message)
        original_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", _mkdir)


def _stub_local_prerequisites(
    monkeypatch: pytest.MonkeyPatch,
    *,
    service_status: str = "ok",
    doctor_status: str = "ok",
    preview_smoke_payload: bool = False,
) -> None:
    async def _collect_service_status(_settings: object, **kwargs: object) -> dict[str, object]:
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

        def _preview_project_onboarding(_path: Path, **_kwargs: object) -> object:
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
def test_init_write_env_help_names_compose_target() -> None:
    """Document the concrete Compose env target in init help text."""
    result = _runner.invoke(app, ["init", "--help"], env={"COLUMNS": "240"})

    assert result.exit_code == 0, result.output
    assert "docker/compose/.env" in result.output


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
        *_args: object,
        **_kwargs: object,
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
def test_init_existing_profile_does_not_suggest_profile_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_local_prerequisites(monkeypatch)
    profile_path = tmp_path / ".awf" / "workspace.yml"
    profile_path.parent.mkdir()
    profile_path.write_text("version: 1\nname: existing\n", encoding="utf-8")

    result = _runner.invoke(app, ["init", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "profile already exists" in result.output
    assert "awf profile preview" in result.output
    assert "awf smoke run --mocked-local --format pretty" in result.output
    assert not any(
        line.strip().startswith("awf profile init") and "--write" in line
        for line in result.output.splitlines()
    )


@pytest.mark.unit
def test_init_runs_status_and_doctor_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"status": 0, "doctor": 0}

    async def _collect_service_status(_settings: object, **_kwargs: object) -> dict[str, object]:
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

    async def _collect_service_status(_settings: object, **_kwargs: object) -> dict[str, object]:
        calls["status"] += 1
        raise RuntimeError("service probe is unavailable")

    async def _collect_doctor_report(
        _settings: object,
        **kwargs: object,
    ) -> object:
        calls["doctor"] += 1
        status_collector = kwargs["status_collector"]
        status = await status_collector(
            _settings, strict_providers=frozenset(), provider_environ={}
        )
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

    async def _collect_service_status(_settings: object, **_kwargs: object) -> dict[str, object]:
        calls["status"] += 1
        return status_payload

    async def _collect_doctor_report(
        _settings: object,
        **kwargs: object,
    ) -> object:
        calls["doctor"] += 1
        status_collector = kwargs["status_collector"]
        collected_status = await status_collector(
            _settings, strict_providers=frozenset(), provider_environ={}
        )
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
def test_init_includes_smoke_workspace_hints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
def test_resolve_service_compose_paths_returns_absolute_asset_root_paths_from_root_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verified AWF asset paths should not depend on launch directory shape."""
    from awf.cli import main as cli_main
    from awf.service import bootstrap as bootstrap_mod

    monkeypatch.chdir(tmp_path)
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env.example").write_text("AWF_API_TOKEN=compose\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: tmp_path)

    compose_file, env_file, env_example = cli_main._resolve_service_compose_paths()  # noqa: SLF001

    assert compose_file == compose / "local-service.yml"
    assert compose_file.is_absolute()
    assert env_file == compose / ".env"
    assert env_file.is_absolute()
    assert env_example == compose / ".env.example"
    assert env_example.is_absolute()


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
    assert captured["bootstrap_calls"][0]["env_file"] is None


@pytest.mark.unit
def test_stub_bootstrap_mode_replaces_settings_constructor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Helper-backed bootstrap tests should not depend on real Settings fields."""
    from awf.common import config as common_config

    class ExplodingSettings:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("real Settings constructor should be stubbed")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(common_config, "Settings", ExplodingSettings)
    captured = _stub_bootstrap_mode(monkeypatch)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert len(captured["settings_instances"]) == 1
    assert captured["settings_instances"][0].kwargs == {
        "_env_file": Path(".env"),
        "github_token": None,
    }


@pytest.mark.unit
def test_init_without_path_seeds_source_compose_env_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify init seeds the compose env target when missing."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    example = tmp_path / ".env.example"
    example.write_text("AWF_API_TOKEN=local\n", encoding="utf-8")
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    env_file = compose / ".env"
    assert env_file.exists()
    assert env_file.read_bytes() == example.read_bytes()
    assert "wrote docker/compose/.env from .env.example" in result.output
    assert not (tmp_path / ".env").exists()


@pytest.mark.unit
def test_init_without_path_prefers_compose_env_example_over_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Prefer compose `.env.example` over root when seeding bootstrap env."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    compose_example = compose / ".env.example"
    compose_example.write_text("AWF_API_TOKEN=compose\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("AWF_API_TOKEN=root\n", encoding="utf-8")
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    env_file = compose / ".env"
    assert env_file.exists()
    assert env_file.read_bytes() == compose_example.read_bytes()
    assert "wrote docker/compose/.env from docker/compose/.env.example" in result.output
    assert not (tmp_path / ".env").exists()


@pytest.mark.unit
def test_init_without_path_merges_existing_root_env_into_source_compose_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Preserve root `.env` values without dropping compose-only template keys."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    compose_example = compose / ".env.example"
    compose_example.write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=compose-example",
                "AWF_POSTGRES_PASSWORD=compose-example",
                "AWF_COMPOSE_ONLY=compose-default",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    root_example = tmp_path / ".env.example"
    root_example.write_text(
        "AWF_API_TOKEN=root-example\nAWF_POSTGRES_PASSWORD=root-example\n",
        encoding="utf-8",
    )
    root_env = tmp_path / ".env"
    root_env.write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "AWF_POSTGRES_PASSWORD=migrated-password",
                "",
                "# Custom docker socket for local service bootstrap",
                "AWF_DOCKER_HOST=unix:///tmp/awf-docker.sock",
                "AWF_ROOT_ONLY=root-value",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    env_file = compose / ".env"
    assert env_file.exists()
    assert env_file.read_text(encoding="utf-8") == (
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "AWF_POSTGRES_PASSWORD=migrated-password",
                "AWF_COMPOSE_ONLY=compose-default",
                "",
                "# Custom docker socket for local service bootstrap",
                "AWF_DOCKER_HOST=unix:///tmp/awf-docker.sock",
                "AWF_ROOT_ONLY=root-value",
            ]
        )
        + "\n"
    )
    assert "wrote docker/compose/.env from docker/compose/.env.example" in result.output
    assert "migrated-token" not in result.output
    assert "migrated-password" not in result.output


@pytest.mark.unit
def test_init_without_path_reports_overlay_only_keys_without_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Audit root-only keys copied into compose env without leaking values."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env.example").write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=compose-example",
                "AWF_COMPOSE_ONLY=compose-default",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "CI_DEPLOY_TOKEN=super-secret-ci-token",
                "AWF_ROOT_ONLY=root-only-secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert (
        "added root .env keys to docker/compose/.env: CI_DEPLOY_TOKEN, AWF_ROOT_ONLY"
        in result.output
    )
    assert "super-secret-ci-token" not in result.output
    assert "root-only-secret" not in result.output


@pytest.mark.unit
def test_init_without_path_json_reports_overlay_only_keys_without_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expose root-only copied key names in JSON without exposing values."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env.example").write_text("AWF_API_TOKEN=compose-example\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "CI_DEPLOY_TOKEN=super-secret-ci-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["env_overlay_keys"] == ["CI_DEPLOY_TOKEN"]
    assert "super-secret-ci-token" not in result.output


@pytest.mark.unit
def test_init_without_path_preserves_root_env_file_header_at_top(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep root `.env` file-header comments at the top of seeded compose env."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env.example").write_text(
        "\n".join(
            [
                "AWF_POSTGRES_PASSWORD=compose-example",
                "AWF_API_TOKEN=compose-example",
                "AWF_COMPOSE_ONLY=compose-default",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "# Existing root .env migrated by awf init.",
                "# Operators may keep local service overrides here.",
                "AWF_API_TOKEN=migrated-token",
                "AWF_ROOT_ONLY=root-value",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert (compose / ".env").read_text(encoding="utf-8") == (
        "\n".join(
            [
                "# Existing root .env migrated by awf init.",
                "# Operators may keep local service overrides here.",
                "AWF_POSTGRES_PASSWORD=compose-example",
                "AWF_API_TOKEN=migrated-token",
                "AWF_COMPOSE_ONLY=compose-default",
                "AWF_ROOT_ONLY=root-value",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_init_without_path_avoids_duplicate_overlay_and_seed_file_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Do not prepend the root header when the seed already has a file header."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env.example").write_text(
        "\n".join(
            [
                "# Compose service defaults.",
                "# Keep local service settings here.",
                "AWF_API_TOKEN=compose-example",
                "AWF_COMPOSE_ONLY=compose-default",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "# Existing root .env migrated by awf init.",
                "# Operators may keep workspace overrides here.",
                "AWF_API_TOKEN=migrated-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert (compose / ".env").read_text(encoding="utf-8") == (
        "\n".join(
            [
                "# Compose service defaults.",
                "# Keep local service settings here.",
                "AWF_API_TOKEN=migrated-token",
                "AWF_COMPOSE_ONLY=compose-default",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_init_without_path_avoids_single_overlay_header_when_seed_has_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Treat a first overlay comment as redundant when seed has a header."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env.example").write_text(
        "\n".join(
            [
                "# Compose service defaults.",
                "AWF_API_TOKEN=compose-example",
                "AWF_COMPOSE_ONLY=compose-default",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "# Custom local settings",
                "AWF_API_TOKEN=migrated-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert (compose / ".env").read_text(encoding="utf-8") == (
        "\n".join(
            [
                "# Compose service defaults.",
                "AWF_API_TOKEN=migrated-token",
                "AWF_COMPOSE_ONLY=compose-default",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_init_without_path_keeps_single_leading_comment_with_overlay_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A single leading comment should stay with the shared overlay key."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env.example").write_text(
        "\n".join(
            [
                "AWF_POSTGRES_PASSWORD=compose-example",
                "AWF_API_TOKEN=compose-example",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "# Existing API token override",
                "AWF_API_TOKEN=migrated-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert (compose / ".env").read_text(encoding="utf-8") == (
        "\n".join(
            [
                "AWF_POSTGRES_PASSWORD=compose-example",
                "# Existing API token override",
                "AWF_API_TOKEN=migrated-token",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_init_without_path_preserves_root_env_header_before_overlay_only_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep root `.env` headers at the top even before root-only keys."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env.example").write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=compose-example",
                "AWF_COMPOSE_ONLY=compose-default",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "# Existing root .env migrated by awf init.",
                "# Operators may keep local service overrides here.",
                "CI_TOKEN=root-ci-token",
                "DEPLOY_ENV=local",
                "AWF_API_TOKEN=migrated-token",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert (compose / ".env").read_text(encoding="utf-8") == (
        "\n".join(
            [
                "# Existing root .env migrated by awf init.",
                "# Operators may keep local service overrides here.",
                "AWF_API_TOKEN=migrated-token",
                "AWF_COMPOSE_ONLY=compose-default",
                "CI_TOKEN=root-ci-token",
                "DEPLOY_ENV=local",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_init_without_path_merges_root_env_into_root_example_when_compose_example_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Preserve root-template-only defaults while applying existing root env values."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=root-example",
                "AWF_POSTGRES_PASSWORD=root-example",
                "AWF_TEMPLATE_ONLY=root-default",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "",
                "# Existing operator override",
                "AWF_DOCKER_HOST=unix:///tmp/awf-docker.sock",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert (compose / ".env").read_text(encoding="utf-8") == (
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "AWF_POSTGRES_PASSWORD=root-example",
                "AWF_TEMPLATE_ONLY=root-default",
                "",
                "# Existing operator override",
                "AWF_DOCKER_HOST=unix:///tmp/awf-docker.sock",
            ]
        )
        + "\n"
    )
    assert "wrote docker/compose/.env from .env.example" in result.output
    assert "migrated-token" not in result.output


@pytest.mark.unit
def test_init_without_path_preserves_context_before_seed_overlay_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep root `.env` comments attached to seed-template override keys."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env.example").write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=compose-example",
                "AWF_DOCKER_HOST=",
                "AWF_COMPOSE_ONLY=compose-default",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    docker_host = f"unix://{tmp_path / 'docker.sock'}"
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "",
                "# My custom Docker host",
                "AWF_DOCKER_HOST=" + docker_host,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert (compose / ".env").read_text(encoding="utf-8") == (
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "",
                "# My custom Docker host",
                "AWF_DOCKER_HOST=" + docker_host,
                "AWF_COMPOSE_ONLY=compose-default",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_init_without_path_deduplicates_root_only_overlay_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep dotenv last-value semantics while avoiding repeated root-only keys."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env.example").write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=compose-example",
                "AWF_COMPOSE_ONLY=compose-default",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "",
                "# Stale root-only service setting",
                "AWF_ROOT_ONLY=stale-value",
                "",
                "# Final root-only service setting",
                "AWF_ROOT_ONLY=final-value",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert (compose / ".env").read_text(encoding="utf-8") == (
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "AWF_COMPOSE_ONLY=compose-default",
                "",
                "# Final root-only service setting",
                "AWF_ROOT_ONLY=final-value",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_init_without_path_preserves_trailing_root_env_overlay_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep comments that trail the final root-only assignment during merge."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env.example").write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=compose-example",
                "AWF_COMPOSE_ONLY=compose-default",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "",
                "# Root-only service setting",
                "AWF_ROOT_ONLY=root-value",
                "",
                "# Keep this note with the migrated root-only setting",
                "# It documents why AWF_ROOT_ONLY exists.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert (compose / ".env").read_text(encoding="utf-8") == (
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "AWF_COMPOSE_ONLY=compose-default",
                "",
                "# Root-only service setting",
                "AWF_ROOT_ONLY=root-value",
                "",
                "# Keep this note with the migrated root-only setting",
                "# It documents why AWF_ROOT_ONLY exists.",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_init_without_path_preserves_comment_only_root_env_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep comment-only root env overlays as operator context."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env.example").write_text(
        "AWF_API_TOKEN=compose-example\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "# Operator left this file as documentation.",
                "# Keep this note in the seeded compose env.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert (compose / ".env").read_text(encoding="utf-8") == (
        "\n".join(
            [
                "AWF_API_TOKEN=compose-example",
                "# Operator left this file as documentation.",
                "# Keep this note in the seeded compose env.",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_init_without_path_keeps_trailing_shared_overlay_context_with_seed_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep final shared-key overlay comments adjacent to the overlaid seed key."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env.example").write_text(
        "\n".join(
            [
                "AWF_API_TOKEN=compose-example",
                "AWF_COMPOSE_ONLY=compose-default",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "# Root-only service setting",
                "AWF_ROOT_ONLY=root-value",
                "AWF_API_TOKEN=migrated-token",
                "",
                "# Keep this note with the migrated API token.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert (compose / ".env").read_text(encoding="utf-8") == (
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "",
                "# Keep this note with the migrated API token.",
                "AWF_COMPOSE_ONLY=compose-default",
                "# Root-only service setting",
                "AWF_ROOT_ONLY=root-value",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_init_without_path_does_not_seed_non_root_compose_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only seed compose env files from verified AWF roots."""
    unrelated_root = tmp_path / "unrelated-repo"
    compose = unrelated_root / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    compose_example = compose / ".env.example"
    compose_example.write_text("AWF_API_TOKEN=wrong\n", encoding="utf-8")

    workspace_root = tmp_path / "workspace-root"
    workspace_compose = workspace_root / "docker" / "compose"
    workspace_compose.mkdir(parents=True)
    (workspace_compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    workspace_example = workspace_root / ".env.example"
    workspace_example.write_text("AWF_API_TOKEN=correct\n", encoding="utf-8")

    monkeypatch.chdir(unrelated_root)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    _stub_bootstrap_mode(monkeypatch, asset_root=workspace_root)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    expected_env_file = workspace_compose / ".env"
    assert expected_env_file.exists()
    assert expected_env_file.read_bytes() == workspace_example.read_bytes()
    assert not (compose / ".env").exists()
    assert (
        "wrote ../workspace-root/docker/compose/.env from ../workspace-root/.env.example"
    ) in result.output


@pytest.mark.unit
def test_init_without_path_does_not_seed_current_compose_dir_without_asset_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Do not treat current-directory compose files as verified AWF assets."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))

    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    compose_example = compose / ".env.example"
    compose_example.write_text("AWF_API_TOKEN=wrong\n", encoding="utf-8")
    root_example = tmp_path / ".env.example"
    root_example.write_text("AWF_API_TOKEN=root\n", encoding="utf-8")
    _stub_bootstrap_mode(monkeypatch, asset_root=None)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert not (compose / ".env").exists()
    env_file = tmp_path / ".env"
    assert env_file.exists()
    assert env_file.read_bytes() == root_example.read_bytes()
    assert "wrote .env from .env.example" in result.output


@pytest.mark.unit
def test_service_env_resolution_ignores_current_compose_env_without_asset_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default resolution remains conservative for init/bootstrap seeding."""
    from awf.cli import main as cli_main
    from awf.service import bootstrap as bootstrap_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)

    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env").write_text("AWF_API_TOKEN=wrong-project\n", encoding="utf-8")

    active_env_file, compose_env_file = cli_main._resolve_service_env_files(Path(".env"))  # noqa: SLF001

    assert active_env_file == Path(".env")
    assert compose_env_file is None


@pytest.mark.unit
def test_service_env_resolution_does_not_forward_root_env_without_asset_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Root `.env` remains a read source, not a Docker Compose `--env-file`."""
    from awf.cli import main as cli_main
    from awf.service import bootstrap as bootstrap_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)

    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text("GITHUB_TOKEN=operator-secret\n", encoding="utf-8")

    active_env_file, compose_env_file = cli_main._resolve_service_env_files(Path(".env"))  # noqa: SLF001

    assert active_env_file == Path(".env")
    assert compose_env_file is None


@pytest.mark.unit
def test_service_env_resolution_ignores_current_compose_env_without_project_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Even explicit service fallback requires an AWF project marker."""
    from awf.cli import main as cli_main
    from awf.service import bootstrap as bootstrap_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)

    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env").write_text("AWF_API_TOKEN=wrong-project\n", encoding="utf-8")

    active_env_file, compose_env_file = cli_main._resolve_service_env_files(  # noqa: SLF001
        Path(".env"),
        allow_current_compose_env_without_asset_root=True,
    )

    assert active_env_file == Path(".env")
    assert compose_env_file is None


@pytest.mark.unit
def test_service_env_resolution_uses_current_compose_env_when_explicitly_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Service commands can opt into source-less local-service compose env fallback."""
    from awf.cli import main as cli_main
    from awf.service import bootstrap as bootstrap_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)

    (tmp_path / ".awf").mkdir()
    (tmp_path / ".awf" / "workspace.yml").write_text("version: 1\n", encoding="utf-8")
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env").write_text("AWF_API_TOKEN=local-service\n", encoding="utf-8")

    active_env_file, compose_env_file = cli_main._resolve_service_env_files(  # noqa: SLF001
        Path(".env"),
        allow_current_compose_env_without_asset_root=True,
    )

    assert active_env_file == compose / ".env"
    assert compose_env_file == compose / ".env"


@pytest.mark.unit
def test_init_without_path_does_not_seed_asset_root_without_compose_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Require the Compose service file before targeting compose `.env`."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))

    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    compose_example = compose / ".env.example"
    compose_example.write_text("AWF_API_TOKEN=wrong\n", encoding="utf-8")
    root_example = tmp_path / ".env.example"
    root_example.write_text("AWF_API_TOKEN=root\n", encoding="utf-8")
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert not (compose / ".env").exists()
    env_file = tmp_path / ".env"
    assert env_file.exists()
    assert env_file.read_bytes() == root_example.read_bytes()
    assert "wrote .env from .env.example" in result.output


@pytest.mark.unit
def test_init_without_path_prefers_asset_root_compose_env_from_subdirectory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Resolve compose env seeding from resolved AWF asset roots."""
    workspace_root = tmp_path / "workspace"
    compose = workspace_root / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")

    example = workspace_root / ".env.example"
    example.write_text("AWF_API_TOKEN=from_asset_root\n", encoding="utf-8")

    project_subdir = workspace_root / "project"
    project_subdir.mkdir()
    monkeypatch.chdir(project_subdir)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))

    _stub_bootstrap_mode(monkeypatch, asset_root=workspace_root)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    env_file = compose / ".env"
    assert env_file.exists()
    assert env_file.read_bytes() == example.read_bytes()
    assert "wrote ../docker/compose/.env from ../.env.example" in result.output


@pytest.mark.unit
def test_init_without_path_passes_seeded_asset_root_env_to_bootstrap_readiness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Provider readiness should use the same env file seeded from an asset root."""
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import doctor as doctor_mod
    from awf.service.bootstrap import ServiceBootstrapResult

    workspace_root = tmp_path / "workspace"
    compose = workspace_root / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")

    secret = "ghp_seeded_from_asset_root"
    example = workspace_root / ".env.example"
    example.write_text(f"AWF_GITHUB_TOKEN={secret}\n", encoding="utf-8")

    project_subdir = workspace_root / "project"
    project_subdir.mkdir()
    monkeypatch.chdir(project_subdir)
    monkeypatch.setenv("AWF_ENV", "local")
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    for key in ("AWF_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.lower(), raising=False)

    captured: dict[str, Any] = {"bootstrap_calls": []}

    async def _collect_doctor_report(settings: Any, **kwargs: Any) -> Any:
        captured["doctor_settings"] = settings
        captured["doctor_kwargs"] = kwargs
        return _doctor_report(_docker_diagnostic())

    async def _bootstrap(received_settings: Any, **kwargs: Any) -> Any:
        captured["bootstrap_calls"].append({"settings": received_settings, **kwargs})
        return ServiceBootstrapResult(
            stages=(),
            service_status={"service": "awf", "status": "ok", "checks": {}},
        )

    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: workspace_root)
    monkeypatch.setattr(doctor_mod, "collect_doctor_report", _collect_doctor_report)
    monkeypatch.setattr(bootstrap_mod, "run_service_bootstrap", _bootstrap)

    result = _runner.invoke(app, ["init", "--provider", "github"])

    assert result.exit_code == 0, result.output
    bootstrap_call = captured["bootstrap_calls"][0]
    settings = bootstrap_call["settings"]
    assert settings.github_token is None
    assert captured["doctor_settings"] is settings
    preflight_environ = captured["doctor_kwargs"]["provider_environ"]
    assert "AWF_GITHUB_TOKEN" not in preflight_environ
    assert secret not in preflight_environ.values()
    assert "provider_environ" not in bootstrap_call
    service_environ = bootstrap_call["service_environ"]
    assert service_environ["AWF_GITHUB_TOKEN"] == secret
    assert bootstrap_call["env_file"] == workspace_root / "docker" / "compose" / ".env"
    assert secret not in result.output


@pytest.mark.unit
def test_init_without_path_uses_asset_root_compose_env_for_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Docker preflight should use the same resolved compose env as bootstrap."""
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import doctor as doctor_mod
    from awf.service.bootstrap import ServiceBootstrapResult

    workspace_root = tmp_path / "workspace"
    compose = workspace_root / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")

    docker_host = f"unix://{tmp_path / 'docker.sock'}"
    (compose / ".env").write_text(f"AWF_DOCKER_HOST={docker_host}\n", encoding="utf-8")

    project_subdir = workspace_root / "project"
    project_subdir.mkdir()
    monkeypatch.chdir(project_subdir)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AWF_DOCKER_HOST", raising=False)

    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: workspace_root)
    captured: dict[str, Any] = {}

    async def _collect_doctor_report(settings: Any, **kwargs: Any) -> Any:
        captured["preflight_settings"] = settings
        captured["doctor_kwargs"] = kwargs
        return _doctor_report(_docker_diagnostic())

    async def _bootstrap(received_settings: Any, **kwargs: Any) -> Any:
        captured["bootstrap_settings"] = received_settings
        captured["bootstrap_kwargs"] = kwargs
        return ServiceBootstrapResult(
            stages=(),
            service_status={"service": "awf", "status": "ok", "checks": {}},
        )

    monkeypatch.setattr(doctor_mod, "collect_doctor_report", _collect_doctor_report)
    monkeypatch.setattr(bootstrap_mod, "run_service_bootstrap", _bootstrap)

    result = _runner.invoke(app, ["init", "--no-write-env"])

    assert result.exit_code == 0, result.output
    assert captured["preflight_settings"].docker_host == docker_host
    doctor_kwargs = captured["doctor_kwargs"]
    assert doctor_kwargs["compose_file"] == compose / "local-service.yml"
    assert doctor_kwargs["compose_env_file"] == compose / ".env"
    assert doctor_kwargs["environ"]["AWF_DOCKER_HOST"] == docker_host
    assert doctor_kwargs["provider_environ"]["AWF_DOCKER_HOST"] == docker_host
    assert captured["bootstrap_kwargs"]["compose_file"] == compose / "local-service.yml"
    assert "provider_environ" not in captured["bootstrap_kwargs"]
    assert captured["bootstrap_kwargs"]["service_environ"]["AWF_DOCKER_HOST"] == docker_host
    assert captured["bootstrap_kwargs"]["env_file"] == compose / ".env"


@pytest.mark.unit
def test_init_without_path_uses_seeded_compose_env_for_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """First-run Docker preflight should see values copied from the env example."""
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import doctor as doctor_mod
    from awf.service.bootstrap import ServiceBootstrapResult

    workspace_root = tmp_path / "workspace"
    compose = workspace_root / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")

    docker_host = f"unix://{tmp_path / 'docker.sock'}"
    compose_example = compose / ".env.example"
    compose_example.write_text(f"AWF_DOCKER_HOST={docker_host}\n", encoding="utf-8")

    project_subdir = workspace_root / "project"
    project_subdir.mkdir()
    monkeypatch.chdir(project_subdir)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("AWF_DOCKER_HOST", raising=False)

    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: workspace_root)
    captured: dict[str, Any] = {}

    async def _collect_doctor_report(settings: Any, **kwargs: Any) -> Any:
        captured["preflight_settings"] = settings
        captured["doctor_kwargs"] = kwargs
        return _doctor_report(_docker_diagnostic())

    async def _bootstrap(received_settings: Any, **kwargs: Any) -> Any:
        captured["bootstrap_settings"] = received_settings
        captured["bootstrap_kwargs"] = kwargs
        return ServiceBootstrapResult(
            stages=(),
            service_status={"service": "awf", "status": "ok", "checks": {}},
        )

    monkeypatch.setattr(doctor_mod, "collect_doctor_report", _collect_doctor_report)
    monkeypatch.setattr(bootstrap_mod, "run_service_bootstrap", _bootstrap)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert (compose / ".env").read_bytes() == compose_example.read_bytes()
    assert captured["preflight_settings"].docker_host == docker_host
    doctor_kwargs = captured["doctor_kwargs"]
    assert doctor_kwargs["compose_env_file"] == compose / ".env"
    assert doctor_kwargs["environ"]["AWF_DOCKER_HOST"] == docker_host
    assert doctor_kwargs["provider_environ"]["AWF_DOCKER_HOST"] == docker_host
    assert "provider_environ" not in captured["bootstrap_kwargs"]
    assert captured["bootstrap_kwargs"]["service_environ"]["AWF_DOCKER_HOST"] == docker_host
    assert captured["bootstrap_kwargs"]["env_file"] == compose / ".env"


@pytest.mark.unit
def test_init_without_path_prefers_asset_root_compose_example_from_subdirectory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Prefer sibling compose examples from non-CWD AWF asset roots."""
    workspace_root = tmp_path / "workspace"
    compose = workspace_root / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")

    compose_example = compose / ".env.example"
    compose_example.write_text("AWF_API_TOKEN=from_compose\n", encoding="utf-8")
    root_example = workspace_root / ".env.example"
    root_example.write_text("AWF_API_TOKEN=from_asset_root\n", encoding="utf-8")

    project_subdir = workspace_root / "project"
    project_subdir.mkdir()
    monkeypatch.chdir(project_subdir)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))

    _stub_bootstrap_mode(monkeypatch, asset_root=workspace_root)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    env_file = compose / ".env"
    assert env_file.exists()
    assert env_file.read_bytes() == compose_example.read_bytes()
    assert ("wrote ../docker/compose/.env from ../docker/compose/.env.example") in result.output
    assert not (workspace_root / ".env").exists()


@pytest.mark.unit
def test_init_without_path_does_not_overwrite_existing_source_compose_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep existing compose `.env` values during bootstrap seeding."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    env_file = compose / ".env"
    env_file.write_text("AWF_API_TOKEN=already_set\n", encoding="utf-8")
    example = tmp_path / ".env.example"
    example.write_text("AWF_API_TOKEN=example\n", encoding="utf-8")
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert env_file.read_text(encoding="utf-8") == "AWF_API_TOKEN=already_set\n"
    assert "kept existing docker/compose/.env" in result.output
    assert not (tmp_path / ".env").exists()


@pytest.mark.unit
def test_init_without_path_preserves_env_created_during_seed_race(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep an env file that appears after the pre-write existence check."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    example = tmp_path / ".env.example"
    example.write_text("AWF_API_TOKEN=example\n", encoding="utf-8")
    _stub_bootstrap_mode(monkeypatch)
    _create_path_before_exclusive_open(
        monkeypatch,
        target_path=".env",
        contents=b"AWF_API_TOKEN=concurrent\n",
    )

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".env").read_bytes() == b"AWF_API_TOKEN=concurrent\n"
    assert "kept existing .env" in result.output


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
def test_init_without_path_does_not_emit_seeded_token_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Avoid printing secret values copied during `awf init` seeding."""
    secret = "super-secret-token"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    (tmp_path / ".env.example").write_text(f"AWF_API_TOKEN={secret}\n", encoding="utf-8")
    _stub_bootstrap_mode(monkeypatch)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert secret not in result.output


@pytest.mark.unit
def test_init_without_path_does_not_emit_seeded_compose_token_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Avoid printing secret values copied during compose `awf init` seeding."""
    secret = "super-secret-token"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    example = compose / ".env.example"
    example.write_text(f"AWF_API_TOKEN={secret}\n", encoding="utf-8")
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert secret not in result.output


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
    """Explain fallback env-example lookup when root seeding cannot run."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    _stub_bootstrap_mode(monkeypatch)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert not (tmp_path / ".env").exists()
    assert "no env template found" in result.output
    assert "current directory" not in result.output
    assert "AWF repository root" in result.output


@pytest.mark.unit
def test_init_without_path_warns_when_compose_env_templates_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Report every compose env seed path checked before skipping seeding."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert not (compose / ".env").exists()
    assert "no env template found" in result.stdout
    assert "looked for docker/compose/.env.example, .env.example" in result.stdout
    assert "skipped docker/compose/.env creation" in result.stdout
    assert "no env template found" not in result.stderr


@pytest.mark.unit
def test_init_env_example_search_paths_deduplicates_all_candidates(
    tmp_path: Path,
) -> None:
    """Keep the no-example message concise when fallback paths overlap."""
    from awf.cli import main as cli_main

    env_file = tmp_path / "docker" / "compose" / ".env"
    root_example = tmp_path / ".env.example"

    assert cli_main._init_env_example_search_paths(env_file, root_example) == (  # noqa: SLF001
        env_file.with_name(".env.example"),
        root_example,
    )


@pytest.mark.unit
def test_init_without_path_warns_when_compose_env_parent_creation_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Warn about directory creation failures separately from file writes."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("AWF_API_TOKEN=local\n", encoding="utf-8")
    captured = _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)
    _fail_path_mkdir(monkeypatch, failing_path="docker/compose")

    result = _runner.invoke(app, ["init"])
    output = result.output

    assert result.exit_code == 0, output
    assert not (compose / ".env").exists()
    assert (
        "warning: could not create parent directory docker/compose "
        "for docker/compose/.env: permission denied"
    ) in result.stdout
    assert "warning: could not create parent directory" not in result.stderr
    assert "warning: could not write docker/compose/.env" not in output
    assert len(captured["bootstrap_calls"]) == 1
    assert "Traceback" not in output


@pytest.mark.unit
def test_init_without_path_warns_when_env_write_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Warn cleanly when init cannot copy the env example."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    (tmp_path / ".env.example").write_text("AWF_API_TOKEN=local\n", encoding="utf-8")
    captured = _stub_bootstrap_mode(monkeypatch)
    _fail_path_write(monkeypatch, failing_path=".env")

    result = _runner.invoke(app, ["init"])
    output = result.output

    assert result.exit_code == 0, output
    assert not (tmp_path / ".env").exists()
    assert "warning: could not write .env from .env.example: permission denied" in result.stdout
    assert "warning: could not write .env from .env.example" not in result.stderr
    assert len(captured["bootstrap_calls"]) == 1
    assert "Traceback" not in output


@pytest.mark.unit
def test_seed_env_file_removes_partial_file_after_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Do not leave a broken env file when a write fails after creation."""
    from awf.cli import main as cli_main

    env_file = tmp_path / ".env"
    env_example = tmp_path / ".env.example"
    env_example.write_bytes(b"AWF_API_TOKEN=local\n")
    original_open = Path.open

    class FailingWriter:
        def __init__(self, handle: Any) -> None:
            self._handle = handle

        def __enter__(self) -> FailingWriter:
            self._handle.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self._handle.__exit__(*args)

        def write(self, contents: bytes) -> int:
            self._handle.write(contents[:8])
            self._handle.flush()
            raise OSError("disk quota exceeded")

    def _open(
        self: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> Any:
        handle = original_open(
            self,
            mode=mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )
        if self == env_file and mode == "xb":
            return FailingWriter(handle)
        return handle

    monkeypatch.setattr(Path, "open", _open)

    action, error, overlay_keys = cli_main._seed_env_file(env_file, env_example)  # noqa: SLF001

    assert action == "write_failed"
    assert error is not None
    assert error["operation"] == "write_env"
    assert error["message"] == "disk quota exceeded"
    assert overlay_keys == ()
    assert not env_file.exists()


@pytest.mark.unit
def test_init_env_warning_uses_display_ready_payload_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Do not reinterpret already-normalized env error payload paths."""
    from awf.cli import main as cli_main

    monkeypatch.chdir(tmp_path)

    warning = cli_main._init_env_warning(  # noqa: SLF001
        {
            "operation": "write_env",
            "path": "/display/env",
            "env_file": "/display/env",
            "env_example": "/display/env.example",
            "message": "permission denied",
        }
    )

    assert warning == (
        "  warning: could not write /display/env from /display/env.example: permission denied"
    )


@pytest.mark.unit
def test_init_env_warning_describes_overlay_read_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Describe overlay read failures as reads, not env writes."""
    from awf.cli import main as cli_main

    monkeypatch.chdir(tmp_path)

    warning = cli_main._init_env_warning(  # noqa: SLF001
        {
            "operation": "read_overlay",
            "path": ".env",
            "env_file": "docker/compose/.env",
            "env_example": "docker/compose/.env.example",
            "message": "permission denied",
        }
    )

    assert warning == (
        "  warning: could not read .env while seeding docker/compose/.env "
        "from docker/compose/.env.example: permission denied"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("failure_mode", "expected_operation", "expected_path"),
    (
        ("mkdir", "create_parent_directory", "."),
        ("read", "read_example", ".env.example"),
        ("write", "write_env", ".env"),
    ),
)
def test_init_without_path_json_marks_env_write_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_mode: str,
    expected_operation: str,
    expected_path: str,
) -> None:
    """Expose env copy failures in machine-readable init output."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    (tmp_path / ".env.example").write_text("AWF_API_TOKEN=local\n", encoding="utf-8")
    _stub_bootstrap_mode(monkeypatch)
    if failure_mode == "mkdir":
        _fail_path_mkdir(monkeypatch, failing_path=".")
    elif failure_mode == "read":
        _fail_path_read_bytes(monkeypatch, failing_path=".env.example")
    else:
        _fail_path_write(monkeypatch, failing_path=".env")

    result = _runner.invoke(app, ["init", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["env_action"] == "write_failed"
    assert payload["env_error"] == {
        "operation": expected_operation,
        "path": expected_path,
        "env_file": ".env",
        "env_example": ".env.example",
        "message": "permission denied",
    }


@pytest.mark.unit
def test_init_without_path_json_normalizes_asset_root_env_write_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep machine-readable env failure paths relative to the launch directory."""
    workspace_root = tmp_path / "workspace"
    compose = workspace_root / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (workspace_root / ".env.example").write_text("AWF_API_TOKEN=local\n", encoding="utf-8")

    project_subdir = workspace_root / "project"
    project_subdir.mkdir()
    monkeypatch.chdir(project_subdir)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))

    _stub_bootstrap_mode(monkeypatch, asset_root=workspace_root)
    _fail_path_write(monkeypatch, failing_path="../docker/compose/.env")

    result = _runner.invoke(app, ["init", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["env_action"] == "write_failed"
    assert payload["env_error"] == {
        "operation": "write_env",
        "path": "../docker/compose/.env",
        "env_file": "../docker/compose/.env",
        "env_example": "../.env.example",
        "message": "permission denied",
    }


@pytest.mark.unit
def test_init_without_path_json_marks_env_overlay_read_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expose overlay read failures without confusing the seed source."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env.example").write_text("AWF_API_TOKEN=compose\n", encoding="utf-8")
    (tmp_path / ".env").write_text("AWF_API_TOKEN=root\n", encoding="utf-8")
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)
    _fail_path_read_bytes(monkeypatch, failing_path=".env")

    result = _runner.invoke(app, ["init", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["env_action"] == "write_failed"
    assert payload["env_error"] == {
        "operation": "read_overlay",
        "path": ".env",
        "env_file": "docker/compose/.env",
        "env_example": "docker/compose/.env.example",
        "message": "permission denied",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("seed_text", "overlay_text"),
    (
        ('AWF_API_TOKEN="template\ncontinued"\n', "AWF_API_TOKEN=root\n"),
        ("AWF_API_TOKEN=template\n", 'AWF_API_TOKEN="root\ncontinued"\n'),
    ),
)
def test_merge_env_seed_contents_rejects_multiline_dotenv_values(
    seed_text: str,
    overlay_text: str,
) -> None:
    """Do not let the line-oriented merge mangle multi-line dotenv values."""
    from awf.cli import main as cli_main

    with pytest.raises(ValueError, match="multi-line dotenv"):
        cli_main._merge_env_seed_contents(  # noqa: SLF001
            seed_text.encode("utf-8"),
            overlay_text.encode("utf-8"),
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("seed_contents", "overlay_contents"),
    (
        (b"AWF_API_TOKEN=compose\nINVALID=\xff\n", b"AWF_API_TOKEN=root\n"),
        (b"AWF_API_TOKEN=compose\n", b"AWF_API_TOKEN=root\nINVALID=\xff\n"),
    ),
)
def test_merge_env_seed_contents_rejects_non_utf8_dotenv_contents(
    seed_contents: bytes,
    overlay_contents: bytes,
) -> None:
    """Expose undecodable dotenv inputs instead of silently skipping overlays."""
    from awf.cli import main as cli_main

    with pytest.raises(ValueError, match="UTF-8"):
        cli_main._merge_env_seed_contents(  # noqa: SLF001
            seed_contents,
            overlay_contents,
        )


@pytest.mark.unit
def test_merge_env_seed_contents_preserves_context_between_duplicate_overlay_keys() -> None:
    """Keep comments between duplicate overlay assignments with the final value."""
    from awf.cli import main as cli_main

    merged_contents, overlay_only_keys = cli_main._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        (
            "\n".join(
                [
                    "AWF_API_TOKEN=compose-example",
                    "AWF_DOCKER_HOST=",
                    "AWF_COMPOSE_ONLY=compose-default",
                ]
            )
            + "\n"
        ).encode("utf-8"),
        (
            "\n".join(
                [
                    "AWF_API_TOKEN=migrated-token",
                    "AWF_DOCKER_HOST=unix:///tmp/first-docker.sock",
                    "",
                    "# Regenerated duplicate Docker host context",
                    "AWF_DOCKER_HOST=unix:///tmp/second-docker.sock",
                    "",
                    "# Operator final Docker host context",
                    "AWF_DOCKER_HOST=unix:///tmp/final-docker.sock",
                ]
            )
            + "\n"
        ).encode("utf-8"),
    )

    assert overlay_only_keys == ()
    assert merged_contents.decode("utf-8") == (
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "",
                "# Regenerated duplicate Docker host context",
                "",
                "# Operator final Docker host context",
                "AWF_DOCKER_HOST=unix:///tmp/final-docker.sock",
                "AWF_COMPOSE_ONLY=compose-default",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_merge_env_seed_contents_preserves_context_before_first_duplicate_overlay_key() -> None:
    """Keep context before the first duplicate with the final overlay value."""
    from awf.cli import main as cli_main

    merged_contents, overlay_only_keys = cli_main._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        (
            "\n".join(
                [
                    "AWF_API_TOKEN=compose-example",
                    "AWF_DOCKER_HOST=",
                    "AWF_COMPOSE_ONLY=compose-default",
                ]
            )
            + "\n"
        ).encode("utf-8"),
        (
            "\n".join(
                [
                    "AWF_API_TOKEN=migrated-token",
                    "# Operator Docker host context",
                    "AWF_DOCKER_HOST=unix:///tmp/first-docker.sock",
                    "# Operator final Docker host context",
                    "AWF_DOCKER_HOST=unix:///tmp/final-docker.sock",
                ]
            )
            + "\n"
        ).encode("utf-8"),
    )

    assert overlay_only_keys == ()
    assert merged_contents.decode("utf-8") == (
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "# Operator Docker host context",
                "# Operator final Docker host context",
                "AWF_DOCKER_HOST=unix:///tmp/final-docker.sock",
                "AWF_COMPOSE_ONLY=compose-default",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_merge_env_seed_contents_preserves_context_before_duplicate_overlay_only_key() -> None:
    """Keep comments before the first overlay-only duplicate with the final value."""
    from awf.cli import main as cli_main

    merged_contents, overlay_only_keys = cli_main._merge_env_seed_contents_with_overlay_keys(  # noqa: SLF001
        b"AWF_API_TOKEN=compose-example\n",
        (
            "\n".join(
                [
                    "AWF_API_TOKEN=migrated-token",
                    "# Operator endpoint settings",
                    "# Migrated from the root env file",
                    "AWF_EXTRA_ENDPOINT=https://first.example.test",
                    "",
                    "# Operator final endpoint context",
                    "AWF_EXTRA_ENDPOINT=https://final.example.test",
                ]
            )
            + "\n"
        ).encode("utf-8"),
    )

    assert overlay_only_keys == ("AWF_EXTRA_ENDPOINT",)
    assert merged_contents.decode("utf-8") == (
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "# Operator endpoint settings",
                "# Migrated from the root env file",
                "",
                "# Operator final endpoint context",
                "AWF_EXTRA_ENDPOINT=https://final.example.test",
            ]
        )
        + "\n"
    )


@pytest.mark.unit
def test_init_without_path_json_marks_multiline_env_overlay_merge_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expose unsupported multi-line overlay merges instead of writing corruption."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env.example").write_text("AWF_API_TOKEN=compose\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        'AWF_API_TOKEN="root-token-line-one\nroot-token-line-two"\n',
        encoding="utf-8",
    )
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["env_action"] == "write_failed"
    assert payload["env_error"] == {
        "operation": "merge_overlay",
        "path": ".env",
        "env_file": "docker/compose/.env",
        "env_example": "docker/compose/.env.example",
        "message": (
            "unsupported multi-line dotenv values; env seeding merge only supports "
            "single-line assignments"
        ),
    }
    assert not (compose / ".env").exists()
    assert "root-token-line-one" not in result.output
    assert "root-token-line-two" not in result.output


@pytest.mark.unit
def test_init_without_path_json_marks_non_utf8_env_overlay_merge_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expose invalid UTF-8 overlays instead of writing a template-only env file."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env.example").write_text("AWF_API_TOKEN=compose\n", encoding="utf-8")
    (tmp_path / ".env").write_bytes(b"AWF_API_TOKEN=root\nINVALID=\xff\n")
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init", "--format", "json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "BOOTSTRAP_LOCAL_CHECKS_FAILED"
    assert payload["env_action"] == "write_failed"
    assert payload["env_error"] == {
        "operation": "merge_overlay",
        "path": ".env",
        "env_file": "docker/compose/.env",
        "env_example": "docker/compose/.env.example",
        "message": "env seeding merge requires UTF-8 dotenv files",
    }
    assert not (compose / ".env").exists()
    assert "root" not in result.output


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
def test_init_without_path_prints_env_success_before_docker_preflight_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Show the created env file even when Docker blocks bootstrap."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    (tmp_path / ".env.example").write_text("AWF_API_TOKEN=local\n", encoding="utf-8")
    captured = _stub_bootstrap_mode(monkeypatch, docker_status="fail")

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 1, result.output
    assert captured["bootstrap_calls"] == []
    env_message = "wrote .env from .env.example"
    docker_failure = "Docker is not available; cannot bootstrap local service."
    assert env_message in result.stdout
    assert result.stdout.index(env_message) < result.stdout.index(docker_failure)
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "AWF_API_TOKEN=local\n"
    assert not (tmp_path / "state").exists()
    assert "Traceback" not in result.output


@pytest.mark.unit
def test_init_without_path_json_includes_env_action_when_docker_preflight_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expose successful env seeding in Docker preflight failure payloads."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    (tmp_path / ".env.example").write_text("AWF_API_TOKEN=local\n", encoding="utf-8")
    captured = _stub_bootstrap_mode(monkeypatch, docker_status="fail")

    result = _runner.invoke(app, ["init", "--format", "json"])

    assert result.exit_code == 1, result.output
    assert captured["bootstrap_calls"] == []
    payload = json.loads(result.output)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "DOCKER_DAEMON_UNREACHABLE"
    assert payload["env_action"] == "wrote_from_example"
    assert "env_error" not in payload
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "AWF_API_TOKEN=local\n"
    assert not (tmp_path / "state").exists()
    assert "Traceback" not in result.output


@pytest.mark.unit
def test_init_without_path_json_includes_env_error_when_docker_preflight_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expose env write failures even when Docker preflight exits early."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    (tmp_path / ".env.example").write_text("AWF_API_TOKEN=local\n", encoding="utf-8")
    captured = _stub_bootstrap_mode(monkeypatch, docker_status="fail")
    _fail_path_write(monkeypatch, failing_path=".env")

    result = _runner.invoke(app, ["init", "--format", "json"])

    assert result.exit_code == 1, result.output
    assert captured["bootstrap_calls"] == []
    payload = json.loads(result.output)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "DOCKER_DAEMON_UNREACHABLE"
    assert payload["env_error"] == {
        "operation": "write_env",
        "path": ".env",
        "env_file": ".env",
        "env_example": ".env.example",
        "message": "permission denied",
    }
    assert not (tmp_path / "state").exists()
    assert "Traceback" not in result.output


@pytest.mark.unit
def test_init_without_path_json_includes_env_action_when_local_checks_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expose successful env seeding when local checks fail before bootstrap."""
    from awf.service import doctor as doctor_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    (tmp_path / ".env.example").write_text("AWF_API_TOKEN=local\n", encoding="utf-8")
    captured = _stub_bootstrap_mode(monkeypatch)

    async def _fail_to_collect_doctor_report(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("doctor probe failed")

    monkeypatch.setattr(doctor_mod, "collect_doctor_report", _fail_to_collect_doctor_report)

    result = _runner.invoke(app, ["init", "--format", "json"])

    assert result.exit_code == 1, result.output
    assert captured["bootstrap_calls"] == []
    payload = json.loads(result.output)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "BOOTSTRAP_LOCAL_CHECKS_FAILED"
    assert payload["message"] == "doctor probe failed"
    assert payload["env_action"] == "wrote_from_example"
    assert "env_error" not in payload
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "AWF_API_TOKEN=local\n"
    assert not (tmp_path / "state").exists()
    assert "Traceback" not in result.output


@pytest.mark.unit
def test_init_without_path_warns_when_env_write_and_docker_preflight_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Warn about env write failures before reporting Docker preflight failure."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    (tmp_path / ".env.example").write_text("AWF_API_TOKEN=local\n", encoding="utf-8")
    captured = _stub_bootstrap_mode(monkeypatch, docker_status="fail")
    _fail_path_write(monkeypatch, failing_path=".env")

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 1, result.output
    assert captured["bootstrap_calls"] == []
    warning = "warning: could not write .env from .env.example: permission denied"
    docker_failure = "Docker is not available; cannot bootstrap local service."
    assert result.stdout.count(warning) == 1
    assert result.stdout.index(warning) < result.stdout.index(docker_failure)
    assert "Traceback" not in result.output


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
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda *_args, **_kwargs: object())

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
    combined = result.output
    assert "Traceback" not in combined


@pytest.mark.unit
def test_init_without_path_json_includes_env_error_when_bootstrap_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from awf.service.bootstrap import ServiceBootstrapError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    (tmp_path / ".env.example").write_text("AWF_API_TOKEN=local\n", encoding="utf-8")
    error = ServiceBootstrapError(
        reason_code="SERVICE_BOOTSTRAP_FAILED",
        message="docker compose failed",
        stage="compose_up",
        command=("docker", "compose", "up", "-d"),
        returncode=1,
        stderr="AWF_API_TOKEN is required",
    )
    _stub_bootstrap_mode(monkeypatch, bootstrap_error=error)
    _fail_path_write(monkeypatch, failing_path=".env")

    result = _runner.invoke(app, ["init", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "SERVICE_BOOTSTRAP_FAILED"
    assert payload["stage"] == "compose_up"
    assert payload["env_error"] == {
        "operation": "write_env",
        "path": ".env",
        "env_file": ".env",
        "env_example": ".env.example",
        "message": "permission denied",
    }
    assert "Traceback" not in result.output


@pytest.mark.unit
def test_init_without_path_rejects_unknown_provider_without_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))

    result = _runner.invoke(app, ["init", "--provider", "bogus"])

    output = result.output
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
def test_init_without_path_uses_compose_env_host_work_dir_for_state_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Prepare the host state directory that Docker Compose will mount."""
    monkeypatch.chdir(tmp_path)
    host_home = tmp_path / "home"
    compose_state_dir = tmp_path / "compose-state"
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env").write_text(
        f"AWF_HOST_WORK_DIR={compose_state_dir}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.delenv("AWF_HOST_WORK_DIR", raising=False)
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert compose_state_dir.exists()
    assert compose_state_dir.is_dir()
    assert not (host_home / ".awf" / "service").exists()
    assert str(compose_state_dir.resolve()) in result.output


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

    output = result.output
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

    output = result.output
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

    output = result.output
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

        output = result.output
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

    output = result.output
    assert result.exit_code == 2
    assert "--include-smoke-request" in output
    assert "project path" in output
    assert "Traceback" not in output


@pytest.mark.unit
def test_readme_recommends_awf_init_for_local_bootstrap() -> None:
    """Assert README guidance matches compose env bootstrap behavior."""
    readme = Path("docs/GETTING_STARTED.md").read_text(encoding="utf-8")

    assert "awf init" in readme
    assert "awf init <path>" in readme
    assert "awf service status --format pretty" in readme
    assert "docker/compose/.env" in readme
    assert "cp .env.example .env" not in readme


@pytest.mark.unit
def test_getting_started_compose_env_snippet_replaces_token_placeholders() -> None:
    """Regression: avoid duplicate token keys in docker/compose/.env examples."""
    readme = Path("docs/GETTING_STARTED.md").read_text(encoding="utf-8")
    snippet_start = readme.index("env_example=docker/compose/.env.example")
    snippet_end = readme.index("uv run --python 3.12 --extra dev awf service bootstrap")
    snippet = readme[snippet_start:snippet_end]

    assert "grep -vE '^(AWF_API_TOKEN|AWF_GITHUB_TOKEN)='" in readme
    assert "} > docker/compose/.env" in readme
    assert ">> docker/compose/.env" not in readme
    assert 'echo "Missing env template: docker/compose/.env.example or .env.example" >&2' in snippet
    assert "exit 1" in snippet
    assert snippet.index("exit 1") < snippet.index("{")


@pytest.mark.unit
def test_project_onboarding_doc_distinguishes_init_modes() -> None:
    doc = Path("docs/PROJECT_ONBOARDING.md").read_text(encoding="utf-8")

    assert "awf init" in doc
    assert "awf init <path>" in doc
    assert "AWF-on-this-machine" in doc or "local service bootstrap" in doc


@pytest.mark.unit
def test_project_onboarding_doc_has_provider_prompts() -> None:
    """Regression: every supported provider has a copy-paste prompt block."""
    readme = Path("docs/GETTING_STARTED.md").read_text(encoding="utf-8")
    doc = Path("docs/PROJECT_ONBOARDING.md").read_text(encoding="utf-8")

    # README links to the onboarding doc
    assert "PROJECT_ONBOARDING.md" in readme

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
        # Grab the block up to the next heading (### or ##) or end of file
        end = doc.find("\n### ", start + 1)
        if end == -1:
            end = doc.find("\n## ", start + 1)
        block = doc[start:end] if end != -1 else doc[start:]
        for keyword in keyword_set:
            assert keyword in block, f"{provider} prompt missing {keyword}"
