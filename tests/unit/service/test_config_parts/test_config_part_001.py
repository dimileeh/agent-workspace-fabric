"""Service configuration tests."""

from __future__ import annotations

import ast
import gc
import inspect
from pathlib import Path

import pytest
from pydantic import AliasChoices, AliasPath, Field

import awf.common.config as common_config
import awf.service.config as service_config
from awf.common.config import (
    DEFAULT_LOCAL_DATABASE_URL,
    ProductionSettingsError,
    Settings,
    settings_guardrails,
    validate_production_settings,
)
from awf.service.config import (
    DEFAULT_LOCAL_SERVICE_API_BASE_URL,
    DEFAULT_LOCAL_SERVICE_DATABASE_URL,
    LOCAL_SERVICE_COMPOSE_FILE,
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
def test_compose_env_file_sentinel_is_public_service_contract() -> None:
    assert isinstance(
        service_config.COMPOSE_ENV_FILE_OMITTED,
        service_config.ComposeEnvFileOmitted,
    )
    assert {
        "COMPOSE_ENV_FILE_OMITTED",
        "ComposeEnvFileInput",
        "ComposeEnvFileOmitted",
    }.issubset(service_config.__all__)

    repo_root = Path(__file__).resolve().parents[4]
    service_modules = (
        repo_root / "src" / "awf" / "service" / "status.py",
        repo_root / "src" / "awf" / "service" / "readiness.py",
        repo_root / "src" / "awf" / "service" / "doctor" / "__init__.py",
        repo_root / "src" / "awf" / "service" / "support_bundle.py",
    )
    private_sentinel_names = {
        "_COMPOSE_ENV_FILE_OMITTED",
        "_ComposeEnvFileInput",
        "_ComposeEnvFileOmitted",
    }
    private_imports: list[tuple[str, str]] = []
    for module_path in service_modules:
        tree = ast.parse(
            module_path.read_text(encoding="utf-8"),
            filename=str(module_path),
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != "awf.service.config":
                continue
            private_imports.extend(
                (module_path.relative_to(repo_root).as_posix(), alias.name)
                for alias in node.names
                if alias.name in private_sentinel_names
            )

    assert private_imports == []


@pytest.mark.unit
def test_custom_adjacent_provider_env_file_is_used_for_non_local_compose(
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=from-env-file\n", encoding="utf-8")

    environ = resolve_local_service_provider_environ(
        provider_environ=None,
        environ={},
        compose_file=compose_file,
    )

    assert environ["OPENAI_API_KEY"] == "from-env-file"
    assert service_config._can_use_adjacent_provider_env_file(env_file, compose_file)  # noqa: SLF001
    assert service_config._can_use_adjacent_provider_env_file(  # noqa: SLF001
        LOCAL_SERVICE_COMPOSE_FILE.parent / ".env",
        LOCAL_SERVICE_COMPOSE_FILE,
    )


@pytest.mark.unit
def test_local_service_environ_preserves_host_port_overrides(tmp_path: Path) -> None:
    env_file = tmp_path / "docker" / "compose" / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        "AWF_POSTGRES_HOST_PORT=15433\nAWF_API_HOST_PORT=9100\n",
        encoding="utf-8",
    )
    environ = local_service_environ({}, env_file=env_file)

    assert environ["AWF_POSTGRES_HOST_PORT"] == "15433"
    assert environ["AWF_API_HOST_PORT"] == "9100"


@pytest.mark.unit
def test_service_settings_default_database_url_uses_postgres_host_port_override() -> None:
    settings = resolve_service_settings(
        Settings(_env_file=None),
        environ={"AWF_POSTGRES_HOST_PORT": "15433"},
    )

    assert settings.database_url == "postgresql+asyncpg://awf:awf_dev@localhost:15433/awf"


@pytest.mark.unit
@pytest.mark.parametrize("host_port", ["not-a-port", "0", "65536"])
def test_service_settings_rejects_invalid_postgres_host_port(host_port: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        resolve_service_settings(
            Settings(_env_file=None),
            environ={"AWF_POSTGRES_HOST_PORT": host_port},
        )

    message = str(exc_info.value)
    assert "AWF_POSTGRES_HOST_PORT" in message
    assert repr(host_port) in message
    assert "integer between 1 and 65535" in message


@pytest.mark.unit
def test_service_settings_default_database_url_uses_compose_env_host_port_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout, _ = _write_awf_source_checkout(tmp_path)
    compose_env_file = checkout / "docker" / "compose" / ".env"
    compose_env_file.write_text("AWF_POSTGRES_HOST_PORT=15433\n", encoding="utf-8")
    monkeypatch.chdir(checkout)
    monkeypatch.delenv("AWF_DATABASE_URL", raising=False)
    monkeypatch.delenv("AWF_POSTGRES_HOST_PORT", raising=False)

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.database_url == "postgresql+asyncpg://awf:awf_dev@localhost:15433/awf"


@pytest.mark.unit
def test_service_settings_uses_checkout_root_compose_env_from_subdirectory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout, _ = _write_awf_source_checkout(tmp_path)
    nested = checkout / "src" / "awf"
    (checkout / ".git").mkdir()
    compose_env_file = checkout / "docker" / "compose" / ".env"
    compose_env_file.write_text("AWF_POSTGRES_HOST_PORT=15433\n", encoding="utf-8")
    monkeypatch.chdir(nested)
    monkeypatch.delenv("AWF_DATABASE_URL", raising=False)
    monkeypatch.delenv("AWF_POSTGRES_HOST_PORT", raising=False)

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.database_url == "postgresql+asyncpg://awf:awf_dev@localhost:15433/awf"


@pytest.mark.unit
def test_settings_constructor_fields_are_not_pydantic_private_dual_storage() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://awf:pw@db.internal:5432/awf",
        api_base_url="http://127.0.0.1:9300",
    )

    assert service_config._settings_init_fields(settings) == frozenset(  # noqa: SLF001
        {"database_url", "api_base_url"}
    )
    assert "_awf_init_fields" not in settings.__dict__
    assert "_awf_init_fields" not in (getattr(settings, "__pydantic_private__", {}) or {})


@pytest.mark.unit
def test_untracked_settings_constructor_fields_do_not_suppress_port_derivation() -> None:
    settings = Settings.model_construct(
        database_url="postgresql+asyncpg://awf:pw@db.internal:5432/awf",
        api_base_url="http://127.0.0.1:9300",
    )

    assert service_config._settings_init_fields(settings) == frozenset()  # noqa: SLF001

    service_settings = resolve_service_settings(
        settings,
        environ={
            "AWF_POSTGRES_HOST_PORT": "15433",
            "AWF_API_HOST_PORT": "9100",
        },
    )

    assert service_settings.database_url == ("postgresql+asyncpg://awf:awf_dev@localhost:15433/awf")
    assert service_settings.api_base_url == "http://localhost:9100"


@pytest.mark.unit
def test_settings_constructor_fields_are_tracked_per_equal_settings_instance() -> None:
    default_settings = Settings(_env_file=None)
    explicit_default_settings = Settings(
        _env_file=None,
        api_base_url="http://localhost:8000",
    )

    assert default_settings == explicit_default_settings
    assert service_config._settings_init_fields(default_settings) == frozenset()  # noqa: SLF001
    assert service_config._settings_init_fields(explicit_default_settings) == frozenset(  # noqa: SLF001
        {"api_base_url"}
    )


@pytest.mark.unit
def test_settings_constructor_fields_track_pydantic_alias_input() -> None:
    class AliasSettings(Settings):
        api_base_url: str = Field(
            default=DEFAULT_LOCAL_SERVICE_API_BASE_URL,
            alias="apiBaseUrl",
        )

    settings = AliasSettings(
        _env_file=None,
        apiBaseUrl=DEFAULT_LOCAL_SERVICE_API_BASE_URL,
    )

    assert service_config._settings_init_fields(settings) == frozenset({"api_base_url"})  # noqa: SLF001

    service_settings = resolve_service_settings(
        settings,
        environ={"AWF_API_HOST_PORT": "9100"},
    )

    assert service_settings.api_base_url == DEFAULT_LOCAL_SERVICE_API_BASE_URL


@pytest.mark.unit
def test_settings_constructor_field_tracking_lock_is_reentrant() -> None:
    lock = common_config._SETTINGS_INIT_FIELDS_LOCK  # noqa: SLF001

    lock.acquire()
    try:
        acquired_recursively = lock.acquire(blocking=False)
        assert acquired_recursively is True
        lock.release()
    finally:
        lock.release()


@pytest.mark.unit
def test_settings_constructor_field_tracking_uses_lock_for_all_dict_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingLock:
        def __init__(self) -> None:
            self.entries = 0

        def __enter__(self) -> RecordingLock:
            self.entries += 1
            return self

        def __exit__(self, *args: object) -> None:
            return None

    lock = RecordingLock()
    monkeypatch.setattr(common_config, "_SETTINGS_INIT_FIELDS_LOCK", lock, raising=False)

    settings = Settings(_env_file=None, api_base_url=DEFAULT_LOCAL_SERVICE_API_BASE_URL)
    after_record = lock.entries
    assert after_record >= 1

    assert service_config._settings_init_fields(settings) == frozenset({"api_base_url"})  # noqa: SLF001
    after_lookup = lock.entries
    assert after_lookup > after_record

    settings_ref = common_config._SettingsIdentityRef(settings)  # noqa: SLF001
    del settings
    gc.collect()

    assert settings_ref() is None
    assert lock.entries > after_lookup


@pytest.mark.unit
def test_dead_settings_identity_refs_do_not_compare_equal() -> None:
    default_settings = Settings(_env_file=None)
    explicit_default_settings = Settings(
        _env_file=None,
        api_base_url=DEFAULT_LOCAL_SERVICE_API_BASE_URL,
    )
    default_ref = common_config._SettingsIdentityRef(default_settings)  # noqa: SLF001
    explicit_default_ref = common_config._SettingsIdentityRef(  # noqa: SLF001
        explicit_default_settings
    )

    assert default_settings == explicit_default_settings
    assert default_ref == common_config._SettingsIdentityRef(default_settings)  # noqa: SLF001

    del default_settings
    del explicit_default_settings
    gc.collect()

    assert default_ref() is None
    assert explicit_default_ref() is None
    refs_compare_equal = default_ref == explicit_default_ref
    assert refs_compare_equal is False


@pytest.mark.unit
def test_default_compose_env_lookup_does_not_expose_asset_root_override() -> None:
    signature = inspect.signature(service_config.resolve_local_service_compose_env_file)

    assert "asset_root" not in signature.parameters


@pytest.mark.unit
def test_default_compose_env_lookup_accepts_current_directory_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_module = tmp_path / "install" / "awf" / "service" / "config.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.write_text("# installed module placeholder\n", encoding="utf-8")
    compose_env_file = tmp_path / "docker" / "compose" / ".env"
    compose_env_file.parent.mkdir(parents=True)
    compose_env_file.write_text("AWF_HOST_WORK_DIR=/tmp/awf-service-state\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(service_config, "__file__", str(fake_module))

    assert service_config.resolve_local_service_compose_env_file() == compose_env_file


@pytest.mark.unit
def test_default_compose_env_lookup_ignores_unmarked_module_ancestor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "install"
    fake_module = (
        install_root
        / ".venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "awf"
        / "service"
        / "config.py"
    )
    fake_module.parent.mkdir(parents=True)
    fake_module.write_text("# installed module placeholder\n", encoding="utf-8")
    unrelated_env_file = install_root / "docker" / "compose" / ".env"
    unrelated_env_file.parent.mkdir(parents=True)
    unrelated_env_file.write_text("AWF_POSTGRES_HOST_PORT=15433\n", encoding="utf-8")
    isolated_cwd = tmp_path / "cwd" / "nested"
    isolated_cwd.mkdir(parents=True)
    monkeypatch.chdir(isolated_cwd)
    monkeypatch.setattr(service_config, "__file__", str(fake_module))

    assert service_config.resolve_local_service_compose_env_file() is None


@pytest.mark.unit
def test_default_compose_env_lookup_ignores_pyproject_only_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_module = tmp_path / "install" / "awf" / "service" / "config.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.write_text("# installed module placeholder\n", encoding="utf-8")
    parent_project = tmp_path / "home"
    parent_project.mkdir()
    (parent_project / "pyproject.toml").write_text("[project]\nname = 'other'\n", encoding="utf-8")
    unrelated_env_file = parent_project / "docker" / "compose" / ".env"
    unrelated_env_file.parent.mkdir(parents=True)
    unrelated_env_file.write_text("AWF_POSTGRES_HOST_PORT=15433\n", encoding="utf-8")
    nested_cwd = parent_project / "child" / "nested"
    nested_cwd.mkdir(parents=True)
    monkeypatch.chdir(nested_cwd)
    monkeypatch.setattr(service_config, "__file__", str(fake_module))

    assert service_config.resolve_local_service_compose_env_file() is None


@pytest.mark.unit
def test_default_compose_env_lookup_accepts_awf_project_root_from_module_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout, fake_module = _write_awf_source_checkout(tmp_path)
    compose_env_file = checkout / "docker" / "compose" / ".env"
    compose_env_file.write_text("AWF_POSTGRES_HOST_PORT=15433\n", encoding="utf-8")
    isolated_cwd = tmp_path / "outside"
    isolated_cwd.mkdir()
    monkeypatch.chdir(isolated_cwd)
    monkeypatch.setattr(service_config, "__file__", str(fake_module))

    assert service_config.resolve_local_service_compose_env_file() == compose_env_file


@pytest.mark.unit
def test_default_compose_env_lookup_ignores_unrelated_git_root_before_module_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout, fake_module = _write_awf_source_checkout(tmp_path)
    compose_env_file = checkout / "docker" / "compose" / ".env"
    compose_env_file.write_text("AWF_POSTGRES_HOST_PORT=15433\n", encoding="utf-8")
    unrelated_repo = tmp_path / "unrelated"
    nested_cwd = unrelated_repo / "src" / "app"
    nested_cwd.mkdir(parents=True)
    (unrelated_repo / ".git").mkdir()
    unrelated_env_file = unrelated_repo / "docker" / "compose" / ".env"
    unrelated_env_file.parent.mkdir(parents=True)
    unrelated_env_file.write_text("AWF_POSTGRES_HOST_PORT=25433\n", encoding="utf-8")
    monkeypatch.chdir(nested_cwd)
    monkeypatch.setattr(service_config, "__file__", str(fake_module))

    assert service_config.resolve_local_service_compose_env_file() == compose_env_file


@pytest.mark.unit
def test_default_compose_env_lookup_skips_duplicate_missing_candidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout, fake_module = _write_awf_source_checkout(tmp_path)
    monkeypatch.chdir(checkout)
    monkeypatch.setattr(service_config, "__file__", str(fake_module))

    assert service_config.resolve_local_service_compose_env_file() is None


@pytest.mark.unit
def test_provider_env_file_accepts_trusted_custom_adjacent_env(tmp_path: Path) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("AWF_POSTGRES_HOST_PORT=15433\n", encoding="utf-8")

    assert (
        service_config._provider_env_file_from_compose_file(  # noqa: SLF001
            compose_file,
            allow_custom_adjacent=True,
        )
        == env_file
    )
    assert service_config._can_use_adjacent_provider_env_file(  # noqa: SLF001
        env_file,
        compose_file,
    )
    assert not service_config._can_use_adjacent_provider_env_file(  # noqa: SLF001
        env_file,
        LOCAL_SERVICE_COMPOSE_FILE,
    )


@pytest.mark.unit
def test_project_dotenv_ancestor_candidates_allows_unrelated_root(tmp_path: Path) -> None:
    nested = tmp_path / "project" / "src" / "pkg"
    nested.mkdir(parents=True)
    unrelated_root = tmp_path / "other"

    candidates = service_config._project_dotenv_ancestor_candidates(  # noqa: SLF001
        nested,
        unrelated_root,
    )

    assert candidates[0] == nested / ".env"
    assert candidates[-1] == Path("/") / ".env"


@pytest.mark.unit
def test_module_path_sourced_default_database_url_uses_compose_postgres_host_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout, fake_module = _write_awf_source_checkout(tmp_path)
    (checkout / ".env").write_text(
        f"AWF_DATABASE_URL={DEFAULT_LOCAL_SERVICE_DATABASE_URL}\n",
        encoding="utf-8",
    )
    compose_env_file = checkout / "docker" / "compose" / ".env"
    compose_env_file.write_text("AWF_POSTGRES_HOST_PORT=15433\n", encoding="utf-8")
    isolated_cwd = tmp_path / "outside"
    isolated_cwd.mkdir()
    monkeypatch.chdir(isolated_cwd)
    monkeypatch.setattr(service_config, "__file__", str(fake_module))
    monkeypatch.setenv("AWF_DATABASE_URL", DEFAULT_LOCAL_SERVICE_DATABASE_URL)
    monkeypatch.delenv("AWF_POSTGRES_HOST_PORT", raising=False)

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.database_url == "postgresql+asyncpg://awf:awf_dev@localhost:15433/awf"


@pytest.mark.unit
def test_service_settings_default_env_file_database_url_uses_postgres_host_port_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"AWF_DATABASE_URL={DEFAULT_LOCAL_SERVICE_DATABASE_URL}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AWF_DATABASE_URL", raising=False)
    monkeypatch.setenv("AWF_POSTGRES_HOST_PORT", "15433")

    settings = resolve_service_settings(Settings())

    assert settings.database_url == "postgresql+asyncpg://awf:awf_dev@localhost:15433/awf"


@pytest.mark.unit
def test_service_settings_exported_default_database_url_uses_postgres_host_port_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_DATABASE_URL", DEFAULT_LOCAL_SERVICE_DATABASE_URL)
    monkeypatch.setenv("AWF_POSTGRES_HOST_PORT", "15433")

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.database_url == "postgresql+asyncpg://awf:awf_dev@localhost:15433/awf"


@pytest.mark.unit
def test_service_settings_host_default_database_url_ignores_compose_env_postgres_host_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout, _ = _write_awf_source_checkout(tmp_path)
    compose_env_file = checkout / "docker" / "compose" / ".env"
    compose_env_file.write_text("AWF_POSTGRES_HOST_PORT=15433\n", encoding="utf-8")
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("AWF_DATABASE_URL", DEFAULT_LOCAL_SERVICE_DATABASE_URL)
    monkeypatch.delenv("AWF_POSTGRES_HOST_PORT", raising=False)

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.database_url == DEFAULT_LOCAL_SERVICE_DATABASE_URL


@pytest.mark.unit
def test_service_settings_sourced_env_default_database_url_uses_compose_env_postgres_host_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout, _ = _write_awf_source_checkout(tmp_path)
    env_file = checkout / ".env"
    env_file.write_text(f"AWF_DATABASE_URL={DEFAULT_LOCAL_SERVICE_DATABASE_URL}\n")
    compose_env_file = checkout / "docker" / "compose" / ".env"
    compose_env_file.write_text("AWF_POSTGRES_HOST_PORT=15433\n", encoding="utf-8")
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("AWF_DATABASE_URL", DEFAULT_LOCAL_SERVICE_DATABASE_URL)
    monkeypatch.delenv("AWF_POSTGRES_HOST_PORT", raising=False)

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.database_url == "postgresql+asyncpg://awf:awf_dev@localhost:15433/awf"


@pytest.mark.unit
def test_project_dotenv_value_continues_past_env_without_requested_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout, _ = _write_awf_source_checkout(tmp_path)
    nested = checkout / "src" / "module"
    nested.mkdir(parents=True)
    (checkout / ".git").mkdir()
    (checkout / ".env").write_text(
        f"AWF_DATABASE_URL={DEFAULT_LOCAL_SERVICE_DATABASE_URL}\n",
        encoding="utf-8",
    )
    compose_env_file = checkout / "docker" / "compose" / ".env"
    compose_env_file.write_text("AWF_POSTGRES_HOST_PORT=15433\n", encoding="utf-8")
    (nested / ".env").write_text("AWF_API_TOKEN=local-token\n", encoding="utf-8")
    monkeypatch.chdir(nested)
    read_env_files: list[Path] = []
    real_dotenv_values = service_config.dotenv_values

    def recording_dotenv_values(env_file: Path) -> dict[str, str | None]:
        read_env_files.append(env_file)
        return real_dotenv_values(env_file)

    monkeypatch.setattr(service_config, "dotenv_values", recording_dotenv_values)

    assert (
        service_config._project_dotenv_value("AWF_DATABASE_URL")  # noqa: SLF001
        == DEFAULT_LOCAL_SERVICE_DATABASE_URL
    )
    assert read_env_files == [nested / ".env", checkout / ".env"]


@pytest.mark.unit
def test_resolve_service_settings_reuses_project_dotenv_candidates_for_default_url_checks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout, _ = _write_awf_source_checkout(tmp_path)
    (checkout / ".env").write_text(
        "\n".join(
            [
                f"AWF_DATABASE_URL={DEFAULT_LOCAL_SERVICE_DATABASE_URL}",
                f"AWF_API_BASE_URL={DEFAULT_LOCAL_SERVICE_API_BASE_URL}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    compose_env_file = checkout / "docker" / "compose" / ".env"
    compose_env_file.write_text(
        "AWF_POSTGRES_HOST_PORT=15433\nAWF_API_HOST_PORT=9100\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("AWF_DATABASE_URL", DEFAULT_LOCAL_SERVICE_DATABASE_URL)
    monkeypatch.setenv("AWF_API_BASE_URL", DEFAULT_LOCAL_SERVICE_API_BASE_URL)
    monkeypatch.delenv("AWF_POSTGRES_HOST_PORT", raising=False)
    monkeypatch.delenv("AWF_API_HOST_PORT", raising=False)

    candidate_calls = 0
    real_project_dotenv_candidates = service_config._project_dotenv_candidates  # noqa: SLF001

    def recording_project_dotenv_candidates() -> tuple[Path, ...]:
        nonlocal candidate_calls
        candidate_calls += 1
        return real_project_dotenv_candidates()

    monkeypatch.setattr(
        service_config,
        "_project_dotenv_candidates",
        recording_project_dotenv_candidates,
    )

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.database_url == "postgresql+asyncpg://awf:awf_dev@localhost:15433/awf"
    assert settings.api_base_url == "http://localhost:9100"
    assert candidate_calls == 1


@pytest.mark.unit
def test_database_url_env_explicit_treats_missing_host_value_as_non_explicit() -> None:
    assert service_config._database_url_env_is_explicit({}, {}) is False  # noqa: SLF001


@pytest.mark.unit
def test_api_base_url_env_explicit_treats_missing_host_value_as_non_explicit() -> None:
    assert service_config._api_base_url_env_is_explicit({}, {}) is False  # noqa: SLF001


@pytest.mark.unit
def test_common_settings_alias_detection_handles_choices_paths_and_unknown_aliases() -> None:
    assert common_config._settings_alias_is_present(  # noqa: SLF001
        AliasChoices("AWF_DATABASE_URL", "DATABASE_URL"),
        {"DATABASE_URL": "postgresql+asyncpg://awf:pw@db:5432/awf"},
    )
    assert common_config._settings_alias_is_present(  # noqa: SLF001
        AliasPath("AWF_NESTED", "DATABASE_URL"),
        {"AWF_NESTED": {"DATABASE_URL": "postgresql+asyncpg://awf:pw@db:5432/awf"}},
    )

    class UnknownAlias:
        choices = "not-a-choice-list"
        path = ()

    assert not common_config._settings_alias_is_present(UnknownAlias(), {})  # noqa: SLF001


@pytest.mark.unit
def test_populate_compose_postgres_password_ignores_unparseable_database_url() -> None:
    environ = {"AWF_DATABASE_URL": "not a database url"}

    service_config._populate_compose_postgres_password(environ)  # noqa: SLF001

    assert "AWF_POSTGRES_PASSWORD" not in environ


@pytest.mark.unit
def test_populate_compose_postgres_password_ignores_urls_without_password() -> None:
    environ = {"AWF_DATABASE_URL": "postgresql+asyncpg://awf@localhost:5432/awf"}

    service_config._populate_compose_postgres_password(environ)  # noqa: SLF001

    assert "AWF_POSTGRES_PASSWORD" not in environ


@pytest.mark.unit
def test_resolve_service_api_base_url_uses_explicit_service_environment_url() -> None:
    settings = Settings(_env_file=None)

    api_base_url = service_config._resolve_service_api_base_url(  # noqa: SLF001
        settings,
        environ={},
        service_environ={"AWF_API_BASE_URL": "http://localhost:9200"},
    )

    assert api_base_url == "http://localhost:9200"


@pytest.mark.unit
def test_resolve_service_api_base_url_ignores_operator_base_url() -> None:
    settings = Settings(_env_file=None)

    api_base_url = service_config._resolve_service_api_base_url(  # noqa: SLF001
        settings,
        environ={"AWF_BASE_URL": "http://operator-host:8800"},
        service_environ={
            "AWF_BASE_URL": "http://operator-host:8800",
            "AWF_API_BASE_URL": "http://api:8000",
        },
    )

    assert api_base_url == "http://api:8000"


@pytest.mark.unit
def test_api_base_url_env_explicit_distinguishes_custom_and_derivable_defaults() -> None:
    assert service_config._api_base_url_env_is_explicit(  # noqa: SLF001
        {"AWF_API_BASE_URL": "https://awf.example.test"},
        {},
    )
    assert not service_config._api_base_url_env_is_explicit(  # noqa: SLF001
        {
            "AWF_API_BASE_URL": DEFAULT_LOCAL_SERVICE_API_BASE_URL,
            "AWF_API_HOST_PORT": "9100",
        },
        {},
    )


@pytest.mark.unit
def test_project_dotenv_candidates_fall_back_to_awf_source_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout, _module = _write_awf_source_checkout(tmp_path)
    nested = checkout / "src" / "awf"
    monkeypatch.chdir(nested)
    monkeypatch.setattr(service_config, "resolve_local_service_compose_env_file", lambda: None)

    candidates = service_config._project_dotenv_candidates()  # noqa: SLF001

    assert candidates == (
        nested / ".env",
        checkout / "src" / ".env",
        checkout / ".env",
    )


@pytest.mark.unit
def test_project_dotenv_ancestor_candidates_stop_at_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    nested = root / "src" / "awf"
    nested.mkdir(parents=True)

    candidates = service_config._project_dotenv_ancestor_candidates(  # noqa: SLF001
        nested,
        root,
    )

    assert candidates == (
        nested / ".env",
        root / "src" / ".env",
        root / ".env",
    )


@pytest.mark.unit
def test_service_settings_explicit_database_url_ignores_postgres_host_port_override() -> None:
    explicit_url = "postgresql+asyncpg://awf:pw@db.internal:5432/awf"

    settings = resolve_service_settings(
        Settings(_env_file=None, database_url=explicit_url),
        environ={"AWF_DATABASE_URL": explicit_url, "AWF_POSTGRES_HOST_PORT": "15433"},
    )

    assert settings.database_url == explicit_url


@pytest.mark.unit
def test_service_settings_explicit_base_database_url_ignores_custom_environ_host_port() -> None:
    explicit_url = "postgresql+asyncpg://awf:pw@db.internal:5432/awf"

    settings = resolve_service_settings(
        Settings(_env_file=None, database_url=explicit_url),
        environ={"AWF_POSTGRES_HOST_PORT": "15433"},
    )

    assert settings.database_url == explicit_url


@pytest.mark.unit
def test_service_settings_explicit_default_database_url_ignores_postgres_host_port_override() -> (
    None
):
    settings = resolve_service_settings(
        Settings(_env_file=None, database_url=DEFAULT_LOCAL_SERVICE_DATABASE_URL),
        environ={"AWF_POSTGRES_HOST_PORT": "15433"},
    )

    assert settings.database_url == DEFAULT_LOCAL_SERVICE_DATABASE_URL


@pytest.mark.unit
def test_service_settings_default_api_base_url_uses_api_host_port_override() -> None:
    settings = resolve_service_settings(
        Settings(_env_file=None),
        environ={"AWF_API_HOST_PORT": "9100"},
    )

    assert settings.api_base_url == "http://localhost:9100"


@pytest.mark.unit
def test_service_settings_default_api_base_url_ignores_operator_base_url() -> None:
    settings = resolve_service_settings(
        Settings(_env_file=None),
        environ={"AWF_BASE_URL": "http://operator-host:8800", "AWF_API_HOST_PORT": "9100"},
    )

    assert settings.api_base_url == "http://localhost:9100"


@pytest.mark.unit
def test_service_settings_default_api_base_url_uses_compose_env_api_host_port_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout, _ = _write_awf_source_checkout(tmp_path)
    compose_env_file = checkout / "docker" / "compose" / ".env"
    compose_env_file.write_text("AWF_API_HOST_PORT=9100\n", encoding="utf-8")
    monkeypatch.chdir(checkout)
    monkeypatch.delenv("AWF_API_BASE_URL", raising=False)
    monkeypatch.delenv("AWF_API_HOST_PORT", raising=False)

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.api_base_url == "http://localhost:9100"


@pytest.mark.unit
def test_service_settings_default_env_file_api_base_url_uses_compose_env_api_host_port_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout, _ = _write_awf_source_checkout(tmp_path)
    env_file = checkout / ".env"
    env_file.write_text("AWF_API_BASE_URL=http://localhost:8000\n", encoding="utf-8")
    compose_env_file = checkout / "docker" / "compose" / ".env"
    compose_env_file.write_text("AWF_API_HOST_PORT=9100\n", encoding="utf-8")
    monkeypatch.chdir(checkout)
    monkeypatch.delenv("AWF_API_BASE_URL", raising=False)
    monkeypatch.delenv("AWF_API_HOST_PORT", raising=False)

    settings = resolve_service_settings(Settings())

    assert settings.api_base_url == "http://localhost:9100"


@pytest.mark.unit
def test_service_settings_sourced_env_default_api_base_url_uses_compose_env_api_host_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout, _ = _write_awf_source_checkout(tmp_path)
    env_file = checkout / ".env"
    env_file.write_text(f"AWF_API_BASE_URL={DEFAULT_LOCAL_SERVICE_API_BASE_URL}\n")
    compose_env_file = checkout / "docker" / "compose" / ".env"
    compose_env_file.write_text("AWF_API_HOST_PORT=9100\n", encoding="utf-8")
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("AWF_API_BASE_URL", DEFAULT_LOCAL_SERVICE_API_BASE_URL)
    monkeypatch.delenv("AWF_API_HOST_PORT", raising=False)

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.api_base_url == "http://localhost:9100"


@pytest.mark.unit
def test_module_path_sourced_default_api_base_url_uses_compose_api_host_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout, fake_module = _write_awf_source_checkout(tmp_path)
    (checkout / ".env").write_text(
        f"AWF_API_BASE_URL={DEFAULT_LOCAL_SERVICE_API_BASE_URL}\n",
        encoding="utf-8",
    )
    compose_env_file = checkout / "docker" / "compose" / ".env"
    compose_env_file.write_text("AWF_API_HOST_PORT=9100\n", encoding="utf-8")
    isolated_cwd = tmp_path / "outside"
    isolated_cwd.mkdir()
    monkeypatch.chdir(isolated_cwd)
    monkeypatch.setattr(service_config, "__file__", str(fake_module))
    monkeypatch.setenv("AWF_API_BASE_URL", DEFAULT_LOCAL_SERVICE_API_BASE_URL)
    monkeypatch.delenv("AWF_API_HOST_PORT", raising=False)

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.api_base_url == "http://localhost:9100"


@pytest.mark.unit
def test_service_settings_host_default_api_base_url_ignores_compose_env_api_host_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout, _ = _write_awf_source_checkout(tmp_path)
    compose_env_file = checkout / "docker" / "compose" / ".env"
    compose_env_file.write_text("AWF_API_HOST_PORT=9100\n", encoding="utf-8")
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("AWF_API_BASE_URL", DEFAULT_LOCAL_SERVICE_API_BASE_URL)
    monkeypatch.delenv("AWF_API_HOST_PORT", raising=False)

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.api_base_url == DEFAULT_LOCAL_SERVICE_API_BASE_URL


@pytest.mark.unit
def test_service_settings_explicit_api_base_url_ignores_api_host_port_override() -> None:
    explicit_url = "http://127.0.0.1:9300"

    settings = resolve_service_settings(
        Settings(_env_file=None, api_base_url=explicit_url),
        environ={"AWF_API_HOST_PORT": "9100"},
    )

    assert settings.api_base_url == explicit_url


@pytest.mark.unit
def test_service_settings_explicit_default_api_base_url_ignores_api_host_port_override() -> None:
    settings = resolve_service_settings(
        Settings(_env_file=None, api_base_url=DEFAULT_LOCAL_SERVICE_API_BASE_URL),
        environ={"AWF_API_HOST_PORT": "9100"},
    )

    assert settings.api_base_url == DEFAULT_LOCAL_SERVICE_API_BASE_URL


@pytest.mark.unit
def test_service_settings_ignores_ambient_api_base_url_for_custom_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_API_BASE_URL", "http://host-shell.example:9300")
    base = Settings(_env_file=None)

    settings = resolve_service_settings(
        base,
        environ={"AWF_API_HOST_PORT": "9100"},
    )

    assert settings.api_base_url == "http://localhost:9100"


@pytest.mark.unit
@pytest.mark.parametrize("host_port", ["not-a-port", "0", "65536"])
def test_service_settings_rejects_invalid_api_host_port(host_port: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        resolve_service_settings(
            Settings(_env_file=None),
            environ={"AWF_API_HOST_PORT": host_port},
        )

    message = str(exc_info.value)
    assert "AWF_API_HOST_PORT" in message
    assert repr(host_port) in message
    assert "integer between 1 and 65535" in message


@pytest.mark.unit
def test_production_guardrails_allow_local_defaults() -> None:
    settings = Settings(_env_file=None, env="local", api_token=None, callbacks_enabled=True)

    diagnostics = settings_guardrails(
        env=settings.env,
        database_url=settings.database_url,
        api_token=settings.api_token,
    )

    assert diagnostics == ()
    validate_production_settings(settings)


@pytest.mark.unit
def test_production_guardrails_allow_ci_defaults() -> None:
    settings = Settings(_env_file=None, env="ci", api_token=None, callbacks_enabled=True)

    diagnostics = settings_guardrails(
        env=settings.env,
        database_url=settings.database_url,
        api_token=settings.api_token,
    )

    assert diagnostics == ()
    validate_production_settings(settings)


@pytest.mark.unit
def test_settings_guardrails_rejects_removed_callbacks_enabled_argument() -> None:
    with pytest.raises(TypeError):
        settings_guardrails(
            env="prod",
            database_url=_NON_DEFAULT_DATABASE_URL,
            api_token=_STRONG_PRODUCTION_API_TOKEN,
            callbacks_enabled=True,  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_production_guardrails_reject_default_local_database_url() -> None:
    settings = Settings(
        _env_file=None,
        env="prod",
        database_url=DEFAULT_LOCAL_DATABASE_URL,
        api_token=_STRONG_PRODUCTION_API_TOKEN,
        callbacks_enabled=False,
    )

    with pytest.raises(ProductionSettingsError) as exc_info:
        validate_production_settings(settings)

    error = exc_info.value
    assert "production_default_database_url" in _diagnostic_codes(error)
    assert "AWF_DATABASE_URL" in _diagnostic_fields(error)


@pytest.mark.unit
@pytest.mark.parametrize("database_url", ["", "   "])
def test_production_guardrails_reject_empty_database_url(database_url: str) -> None:
    settings = Settings(
        _env_file=None,
        env="prod",
        database_url=DEFAULT_LOCAL_DATABASE_URL,
        api_token=_STRONG_PRODUCTION_API_TOKEN,
        callbacks_enabled=False,
    )

    with pytest.raises(ProductionSettingsError) as exc_info:
        validate_production_settings(settings, database_url=database_url)

    error = exc_info.value
    assert "production_default_database_url" in _diagnostic_codes(error)
    assert "AWF_DATABASE_URL" in _diagnostic_fields(error)


@pytest.mark.unit
def test_production_guardrails_allow_non_default_credentials_with_malformed_port() -> None:
    settings = Settings(
        _env_file=None,
        env="prod",
        database_url="postgresql+asyncpg://awf:prod-pass@db.internal:not-a-port/awf",
        api_token=_STRONG_PRODUCTION_API_TOKEN,
        callbacks_enabled=False,
    )

    validate_production_settings(settings)


@pytest.mark.unit
def test_production_guardrails_reject_default_credentials_with_malformed_port() -> None:
    settings = Settings(
        _env_file=None,
        env="prod",
        database_url="postgresql+asyncpg://awf:awf_dev@db.internal:not-a-port/awf",
        api_token=_STRONG_PRODUCTION_API_TOKEN,
        callbacks_enabled=False,
    )

    with pytest.raises(ProductionSettingsError) as exc_info:
        validate_production_settings(settings)

    error = exc_info.value
    assert "production_default_database_url" in _diagnostic_codes(error)
    assert "AWF_DATABASE_URL" in _diagnostic_fields(error)


@pytest.mark.unit
@pytest.mark.parametrize(
    "api_token",
    [
        None,
        "",
        " ",
        "local-dev-token",
        "changeme",
        "default",
        "short",
        "secretsecretsecretsecret",
        "secret-secret-secret-secret",
        "secret-secret-secret-secret-",
        "admin-admin-admin-admin-admin",
        "apikey-apikey-apikey-apikey",
        "api-key-api-key-api-key-api-key",
        "api_key_api_key_api_key_api_key",
        "bearer-bearer-bearer-bearer",
    ],
)
def test_production_guardrails_reject_missing_or_weak_api_token(
    api_token: str | None,
) -> None:
    settings = Settings(
        _env_file=None,
        env="prod",
        database_url=_NON_DEFAULT_DATABASE_URL,
        api_token=api_token,
        callbacks_enabled=False,
    )

    with pytest.raises(ProductionSettingsError) as exc_info:
        validate_production_settings(settings)

    error = exc_info.value
    assert "production_api_token_weak" in _diagnostic_codes(error) or (
        "production_api_token_missing" in _diagnostic_codes(error)
    )
    assert "AWF_API_TOKEN" in _diagnostic_fields(error)


@pytest.mark.unit
def test_production_guardrails_reject_callbacks_without_api_token() -> None:
    settings = Settings(
        _env_file=None,
        env="prod",
        database_url=_NON_DEFAULT_DATABASE_URL,
        api_token=None,
        callbacks_enabled=True,
    )

    with pytest.raises(ProductionSettingsError) as exc_info:
        validate_production_settings(settings)

    error = exc_info.value
    assert _diagnostic_codes(error) == {"production_api_token_missing"}
    assert _diagnostic_fields(error) == {"AWF_API_TOKEN"}


@pytest.mark.unit
def test_production_guardrails_allow_authenticated_callbacks() -> None:
    settings = Settings(
        _env_file=None,
        env="prod",
        database_url=_NON_DEFAULT_DATABASE_URL,
        api_token=_STRONG_PRODUCTION_API_TOKEN,
        callbacks_enabled=True,
    )

    diagnostics = settings_guardrails(
        env=settings.env,
        database_url=settings.database_url,
        api_token=settings.api_token,
    )

    assert diagnostics == ()
    validate_production_settings(settings)


@pytest.mark.unit
def test_production_guardrail_diagnostics_redact_sensitive_values() -> None:
    settings = Settings(
        _env_file=None,
        env="prod",
        database_url=DEFAULT_LOCAL_DATABASE_URL,
        api_token="local-dev-token",
        callbacks_enabled=True,
    )

    with pytest.raises(ProductionSettingsError) as exc_info:
        validate_production_settings(settings)

    error = exc_info.value
    rendered = f"{error} {_diagnostic_text(error)}"
    assert DEFAULT_LOCAL_DATABASE_URL not in rendered
    assert "awf_dev" not in rendered
    assert "local-dev-token" not in rendered


@pytest.mark.unit
def test_production_settings_error_without_diagnostics_has_generic_message() -> None:
    assert str(ProductionSettingsError(())) == "Production settings validation failed."


@pytest.mark.unit
def test_settings_guardrail_helpers_handle_short_tokens_and_bracketed_hosts() -> None:
    assert common_config._is_repeated_weak_api_token_value("api") is False  # noqa: SLF001
    assert common_config._normalize_callback_allowed_host("   ") == ""  # noqa: SLF001
    assert (
        common_config._normalize_callback_allowed_host("[Operator.Example.COM]:8443")  # noqa: SLF001
        == "operator.example.com"
    )
    assert (
        common_config._normalize_callback_allowed_host("[Operator.Example.COM")  # noqa: SLF001
        == "[operator.example.com"
    )


@pytest.mark.unit
def test_service_settings_resolution_runs_production_guardrails_after_db_resolution() -> None:
    base = Settings(
        _env_file=None,
        env="prod",
        api_token=_STRONG_PRODUCTION_API_TOKEN,
        callbacks_enabled=False,
    )

    with pytest.raises(ProductionSettingsError) as exc_info:
        resolve_service_settings(base, environ={})

    error = exc_info.value
    assert "production_default_database_url" in _diagnostic_codes(error)
    assert "AWF_DATABASE_URL" in _diagnostic_fields(error)
