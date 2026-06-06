"""Focused coverage for local service environment resolution helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from awf.common.config import Settings
from awf.service import bootstrap as bootstrap_mod
from awf.service import config as config_mod


def _seed_source_root(root: Path) -> None:
    (root / "src" / "awf").mkdir(parents=True)
    (root / "src" / "awf" / "__init__.py").write_text("", encoding="utf-8")
    (root / "docker" / "compose").mkdir(parents=True)
    (root / "docker" / "compose" / "local-service.yml").write_text(
        "services: {}\n",
        encoding="utf-8",
    )
    (root / "compose.yaml").write_text(
        "include:\n  - ./docker/compose/local-service.yml\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text("[project]\nname = 'awf-test'\n", encoding="utf-8")


@pytest.mark.unit
def test_project_dotenv_lookup_skips_missing_candidates_and_caches_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("AWF_API_HOST_PORT=8123\n", encoding="utf-8")
    calls: list[Path] = []

    def _read_env(path: Path, **_kwargs: object) -> dict[str, str]:
        calls.append(path)
        return {"AWF_API_HOST_PORT": "8123"}

    monkeypatch.setattr(
        config_mod,
        "_project_dotenv_candidates",
        lambda: (tmp_path / "missing.env", env_file),
    )
    monkeypatch.setattr(config_mod, "compose_env_file_values", _read_env)
    lookup = config_mod._ProjectDotenvLookup()

    assert lookup.value("AWF_API_HOST_PORT") == "8123"
    assert lookup.value("awf_api_host_port") == "8123"
    assert calls == [env_file]


@pytest.mark.unit
def test_service_config_payload_formats_cutoff_and_redacts_invalid_database_url() -> None:
    cutoff = datetime(2026, 6, 6, 10, 0, tzinfo=UTC)
    payload = config_mod.service_config_payload(
        config_mod.ServiceSettings(
            service_name="awf",
            env="development",
            api_base_url="http://localhost:8000",
            database_url="not a database url",
            docker_host="unix:///var/run/docker.sock",
            agent_runtime_image="awf-agent:latest",
            work_dir="/tmp/awf",
            api_token="api-token",
            github_token="gh-token",
            worker_poll_interval_seconds=1.0,
            worker_max_concurrent_provisions=1,
            network_posture_open_legacy_cutoff=cutoff,
        )
    )

    assert payload["network_posture_open_legacy_cutoff"] == cutoff.isoformat()
    assert payload["database_url"] == "<redacted>"
    assert payload["api_token"] == "<redacted>"
    assert payload["github_token"] == "<redacted>"


@pytest.mark.unit
def test_service_settings_rejects_non_positive_startup_log_tail() -> None:
    with pytest.raises(ValueError, match="service_startup_log_tail_lines must be > 0"):
        config_mod.ServiceSettings(
            service_name="awf",
            env="development",
            api_base_url="http://localhost:8000",
            database_url=config_mod.DEFAULT_LOCAL_SERVICE_DATABASE_URL,
            docker_host="unix:///var/run/docker.sock",
            agent_runtime_image="awf-agent:latest",
            work_dir="/tmp/awf",
            api_token=None,
            github_token=None,
            worker_poll_interval_seconds=1.0,
            worker_max_concurrent_provisions=1,
            service_startup_log_tail_lines=0,
        )


@pytest.mark.unit
def test_local_service_environ_derives_postgres_password_and_defaults() -> None:
    environ = config_mod.local_service_environ(
        {
            "AWF_DATABASE_URL": "postgresql+asyncpg://awf:secret@localhost:5432/awf",
            "AWF_API_TOKEN": "",
        },
        env_file=None,
    )

    assert environ["AWF_POSTGRES_PASSWORD"] == "secret"
    assert environ["AWF_API_TOKEN"] == config_mod.DEFAULT_LOCAL_SERVICE_API_TOKEN


@pytest.mark.unit
def test_local_service_environ_ignores_unparseable_database_url() -> None:
    environ = config_mod.local_service_environ(
        {"AWF_DATABASE_URL": "://not-a-url"},
        env_file=None,
    )

    assert environ["AWF_POSTGRES_PASSWORD"] == config_mod.DEFAULT_LOCAL_SERVICE_POSTGRES_PASSWORD


@pytest.mark.unit
def test_provider_environ_uses_local_service_asset_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _seed_source_root(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("XAI_API_KEY=asset-key\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: tmp_path)

    environ = config_mod.resolve_local_service_provider_environ(
        provider_environ=None,
        environ={},
        compose_file=config_mod.LOCAL_SERVICE_COMPOSE_FILE,
    )

    assert environ["XAI_API_KEY"] == "asset-key"


@pytest.mark.unit
def test_provider_environ_uses_custom_adjacent_env_file_when_omitted(
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "custom-compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text("CURSOR_API_KEY=cursor-key\n", encoding="utf-8")

    environ = config_mod.resolve_local_service_provider_environ(
        provider_environ=None,
        environ={},
        compose_file=compose_file,
    )

    assert environ["CURSOR_API_KEY"] == "cursor-key"


@pytest.mark.unit
def test_provider_environ_prefers_explicit_mapping() -> None:
    provider_environ = {"CURSOR_API_KEY": "explicit"}

    environ = config_mod.resolve_local_service_provider_environ(
        provider_environ=provider_environ,
        environ={"CURSOR_API_KEY": "host"},
        compose_file=None,
    )

    assert environ is provider_environ


@pytest.mark.unit
def test_provider_environ_omitted_custom_adjacent_missing_returns_host_env(
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "custom-compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")

    environ = config_mod.resolve_local_service_provider_environ(
        provider_environ=None,
        environ={"KEEP": "host"},
        compose_file=compose_file,
    )

    assert environ == {"KEEP": "host"}


@pytest.mark.unit
def test_provider_environ_ignores_custom_adjacent_env_file_when_explicitly_disabled(
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "custom-compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text("CURSOR_API_KEY=cursor-key\n", encoding="utf-8")

    environ = config_mod.resolve_local_service_provider_environ(
        provider_environ=None,
        environ={"KEEP": "host"},
        compose_file=compose_file,
        compose_env_file=None,
    )

    assert environ == {"KEEP": "host"}


@pytest.mark.unit
def test_local_service_asset_path_confines_absolute_paths_to_asset_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asset_root = tmp_path / "asset"
    inside = asset_root / ".env"
    outside = tmp_path / "outside.env"
    inside.parent.mkdir(parents=True)
    inside.write_text("", encoding="utf-8")
    outside.write_text("", encoding="utf-8")
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: asset_root)

    assert config_mod._local_service_asset_path(inside) == inside
    assert config_mod._local_service_asset_path(outside) is None


@pytest.mark.unit
def test_local_service_asset_path_returns_none_without_asset_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)

    assert config_mod._local_service_asset_path(Path(".env")) is None


@pytest.mark.unit
def test_local_service_compose_path_helpers_cover_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("AWF_API_TOKEN=token\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(config_mod, "_awf_source_search_roots", lambda _start: ())

    assert config_mod.resolve_local_service_compose_env_file() == env_file
    assert config_mod.resolve_local_service_compose_env_file(Path("custom.env")) is None
    assert config_mod._is_local_service_compose_file_path(tmp_path / "compose.yaml")
    assert not config_mod._is_local_service_compose_env_path(tmp_path / "other.env")
    assert not config_mod._can_use_adjacent_provider_env_file(
        tmp_path / "other.env",
        config_mod.LOCAL_SERVICE_COMPOSE_FILE,
    )


@pytest.mark.unit
def test_resolve_local_service_compose_env_file_handles_absolute_and_relative_paths(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("AWF_API_TOKEN=token\n", encoding="utf-8")

    assert config_mod.resolve_local_service_compose_env_file(env_file) == env_file
    assert config_mod.resolve_local_service_compose_env_file(tmp_path / "missing.env") is None


@pytest.mark.unit
def test_project_dotenv_candidates_include_ancestors_when_source_env_resolves(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    nested = root / "apps" / "console"
    nested.mkdir(parents=True)
    source_env = root / ".env"
    monkeypatch.chdir(nested)
    monkeypatch.setattr(config_mod, "resolve_local_service_compose_env_file", lambda: source_env)

    candidates = config_mod._project_dotenv_candidates()

    assert candidates == (nested / ".env", root / "apps" / ".env", root / ".env")
    assert config_mod._project_dotenv_from_compose_env_file(source_env) == root / ".env"


@pytest.mark.unit
def test_project_dotenv_candidates_fall_back_to_source_roots_and_dedupe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    nested = root / "sub"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    monkeypatch.setattr(config_mod, "resolve_local_service_compose_env_file", lambda: None)
    monkeypatch.setattr(config_mod, "_awf_source_search_roots", lambda _cwd: (root,))

    candidates = config_mod._project_dotenv_candidates()

    assert candidates == (nested / ".env", root / ".env")
    assert config_mod._dedupe_paths([root / ".env", root / "sub" / ".." / ".env"]) == (
        root / ".env",
    )


@pytest.mark.unit
def test_explicit_url_helpers_treat_project_defaults_as_derivable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                f"AWF_API_BASE_URL={config_mod.DEFAULT_LOCAL_SERVICE_API_BASE_URL}",
                f"AWF_DATABASE_URL={config_mod.DEFAULT_LOCAL_SERVICE_DATABASE_URL}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_mod, "_project_dotenv_candidates", lambda: (env_file,))
    lookup = config_mod._ProjectDotenvLookup()

    assert not config_mod._api_base_url_env_is_explicit(
        {"AWF_API_BASE_URL": config_mod.DEFAULT_LOCAL_SERVICE_API_BASE_URL},
        {"AWF_API_HOST_PORT": "8010"},
        project_dotenv_lookup=lookup,
    )
    assert not config_mod._database_url_env_is_explicit(
        {"AWF_DATABASE_URL": config_mod.DEFAULT_LOCAL_SERVICE_DATABASE_URL},
        {"AWF_POSTGRES_HOST_PORT": "5433"},
        project_dotenv_lookup=lookup,
    )
    assert not config_mod._api_base_url_env_is_explicit({}, {})
    assert config_mod._api_base_url_env_is_explicit({"AWF_API_BASE_URL": "http://api"}, {})
    assert not config_mod._api_base_url_env_is_explicit(
        {
            "AWF_API_BASE_URL": config_mod.DEFAULT_LOCAL_SERVICE_API_BASE_URL,
            "AWF_API_HOST_PORT": "8010",
        },
        {},
    )
    assert not config_mod._database_url_env_is_explicit({}, {})
    assert config_mod._database_url_env_is_explicit(
        {"AWF_DATABASE_URL": "postgresql+asyncpg://awf:pw@db:5432/awf"},
        {},
    )
    assert not config_mod._database_url_env_is_explicit(
        {
            "AWF_DATABASE_URL": config_mod.DEFAULT_LOCAL_SERVICE_DATABASE_URL,
            "AWF_POSTGRES_HOST_PORT": "5433",
        },
        {},
    )


@pytest.mark.unit
def test_default_url_helpers_parse_ports_and_reject_invalid_values() -> None:
    assert (
        config_mod._default_local_service_api_base_url({"AWF_API_HOST_PORT": "8010"})
        == "http://localhost:8010"
    )
    assert config_mod._default_local_service_database_url(
        {"AWF_POSTGRES_HOST_PORT": "5544"}
    ).endswith("@localhost:5544/awf")
    with pytest.raises(ValueError, match="AWF_API_HOST_PORT must be an integer"):
        config_mod._default_local_service_api_base_url({"AWF_API_HOST_PORT": "not-int"})
    with pytest.raises(ValueError, match="AWF_POSTGRES_HOST_PORT must be an integer"):
        config_mod._default_local_service_database_url({"AWF_POSTGRES_HOST_PORT": "70000"})


@pytest.mark.unit
def test_settings_explicit_helpers_distinguish_init_fields_and_env_defaults() -> None:
    base = Settings(_env_file=None)
    explicit_api = Settings(_env_file=None, api_base_url="http://custom")
    explicit_db = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://awf:pw@db:5432/awf",
    )

    assert not config_mod._settings_api_base_url_is_explicit(
        base,
        {"AWF_API_HOST_PORT": "8010"},
        require_init_field=True,
    )
    assert config_mod._settings_api_base_url_is_explicit(explicit_api, {})
    assert not config_mod._settings_database_url_is_explicit(
        base,
        {"AWF_POSTGRES_HOST_PORT": "5433"},
        require_init_field=True,
    )
    assert config_mod._settings_database_url_is_explicit(explicit_db, {})


@pytest.mark.unit
def test_resolve_service_work_dir_uses_host_then_container_overrides() -> None:
    settings = Settings(_env_file=None)

    assert (
        config_mod._resolve_service_work_dir(
            settings,
            {},
            host_environ={"AWF_HOST_WORK_DIR": "/from-host-env"},
        )
        == "/from-host-env"
    )
    assert (
        config_mod._resolve_service_work_dir(
            settings,
            {},
            host_environ={"AWF_WORK_DIR": "/from-host-container-env"},
        )
        == "/from-host-container-env"
    )
    custom_settings = Settings(_env_file=None, work_dir="/from-settings")
    assert config_mod._resolve_service_work_dir(custom_settings, {}, host_environ={}) == (
        "/from-settings"
    )
    assert (
        config_mod._resolve_service_work_dir(
            settings,
            {"AWF_HOST_WORK_DIR": "/from-service-env"},
            host_environ={},
        )
        == "/from-service-env"
    )
    assert (
        config_mod._resolve_service_work_dir(
            settings,
            {"AWF_WORK_DIR": "/from-container-env"},
            host_environ={},
        )
        == "/from-container-env"
    )


@pytest.mark.unit
def test_resolve_service_settings_reads_project_dotenv_defaults_when_host_shell_matches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _seed_source_root(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                f"AWF_API_BASE_URL={config_mod.DEFAULT_LOCAL_SERVICE_API_BASE_URL}",
                f"AWF_DATABASE_URL={config_mod.DEFAULT_LOCAL_SERVICE_DATABASE_URL}",
                "AWF_API_HOST_PORT=8010",
                "AWF_POSTGRES_HOST_PORT=5433",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: tmp_path)
    for key in (
        "AWF_API_BASE_URL",
        "AWF_DATABASE_URL",
        "AWF_API_HOST_PORT",
        "AWF_POSTGRES_HOST_PORT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWF_API_BASE_URL", config_mod.DEFAULT_LOCAL_SERVICE_API_BASE_URL)
    monkeypatch.setenv("AWF_DATABASE_URL", config_mod.DEFAULT_LOCAL_SERVICE_DATABASE_URL)

    settings = config_mod.resolve_service_settings(Settings(_env_file=None))

    assert settings.api_base_url == "http://localhost:8010"
    assert settings.database_url.endswith("@localhost:5433/awf")
