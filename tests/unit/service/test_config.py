"""Service configuration tests."""

from __future__ import annotations

import gc
import inspect
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

import awf.common.config as common_config
import awf.service.config as service_config
from awf.common.config import (
    DEFAULT_LOCAL_DATABASE_URL,
    DEFAULT_MIN_FREE_DISK_BYTES,
    ProductionSettingsError,
    Settings,
    settings_guardrails,
    validate_production_settings,
)
from awf.service.config import (
    DEFAULT_LOCAL_SERVICE_API_BASE_URL,
    DEFAULT_LOCAL_SERVICE_DATABASE_URL,
    DEFAULT_LOCAL_SERVICE_WORK_DIR,
    _redact_database_url,
    _resolve_service_work_dir,
    local_service_environ,
    resolve_service_settings,
    service_config_payload,
)

_NON_DEFAULT_DATABASE_URL = "postgresql+asyncpg://awf:prod-pass@db.internal:5432/awf"
_STRONG_PRODUCTION_API_TOKEN = "prod-token-for-awf-operator-apis-32"


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
    compose_env_file = tmp_path / "docker" / "compose" / ".env"
    compose_env_file.parent.mkdir(parents=True)
    compose_env_file.write_text("AWF_POSTGRES_HOST_PORT=15433\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AWF_DATABASE_URL", raising=False)
    monkeypatch.delenv("AWF_POSTGRES_HOST_PORT", raising=False)

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.database_url == "postgresql+asyncpg://awf:awf_dev@localhost:15433/awf"


@pytest.mark.unit
def test_service_settings_uses_checkout_root_compose_env_from_subdirectory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    nested = checkout / "src" / "awf"
    nested.mkdir(parents=True)
    (checkout / ".git").mkdir()
    (checkout / "pyproject.toml").write_text("[project]\nname = 'awf'\n", encoding="utf-8")
    compose_env_file = checkout / "docker" / "compose" / ".env"
    compose_env_file.parent.mkdir(parents=True)
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
    checkout = tmp_path / "checkout"
    fake_module = checkout / "src" / "awf" / "service" / "config.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.write_text("# source module placeholder\n", encoding="utf-8")
    (checkout / "src" / "awf" / "__init__.py").write_text("", encoding="utf-8")
    (checkout / "pyproject.toml").write_text("[project]\nname = 'awf'\n", encoding="utf-8")
    compose_file = checkout / "docker" / "compose" / "local-service.yml"
    compose_file.parent.mkdir(parents=True)
    compose_file.write_text("services: {}\n", encoding="utf-8")
    compose_env_file = checkout / "docker" / "compose" / ".env"
    compose_env_file.write_text("AWF_POSTGRES_HOST_PORT=15433\n", encoding="utf-8")
    isolated_cwd = tmp_path / "outside"
    isolated_cwd.mkdir()
    monkeypatch.chdir(isolated_cwd)
    monkeypatch.setattr(service_config, "__file__", str(fake_module))

    assert service_config.resolve_local_service_compose_env_file() == compose_env_file


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
    compose_env_file = tmp_path / "docker" / "compose" / ".env"
    compose_env_file.parent.mkdir(parents=True)
    compose_env_file.write_text("AWF_POSTGRES_HOST_PORT=15433\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_DATABASE_URL", DEFAULT_LOCAL_SERVICE_DATABASE_URL)
    monkeypatch.delenv("AWF_POSTGRES_HOST_PORT", raising=False)

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.database_url == DEFAULT_LOCAL_SERVICE_DATABASE_URL


