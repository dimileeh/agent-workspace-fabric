"""Workspace companion-service request schemas."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from awf.common.companions import (
    RESERVED_COMPANION_SERVICE_NAMES,
    companion_name_is_git_branch_component,
    companion_volume_source_is_repo_relative,
)

ServiceName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")]
CompanionPath = Annotated[str, Field(min_length=1, max_length=1024)]
CompanionVolume = tuple[CompanionPath, CompanionPath]
_ENVIRONMENT_KEY_PATTERN_TEXT = r"^[A-Za-z_][A-Za-z0-9_]*$"
EnvironmentKey = Annotated[
    str, Field(min_length=1, max_length=256, pattern=_ENVIRONMENT_KEY_PATTERN_TEXT)
]

_ENVIRONMENT_KEY_PATTERN = re.compile(_ENVIRONMENT_KEY_PATTERN_TEXT)
_ENVIRONMENT_NAME_START_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_")
_ENVIRONMENT_VALUE_INTERPOLATION_PATTERN_TEXT = r"(^|[^$])(?:\$\$)*\$(?:[A-Za-z_]|\{[A-Za-z_])"
_NAMED_VOLUME_SOURCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_YAML_UNSAFE_COMPANION_PATH_PATTERN = re.compile(r'["\\\x00-\x1f\x7f-\x9f]')
_ENVIRONMENT_SCHEMA: dict[str, Any] = {
    "propertyNames": {
        "maxLength": 256,
        "minLength": 1,
        "pattern": _ENVIRONMENT_KEY_PATTERN_TEXT,
    },
    "patternProperties": {
        _ENVIRONMENT_KEY_PATTERN_TEXT: {
            "type": "string",
            "not": {"pattern": _ENVIRONMENT_VALUE_INTERPOLATION_PATTERN_TEXT},
        },
    },
}
_ENVIRONMENT_SECRETS_SCHEMA: dict[str, Any] = {
    "propertyNames": {
        "maxLength": 256,
        "minLength": 1,
        "pattern": _ENVIRONMENT_KEY_PATTERN_TEXT,
    },
}


class CompanionEnvironmentSecretRequest(BaseModel):
    """Env-backed secret reference for a companion container environment key."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: Literal["env"] = "env"
    kind: Literal["env"] = "env"
    value_from: EnvironmentKey
    required: bool = True


def _validate_companion_repo_relative_path(field_name: str, value: str) -> None:
    if _is_absolute_or_escaping_path(value):
        raise ValueError(f"companion {field_name} must be a repo-relative path")
    _validate_companion_yaml_safe_path(field_name, value)


def _validate_companion_repo_relative_volume_source(value: str) -> None:
    _validate_companion_repo_relative_path("volume source", value)
    if ":" in value:
        raise ValueError("companion volume source must not contain colon characters")


def _validate_companion_volume_target(value: str) -> None:
    if ":" in value or not PurePosixPath(value).is_absolute():
        raise ValueError(
            "companion volume target must be an absolute container path without mount options"
        )
    _validate_companion_yaml_safe_path("volume target", value)


def _validate_companion_named_volume_source(value: str) -> None:
    if not _NAMED_VOLUME_SOURCE_PATTERN.fullmatch(value):
        raise ValueError(
            "companion volume source named volume must start with an ASCII letter or digit "
            "and contain only ASCII letters, digits, dots, underscores, and hyphens"
        )


def _validate_companion_yaml_safe_path(field_name: str, value: str) -> None:
    if _YAML_UNSAFE_COMPANION_PATH_PATTERN.search(value):
        raise ValueError(
            f"companion {field_name} must be a YAML-safe path without control, "
            "double quote, or backslash characters"
        )
    if "$" in value:
        raise ValueError(
            f"companion {field_name} must not contain Docker Compose interpolation "
            "markers or dollar characters"
        )


def _value_has_compose_interpolation(value: str) -> bool:
    cursor = 0
    while cursor < len(value):
        dollar_index = value.find("$", cursor)
        if dollar_index == -1:
            return False
        next_index = dollar_index + 1
        if next_index >= len(value):
            return False
        next_char = value[next_index]
        if next_char == "$":
            cursor = next_index + 1
            continue
        if next_char in _ENVIRONMENT_NAME_START_CHARS:
            return True
        if (
            next_char == "{"
            and next_index + 1 < len(value)
            and value[next_index + 1] in _ENVIRONMENT_NAME_START_CHARS
        ):
            return True
        cursor = next_index
    return False


def _is_absolute_or_escaping_path(value: str) -> bool:
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    return (
        posix_path.is_absolute()
        or windows_path.drive != ""
        or windows_path.root != ""
        or ".." in posix_path.parts
        or ".." in windows_path.parts
    )


