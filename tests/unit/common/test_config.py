"""Focused regressions for common settings configuration."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import AliasChoices, AliasPath, BaseModel, Field, ValidationError
from pydantic_settings import SettingsConfigDict

from awf.common.config import Settings, _settings_constructor_fields_from_values

_EXPLICIT_DATABASE_URL = "postgresql+asyncpg://awf:pw@db.internal:5432/awf"


@pytest.mark.unit
def test_settings_constructor_fields_follow_case_insensitive_model_config() -> None:
    fields = _settings_constructor_fields_from_values(
        Settings,
        {"DATABASE_URL": _EXPLICIT_DATABASE_URL},
    )

    assert fields == frozenset({"database_url"})


@pytest.mark.unit
def test_settings_constructor_fields_preserve_case_sensitive_model_config() -> None:
    class CaseSensitiveSettings(Settings):
        model_config = SettingsConfigDict(case_sensitive=True)

    assert (
        _settings_constructor_fields_from_values(
            CaseSensitiveSettings,
            {"DATABASE_URL": _EXPLICIT_DATABASE_URL},
        )
        == frozenset()
    )
    assert _settings_constructor_fields_from_values(
        CaseSensitiveSettings,
        {"database_url": _EXPLICIT_DATABASE_URL},
    ) == frozenset({"database_url"})


@pytest.mark.unit
def test_settings_constructor_fields_apply_configured_case_to_aliases() -> None:
    class AliasSettings(Settings):
        api_base_url: str = Field(default="http://localhost:8000", alias="serviceUrl")
        database_url: str = Field(
            default=_EXPLICIT_DATABASE_URL,
            validation_alias=AliasChoices("databaseUrl", "connectionUrl"),
        )

    class CaseSensitiveAliasSettings(AliasSettings):
        model_config = SettingsConfigDict(case_sensitive=True)

    differently_cased_values = {
        "SERVICEURL": "http://localhost:9100",
        "CONNECTIONURL": _EXPLICIT_DATABASE_URL,
    }
    exactly_cased_values = {
        "serviceUrl": "http://localhost:9100",
        "connectionUrl": _EXPLICIT_DATABASE_URL,
    }

    assert _settings_constructor_fields_from_values(
        AliasSettings,
        differently_cased_values,
    ) == frozenset({"api_base_url", "database_url"})
    assert (
        _settings_constructor_fields_from_values(
            CaseSensitiveAliasSettings,
            differently_cased_values,
        )
        == frozenset()
    )
    assert _settings_constructor_fields_from_values(
        CaseSensitiveAliasSettings,
        exactly_cased_values,
    ) == frozenset({"api_base_url", "database_url"})


@pytest.mark.unit
def test_settings_constructor_fields_normalize_only_alias_path_root() -> None:
    class AliasPathSettings(Settings):
        database_url: str = Field(
            default=_EXPLICIT_DATABASE_URL,
            validation_alias=AliasPath("payload", "database_url"),
        )

    nested: dict[str, Any] = {"DATABASE_URL": _EXPLICIT_DATABASE_URL}
    values = {"PAYLOAD": nested}

    assert _settings_constructor_fields_from_values(
        AliasPathSettings,
        values,
    ) == frozenset({"database_url"})
    assert values == {"PAYLOAD": {"DATABASE_URL": _EXPLICIT_DATABASE_URL}}


@pytest.mark.unit
def test_case_insensitive_init_keys_do_not_normalize_nested_model_keys() -> None:
    class NestedValue(BaseModel):
        database_url: str

    class NestedSettings(Settings):
        nested: NestedValue

    with pytest.raises(ValidationError) as exc_info:
        NestedSettings(
            _env_file=None,
            NESTED={"DATABASE_URL": _EXPLICIT_DATABASE_URL},
        )

    assert [error["loc"] for error in exc_info.value.errors()] == [("nested", "database_url")]
    settings = NestedSettings(
        _env_file=None,
        NESTED={"database_url": _EXPLICIT_DATABASE_URL},
    )
    assert settings.nested.database_url == _EXPLICIT_DATABASE_URL