@pytest.mark.unit
def test_service_settings_sourced_env_default_database_url_uses_compose_env_postgres_host_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"AWF_DATABASE_URL={DEFAULT_LOCAL_SERVICE_DATABASE_URL}\n")
    compose_env_file = tmp_path / "docker" / "compose" / ".env"
    compose_env_file.parent.mkdir(parents=True)
    compose_env_file.write_text("AWF_POSTGRES_HOST_PORT=15433\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_DATABASE_URL", DEFAULT_LOCAL_SERVICE_DATABASE_URL)
    monkeypatch.delenv("AWF_POSTGRES_HOST_PORT", raising=False)

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.database_url == "postgresql+asyncpg://awf:awf_dev@localhost:15433/awf"


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
def test_service_settings_default_api_base_url_uses_compose_env_api_host_port_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_env_file = tmp_path / "docker" / "compose" / ".env"
    compose_env_file.parent.mkdir(parents=True)
    compose_env_file.write_text("AWF_API_HOST_PORT=9100\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AWF_API_BASE_URL", raising=False)
    monkeypatch.delenv("AWF_API_HOST_PORT", raising=False)

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.api_base_url == "http://localhost:9100"


@pytest.mark.unit
def test_service_settings_default_env_file_api_base_url_uses_compose_env_api_host_port_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("AWF_API_BASE_URL=http://localhost:8000\n", encoding="utf-8")
    compose_env_file = tmp_path / "docker" / "compose" / ".env"
    compose_env_file.parent.mkdir(parents=True)
    compose_env_file.write_text("AWF_API_HOST_PORT=9100\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AWF_API_BASE_URL", raising=False)
    monkeypatch.delenv("AWF_API_HOST_PORT", raising=False)

    settings = resolve_service_settings(Settings())

    assert settings.api_base_url == "http://localhost:9100"


@pytest.mark.unit
def test_service_settings_sourced_env_default_api_base_url_uses_compose_env_api_host_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"AWF_API_BASE_URL={DEFAULT_LOCAL_SERVICE_API_BASE_URL}\n")
    compose_env_file = tmp_path / "docker" / "compose" / ".env"
    compose_env_file.parent.mkdir(parents=True)
    compose_env_file.write_text("AWF_API_HOST_PORT=9100\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_API_BASE_URL", DEFAULT_LOCAL_SERVICE_API_BASE_URL)
    monkeypatch.delenv("AWF_API_HOST_PORT", raising=False)

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.api_base_url == "http://localhost:9100"


