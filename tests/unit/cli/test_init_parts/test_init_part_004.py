"""CLI coverage for first-run onboarding guidance."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
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
def test_init_without_path_ensures_state_directory_and_prints_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    state_dir = tmp_path / "state-fresh"
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(state_dir))
    _stub_bootstrap_mode(monkeypatch)

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

    assert result.exit_code == 0, result.output
    assert compose_state_dir.exists()
    assert compose_state_dir.is_dir()
    assert not (host_home / ".awf" / "service").exists()
    assert str(compose_state_dir.resolve()) in result.output


@pytest.mark.unit
def test_init_without_path_prefers_shell_host_work_dir_over_seeded_compose_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Honor a shell state-dir override during first-run compose env seeding."""
    monkeypatch.chdir(tmp_path)
    host_home = tmp_path / "home"
    shell_state_dir = tmp_path / "shell-state"
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env.example").write_text(
        "AWF_API_HOST_PORT=8000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(shell_state_dir))
    _stub_bootstrap_mode(monkeypatch, asset_root=tmp_path)

    result = invoke_init_service_bootstrap()

    assert result.exit_code == 0, result.output
    assert shell_state_dir.exists()
    assert shell_state_dir.is_dir()
    assert not (host_home / ".awf" / "service").exists()
    assert str(shell_state_dir.resolve()) in result.output


@pytest.mark.unit
def test_init_without_path_uses_host_home_when_service_env_sets_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """HOME is a host path fallback, not a service env setting."""
    from awf.service import config as config_mod

    monkeypatch.chdir(tmp_path)
    host_home = tmp_path / "host-home"
    service_home = tmp_path / "service-home"
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.delenv("AWF_HOST_WORK_DIR", raising=False)
    monkeypatch.setattr(
        config_mod,
        "local_service_environ",
        lambda **_kwargs: {"HOME": str(service_home)},
    )
    _stub_bootstrap_mode(monkeypatch)

    result = invoke_init_service_bootstrap()

    assert result.exit_code == 0, result.output
    assert (host_home / ".awf" / "service").exists()
    assert not (service_home / ".awf" / "service").exists()
    assert str((host_home / ".awf" / "service").resolve()) in result.output


@pytest.mark.unit
def test_init_with_path_keeps_existing_project_onboarding_behavior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_local_prerequisites(monkeypatch)

    result = _runner.invoke(app, ["init", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "AWF init: local onboarding readiness check" in result.output
    assert "awf init <path> --write-profile --yes" in result.output


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

    output = result.stderr
    assert result.exit_code == 2
    assert "--skip-agent-runtime-build" in output
    assert "not valid for project onboarding" in output
    assert "awf service bootstrap" in output
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

    output = result.stderr
    assert result.exit_code == 2
    assert "--no-write-env" in output
    assert "not valid for project onboarding" in output
    assert "awf service bootstrap" in output
    assert "Traceback" not in output


@pytest.mark.unit
def test_init_with_path_accepts_format_json_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_local_prerequisites(monkeypatch)

    result = _runner.invoke(
        app,
        ["init", str(tmp_path), "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "preview"
    assert payload["profile_exists"] is False


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

    output = result.stderr
    assert result.exit_code == 2
    assert "--include-smoke-request" in output
    assert "project path" in output
    assert "Traceback" not in output


@pytest.mark.unit
def test_getting_started_recommends_setup_start_then_project_init() -> None:
    """Assert public first-run guidance follows the locked T01 grammar."""
    readme = Path("docs/GETTING_STARTED.md").read_text(encoding="utf-8")
    _, start_heading, first_run_tail = readme.partition("### Recommended First-Run Sequence")
    assert start_heading, "Markdown heading '### Recommended First-Run Sequence' not found"
    first_run, end_heading, _ = first_run_tail.partition("### Configure Environment")
    assert end_heading, (
        "Markdown heading '### Configure Environment' not found after "
        "'### Recommended First-Run Sequence'"
    )

    assert "awf setup" in first_run
    assert "awf start" in first_run
    assert "awf init <path>" in first_run
    assert "awf smoke run --project <path> --mocked-local --format pretty" in first_run
    assert "awf service bootstrap" not in first_run
    assert "AWF_SETUP_PLACEHOLDER" not in first_run
    assert "AWF_START_PLACEHOLDER" not in first_run
    assert "awf service status --format pretty" in readme
    assert "docker/compose/.env" in readme
    assert "`awf init`. With no arguments it bootstraps" not in readme
    assert "cp .env.example .env" not in readme


@pytest.mark.unit
def test_getting_started_compose_env_snippet_feeds_setup_and_start() -> None:
    """Regression: avoid duplicate token keys in docker/compose/.env examples."""
    readme = Path("docs/GETTING_STARTED.md").read_text(encoding="utf-8")
    snippet_start = readme.index("env_example=docker/compose/.env.example")
    snippet_end = readme.index("uv run --python 3.12 --extra dev awf setup")
    snippet = readme[snippet_start:snippet_end]

    assert "grep -vE '^(AWF_API_TOKEN|AWF_GITHUB_TOKEN)='" in readme
    assert "} > docker/compose/.env" in readme
    assert ">> docker/compose/.env" not in readme
    assert 'echo "Missing env template: docker/compose/.env.example or .env.example" >&2' in snippet
    assert "exit 1" in snippet
    assert snippet.index("exit 1") < snippet.index("{")
    assert "uv run --python 3.12 --extra dev awf setup" in readme
    assert "uv run --python 3.12 --extra dev awf start" in readme


@pytest.mark.unit
def test_project_onboarding_doc_distinguishes_init_modes() -> None:
    """Assert onboarding docs distinguish project init from service startup."""
    doc = Path("docs/PROJECT_ONBOARDING.md").read_text(encoding="utf-8")

    assert "awf setup" in doc
    assert "awf start" in doc
    assert "awf init" in doc
    assert "awf init <path>" in doc
    assert "`awf init` (no path)" not in doc
    assert "AWF_SETUP_PLACEHOLDER" not in doc
    assert "AWF_START_PLACEHOLDER" not in doc


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


@pytest.mark.unit
def test_init_without_path_rejects_unknown_provider_without_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))

    result = _runner.invoke(app, ["init", "--provider", "bogus"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "AWF_INIT_REQUIRES_PROJECT_PATH" in result.stderr
    assert "--provider" in result.stderr
    assert "unknown provider" not in result.stderr
    assert "Traceback" not in result.stderr


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

    result = invoke_init_service_bootstrap()

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
