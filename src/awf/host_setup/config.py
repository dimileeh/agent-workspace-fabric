"""Host setup config schema and safe YAML IO helpers."""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from awf.host_setup.source_assets import SourceCheckoutAssetMetadata

HOST_SETUP_CONFIG_CORRUPT = "HOST_SETUP_CONFIG_CORRUPT"
HOST_SETUP_CONFIG_SECRET_VALUE = "HOST_SETUP_CONFIG_SECRET_VALUE"
HOST_SETUP_CONFIG_WRITE_FAILED = "HOST_SETUP_CONFIG_WRITE_FAILED"
HOST_SETUP_CONFIG_VERSION = 1
DEFAULT_INSTALL_CHANNEL = "stable"
DEFAULT_API_HOST_PORT = 8000
DEFAULT_HOST_SETUP_WORK_DIR = "~/.awf/service"

_SAFE_CREDENTIAL_REF_PREFIXES = ("keyring://", "env://", "plain-file://")
_SECRET_VALUE_PREFIXES = (
    "sk-",
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "github_pat_",
    "xoxb-",
    "xoxp-",
    "glpat-",
)
_SECRET_KEY_NAMES = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "secret",
        "client_secret",
        "password",
        "private_key",
        "github_token",
        "openai_api_key",
    }
)


class _SecretPayloadError(ValueError):
    """Internal sanitized error for secret-bearing config payloads."""

    def __init__(self, *, path: tuple[str, ...], issue: str) -> None:
        """Build a sanitized secret-payload diagnostic."""
        self.path = path
        self.issue = issue
        super().__init__(f"{issue} at {_format_path(path)}")

    def details(self) -> dict[str, object]:
        """Return secret-free diagnostic details for config errors."""
        return {
            "issue": self.issue,
            "path": _format_path(self.path),
        }


class _RecursivePayloadError(ValueError):
    """Internal sanitized error for recursive YAML payloads."""

    def __init__(self, *, path: tuple[str, ...]) -> None:
        """Build a sanitized recursive-payload diagnostic."""
        self.path = path
        super().__init__(f"recursive YAML alias at {_format_path(path)}")

    def details(self) -> dict[str, object]:
        """Return secret-free diagnostic details for config errors."""
        return {
            "error_type": "recursive_yaml_alias",
            "path": _format_path(self.path),
        }


class HostSetupConfigError(RuntimeError):
    """Reason-coded host setup config failure."""

    def __init__(
        self,
        *,
        reason_code: str,
        message: str,
        path: Path,
        details: Mapping[str, object] | None = None,
    ) -> None:
        """Build a reason-coded config error without embedding secret values."""
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.path = path
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable diagnostic payload."""
        payload: dict[str, object] = {
            "status": "failed",
            "reason_code": self.reason_code,
            "message": self.message,
            "path": str(self.path),
        }
        if self.details:
            payload["details"] = self.details
        return payload


class _HostSetupBaseModel(BaseModel):
    """Shared strict, immutable Pydantic base for host setup config models."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
        hide_input_in_errors=True,
    )


class InstallConfig(_HostSetupBaseModel):
    """AWF install channel metadata."""

    channel: str = Field(default=DEFAULT_INSTALL_CHANNEL, min_length=1, max_length=64)


class ApiConfig(_HostSetupBaseModel):
    """Local AWF API host settings."""

    host_port: int = Field(default=DEFAULT_API_HOST_PORT, ge=1, le=65535)


class ProviderConfig(_HostSetupBaseModel):
    """Provider setup state with a credential reference, never a credential value."""

    credential_ref: str | None = Field(default=None, min_length=1, max_length=512)
    source: str | None = Field(default=None, min_length=1, max_length=128)
    status: str = Field(default="missing", min_length=1, max_length=128)

    @field_validator("credential_ref")
    @classmethod
    def _validate_credential_ref(cls, value: str | None) -> str | None:
        """Require provider credentials to be safe references, never raw secrets."""
        if value is None:
            return None
        if _looks_like_secret_value(value):
            raise ValueError("credential_ref must be a reference, not a secret value")
        if not value.startswith(_SAFE_CREDENTIAL_REF_PREFIXES):
            raise ValueError(
                "credential_ref must use keyring://, env://, or plain-file:// references"
            )
        return value


