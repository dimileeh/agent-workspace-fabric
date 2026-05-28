"""CLI coverage for first-run onboarding guidance."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from tests.unit.cli.test_init_parts._bootstrap_helper import invoke_init_service_bootstrap


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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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
    """Keep final shared-key overlay comments at the end of the seeded file."""
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

    result = invoke_init_service_bootstrap()

    assert result.exit_code == 0, result.output
    assert (compose / ".env").read_text(encoding="utf-8") == (
        "\n".join(
            [
                "AWF_API_TOKEN=migrated-token",
                "AWF_COMPOSE_ONLY=compose-default",
                "# Root-only service setting",
                "AWF_ROOT_ONLY=root-value",
                "",
                "# Keep this note with the migrated API token.",
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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

    assert result.exit_code == 0, result.output
    assert not (compose / ".env").exists()
    env_file = tmp_path / ".env"
    assert env_file.exists()
    assert env_file.read_bytes() == root_example.read_bytes()
    assert "wrote .env from .env.example" in result.output


@pytest.mark.unit
def test_init_without_path_seeds_local_env_for_packaged_bootstrap_assets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Installed AWF packages must not try to write env files into package assets."""
    from awf.service import bootstrap as bootstrap_mod

    packaged_root = tmp_path / "site-packages" / "awf" / "bootstrap_assets"
    compose = packaged_root / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (packaged_root / ".env.example").write_text("AWF_API_TOKEN=packaged\n", encoding="utf-8")

    project_dir = tmp_path / "operator-project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    monkeypatch.setenv("AWF_HOST_WORK_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        bootstrap_mod,
        "is_packaged_bootstrap_asset_root",
        lambda path: path.resolve() == packaged_root.resolve(),
    )
    _stub_bootstrap_mode(monkeypatch, asset_root=packaged_root)

    result = invoke_init_service_bootstrap()

    assert result.exit_code == 0, result.output
    assert (project_dir / ".env").read_text(encoding="utf-8") == "AWF_API_TOKEN=packaged\n"
    assert not (compose / ".env").exists()
    assert "wrote .env from ../site-packages/awf/bootstrap_assets/.env.example" in result.output


@pytest.mark.unit
def test_service_env_resolution_ignores_current_compose_env_without_asset_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default resolution remains conservative for init/bootstrap seeding."""
    from awf.cli import init_ops as cli_main
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
def test_service_env_resolution_does_not_rediscover_asset_root_for_literal_env_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fallback literal `.env` should not run a second compose asset lookup."""
    from awf.cli import init_ops as cli_main
    from awf.service import bootstrap as bootstrap_mod

    asset_root = tmp_path / "awf"
    compose = asset_root / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env").write_text("AWF_API_TOKEN=from-compose\n", encoding="utf-8")
    working_dir = tmp_path / "project"
    working_dir.mkdir()

    monkeypatch.chdir(working_dir)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: asset_root)

    active_env_file, compose_env_file = cli_main._resolve_service_env_files(Path(".env"))  # noqa: SLF001

    assert active_env_file == Path(".env")
    assert compose_env_file is None


@pytest.mark.unit
def test_service_env_resolution_does_not_forward_root_env_without_asset_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Root `.env` remains a read source, not a Docker Compose `--env-file`."""
    from awf.cli import init_ops as cli_main
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
def test_compose_root_env_file_requires_absolute_compose_env_path(tmp_path: Path) -> None:
    """Only verified absolute Compose env paths participate in root-env overlays."""
    from awf.cli import init_ops as cli_main

    assert cli_main._compose_root_env_file(Path("docker/compose/.env")) is None  # noqa: SLF001
    assert cli_main._compose_root_env_file(  # noqa: SLF001
        tmp_path / "docker" / "compose" / ".env"
    ) == (tmp_path / ".env")


@pytest.mark.unit
def test_compose_root_env_file_uses_resolved_path_for_symlinked_env_file(
    tmp_path: Path,
) -> None:
    """A symlinked Compose env file must not be paired by lexical path alone."""
    from awf.cli import init_ops as cli_main

    compose_dir = tmp_path / "docker" / "compose"
    compose_dir.mkdir(parents=True)
    external_env = tmp_path / "external" / ".env"
    external_env.parent.mkdir()
    external_env.write_text("GITHUB_TOKEN=operator-secret\n", encoding="utf-8")
    (compose_dir / ".env").symlink_to(external_env)

    assert cli_main._compose_root_env_file(compose_dir / ".env") is None  # noqa: SLF001


@pytest.mark.unit
def test_trusted_service_compose_env_file_rejects_unrelated_local_service_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The trusted compose env path must come from the verified AWF asset root."""
    from awf.cli import init_ops as cli_main
    from awf.service import bootstrap as bootstrap_mod
    from awf.service.config import LOCAL_SERVICE_COMPOSE_ENV_FILE, LOCAL_SERVICE_COMPOSE_FILE

    asset_root = tmp_path / "asset-root"
    asset_compose_file = asset_root / LOCAL_SERVICE_COMPOSE_FILE
    asset_env_file = asset_root / LOCAL_SERVICE_COMPOSE_ENV_FILE
    asset_env_file.parent.mkdir(parents=True)
    asset_compose_file.write_text("services: {}\n", encoding="utf-8")
    asset_env_file.write_text("AWF_API_TOKEN=asset\n", encoding="utf-8")

    unrelated_root = tmp_path / "unrelated"
    unrelated_compose_file = unrelated_root / LOCAL_SERVICE_COMPOSE_FILE
    unrelated_env_file = unrelated_root / LOCAL_SERVICE_COMPOSE_ENV_FILE
    unrelated_env_file.parent.mkdir(parents=True)
    unrelated_compose_file.write_text("services: {}\n", encoding="utf-8")
    unrelated_env_file.write_text("AWF_API_TOKEN=unrelated\n", encoding="utf-8")

    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: asset_root)

    assert (
        cli_main._trusted_service_compose_env_file(  # noqa: SLF001
            unrelated_compose_file,
            unrelated_env_file,
        )
        is None
    )
    assert (
        cli_main._trusted_service_compose_env_file(  # noqa: SLF001
            asset_compose_file,
            asset_env_file,
        )
        == asset_env_file
    )


