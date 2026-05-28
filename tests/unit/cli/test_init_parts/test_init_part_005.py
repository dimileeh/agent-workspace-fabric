"""CLI coverage for first-run onboarding guidance."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

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