class ClientIntegrationConfig(_HostSetupBaseModel):
    """Client integration state written by setup flows."""

    status: str = Field(default="not_configured", min_length=1, max_length=128)
    updated_at: datetime | None = None


class ConsentConfig(_HostSetupBaseModel):
    """Machine-level consent flags recorded by setup flows."""

    plain_file_secrets: bool = False
    source_checkout_assets: bool = False


class HostSetupConfig(_HostSetupBaseModel):
    """Versioned host setup config persisted at ``~/.awf/config.yml``."""

    version: int = HOST_SETUP_CONFIG_VERSION
    install: InstallConfig = Field(default_factory=InstallConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    work_dir: str = Field(default=DEFAULT_HOST_SETUP_WORK_DIR, min_length=1, max_length=4096)
    providers: Mapping[str, ProviderConfig] = Field(default_factory=dict, validate_default=True)
    clients: Mapping[str, ClientIntegrationConfig] = Field(
        default_factory=dict, validate_default=True
    )
    consent: ConsentConfig = Field(default_factory=ConsentConfig)
    source_checkout: SourceCheckoutAssetMetadata | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_secret_payload(cls, value: object) -> object:
        """Reject secret-bearing keys or values before schema validation."""
        _ensure_no_secret_payload(value)
        return value

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: int) -> int:
        """Reject host setup config versions this code cannot interpret."""
        if value != HOST_SETUP_CONFIG_VERSION:
            raise ValueError(f"unsupported host setup config version: {value}")
        return value

    @field_validator("providers", mode="after")
    @classmethod
    def _freeze_providers(
        cls,
        value: Mapping[str, ProviderConfig],
    ) -> Mapping[str, ProviderConfig]:
        """Prevent post-validation provider state from being mutated in place."""
        return MappingProxyType(dict(value))

    @field_validator("clients", mode="after")
    @classmethod
    def _freeze_clients(
        cls,
        value: Mapping[str, ClientIntegrationConfig],
    ) -> Mapping[str, ClientIntegrationConfig]:
        """Prevent post-validation client state from being mutated in place."""
        return MappingProxyType(dict(value))

    @field_serializer("providers")
    def _serialize_providers(
        self,
        value: Mapping[str, ProviderConfig],
    ) -> dict[str, ProviderConfig]:
        """Serialize immutable provider mappings as plain mappings."""
        return dict(value)

    @field_serializer("clients")
    def _serialize_clients(
        self,
        value: Mapping[str, ClientIntegrationConfig],
    ) -> dict[str, ClientIntegrationConfig]:
        """Serialize immutable client mappings as plain mappings."""
        return dict(value)


def default_host_setup_config_path(*, home: str | Path | None = None) -> Path:
    """Return the default host setup config path."""
    unresolved_home = Path("~") if home is None else Path(home)
    unresolved_path = unresolved_home / ".awf" / "config.yml"
    try:
        base = Path.home() if home is None else unresolved_home.expanduser()
    except (OSError, RuntimeError) as exc:
        raise _config_path_resolution_error(unresolved_path, exc) from exc
    return base / ".awf" / "config.yml"