@pytest.mark.unit
def test_service_settings_host_default_api_base_url_ignores_compose_env_api_host_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compose_env_file = tmp_path / "docker" / "compose" / ".env"
    compose_env_file.parent.mkdir(parents=True)
    compose_env_file.write_text("AWF_API_HOST_PORT=9100\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
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


@pytest.mark.unit
def test_agent_watchdog_defaults_are_conservative_and_documented_in_payload() -> None:
    settings = resolve_service_settings(Settings(_env_file=None), environ={})
    payload = service_config_payload(settings)
    rendered = json.dumps(payload)

    assert settings.agent_wall_timeout_seconds == 7200
    assert settings.agent_idle_timeout_seconds == 3600
    assert payload["agent_wall_timeout_seconds"] == 7200
    assert payload["agent_idle_timeout_seconds"] == 3600
    assert "agent_wall_timeout_seconds" in rendered
    assert "agent_idle_timeout_seconds" in rendered


@pytest.mark.unit
def test_agent_watchdog_settings_flow_from_settings_to_service_settings() -> None:
    base = Settings(
        _env_file=None,
        agent_wall_timeout_seconds=1234,
        agent_idle_timeout_seconds=56,
    )

    settings = resolve_service_settings(base, environ={})

    assert settings.agent_wall_timeout_seconds == 1234
    assert settings.agent_idle_timeout_seconds == 56


@pytest.mark.unit
def test_planning_max_iterations_default_is_three_and_in_payload() -> None:
    settings = resolve_service_settings(Settings(_env_file=None), environ={})
    payload = service_config_payload(settings)

    assert Settings(_env_file=None).planning_max_iterations_default == 3
    assert settings.planning_max_iterations_default == 3
    assert payload["planning_max_iterations_default"] == 3


@pytest.mark.unit
def test_planning_max_iterations_default_flows_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_PLANNING_MAX_ITERATIONS_DEFAULT", "4")

    settings = resolve_service_settings(Settings(_env_file=None), environ=os.environ)

    assert settings.planning_max_iterations_default == 4


@pytest.mark.unit
def test_empty_local_capacity_environment_values_are_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_LOCAL_CAPACITY_CPU_CORES", "")
    monkeypatch.setenv("AWF_LOCAL_CAPACITY_MEMORY_GB", "")
    monkeypatch.setenv("AWF_LOCAL_CAPACITY_DIND_SLOTS", "")

    settings = Settings(_env_file=None)

    assert settings.local_capacity_cpu_cores is None
    assert settings.local_capacity_memory_gb is None
    assert settings.local_capacity_dind_slots is None


@pytest.mark.unit
def test_min_free_disk_threshold_defaults_to_conservative_10_gib_payload() -> None:
    settings = resolve_service_settings(Settings(_env_file=None), environ={})
    payload = service_config_payload(settings)

    assert DEFAULT_MIN_FREE_DISK_BYTES == 10 * 1024 * 1024 * 1024
    assert settings.min_free_disk_bytes == DEFAULT_MIN_FREE_DISK_BYTES
    assert payload["min_free_disk_bytes"] == DEFAULT_MIN_FREE_DISK_BYTES


@pytest.mark.unit
def test_min_free_disk_threshold_flows_from_settings_to_service_settings() -> None:
    base = Settings(_env_file=None, min_free_disk_bytes=123456)

    settings = resolve_service_settings(base, environ={"AWF_MIN_FREE_DISK_BYTES": "123456"})

    assert settings.min_free_disk_bytes == 123456


@pytest.mark.unit
def test_local_service_work_dir_defaults_to_compose_host_state_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host_work_dir = tmp_path / "awf-service"
    monkeypatch.delenv("AWF_WORK_DIR", raising=False)
    settings = resolve_service_settings(
        Settings(_env_file=None),
        environ={"HOME": str(tmp_path), "AWF_HOST_WORK_DIR": str(host_work_dir)},
    )

    assert DEFAULT_LOCAL_SERVICE_WORK_DIR == "~/.awf/service"
    assert settings.work_dir == str(host_work_dir)


@pytest.mark.unit
def test_local_service_ignores_project_default_awf_work_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AWF_WORK_DIR", ".awf")
    monkeypatch.delenv("AWF_HOST_WORK_DIR", raising=False)

    settings = resolve_service_settings(
        Settings(_env_file=None),
        environ={"HOME": str(tmp_path), "AWF_WORK_DIR": ".awf"},
    )

    assert settings.work_dir == str(tmp_path / ".awf" / "service")


@pytest.mark.unit
def test_compose_host_work_dir_takes_precedence_over_shell_awf_work_dir(
    tmp_path: Path,
) -> None:
    shell_work_dir = tmp_path / "project"
    host_work_dir = tmp_path / "compose-default"

    settings = resolve_service_settings(
        Settings(_env_file=None, work_dir=str(shell_work_dir)),
        environ={
            "AWF_WORK_DIR": str(shell_work_dir),
            "AWF_HOST_WORK_DIR": str(host_work_dir),
        },
    )

    assert settings.work_dir == str(host_work_dir)


@pytest.mark.unit
def test_local_service_work_dir_resolves_from_compose_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host_work_dir = tmp_path / "compose-service-state"
    compose_env_file = tmp_path / "docker" / "compose" / ".env"
    compose_env_file.parent.mkdir(parents=True)
    compose_env_file.write_text(f"AWF_HOST_WORK_DIR={host_work_dir}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AWF_WORK_DIR", raising=False)
    monkeypatch.delenv("AWF_HOST_WORK_DIR", raising=False)

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.work_dir == str(host_work_dir)


@pytest.mark.unit
def test_project_default_awf_work_dir_does_not_hide_compose_host_work_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host_work_dir = tmp_path / "compose-service-state"
    compose_env_file = tmp_path / "docker" / "compose" / ".env"
    compose_env_file.parent.mkdir(parents=True)
    compose_env_file.write_text(f"AWF_HOST_WORK_DIR={host_work_dir}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AWF_WORK_DIR", ".awf")
    monkeypatch.delenv("AWF_HOST_WORK_DIR", raising=False)

    settings = resolve_service_settings(Settings(_env_file=None))

    assert settings.work_dir == str(host_work_dir)


@pytest.mark.unit
def test_workspace_cleanup_policy_defaults_are_documented_in_payload() -> None:
    settings = resolve_service_settings(Settings(_env_file=None), environ={})
    payload = service_config_payload(settings)

    assert settings.completed_workspace_retention_hours == 168
    assert settings.workspace_cleanup_enabled is True
    assert settings.workspace_cleanup_scan_interval_seconds == 3600
    assert settings.workspace_cleanup_batch_limit == 50
    assert payload["completed_workspace_retention_hours"] == 168
    assert payload["workspace_cleanup_enabled"] is True
    assert payload["workspace_cleanup_scan_interval_seconds"] == 3600
    assert payload["workspace_cleanup_batch_limit"] == 50


@pytest.mark.unit
def test_workspace_cleanup_policy_flows_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_COMPLETED_WORKSPACE_RETENTION_HOURS", "12")
    monkeypatch.setenv("AWF_WORKSPACE_CLEANUP_ENABLED", "false")
    monkeypatch.setenv("AWF_WORKSPACE_CLEANUP_SCAN_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("AWF_WORKSPACE_CLEANUP_BATCH_LIMIT", "7")

    settings = resolve_service_settings(Settings(_env_file=None), environ=os.environ)

    assert settings.completed_workspace_retention_hours == 12
    assert settings.workspace_cleanup_enabled is False
    assert settings.workspace_cleanup_scan_interval_seconds == 300
    assert settings.workspace_cleanup_batch_limit == 7


@pytest.mark.unit
def test_network_posture_legacy_cutoff_is_unset_by_default_and_in_payload() -> None:
    settings = resolve_service_settings(Settings(_env_file=None), environ={})
    payload = service_config_payload(settings)

    assert settings.network_posture_open_legacy_cutoff is None
    assert payload["network_posture_open_legacy_cutoff"] is None


@pytest.mark.unit
def test_network_posture_legacy_cutoff_treats_blank_string_as_unset() -> None:
    settings = Settings(_env_file=None, network_posture_open_legacy_cutoff=" ")

    assert settings.network_posture_open_legacy_cutoff is None


@pytest.mark.unit
def test_network_posture_legacy_cutoff_flows_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWF_NETWORK_POSTURE_OPEN_LEGACY_CUTOFF", "2026-05-02T13:00:00Z")

    settings = resolve_service_settings(Settings(_env_file=None), environ=os.environ)
    payload = service_config_payload(settings)

    assert settings.network_posture_open_legacy_cutoff == datetime(2026, 5, 2, 13, 0, tzinfo=UTC)
    assert payload["network_posture_open_legacy_cutoff"] == "2026-05-02T13:00:00+00:00"
    json.dumps(payload)


@pytest.mark.unit
def test_local_service_config_resolves_stable_worker_node_id_in_payload() -> None:
    settings = resolve_service_settings(Settings(_env_file=None), environ={})
    payload = service_config_payload(settings)

    assert settings.node_id == "local"
    assert payload["node_id"] == "local"


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
    compose_path = Path(__file__).resolve().parents[3] / "docker" / "compose" / "local-service.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    for service_name in ("api", "worker", "migrate"):
        service = compose["services"][service_name]
        assert service["environment"]["AWF_WORKER_NODE_ID"] == "local"
        assert service["environment"]["AWF_PLANNING_MAX_ITERATIONS_DEFAULT"].endswith(":-3}")
        assert service["environment"]["AWF_GITHUB_TOKEN"].startswith("${AWF_GITHUB_TOKEN:-")
        assert service["environment"]["GH_TOKEN"].startswith("${AWF_GITHUB_TOKEN:-")
        assert service["environment"]["GITHUB_TOKEN"].startswith("${AWF_GITHUB_TOKEN:-")
