"""Service configuration tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import awf.service.bootstrap as bootstrap
import awf.service.config as service_config
from awf.common.config import (
    ProductionSettingsError,
    Settings,
)
from awf.service.config import (
    LOCAL_SERVICE_COMPOSE_FILE,
    _redact_database_url,
    _resolve_service_work_dir,
    local_service_environ,
    resolve_local_service_provider_environ,
    resolve_service_settings,
)

_NON_DEFAULT_DATABASE_URL = "postgresql+asyncpg://awf:prod-pass@db.internal:5432/awf"
_STRONG_PRODUCTION_API_TOKEN = "prod-token-for-awf-operator-apis-32"


def _write_awf_source_root(checkout: Path) -> Path:
    fake_module = checkout / "src" / "awf" / "service" / "config.py"
    fake_module.parent.mkdir(parents=True, exist_ok=True)
    fake_module.write_text("# source module placeholder\n", encoding="utf-8")
    (checkout / "src" / "awf" / "__init__.py").write_text("", encoding="utf-8")
    (checkout / "pyproject.toml").write_text("[project]\nname = 'awf'\n", encoding="utf-8")
    compose_file = checkout / "docker" / "compose" / "local-service.yml"
    compose_file.parent.mkdir(parents=True, exist_ok=True)
    compose_file.write_text("services: {}\n", encoding="utf-8")
    return fake_module


def _write_awf_source_checkout(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "checkout"
    return checkout, _write_awf_source_root(checkout)


def _diagnostic_codes(error: ProductionSettingsError) -> set[str]:
    return {diagnostic.code for diagnostic in error.diagnostics}


def _diagnostic_fields(error: ProductionSettingsError) -> set[str]:
    return {diagnostic.field for diagnostic in error.diagnostics}


def _diagnostic_text(error: ProductionSettingsError) -> str:
    return " ".join(
        " ".join(
            (
                diagnostic.code,
                diagnostic.field,
                diagnostic.message,
                diagnostic.remediation,
            )
        )
        for diagnostic in error.diagnostics
    )


@pytest.mark.unit
def test_local_service_environ_loads_compose_env_with_host_override(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWF_GITHUB_TOKEN=ghp_compose_token\nGH_TOKEN=ghp_compose_gh_token\nEMPTY_VALUE=\n",
        encoding="utf-8",
    )

    environ = local_service_environ(
        {"AWF_GITHUB_TOKEN": "ghp_host_token", "PATH": "/usr/bin"},
        env_file=env_file,
    )

    assert environ["AWF_GITHUB_TOKEN"] == "ghp_host_token"
    assert environ["GH_TOKEN"] == "ghp_compose_gh_token"
    assert environ["EMPTY_VALUE"] == ""
    assert environ["PATH"] == "/usr/bin"


@pytest.mark.unit
def test_local_service_environ_derives_compose_postgres_password_from_database_url(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWF_DATABASE_URL=postgresql+asyncpg://awf:compose-secret@localhost:5433/awf\n",
        encoding="utf-8",
    )

    environ = local_service_environ({}, env_file=env_file)

    assert environ["AWF_POSTGRES_PASSWORD"] == "compose-secret"


@pytest.mark.unit
def test_local_service_environ_preserves_explicit_compose_postgres_password(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "AWF_DATABASE_URL=postgresql+asyncpg://awf:database-url-secret@localhost:5433/awf",
                "AWF_POSTGRES_PASSWORD=explicit-secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    environ = local_service_environ({}, env_file=env_file)

    assert environ["AWF_POSTGRES_PASSWORD"] == "explicit-secret"


@pytest.mark.unit
def test_provider_environ_ignores_cwd_compose_env_without_asset_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_env = tmp_path / "docker" / "compose" / ".env"
    compose_env.parent.mkdir(parents=True)
    compose_env.write_text("AWF_GITHUB_TOKEN=from-unrelated-project\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap, "get_bootstrap_asset_root", lambda: None)

    environ = resolve_local_service_provider_environ(
        provider_environ=None,
        environ={"PATH": "/usr/bin"},
        compose_file=LOCAL_SERVICE_COMPOSE_FILE,
        compose_env_file=None,
    )

    assert environ == {"PATH": "/usr/bin"}


@pytest.mark.unit
def test_provider_environ_ignores_absolute_default_compose_env_without_asset_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "docker" / "compose" / "local-service.yml"
    compose_file.parent.mkdir(parents=True)
    compose_file.write_text("services: {}\n", encoding="utf-8")
    compose_env = compose_file.parent / ".env"
    compose_env.write_text("AWF_GITHUB_TOKEN=from-unrelated-project\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap, "get_bootstrap_asset_root", lambda: None)

    environ = resolve_local_service_provider_environ(
        provider_environ=None,
        environ={"PATH": "/usr/bin"},
        compose_file=compose_file,
        compose_env_file=None,
    )

    assert environ == {"PATH": "/usr/bin"}


@pytest.mark.unit
def test_provider_environ_loads_default_compose_env_from_asset_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset_root = tmp_path / "awf"
    compose_env = asset_root / ".env"
    asset_root.mkdir(parents=True)
    compose_env.write_text("AWF_GITHUB_TOKEN=from-asset-root\n", encoding="utf-8")
    monkeypatch.chdir(asset_root)
    monkeypatch.setattr(bootstrap, "get_bootstrap_asset_root", lambda: asset_root)

    environ = resolve_local_service_provider_environ(
        provider_environ=None,
        environ={"PATH": "/usr/bin"},
        compose_file=LOCAL_SERVICE_COMPOSE_FILE,
        compose_env_file=None,
    )

    assert environ["AWF_GITHUB_TOKEN"] == "from-asset-root"
    assert environ["PATH"] == "/usr/bin"


@pytest.mark.unit
def test_provider_environ_loads_absolute_default_compose_env_from_asset_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset_root = tmp_path / "awf"
    compose_file = asset_root / "docker" / "compose" / "local-service.yml"
    compose_file.parent.mkdir(parents=True)
    compose_file.write_text("services: {}\n", encoding="utf-8")
    compose_env = asset_root / ".env"
    compose_env.write_text("AWF_GITHUB_TOKEN=from-asset-root\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap, "get_bootstrap_asset_root", lambda: asset_root)

    environ = resolve_local_service_provider_environ(
        provider_environ=None,
        environ={"PATH": "/usr/bin"},
        compose_file=compose_file,
        compose_env_file=None,
    )

    assert environ["AWF_GITHUB_TOKEN"] == "from-asset-root"
    assert environ["PATH"] == "/usr/bin"


@pytest.mark.unit
def test_local_service_asset_path_rejects_absolute_path_outside_asset_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset_root = tmp_path / "awf"
    asset_root.mkdir()
    inside = asset_root / LOCAL_SERVICE_COMPOSE_FILE
    outside = tmp_path / "outside" / LOCAL_SERVICE_COMPOSE_FILE
    monkeypatch.setattr(bootstrap, "get_bootstrap_asset_root", lambda: asset_root)

    assert service_config._local_service_asset_path(inside) == inside.resolve()  # noqa: SLF001
    assert service_config._local_service_asset_path(outside) is None  # noqa: SLF001


@pytest.mark.unit
def test_provider_environ_uses_explicit_compose_env_without_asset_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_env = tmp_path / "compose.env"
    compose_env.write_text("AWF_GITHUB_TOKEN=from-explicit-env\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "get_bootstrap_asset_root", lambda: None)

    environ = resolve_local_service_provider_environ(
        provider_environ=None,
        environ={"PATH": "/usr/bin"},
        compose_file=LOCAL_SERVICE_COMPOSE_FILE,
        compose_env_file=compose_env,
    )

    assert environ["AWF_GITHUB_TOKEN"] == "from-explicit-env"
    assert environ["PATH"] == "/usr/bin"


@pytest.mark.unit
def test_host_awf_host_work_dir_overrides_host_awf_work_dir(tmp_path: Path) -> None:
    settings = resolve_service_settings(
        Settings(_env_file=None),
        environ={
            "AWF_WORK_DIR": str(tmp_path / "explicit-service-state"),
            "AWF_HOST_WORK_DIR": str(tmp_path / "host-service-state"),
        },
    )

    assert settings.work_dir == str(tmp_path / "host-service-state")


@pytest.mark.unit
def test_explicit_awf_work_dir_environment_is_service_work_dir(tmp_path: Path) -> None:
    work_dir = tmp_path / "explicit-service-state"

    settings = resolve_service_settings(
        Settings(_env_file=None),
        environ={"AWF_WORK_DIR": str(work_dir)},
    )

    assert settings.work_dir == str(work_dir)


@pytest.mark.unit
def test_compose_file_awf_work_dir_is_used_when_host_env_has_no_work_dir(tmp_path: Path) -> None:
    work_dir = tmp_path / "compose-explicit-service-state"

    resolved = _resolve_service_work_dir(
        Settings(_env_file=None),
        environ={"AWF_WORK_DIR": str(work_dir)},
        host_environ={},
    )

    assert resolved == str(work_dir)


@pytest.mark.unit
def test_host_awf_work_dir_precedes_compose_file_default(tmp_path: Path) -> None:
    compose_env_file = tmp_path / "compose.env"
    compose_env_file.write_text(
        f"AWF_HOST_WORK_DIR={tmp_path / 'compose-service-state'}\n",
        encoding="utf-8",
    )
    environ = local_service_environ(
        {"AWF_WORK_DIR": str(tmp_path / "explicit-service-state")},
        env_file=compose_env_file,
    )

    work_dir = _resolve_service_work_dir(
        Settings(_env_file=None),
        environ,
        host_environ={"AWF_WORK_DIR": str(tmp_path / "explicit-service-state")},
    )

    assert work_dir == str(tmp_path / "explicit-service-state")


@pytest.mark.unit
def test_project_default_awf_work_dir_defers_to_compose_file_host_work_dir(tmp_path: Path) -> None:
    compose_env_file = tmp_path / "compose.env"
    compose_env_file.write_text(
        f"AWF_HOST_WORK_DIR={tmp_path / 'compose-service-state'}\n",
        encoding="utf-8",
    )
    environ = local_service_environ(
        {"HOME": str(tmp_path / "home"), "AWF_WORK_DIR": ".awf"},
        env_file=compose_env_file,
    )

    work_dir = _resolve_service_work_dir(
        Settings(_env_file=None),
        environ,
        host_environ={"HOME": str(tmp_path / "home"), "AWF_WORK_DIR": ".awf"},
    )

    assert work_dir == str(tmp_path / "compose-service-state")


@pytest.mark.unit
def test_redact_database_url_handles_malformed_secret_values() -> None:
    assert _redact_database_url("postgresql://user:secret@host:bad/db") == "<redacted>"
    assert _redact_database_url("") == ""


@pytest.mark.unit
def test_local_service_compose_sets_stable_worker_node_id_for_control_plane_services() -> None:
    compose_path = Path(__file__).resolve().parents[4] / "docker" / "compose" / "local-service.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    for service_name in ("api", "worker", "migrate"):
        service = compose["services"][service_name]
        assert service["environment"]["AWF_WORKER_NODE_ID"] == "local"
        assert service["environment"]["AWF_PLANNING_MAX_ITERATIONS_DEFAULT"].endswith(":-3}")
        assert service["environment"]["AWF_GITHUB_TOKEN"].startswith("${AWF_GITHUB_TOKEN:-")
        assert service["environment"]["GH_TOKEN"].startswith("${AWF_GITHUB_TOKEN:-")
        assert service["environment"]["GITHUB_TOKEN"].startswith("${AWF_GITHUB_TOKEN:-")


@pytest.mark.unit
def test_explicit_worker_node_id_is_preserved_for_non_local_multi_node_deployments() -> None:
    base = Settings(_env_file=None, worker_node_id="prod-node-a")

    settings = resolve_service_settings(
        base,
        environ={"AWF_WORKER_NODE_ID": "prod-node-a"},
    )

    assert settings.node_id == "prod-node-a"


@pytest.mark.unit
def test_local_service_accepts_standard_gh_token_fallback() -> None:
    settings = resolve_service_settings(
        Settings(_env_file=None),
        environ={"GH_TOKEN": "ghp_service_token"},
    )

    assert settings.github_token == "ghp_service_token"


@pytest.mark.unit
def test_awf_github_token_precedes_standard_gh_token_fallback() -> None:
    settings = resolve_service_settings(
        Settings(_env_file=None, github_token="ghp_awf_token"),
        environ={
            "AWF_GITHUB_TOKEN": "ghp_awf_token",
            "GH_TOKEN": "ghp_standard_token",
        },
    )

    assert settings.github_token == "ghp_awf_token"


@pytest.mark.unit
def test_awf_github_token_resolves_from_explicit_service_environment() -> None:
    settings = resolve_service_settings(
        Settings(_env_file=None),
        environ={"AWF_GITHUB_TOKEN": "ghp_explicit_awf_token"},
    )

    assert settings.github_token == "ghp_explicit_awf_token"