def read_host_setup_config(*, path: str | Path | None = None) -> HostSetupConfig:
    """Read host setup config, returning defaults when the config is absent."""
    config_path = _resolve_config_path(path)

    try:
        if not config_path.exists():
            return HostSetupConfig()
        raw_text = config_path.read_text(encoding="utf-8")
        raw: object = yaml.safe_load(raw_text)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise _config_corrupt_error(
            config_path,
            details={"error_type": type(exc).__name__},
        ) from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise _config_corrupt_error(config_path, details={"error_type": "non_mapping_yaml"})

    try:
        _ensure_no_secret_payload(raw)
    except _SecretPayloadError as exc:
        raise _config_secret_error(config_path, details=exc.details()) from exc
    except _RecursivePayloadError as exc:
        raise _config_corrupt_error(config_path, details=exc.details()) from exc

    try:
        return HostSetupConfig.model_validate(raw)
    except ValidationError as exc:
        if _validation_contains_secret_error(exc):
            raise _config_secret_error(
                config_path,
                details=_validation_error_details(exc),
            ) from exc
        raise _config_corrupt_error(
            config_path,
            details=_validation_error_details(exc),
        ) from exc


def write_host_setup_config(
    config: HostSetupConfig,
    *,
    path: str | Path | None = None,
) -> None:
    """Atomically write host setup config with conservative permissions."""
    owns_parent_permissions = path is None
    config_path = _resolve_config_path(path)
    payload = cast(dict[str, object], config.model_dump(mode="json", exclude_none=True))
    try:
        _ensure_no_secret_payload(payload)
    except _SecretPayloadError as exc:
        raise _config_secret_error(config_path, details=exc.details()) from exc
    except _RecursivePayloadError as exc:
        raise _config_corrupt_error(config_path, details=exc.details()) from exc

    tmp_path = config_path.with_name(f".{config_path.name}.{secrets.token_hex(8)}.tmp")
    text = yaml.safe_dump(payload, sort_keys=False)
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if owns_parent_permissions:
            _chmod_best_effort(config_path.parent, 0o700)
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        _chmod_best_effort(tmp_path, 0o600)
        tmp_path.replace(config_path)
        _chmod_best_effort(config_path, 0o600)
    except OSError as exc:
        with suppress(OSError):
            tmp_path.unlink()
        raise _config_write_failed_error(
            config_path,
            details={"error_type": type(exc).__name__},
        ) from exc


def _resolve_config_path(path: str | Path | None) -> Path:
    """Return the explicit config path or the default host setup path."""
    if path is None:
        return default_host_setup_config_path()
    base = Path(path)
    try:
        return base.expanduser()
    except (OSError, RuntimeError) as exc:
        raise _config_path_resolution_error(base, exc) from exc


def _config_path_resolution_error(path: Path, exc: OSError | RuntimeError) -> HostSetupConfigError:
    """Build a sanitized error for config path expansion failures."""
    return _config_corrupt_error(
        path,
        message="Unable to resolve host setup config path.",
        details={"error_type": type(exc).__name__},
    )


def _ensure_no_secret_payload(
    value: object,
    *,
    path: tuple[str, ...] = (),
    _active_container_ids: set[int] | None = None,
) -> None:
    """Recursively reject secret-looking keys and values in config payloads."""
    active_container_ids = set() if _active_container_ids is None else _active_container_ids
    if isinstance(value, BaseModel):
        _ensure_no_secret_payload(
            value.model_dump(mode="json"),
            path=path,
            _active_container_ids=active_container_ids,
        )
        return
    if isinstance(value, Mapping):
        container_id = id(value)
        if container_id in active_container_ids:
            raise _RecursivePayloadError(path=path)
        active_container_ids.add(container_id)
        try:
            for raw_key, raw_value in value.items():
                if isinstance(raw_key, str):
                    if _is_secret_key(raw_key):
                        raise _SecretPayloadError(
                            path=(*path, raw_key),
                            issue="secret-bearing key",
                        )
                    if _looks_like_secret_value(raw_key):
                        raise _SecretPayloadError(
                            path=(*path, "<secret-key>"),
                            issue="secret-like key",
                        )
                child_path = (*path, str(raw_key))
                _ensure_no_secret_payload(
                    raw_value,
                    path=child_path,
                    _active_container_ids=active_container_ids,
                )
        finally:
            active_container_ids.remove(container_id)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        container_id = id(value)
        if container_id in active_container_ids:
            raise _RecursivePayloadError(path=path)
        active_container_ids.add(container_id)
        try:
            for index, item in enumerate(value):
                _ensure_no_secret_payload(
                    item,
                    path=(*path, f"[{index}]"),
                    _active_container_ids=active_container_ids,
                )
        finally:
            active_container_ids.remove(container_id)
        return
    if isinstance(value, str) and _looks_like_secret_value(value):
        raise _SecretPayloadError(path=path, issue="secret-like value")


