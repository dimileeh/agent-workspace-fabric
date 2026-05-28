"""CLI coverage for first-run onboarding guidance."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import typer
import yaml
from typer.testing import CliRunner

from awf.cli.main import app
from tests.unit.cli.test_init_parts._bootstrap_helper import invoke_init_service_bootstrap

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
                path=_path,
                draft=SimpleNamespace(template="generic"),
                smoke_request={"dummy": "payload"},
                to_dict=lambda: {
                    "path": str(_path),
                    "draft": {"template": "generic"},
                    "diagnostics": {},
                },
            )

        monkeypatch.setattr(
            "awf.profiles.onboarding.preview_project_onboarding",
            _preview_project_onboarding,
        )


def _read_written_profile(project: Path) -> dict[str, Any]:
    raw = yaml.safe_load((project / ".awf" / "workspace.yml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    awf_profile = raw.get("awf")
    assert isinstance(awf_profile, dict)
    return awf_profile


@pytest.mark.unit
def test_init_profile_marker_paths_are_shared_with_smoke_service() -> None:
    from awf.cli import init_ops as cli_main
    from awf.service import smoke

    assert cli_main._PROJECT_PROFILE_MARKER_PATHS is smoke._PROFILE_MARKER_PATHS


@pytest.mark.unit
def test_init_command_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_local_prerequisites(monkeypatch)

    result = _runner.invoke(app, ["init", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "AWF init: local onboarding readiness check" in result.output


@pytest.mark.unit
def test_init_help_documents_project_onboarding_and_new_first_run_flow() -> None:
    """Document that public init is now only project onboarding."""
    result = _runner.invoke(app, ["init", "--help"], env={"COLUMNS": "240"})

    assert result.exit_code == 0, result.output
    assert "current runnable first path" in result.output
    assert "awf service bootstrap" in result.output
    assert "awf init <path>" in result.output
    assert "Path to a checked-out repository" in result.output
    assert "docker/compose/.env" not in result.output
    assert "--write-env" not in result.output
    assert "--timeout-seconds" not in result.output
    assert "--poll-interval-seconds" not in result.output
    assert "--skip-agent-runtime-build" not in result.output
    assert "--provider" not in result.output


@pytest.mark.unit
def test_init_without_path_returns_migration_error() -> None:
    from awf.cli import main as cli_main

    # Service bootstrap is no longer exposed through cli_main; no-path init now
    # fails with migration guidance before any service bootstrap path can run.
    assert not hasattr(cli_main, "_run_init_service_bootstrap")

    result = _runner.invoke(app, ["init"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "AWF_INIT_REQUIRES_PROJECT_PATH" in result.stderr
    assert "`awf init` no longer bootstraps the local service stack" in result.stderr
    assert "awf service bootstrap" in result.stderr
    assert "awf init <path>" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.unit
def test_init_without_path_json_returns_migration_payload() -> None:
    result = _runner.invoke(app, ["init", "--format", "json"])

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload == {
        "status": "error",
        "reason_code": "AWF_INIT_REQUIRES_PROJECT_PATH",
        "command": "awf init",
        "message": "`awf init` requires a project path.",
        "next_steps": [
            "Run awf service bootstrap to start local AWF Core.",
            "Run awf init <path> to onboard a project repository.",
        ],
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    ("extra_args", "expected_flag"),
    (
        (["--write-env"], "--write-env"),
        (["--no-write-env"], "--no-write-env"),
        (["--timeout-seconds", "5"], "--timeout-seconds"),
        (["--timeout-seconds", "not-a-number"], "--timeout-seconds"),
        (["--timeout-seconds", "-1"], "--timeout-seconds"),
        (["--poll-interval-seconds", "0.5"], "--poll-interval-seconds"),
        (["--poll-interval-seconds", "not-a-number"], "--poll-interval-seconds"),
        (["--poll-interval-seconds", "0"], "--poll-interval-seconds"),
        (["--skip-agent-runtime-build"], "--skip-agent-runtime-build"),
        (["--provider", "github"], "--provider"),
    ),
)
def test_init_without_path_rejects_legacy_bootstrap_flags_with_migration(
    extra_args: list[str],
    expected_flag: str,
) -> None:
    result = _runner.invoke(app, ["init", *extra_args])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "AWF_INIT_REQUIRES_PROJECT_PATH" in result.stderr
    assert expected_flag in result.stderr
    assert "awf service bootstrap" in result.stderr
    assert "awf init <path>" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    ("extra_args", "expected_flag"),
    (
        (["--include-smoke-request"], "--include-smoke-request"),
        (["--guided"], "--guided"),
        (["--no-guided"], "--no-guided"),
        (["--write-profile"], "--write-profile"),
        (["--yes"], "--yes"),
        (["--template", "python"], "--template"),
        (["--force"], "--force"),
    ),
)
def test_init_without_path_rejects_project_mode_flags_as_path_required(
    extra_args: list[str],
    expected_flag: str,
) -> None:
    result = _runner.invoke(app, ["init", *extra_args])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "AWF_INIT_REQUIRES_PROJECT_PATH" in result.stderr
    assert "Project path required for flag(s):" in result.stderr
    assert expected_flag in result.stderr
    assert "Rejected legacy no-path init flag(s)" not in result.stderr
    assert "awf init <path>" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.unit
def test_init_without_path_json_rejects_invalid_legacy_timeout_with_migration() -> None:
    result = _runner.invoke(
        app,
        ["init", "--timeout-seconds", "not-a-number", "--format", "json"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["reason_code"] == "AWF_INIT_REQUIRES_PROJECT_PATH"
    assert payload["rejected_flags"] == ["--timeout-seconds"]


@pytest.mark.unit
def test_init_without_path_json_reports_project_mode_flags_as_path_required() -> None:
    result = _runner.invoke(app, ["init", "--write-profile", "--format", "json"])

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["reason_code"] == "AWF_INIT_REQUIRES_PROJECT_PATH"
    assert payload["path_required_flags"] == ["--write-profile"]
    assert "rejected_flags" not in payload


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
def test_init_write_profile_yes_creates_default_workspace_yml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awf.profiles.models import WorkspaceProfile

    _stub_local_prerequisites(monkeypatch)

    result = _runner.invoke(app, ["init", str(tmp_path), "--write-profile", "--yes"])

    assert result.exit_code == 0, result.output
    profile = _read_written_profile(tmp_path)
    WorkspaceProfile.model_validate(profile)
    assert profile["name"] == "generic"
    assert profile["security"]["egress"]["mode"] == "restricted"
    assert "Wrote AWF profile" in result.output


@pytest.mark.unit
def test_init_write_profile_json_reports_profile_exists_after_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_local_prerequisites(monkeypatch)

    result = _runner.invoke(
        app,
        ["init", str(tmp_path), "--write-profile", "--yes", "--format", "json"],
    )

    written_path = tmp_path / ".awf" / "workspace.yml"
    assert result.exit_code == 0, result.output
    assert written_path.exists()
    payload = json.loads(result.output)
    assert payload["mode"] == "write"
    assert payload["written_path"] == str(written_path)
    assert payload["profile_exists"] is True


@pytest.mark.unit
def test_init_write_profile_requires_yes_when_not_guided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_local_prerequisites(monkeypatch)

    result = _runner.invoke(app, ["init", str(tmp_path), "--write-profile"])

    assert result.exit_code == 2, result.output
    assert "--write-profile" in result.output
    assert "--yes" in result.output
    assert not (tmp_path / ".awf" / "workspace.yml").exists()


@pytest.mark.unit
def test_init_yes_requires_write_profile(tmp_path: Path) -> None:
    result = _runner.invoke(app, ["init", str(tmp_path), "--yes"])

    assert result.exit_code == 2, result.output
    assert "--yes" in result.output
    assert "--write-profile" in result.output
    assert not (tmp_path / ".awf" / "workspace.yml").exists()


@pytest.mark.unit
def test_init_guided_requires_interactive_stdio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_local_prerequisites(monkeypatch)

    monkeypatch.setattr("awf.cli.init_ops._stdio_is_interactive", lambda: False)
    monkeypatch.setattr(
        "awf.cli.main._prompt_project_onboarding_choices",
        MagicMock(side_effect=AssertionError("should not prompt without a TTY")),
    )

    result = _runner.invoke(app, ["init", str(tmp_path), "--guided"])

    assert result.exit_code == 2, result.output
    assert "--guided requires an interactive terminal" in result.output
    assert "--yes" in result.output
    assert not (tmp_path / ".awf" / "workspace.yml").exists()


@pytest.mark.unit
def test_init_guided_writes_answers_into_workspace_yml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_local_prerequisites(monkeypatch)
    monkeypatch.setattr("awf.cli.init_ops._stdio_is_interactive", lambda: True)

    result = _runner.invoke(
        app,
        ["init", str(tmp_path), "--guided"],
        input="\nopen\nNeeds package registry and model-provider access.\ny\npytest -q\nn\ny\n",
    )

    assert result.exit_code == 0, result.output
    profile = _read_written_profile(tmp_path)
    assert profile["security"]["egress"]["mode"] == "open"
    assert (
        profile["security"]["egress"]["open_explanation"]
        == "Needs package registry and model-provider access."
    )
    assert profile["phases"]["validate"] == [{"command": "pytest -q", "required": True}]


@pytest.mark.unit
def test_init_guided_accepts_multiple_validation_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_local_prerequisites(monkeypatch)
    monkeypatch.setattr("awf.cli.init_ops._stdio_is_interactive", lambda: True)

    result = _runner.invoke(
        app,
        ["init", str(tmp_path), "--guided"],
        input="\n\ny\nmake lint\ny\npytest -q\nn\ny\n",
    )

    assert result.exit_code == 0, result.output
    profile = _read_written_profile(tmp_path)
    assert profile["phases"]["validate"] == [
        {"command": "make lint", "required": True},
        {"command": "pytest -q", "required": True},
    ]


@pytest.mark.unit
def test_init_write_profile_guided_declined_confirmation_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_local_prerequisites(monkeypatch)
    monkeypatch.setattr("awf.cli.init_ops._stdio_is_interactive", lambda: True)

    result = _runner.invoke(
        app,
        ["init", str(tmp_path), "--write-profile", "--guided"],
        input="\n\n\nn\n",
    )

    assert result.exit_code == 0, result.output
    assert not (tmp_path / ".awf" / "workspace.yml").exists()
    assert "Wrote AWF profile" not in result.output


@pytest.mark.unit
def test_init_guided_egress_choices_follow_model_enum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awf.cli import init_ops as cli_main

    class CustomEgressMode(StrEnum):
        restricted = "private-default"
        private = "private"

    preview = SimpleNamespace(
        draft=SimpleNamespace(
            template="generic",
            profile=SimpleNamespace(phases=SimpleNamespace(validate_commands=["pytest -q"])),
        )
    )
    captured: dict[str, object] = {}
    prompt_defaults: list[object] = []
    prompt_answers = iter(["generic", "private"])

    def prompt(_label: str, **kwargs: object) -> str:
        prompt_defaults.append(kwargs.get("default"))
        return next(prompt_answers)

    monkeypatch.setattr("awf.cli.main.typer.prompt", prompt)
    monkeypatch.setattr("awf.cli.main.typer.confirm", lambda *_args, **_kwargs: False)

    def customize_preview(received_preview: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return received_preview

    result, wants_write = cli_main._prompt_project_onboarding_choices(  # noqa: SLF001
        tmp_path,
        preview=preview,
        include_smoke_request=False,
        supported_templates=("generic",),
        egress_mode_type=CustomEgressMode,
        preview_factory=lambda *_args, **_kwargs: preview,
        customize_preview=customize_preview,
    )

    assert result is preview
    assert wants_write is False
    assert prompt_defaults == ["generic", CustomEgressMode.restricted.value]
    assert captured["egress_mode"] == CustomEgressMode.private
    assert captured["validation_commands"] is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("preview_error", "expected_code", "expected_message"),
    (
        (
            ValueError("unsupported onboarding template: python"),
            2,
            "error: unsupported onboarding template: python",
        ),
        (
            OSError("permission denied reading pyproject.toml"),
            1,
            "error: could not build onboarding preview: permission denied reading pyproject.toml",
        ),
    ),
)
def test_init_guided_template_change_preview_failure_is_reported_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    preview_error: Exception,
    expected_code: int,
    expected_message: str,
) -> None:
    from awf.cli import init_ops as cli_main
    from awf.profiles.models import EgressMode

    preview = SimpleNamespace(
        draft=SimpleNamespace(
            template="generic",
            profile=SimpleNamespace(phases=SimpleNamespace(validate_commands=[])),
        )
    )

    def _raise_preview_failure(_path: Path, **_kwargs: object) -> object:
        raise preview_error

    monkeypatch.setattr("awf.cli.main.typer.prompt", lambda *_args, **_kwargs: "python")
    monkeypatch.setattr(
        "awf.cli.main.typer.confirm",
        MagicMock(side_effect=AssertionError("should not continue after preview failure")),
    )

    with pytest.raises(typer.Exit) as exc_info:
        cli_main._prompt_project_onboarding_choices(  # noqa: SLF001
            tmp_path,
            preview=preview,
            include_smoke_request=False,
            supported_templates=("generic", "python"),
            egress_mode_type=EgressMode,
            preview_factory=_raise_preview_failure,
            customize_preview=MagicMock(
                side_effect=AssertionError("should not customize a failed preview")
            ),
        )

    captured = capsys.readouterr()
    assert exc_info.value.exit_code == expected_code
    assert expected_message in captured.err
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


@pytest.mark.unit
def test_init_write_profile_existing_profile_requires_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_local_prerequisites(monkeypatch)
    profile_path = tmp_path / ".awf" / "workspace.yml"
    profile_path.parent.mkdir()
    profile_path.write_text("original: true\n", encoding="utf-8")

    blocked = _runner.invoke(
        app,
        ["init", str(tmp_path), "--write-profile", "--yes"],
    )
    overwritten = _runner.invoke(
        app,
        ["init", str(tmp_path), "--write-profile", "--yes", "--force"],
    )

    assert blocked.exit_code == 1, blocked.output
    assert "already exists" in blocked.output
    assert overwritten.exit_code == 0, overwritten.output
    assert _read_written_profile(tmp_path)["name"] == "generic"


@pytest.mark.unit
def test_init_write_profile_alternate_profile_marker_requires_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_local_prerequisites(monkeypatch)
    alternate_profile_path = tmp_path / ".awf" / "workspace.yaml"
    alternate_profile_path.parent.mkdir()
    alternate_profile_path.write_text("version: 1\nname: existing\n", encoding="utf-8")
    canonical_profile_path = tmp_path / ".awf" / "workspace.yml"

    blocked = _runner.invoke(
        app,
        ["init", str(tmp_path), "--write-profile", "--yes"],
    )
    forced = _runner.invoke(
        app,
        ["init", str(tmp_path), "--write-profile", "--yes", "--force"],
    )

    assert blocked.exit_code == 1, blocked.output
    assert f"{alternate_profile_path} already exists" in blocked.output
    assert forced.exit_code == 0, forced.output
    assert canonical_profile_path.is_file()
    assert alternate_profile_path.is_file()


@pytest.mark.unit
def test_init_json_mode_never_prompts_and_reports_structured_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_local_prerequisites(monkeypatch)

    result = _runner.invoke(app, ["init", str(tmp_path), "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["path"] == str(tmp_path.resolve())
    assert payload["profile_exists"] is False
    assert payload["guided"] is False
    assert payload["mode"] == "preview"
    assert payload["service_status"]["status"] == "ok"
    assert payload["doctor_status"] == "ok"
    assert "written_path" not in payload
    assert "prompt" not in payload
    assert not (tmp_path / ".awf" / "workspace.yml").exists()


@pytest.mark.unit
def test_init_write_profile_continues_when_local_checks_are_unhealthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_local_prerequisites(monkeypatch, service_status="fail", doctor_status="fail")

    result = _runner.invoke(app, ["init", str(tmp_path), "--write-profile", "--yes"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".awf" / "workspace.yml").exists()
    assert "Local prerequisites are not fully ready" in result.output
    assert "Wrote AWF profile" in result.output


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
def test_init_reports_unexpected_preview_failure_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_local_prerequisites(monkeypatch)

    def _raise_preview_failure(_path: Path, **_kwargs: object) -> object:
        raise OSError("permission denied reading pyproject.toml")

    monkeypatch.setattr(
        "awf.profiles.onboarding.preview_project_onboarding",
        _raise_preview_failure,
    )

    result = _runner.invoke(app, ["init", str(tmp_path)])

    assert result.exit_code == 1, result.output
    assert (
        "error: could not build onboarding preview: permission denied reading pyproject.toml"
    ) in result.output
    assert "Traceback" not in result.output


@pytest.mark.unit
def test_init_prints_clear_next_steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_local_prerequisites(monkeypatch)

    result = _runner.invoke(app, ["init", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "awf init <path> --write-profile --yes" in result.output
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

    assert result.exit_code == 0, result.output
    assert calls["status"] == 1
    assert calls["doctor"] == 1
    assert "service status: fail" in result.output
    assert "AWF doctor: fail" in result.output
    assert "Local prerequisites are not fully ready yet" in result.output


@pytest.mark.unit
def test_init_cached_service_status_lets_doctor_handle_probe_exceptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"status": 0, "doctor": 0, "status_collector_raised": 0}

    async def _collect_service_status(_settings: object, **_kwargs: object) -> dict[str, object]:
        calls["status"] += 1
        raise RuntimeError("service probe is unavailable")

    async def _collect_doctor_report(
        _settings: object,
        **kwargs: object,
    ) -> object:
        calls["doctor"] += 1
        status_collector = kwargs["status_collector"]
        with pytest.raises(RuntimeError, match="service probe is unavailable"):
            await status_collector(_settings, strict_providers=frozenset(), provider_environ={})
        calls["status_collector_raised"] += 1
        return SimpleNamespace(status="fail", diagnostics=())

    monkeypatch.setattr("awf.service.config.resolve_service_settings", lambda: object())
    monkeypatch.setattr("awf.service.status.collect_service_status", _collect_service_status)
    monkeypatch.setattr("awf.service.doctor.collect_doctor_report", _collect_doctor_report)
    monkeypatch.setattr(
        "awf.service.doctor.render_doctor_pretty",
        lambda report: f"AWF doctor: {getattr(report, 'status', 'unknown')}\n",
    )

    result = _runner.invoke(app, ["init", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert calls == {"status": 1, "doctor": 1, "status_collector_raised": 1}
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
def test_init_cached_service_status_accepts_readiness_collector_context(
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
            _settings,
            strict_providers=frozenset(),
            provider_environ=kwargs["provider_environ"],
            environ=kwargs["environ"],
            compose_file=tmp_path / "docker" / "compose" / "local-service.yml",
            compose_env_file=tmp_path / "docker" / "compose" / ".env",
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

    assert result.exit_code == 0, result.output
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


@pytest.mark.unit
def test_resolve_service_compose_paths_returns_absolute_asset_root_paths_from_root_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verified AWF asset paths should not depend on launch directory shape."""
    from awf.cli import init_ops as cli_main
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
def test_resolve_service_compose_paths_anchors_root_fallback_from_subdirectory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Asset-root fallback env paths should not depend on launch directory."""
    from awf.cli import init_ops as cli_main
    from awf.service import bootstrap as bootstrap_mod
    from awf.service.config import LOCAL_SERVICE_COMPOSE_FILE

    asset_root = tmp_path / "workspace"
    asset_root.mkdir()
    project_subdir = asset_root / "project"
    project_subdir.mkdir()
    monkeypatch.chdir(project_subdir)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: asset_root)

    compose_file, env_file, env_example = cli_main._resolve_service_compose_paths()  # noqa: SLF001

    assert compose_file == asset_root / LOCAL_SERVICE_COMPOSE_FILE
    assert compose_file.is_absolute()
    assert env_file == asset_root / ".env"
    assert env_file.is_absolute()
    assert env_example == asset_root / ".env.example"
    assert env_example.is_absolute()


@pytest.mark.unit
def test_init_without_path_runs_service_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(state_dir))
    captured = _stub_bootstrap_mode(monkeypatch)

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap(["--format", "json"])

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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