class WorkspaceCompanionRequest(BaseModel):
    """Managed cross-repo companion service requested for one workspace."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: ServiceName
    repo_url: Annotated[str, Field(min_length=1, max_length=512)]
    base_branch: Annotated[str | None, Field(default=None, min_length=1, max_length=256)] = None
    build_context: Annotated[str, Field(default=".", min_length=1, max_length=1024)] = "."
    dockerfile: Annotated[str, Field(default="Dockerfile", min_length=1, max_length=1024)] = (
        "Dockerfile"
    )
    env_file: Annotated[str | None, Field(default=None, min_length=1, max_length=1024)] = None
    environment: dict[EnvironmentKey, str] = Field(
        default_factory=dict,
        json_schema_extra=_ENVIRONMENT_SCHEMA,
        description=(
            "Literal environment variables for the companion container. Docker Compose "
            "interpolation syntax such as $VAR and ${VAR} is rejected."
        ),
    )
    environment_secrets: dict[EnvironmentKey, CompanionEnvironmentSecretRequest] = Field(
        default_factory=dict,
        json_schema_extra=_ENVIRONMENT_SECRETS_SCHEMA,
        description=(
            "Env-backed secret references for companion container environment variables. "
            "Values are resolved by AWF from the worker environment and must not contain "
            "raw secret values."
        ),
    )
    compose_up_timeout_seconds: int | None = Field(
        default=None,
        ge=1,
        le=1800,
        description=(
            "Optional per-companion docker compose up wait timeout in seconds. "
            "When omitted, AWF uses the workspace profile docker startup timeout."
        ),
    )
    depends_on: list[ServiceName] = Field(default_factory=list, max_length=64)
    healthcheck_cmd: Annotated[str | None, Field(default=None, min_length=1, max_length=4096)] = (
        None
    )
    ports: list[tuple[int, int]] = Field(
        default_factory=list,
        max_length=64,
        description=(
            "Port mappings use [container_port, host_port] order. AWF renders "
            "them to Docker Compose as host_port:container_port."
        ),
    )
    command: Annotated[str | None, Field(default=None, min_length=1, max_length=4096)] = None
    volumes: list[CompanionVolume] = Field(default_factory=list, max_length=64)

    @field_validator("name")
    @classmethod
    def _validate_companion_name(cls, value: str) -> str:
        if value in RESERVED_COMPANION_SERVICE_NAMES:
            raise ValueError(f"companion name {value!r} is reserved")
        if not companion_name_is_git_branch_component(value):
            raise ValueError("companion name must be usable as a Git branch path component")
        return value

    @field_validator("build_context", "dockerfile", "env_file")
    @classmethod
    def _validate_repo_relative_path(cls, value: str | None, info: Any) -> str | None:
        if value is not None:
            _validate_companion_repo_relative_path(str(info.field_name), value)
        return value

    @field_validator("environment", mode="before")
    @classmethod
    def _validate_environment_keys(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            for key in value:
                if not isinstance(key, str) or not _ENVIRONMENT_KEY_PATTERN.fullmatch(key):
                    raise ValueError(
                        "companion environment keys must be Docker-compatible "
                        "environment variable names"
                    )
        return value

    @field_validator("environment")
    @classmethod
    def _validate_environment_values(cls, value: dict[str, str]) -> dict[str, str]:
        for environment_value in value.values():
            if _value_has_compose_interpolation(environment_value):
                raise ValueError(
                    "companion environment values must not contain Docker Compose "
                    "interpolation syntax"
                )
        return value

    @field_validator("environment_secrets", mode="before")
    @classmethod
    def _validate_environment_secret_keys(cls, value: Any) -> Any:
        """Reject secret target keys that Docker Compose cannot export."""
        if isinstance(value, Mapping):
            for key in value:
                if not isinstance(key, str) or not _ENVIRONMENT_KEY_PATTERN.fullmatch(key):
                    raise ValueError(
                        "companion environment secret keys must be Docker-compatible "
                        "environment variable names"
                    )
        return value

    @model_validator(mode="after")
    def _validate_environment_secret_overlap(self) -> WorkspaceCompanionRequest:
        """Reject ambiguous keys supplied as both literals and env-backed secrets."""
        overlap = sorted(set(self.environment) & set(self.environment_secrets))
        if overlap:
            raise ValueError(
                "companion environment and environment_secrets keys must not overlap: "
                f"{', '.join(overlap)}"
            )
        return self

    @field_validator("healthcheck_cmd")
    @classmethod
    def _validate_healthcheck_cmd(cls, value: str | None) -> str | None:
        if value is not None and _value_has_compose_interpolation(value):
            raise ValueError(
                "companion healthcheck command must not contain Docker Compose interpolation syntax"
            )
        return value

    @field_validator("command")
    @classmethod
    def _validate_command(cls, value: str | None) -> str | None:
        if value is not None and _value_has_compose_interpolation(value):
            raise ValueError(
                "companion command must not contain Docker Compose interpolation syntax"
            )
        return value

    @field_validator("ports")
    @classmethod
    def _validate_ports(cls, value: list[tuple[int, int]]) -> list[tuple[int, int]]:
        host_ports: set[int] = set()
        for container_port, host_port in value:
            if not (1 <= container_port <= 65535 and 1 <= host_port <= 65535):
                raise ValueError("companion ports must be valid TCP port numbers")
            if host_port in host_ports:
                raise ValueError(f"duplicate companion host port {host_port}")
            host_ports.add(host_port)
        return value

    @field_validator("volumes")
    @classmethod
    def _validate_volume_sources(cls, value: list[tuple[str, str]]) -> list[tuple[str, str]]:
        for source, target in value:
            if companion_volume_source_is_repo_relative(source):
                _validate_companion_repo_relative_volume_source(source)
            elif _is_absolute_or_escaping_path(source):
                raise ValueError("volume source must be a named volume or repo-relative path")
            else:
                _validate_companion_named_volume_source(source)
            _validate_companion_volume_target(target)
        return value