def _is_secret_key(key: str) -> bool:
    """Return whether a normalized config key is reserved for secret values."""
    normalized = key.strip().lower().replace("-", "_")
    return normalized in _SECRET_KEY_NAMES


def _looks_like_secret_value(value: str) -> bool:
    """Return whether a string resembles a raw credential value."""
    stripped = value.strip()
    lower = stripped.lower()
    return lower.startswith("bearer ") or lower.startswith(_SECRET_VALUE_PREFIXES)


def _format_path(path: tuple[str, ...]) -> str:
    """Format a nested config path for secret-free diagnostics."""
    return ".".join(path) if path else "<root>"


def _chmod_best_effort(path: Path, mode: int) -> None:
    """Apply POSIX file permissions when supported by the host platform."""
    if os.name != "posix":
        return
    with suppress(OSError):
        path.chmod(mode)


def _config_corrupt_error(
    path: Path,
    *,
    message: str = "Host setup config is corrupt or unsupported.",
    details: Mapping[str, object],
) -> HostSetupConfigError:
    """Build a reason-coded corrupt-config error."""
    return HostSetupConfigError(
        reason_code=HOST_SETUP_CONFIG_CORRUPT,
        message=message,
        path=path,
        details=details,
    )


def _config_write_failed_error(
    path: Path,
    *,
    details: Mapping[str, object],
) -> HostSetupConfigError:
    """Build a reason-coded write-failure config error."""
    return HostSetupConfigError(
        reason_code=HOST_SETUP_CONFIG_WRITE_FAILED,
        message="Unable to write host setup config.",
        path=path,
        details=details,
    )


def _config_secret_error(
    path: Path,
    *,
    details: Mapping[str, object],
) -> HostSetupConfigError:
    """Build a reason-coded secret-bearing-config error."""
    return HostSetupConfigError(
        reason_code=HOST_SETUP_CONFIG_SECRET_VALUE,
        message="Host setup config contains a secret value or secret-bearing key.",
        path=path,
        details=details,
    )


def _validation_error_details(exc: ValidationError) -> dict[str, object]:
    """Return sanitized Pydantic validation details for config diagnostics."""
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    return {
        "error_count": exc.error_count(),
        "error_types": sorted({str(error.get("type", "validation_error")) for error in errors}),
        "locations": [_format_validation_location(error.get("loc", ())) for error in errors],
    }


def _validation_contains_secret_error(exc: ValidationError) -> bool:
    """Return whether validation failed because a secret reached the boundary."""
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    return any(
        "secret" in str(error.get("msg", "")).lower()
        or "credential_ref must be a reference" in str(error.get("msg", ""))
        for error in errors
    )


def _format_validation_location(location: object) -> str:
    """Format a Pydantic validation location for diagnostics."""
    if not isinstance(location, tuple):
        return str(location)
    return ".".join(str(item) for item in location) if location else "<root>"


__all__ = [
    "DEFAULT_API_HOST_PORT",
    "DEFAULT_HOST_SETUP_WORK_DIR",
    "DEFAULT_INSTALL_CHANNEL",
    "HOST_SETUP_CONFIG_CORRUPT",
    "HOST_SETUP_CONFIG_SECRET_VALUE",
    "HOST_SETUP_CONFIG_WRITE_FAILED",
    "HOST_SETUP_CONFIG_VERSION",
    "ApiConfig",
    "ClientIntegrationConfig",
    "ConsentConfig",
    "HostSetupConfig",
    "HostSetupConfigError",
    "InstallConfig",
    "ProviderConfig",
    "default_host_setup_config_path",
    "read_host_setup_config",
    "write_host_setup_config",
]