@pytest.mark.unit
def test_service_runtime_env_resolution_rejects_unrelated_local_service_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The live runtime path must use the verified AWF asset-root guard."""
    from awf.cli import init_ops as cli_main
    from awf.service import bootstrap as bootstrap_mod
    from awf.service.config import LOCAL_SERVICE_COMPOSE_ENV_FILE, LOCAL_SERVICE_COMPOSE_FILE

    asset_root = tmp_path / "asset-root"
    asset_compose_file = asset_root / LOCAL_SERVICE_COMPOSE_FILE
    asset_env_file = asset_root / LOCAL_SERVICE_COMPOSE_ENV_FILE
    asset_env_file.parent.mkdir(parents=True)
    asset_compose_file.write_text("services: {}\n", encoding="utf-8")
    asset_env_file.write_text("AWF_API_TOKEN=asset\n", encoding="utf-8")

    unrelated_root = tmp_path / "unrelated"
    unrelated_compose_file = unrelated_root / LOCAL_SERVICE_COMPOSE_FILE
    unrelated_env_file = unrelated_root / LOCAL_SERVICE_COMPOSE_ENV_FILE
    unrelated_env_file.parent.mkdir(parents=True)
    unrelated_compose_file.write_text("services: {}\n", encoding="utf-8")
    unrelated_env_file.write_text("AWF_API_TOKEN=unrelated\n", encoding="utf-8")

    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: asset_root)

    active_env_file, compose_env_file = cli_main._resolve_service_runtime_env_files(  # noqa: SLF001
        unrelated_compose_file,
        unrelated_env_file,
    )

    assert active_env_file == unrelated_env_file
    assert compose_env_file is None


@pytest.mark.unit
def test_service_env_resolution_ignores_current_compose_env_without_project_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A current-directory compose env is not enough without a verified asset root."""
    from awf.cli import init_ops as cli_main
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
def test_service_env_resolution_ignores_current_compose_env_with_project_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A project marker does not replace verified AWF source asset discovery."""
    from awf.cli import init_ops as cli_main
    from awf.service import bootstrap as bootstrap_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)

    (tmp_path / ".awf").mkdir()
    (tmp_path / ".awf" / "workspace.yml").write_text("version: 1\n", encoding="utf-8")
    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    (compose / ".env").write_text("AWF_API_TOKEN=local-service\n", encoding="utf-8")

    active_env_file, compose_env_file = cli_main._resolve_service_env_files(Path(".env"))  # noqa: SLF001

    assert active_env_file == Path(".env")
    assert compose_env_file is None


@pytest.mark.unit
def test_service_compose_env_file_rejects_matching_path_outside_asset_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only the verified AWF asset-root compose env can become a Compose env-file."""
    from awf.cli import init_ops as cli_main
    from awf.service import bootstrap as bootstrap_mod

    asset_root = tmp_path / "awf"
    asset_compose = asset_root / "docker" / "compose"
    asset_compose.mkdir(parents=True)
    (asset_compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    rogue_env = tmp_path / "other" / "docker" / "compose" / ".env"
    rogue_env.parent.mkdir(parents=True)
    rogue_env.write_text("AWF_API_TOKEN=wrong-project\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: asset_root)

    assert cli_main._service_compose_env_file(rogue_env) is None  # noqa: SLF001


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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap(["--provider", "github"])

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
def test_init_bootstrap_helper_rejects_unknown_provider_before_local_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy helper provider validation should fail before Compose discovery."""
    from awf.cli import init_ops

    def _fail_compose_resolution() -> None:
        pytest.fail("provider validation should run before Compose path resolution")

    monkeypatch.setattr(init_ops, "_resolve_service_compose_paths", _fail_compose_resolution)

    result = invoke_init_service_bootstrap(["--provider", "bogus"])

    assert result.exit_code == 2
    assert "error: unknown provider(s): bogus" in result.stderr
    assert "Traceback" not in result.stderr


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

    result = invoke_init_service_bootstrap(["--no-write-env"])

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap(["--no-write-env"])

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

    result = invoke_init_service_bootstrap()

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

    result = invoke_init_service_bootstrap()

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
    from awf.cli import init_ops as cli_main

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

    result = invoke_init_service_bootstrap()
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

    result = invoke_init_service_bootstrap()
    output = result.output

    assert result.exit_code == 0, output
    assert not (tmp_path / ".env").exists()
    assert "warning: could not write .env from .env.example: permission denied" in result.stdout
    assert "warning: could not write .env from .env.example" not in result.stderr
    assert len(captured["bootstrap_calls"]) == 1
    assert "Traceback" not in output
