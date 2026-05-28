"""Host setup config and source-checkout asset tests."""

from __future__ import annotations

import os
import stat
from collections import UserList
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import awf.host_setup as host_setup
import awf.host_setup.config as host_setup_config
import awf.host_setup.source_assets as source_assets
from awf.host_setup import (
    ApiConfig,
    ClientIntegrationConfig,
    ConsentConfig,
    HostSetupConfig,
    HostSetupConfigError,
    InstallConfig,
    ProviderConfig,
    SourceCheckoutError,
    default_host_setup_config_path,
    read_host_setup_config,
    validate_source_checkout,
    verified_source_from_metadata,
    write_host_setup_config,
)
from awf.host_setup.config import _ensure_no_secret_payload, _SecretPayloadError
from awf.host_setup.source_assets import (
    SOURCE_CHECKOUT_MARKERS,
    SOURCE_CHECKOUT_REQUIRED_MARKER_PATHS,
)

_FIXED_NOW = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)


def _write_valid_source_checkout(root: Path) -> Path:
    for marker in SOURCE_CHECKOUT_MARKERS:
        path = root / marker.path
        if marker.kind == "dir":
            path.mkdir(parents=True, exist_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{marker.path}\n", encoding="utf-8")
    return root


@pytest.mark.unit
def test_host_setup_config_round_trips_with_conservative_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    checkout = _write_valid_source_checkout(tmp_path / "checkout")
    verified = validate_source_checkout(checkout, clock=lambda: _FIXED_NOW)
    config = HostSetupConfig(
        install=InstallConfig(channel="development"),
        api=ApiConfig(host_port=8123),
        work_dir=str(tmp_path / "awf-work"),
        providers={
            "github": ProviderConfig(
                credential_ref="keyring://awf/github/token",
                source="gh",
                status="ready",
            )
        },
        clients={
            "codex": ClientIntegrationConfig(
                status="configured",
                updated_at=_FIXED_NOW,
            )
        },
        consent=ConsentConfig(
            plain_file_secrets=True,
            source_checkout_assets=True,
        ),
        source_checkout=verified.to_metadata(),
    )
    config_path = default_host_setup_config_path()

    write_host_setup_config(config)

    if os.name == "posix":
        assert stat.S_IMODE(config_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert read_host_setup_config() == config

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert raw["providers"]["github"]["credential_ref"] == "keyring://awf/github/token"
    assert "token:" not in config_path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_host_setup_config_write_preserves_explicit_parent_permissions(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "shared"
    config_dir.mkdir()
    if os.name == "posix":
        config_dir.chmod(0o755)
    config_path = config_dir / "config.yml"

    write_host_setup_config(HostSetupConfig(api=ApiConfig(host_port=8124)), path=config_path)

    if os.name == "posix":
        assert stat.S_IMODE(config_dir.stat().st_mode) == 0o755
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert read_host_setup_config(path=config_path).api.host_port == 8124


@pytest.mark.unit
def test_host_setup_config_write_preserves_explicit_awf_parent_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_dir = tmp_path / "repo" / ".awf"
    config_dir.mkdir(parents=True)
    if os.name == "posix":
        config_dir.chmod(0o755)
    config_path = config_dir / "config.yml"

    write_host_setup_config(HostSetupConfig(api=ApiConfig(host_port=8124)), path=config_path)

    if os.name == "posix":
        assert stat.S_IMODE(config_dir.stat().st_mode) == 0o755
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert read_host_setup_config(path=config_path).api.host_port == 8124


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions and symlinks only")
def test_host_setup_config_write_preserves_parent_for_explicit_symlink_to_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    default_path = default_host_setup_config_path()
    config_dir = tmp_path / "shared"
    config_dir.mkdir()
    config_dir.chmod(0o755)
    config_path = config_dir / "config.yml"
    config_path.symlink_to(default_path)

    write_host_setup_config(HostSetupConfig(api=ApiConfig(host_port=8126)), path=config_path)

    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o755
    assert not config_path.is_symlink()
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert read_host_setup_config(path=config_path).api.host_port == 8126


@pytest.mark.unit
def test_host_setup_config_write_secures_explicit_default_parent_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config_path = default_host_setup_config_path()

    if os.name == "posix":
        original_umask = os.umask(0o022)
        try:
            write_host_setup_config(
                HostSetupConfig(api=ApiConfig(host_port=8125)),
                path=config_path,
            )
        finally:
            os.umask(original_umask)
    else:
        write_host_setup_config(
            HostSetupConfig(api=ApiConfig(host_port=8125)),
            path=config_path,
        )

    if os.name == "posix":
        assert stat.S_IMODE(config_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert read_host_setup_config(path=config_path).api.host_port == 8125


@pytest.mark.unit
def test_host_setup_config_write_uses_unique_temp_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = default_host_setup_config_path(home=tmp_path / "home")
    opened_paths: list[Path] = []
    original_open = os.open

    def _record_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
    ) -> int:
        opened_paths.append(Path(os.fsdecode(path)))
        return original_open(path, flags, mode)

    monkeypatch.setattr(os, "open", _record_open)

    write_host_setup_config(HostSetupConfig(api=ApiConfig(host_port=8124)), path=config_path)
    write_host_setup_config(HostSetupConfig(api=ApiConfig(host_port=8125)), path=config_path)

    assert len(opened_paths) == 2
    assert opened_paths[0].parent == config_path.parent
    assert opened_paths[1].parent == config_path.parent
    assert opened_paths[0].name.startswith(f".{config_path.name}.")
    assert opened_paths[1].name.startswith(f".{config_path.name}.")
    assert opened_paths[0].name.endswith(".tmp")
    assert opened_paths[1].name.endswith(".tmp")
    assert opened_paths[0] != opened_paths[1]
    assert read_host_setup_config(path=config_path).api.host_port == 8125


@pytest.mark.unit
def test_host_setup_config_write_parent_creation_error_is_reason_coded(
    tmp_path: Path,
) -> None:
    config_path = default_host_setup_config_path(home=tmp_path / "home")
    config_path.parent.parent.mkdir(parents=True)
    config_path.parent.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(HostSetupConfigError) as exc_info:
        write_host_setup_config(HostSetupConfig(), path=config_path)

    error = exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_WRITE_FAILED"
    assert error.path == config_path
    assert error.details == {"error_type": "FileExistsError"}
    assert "not a directory" not in str(error.to_dict())


@pytest.mark.unit
def test_host_setup_config_write_persistence_error_uses_write_failed_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = default_host_setup_config_path(home=tmp_path / "home")

    def _open_fails(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
    ) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(os, "open", _open_fails)

    with pytest.raises(HostSetupConfigError) as exc_info:
        write_host_setup_config(HostSetupConfig(), path=config_path)

    error = exc_info.value
    assert host_setup.HOST_SETUP_CONFIG_WRITE_FAILED == "HOST_SETUP_CONFIG_WRITE_FAILED"
    assert error.reason_code == "HOST_SETUP_CONFIG_WRITE_FAILED"
    assert error.path == config_path
    assert error.details == {"error_type": "OSError"}
    assert "disk full" not in str(error.to_dict())


@pytest.mark.unit
@pytest.mark.parametrize("operation", ["read", "write"])
def test_host_setup_config_path_resolution_failure_for_default_home_is_reason_coded(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    def _home_unavailable() -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", _home_unavailable)

    with pytest.raises(HostSetupConfigError) as exc_info:
        if operation == "read":
            read_host_setup_config()
        else:
            write_host_setup_config(HostSetupConfig())

    error = exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_CORRUPT"
    assert error.path == Path("~/.awf/config.yml")
    assert error.details == {"error_type": "RuntimeError"}
    assert "Could not determine home directory" not in str(error.to_dict())


@pytest.mark.unit
@pytest.mark.parametrize("operation", ["read", "write"])
def test_host_setup_config_path_resolution_failure_for_explicit_path_is_reason_coded(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    config_path = Path("~/awf-config.yml")

    def _expanduser_unavailable(self: Path) -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "expanduser", _expanduser_unavailable)

    with pytest.raises(HostSetupConfigError) as exc_info:
        if operation == "read":
            read_host_setup_config(path=config_path)
        else:
            write_host_setup_config(HostSetupConfig(), path=config_path)

    error = exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_CORRUPT"
    assert error.path == config_path
    assert error.details == {"error_type": "RuntimeError"}
    assert "Could not determine home directory" not in str(error.to_dict())


@pytest.mark.unit
def test_default_host_setup_config_path_resolution_failure_for_explicit_home_is_reason_coded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = Path("~/awf-home")

    def _expanduser_unavailable(self: Path) -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "expanduser", _expanduser_unavailable)

    with pytest.raises(HostSetupConfigError) as exc_info:
        default_host_setup_config_path(home=home)

    error = exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_CORRUPT"
    assert error.path == home / ".awf" / "config.yml"
    assert error.details == {"error_type": "RuntimeError"}
    assert "Could not determine home directory" not in str(error.to_dict())


@pytest.mark.unit
def test_host_setup_config_rejects_secret_values(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        ProviderConfig(credential_ref="ghp_abcdefghijklmnopqrstuvwxyz123456")
    with pytest.raises(ValidationError):
        HostSetupConfig.model_validate(
            {"providers": {"openai": {"credential_ref": "sk-raw-secret-value"}}}
        )
    with pytest.raises(ValidationError):
        HostSetupConfig.model_validate({"providers": {"github": {"token": "ghp_raw"}}})

    config_path = default_host_setup_config_path(home=tmp_path / "home")
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "version: 1\nproviders:\n  github:\n    token: ghp_raw_secret\n",
        encoding="utf-8",
    )

    with pytest.raises(HostSetupConfigError) as exc_info:
        read_host_setup_config(path=config_path)

    error = exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_SECRET_VALUE"
    assert error.path == config_path
    assert "ghp_raw_secret" not in str(error.to_dict())


@pytest.mark.unit
def test_host_setup_config_rejects_duplicate_yaml_keys_before_secret_scan(
    tmp_path: Path,
) -> None:
    config_path = default_host_setup_config_path(home=tmp_path / "home")
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "\n".join(
            [
                "version: 1",
                "providers:",
                "  github:",
                "    credential_ref: ghp_raw_secret",
                "    credential_ref: env://GITHUB_TOKEN",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(HostSetupConfigError) as exc_info:
        read_host_setup_config(path=config_path)

    error = exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_CORRUPT"
    assert error.path == config_path
    assert error.details == {"error_type": "duplicate_mapping_key"}
    assert "ghp_raw_secret" not in str(error.to_dict())


@pytest.mark.unit
def test_host_setup_config_reads_missing_and_empty_yaml_as_defaults(tmp_path: Path) -> None:
    config_path = default_host_setup_config_path(home=tmp_path / "home")

    assert read_host_setup_config(path=config_path) == HostSetupConfig()

    config_path.parent.mkdir(parents=True)
    config_path.write_text("", encoding="utf-8")

    assert read_host_setup_config(path=config_path) == HostSetupConfig()


@pytest.mark.unit
def test_host_setup_config_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    config_path = default_host_setup_config_path(home=tmp_path / "home")
    config_path.parent.mkdir(parents=True)
    config_path.write_text("- not-a-mapping\n", encoding="utf-8")

    with pytest.raises(HostSetupConfigError) as exc_info:
        read_host_setup_config(path=config_path)

    error = exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_CORRUPT"
    assert error.path == config_path
    assert error.details == {"error_type": "non_mapping_yaml"}


@pytest.mark.unit
def test_provider_config_accepts_missing_ref_and_rejects_unsafe_ref() -> None:
    assert ProviderConfig(credential_ref=None).credential_ref is None

    with pytest.raises(ValidationError) as exc_info:
        ProviderConfig(credential_ref="literal-token-file")

    assert "literal-token-file" not in str(exc_info.value)


@pytest.mark.unit
def test_host_setup_config_read_wraps_unsupported_version(tmp_path: Path) -> None:
    config_path = default_host_setup_config_path(home=tmp_path / "home")
    config_path.parent.mkdir(parents=True)
    config_path.write_text("version: 2\n", encoding="utf-8")

    with pytest.raises(HostSetupConfigError) as exc_info:
        read_host_setup_config(path=config_path)

    error = exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_CORRUPT"
    assert error.details == {
        "error_count": 1,
        "error_types": ["value_error"],
        "locations": ["version"],
    }
    assert "unsupported host setup config version" not in str(error.to_dict())


@pytest.mark.unit
def test_host_setup_config_read_wraps_validation_secret_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = default_host_setup_config_path(home=tmp_path / "home")
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "version: 1\nproviders:\n  github:\n    credential_ref: ghp_raw_secret\n",
        encoding="utf-8",
    )

    def _allow_secret_payload(value: object) -> None:
        del value

    monkeypatch.setattr(host_setup_config, "_ensure_no_secret_payload", _allow_secret_payload)

    with pytest.raises(HostSetupConfigError) as exc_info:
        read_host_setup_config(path=config_path)

    error = exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_SECRET_VALUE"
    assert error.details == {
        "error_count": 1,
        "error_types": ["value_error"],
        "locations": ["providers.github.credential_ref"],
    }
    assert "ghp_raw_secret" not in str(error.to_dict())


@pytest.mark.unit
def test_host_setup_config_rejects_secret_like_mapping_keys(tmp_path: Path) -> None:
    raw_secret_key = "ghp_raw_secret"
    safe_provider = ProviderConfig(credential_ref="env://GITHUB_TOKEN")

    with pytest.raises(ValidationError) as exc_info:
        HostSetupConfig(providers={raw_secret_key: safe_provider})

    assert raw_secret_key not in str(exc_info.value)

    config_path = default_host_setup_config_path(home=tmp_path / "home")
    bypassed_config = HostSetupConfig.model_construct(
        version=1,
        install=InstallConfig(),
        api=ApiConfig(),
        work_dir="~/.awf/service",
        providers={raw_secret_key: safe_provider},
        clients={},
        consent=ConsentConfig(),
        source_checkout=None,
    )

    with pytest.raises(HostSetupConfigError) as write_exc_info:
        write_host_setup_config(bypassed_config, path=config_path)

    error = write_exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_SECRET_VALUE"
    assert error.details == {"issue": "secret-like key", "path": "providers.<secret-key>"}
    assert raw_secret_key not in str(error.to_dict())
    assert not config_path.exists()


@pytest.mark.unit
def test_host_setup_config_write_wraps_recursive_payload_scan_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = default_host_setup_config_path(home=tmp_path / "home")
    config = HostSetupConfig()

    def _raise_recursive_payload_error(value: object) -> None:
        raise host_setup_config._RecursivePayloadError(path=("providers",))

    monkeypatch.setattr(
        host_setup_config, "_ensure_no_secret_payload", _raise_recursive_payload_error
    )

    with pytest.raises(HostSetupConfigError) as exc_info:
        write_host_setup_config(config, path=config_path)

    error = exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_CORRUPT"
    assert error.path == config_path
    assert error.details == {
        "error_type": "recursive_yaml_alias",
        "path": "providers",
    }
    assert not config_path.exists()


@pytest.mark.unit
def test_host_setup_config_rejects_in_place_provider_mutation() -> None:
    empty_config = HostSetupConfig()

    with pytest.raises(TypeError):
        empty_config.providers["github"] = {"token": "ghp_raw"}

    assert "github" not in empty_config.providers

    configured = HostSetupConfig(
        providers={"github": ProviderConfig(credential_ref="env://GITHUB_TOKEN")}
    )

    with pytest.raises(TypeError):
        configured.providers["openai"] = {"credential_ref": "sk-raw-secret-value"}

    assert set(configured.providers) == {"github"}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw_config", "expected_issue", "expected_path"),
    [
        (
            "version: 1\naudit:\n  - sk-raw-secret-value\n",
            "secret-like value",
            "audit.[0]",
        ),
        (
            "version: 1\naudit:\n  - token: ghp_raw_secret\n",
            "secret-bearing key",
            "audit.[0].token",
        ),
    ],
)
def test_host_setup_config_rejects_secret_payloads_inside_lists(
    tmp_path: Path,
    raw_config: str,
    expected_issue: str,
    expected_path: str,
) -> None:
    config_path = default_host_setup_config_path(home=tmp_path / "home")
    config_path.parent.mkdir(parents=True)
    config_path.write_text(raw_config, encoding="utf-8")

    with pytest.raises(HostSetupConfigError) as exc_info:
        read_host_setup_config(path=config_path)

    error = exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_SECRET_VALUE"
    assert error.path == config_path
    assert error.details == {"issue": expected_issue, "path": expected_path}
    assert "sk-raw-secret-value" not in str(error.to_dict())
    assert "ghp_raw_secret" not in str(error.to_dict())


@pytest.mark.unit
def test_host_setup_config_treats_recursive_yaml_alias_as_corrupt(tmp_path: Path) -> None:
    config_path = default_host_setup_config_path(home=tmp_path / "home")
    config_path.parent.mkdir(parents=True)
    config_path.write_text("version: 1\naudit: &a [*a]\n", encoding="utf-8")

    with pytest.raises(HostSetupConfigError) as exc_info:
        read_host_setup_config(path=config_path)

    error = exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_CORRUPT"
    assert error.path == config_path
    assert error.details == {"error_type": "recursive_yaml_alias", "path": "audit.[0]"}


@pytest.mark.unit
def test_secret_payload_scan_rejects_tuple_nested_secret_payloads() -> None:
    with pytest.raises(_SecretPayloadError) as exc_info:
        _ensure_no_secret_payload({"audit": ({"token": "ghp_raw_secret"},)})

    assert exc_info.value.details() == {
        "issue": "secret-bearing key",
        "path": "audit.[0].token",
    }


@pytest.mark.unit
def test_secret_payload_scan_rejects_sequence_container_secret_payloads() -> None:
    with pytest.raises(_SecretPayloadError) as exc_info:
        _ensure_no_secret_payload({"audit": UserList(["sk-raw-secret-value"])})

    assert exc_info.value.details() == {
        "issue": "secret-like value",
        "path": "audit.[0]",
    }


@pytest.mark.unit
def test_secret_payload_scan_rejects_recursive_mappings() -> None:
    payload: dict[str, object] = {}
    payload["self"] = payload

    with pytest.raises(host_setup_config._RecursivePayloadError) as exc_info:
        _ensure_no_secret_payload(payload)

    assert exc_info.value.details() == {
        "error_type": "recursive_yaml_alias",
        "path": "self",
    }


@pytest.mark.unit
def test_secret_payload_scan_tracks_non_string_mapping_keys() -> None:
    with pytest.raises(_SecretPayloadError) as exc_info:
        _ensure_no_secret_payload({1: ["sk-raw-secret-value"]})

    assert exc_info.value.details() == {
        "issue": "secret-like value",
        "path": "1.[0]",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw_secret",
    [
        "SK-proj-raw-secret-value",
        "GHP_raw_secret_value",
        "XOXB-raw-secret-value",
    ],
)
def test_secret_payload_scan_rejects_uppercase_token_prefixes(raw_secret: str) -> None:
    with pytest.raises(_SecretPayloadError) as exc_info:
        _ensure_no_secret_payload({"audit": [raw_secret]})

    assert exc_info.value.details() == {
        "issue": "secret-like value",
        "path": "audit.[0]",
    }
    assert raw_secret not in str(exc_info.value.details())


@pytest.mark.unit
def test_corrupt_config_has_reason_code_and_path_details(tmp_path: Path) -> None:
    config_path = default_host_setup_config_path(home=tmp_path / "home")
    config_path.parent.mkdir(parents=True)
    config_path.write_text("version: 1\napi: [unterminated\n", encoding="utf-8")

    with pytest.raises(HostSetupConfigError) as exc_info:
        read_host_setup_config(path=config_path)

    error = exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_CORRUPT"
    assert error.path == config_path
    assert str(config_path) in str(error.to_dict())
    assert "unterminated" not in str(error.to_dict())


@pytest.mark.unit
def test_unreadable_config_exists_check_is_reason_coded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = default_host_setup_config_path(home=tmp_path / "home")
    original_exists = Path.exists

    def _permission_denied_for_config(self: Path) -> bool:
        if self == config_path:
            raise PermissionError("permission denied")
        return original_exists(self)

    monkeypatch.setattr(Path, "exists", _permission_denied_for_config)

    with pytest.raises(HostSetupConfigError) as exc_info:
        read_host_setup_config(path=config_path)

    error = exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_CORRUPT"
    assert error.path == config_path
    assert error.details == {"error_type": "PermissionError"}
    assert "permission denied" not in str(error.to_dict())


@pytest.mark.unit
def test_host_setup_error_to_dict_omits_empty_details(tmp_path: Path) -> None:
    error = HostSetupConfigError(
        reason_code="HOST_SETUP_CONFIG_CORRUPT",
        message="Host setup config is corrupt or unsupported.",
        path=tmp_path / "config.yml",
    )

    assert error.to_dict() == {
        "status": "failed",
        "reason_code": "HOST_SETUP_CONFIG_CORRUPT",
        "message": "Host setup config is corrupt or unsupported.",
        "path": str(tmp_path / "config.yml"),
    }


@pytest.mark.unit
def test_config_path_helpers_handle_resolution_and_platform_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yml"

    def _default_path_unavailable() -> Path:
        raise HostSetupConfigError(
            reason_code="HOST_SETUP_CONFIG_CORRUPT",
            message="Unable to resolve host setup config path.",
            path=Path("~/.awf/config.yml"),
        )

    monkeypatch.setattr(
        host_setup_config,
        "default_host_setup_config_path",
        _default_path_unavailable,
    )
    write_host_setup_config(HostSetupConfig(), path=config_path)
    assert read_host_setup_config(path=config_path) == HostSetupConfig()

    def _absolute_unavailable(self: Path) -> Path:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "absolute", _absolute_unavailable)
    assert host_setup_config._normalized_config_path(config_path) == config_path

    monkeypatch.setattr(host_setup_config.os, "name", "nt")
    host_setup_config._chmod_best_effort(tmp_path / "missing", 0o600)


@pytest.mark.unit
def test_validation_location_formatter_handles_non_tuple_and_root_locations() -> None:
    assert host_setup_config._format_validation_location("version") == "version"
    assert (
        host_setup_config._format_validation_location(("providers", "github", "credential_ref"))
        == "providers.github.credential_ref"
    )
    assert host_setup_config._format_validation_location(()) == "<root>"


@pytest.mark.unit
def test_valid_source_checkout_returns_verified_asset_paths(tmp_path: Path) -> None:
    checkout = _write_valid_source_checkout(tmp_path / "checkout")

    verified = validate_source_checkout(checkout, clock=lambda: _FIXED_NOW)

    assert verified.root == checkout.resolve()
    assert verified.compose_file == checkout.resolve() / "docker/compose/local-service.yml"
    assert verified.agent_runtime_dockerfile == (
        checkout.resolve() / "docker/agent-runtime.Dockerfile"
    )
    assert verified.control_plane_dockerfile == (
        checkout.resolve() / "docker/control-plane.Dockerfile"
    )
    assert verified.workspace_base_template == (
        checkout.resolve() / "docker/compose/workspace.base.yml.j2"
    )
    assert verified.docs_dir == checkout.resolve() / "docs"
    assert verified.releasing_path == checkout.resolve() / "RELEASING.md"
    assert verified.markers == tuple(SOURCE_CHECKOUT_REQUIRED_MARKER_PATHS)

    metadata = verified.to_metadata()
    assert HostSetupConfig(source_checkout=metadata).source_checkout == metadata
    assert (
        verified_source_from_metadata(metadata, clock=lambda: _FIXED_NOW).root == checkout.resolve()
    )


@pytest.mark.unit
def test_invalid_source_checkout_reports_missing_marker_details(tmp_path: Path) -> None:
    checkout = _write_valid_source_checkout(tmp_path / "checkout")
    (checkout / "RELEASING.md").unlink()

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(checkout, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.root == checkout.resolve()
    assert error.missing_markers == ("RELEASING.md",)
    assert error.to_dict()["missing_markers"] == ["RELEASING.md"]
    assert "details" not in error.to_dict()


@pytest.mark.unit
def test_source_checkout_requires_workspace_compose_template(tmp_path: Path) -> None:
    checkout = _write_valid_source_checkout(tmp_path / "checkout")
    template = checkout / "docker/compose/workspace.base.yml.j2"
    if template.exists():
        template.unlink()

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(checkout, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.missing_markers == ("docker/compose/workspace.base.yml.j2",)
    assert error.to_dict()["missing_markers"] == ["docker/compose/workspace.base.yml.j2"]
    assert "details" not in error.to_dict()


@pytest.mark.parametrize(
    ("missing_marker", "kind"),
    (
        ("uv.lock", "file"),
        ("alembic.ini", "file"),
        (".env.example", "file"),
        ("openapi.json", "file"),
        ("migrations", "dir"),
    ),
)
@pytest.mark.unit
def test_source_checkout_requires_control_plane_docker_build_inputs(
    tmp_path: Path,
    missing_marker: str,
    kind: str,
) -> None:
    checkout = _write_valid_source_checkout(tmp_path / "checkout")
    marker_path = checkout / missing_marker
    if marker_path.exists():
        if kind == "dir":
            marker_path.rmdir()
        else:
            marker_path.unlink()

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(checkout, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.missing_markers == (missing_marker,)
    assert error.to_dict()["missing_markers"] == [missing_marker]
    assert "details" not in error.to_dict()


@pytest.mark.unit
def test_source_checkout_marker_probe_oserror_reports_unreadable_not_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = _write_valid_source_checkout(tmp_path / "checkout")
    releasing_path = (checkout / "RELEASING.md").resolve()
    original_is_file = Path.is_file

    def _is_file(self: Path) -> bool:
        if self.resolve() == releasing_path:
            raise OSError("permission denied")
        return original_is_file(self)

    monkeypatch.setattr(Path, "is_file", _is_file)

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(checkout, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.missing_markers == ()
    assert error.details["unreadable_paths"] == [str(releasing_path)]
    assert "missing_markers" not in error.to_dict()


@pytest.mark.unit
def test_source_checkout_unreadable_marker_reports_unreadable_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = _write_valid_source_checkout(tmp_path / "checkout")
    unreadable_marker = (checkout / "RELEASING.md").resolve()

    def _path_readable(path: Path) -> bool:
        return path.resolve() != unreadable_marker

    monkeypatch.setattr(source_assets, "_path_readable", _path_readable)

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(checkout, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.missing_markers == ()
    assert error.details == {"unreadable_paths": [str(unreadable_marker)]}


@pytest.mark.unit
def test_source_checkout_resolve_failure_uses_expanded_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    candidate = Path("~/missing-awf-source-checkout")
    expanded = home / "missing-awf-source-checkout"
    original_resolve = Path.resolve

    def _resolve_unavailable(self: Path, *args: object, **kwargs: object) -> Path:
        if self == expanded:
            raise OSError("permission denied")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve_unavailable)

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(candidate, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.root == expanded.absolute()
    assert error.details == {"path_status": "missing"}


@pytest.mark.unit
def test_source_checkout_expanduser_failure_remains_reason_coded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = Path("~/missing-awf-source-checkout")

    def _expanduser_unavailable(self: Path) -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "expanduser", _expanduser_unavailable)

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(candidate, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.root == candidate.absolute()
    assert error.details == {"path_status": "missing"}


@pytest.mark.unit
def test_source_checkout_root_is_dir_oserror_is_reason_coded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    resolved = checkout.resolve()
    original_is_dir = Path.is_dir

    def _is_dir(self: Path) -> bool:
        if self == resolved:
            raise OSError("permission denied")
        return original_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", _is_dir)

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(checkout, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.root == resolved
    assert error.details == {"path_status": "unreadable", "error_type": "OSError"}


@pytest.mark.unit
def test_source_checkout_root_exists_oserror_reports_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout_file = tmp_path / "checkout-file"
    checkout_file.write_text("not a directory\n", encoding="utf-8")
    resolved = checkout_file.resolve()
    original_exists = Path.exists

    def _exists(self: Path) -> bool:
        if self == resolved:
            raise OSError("permission denied")
        return original_exists(self)

    monkeypatch.setattr(Path, "exists", _exists)

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(checkout_file, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.root == resolved
    assert error.details == {"path_status": "unreadable"}


@pytest.mark.unit
def test_source_checkout_file_root_reports_not_directory(tmp_path: Path) -> None:
    checkout_file = tmp_path / "checkout-file"
    checkout_file.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(checkout_file, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.root == checkout_file.resolve()
    assert error.details == {"path_status": "not_directory"}


@pytest.mark.unit
def test_unreadable_source_checkout_reports_source_checkout_invalid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = _write_valid_source_checkout(tmp_path / "checkout")
    resolved = checkout.resolve()

    def _unreadable_root(path: Path) -> bool:
        return path.resolve() != resolved

    monkeypatch.setattr(source_assets, "_path_readable", _unreadable_root)

    with pytest.raises(SourceCheckoutError) as exc_info:
        validate_source_checkout(checkout, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_INVALID"
    assert error.root == resolved
    assert error.missing_markers == ()
    assert error.details["unreadable_paths"] == [str(resolved)]


@pytest.mark.unit
def test_stale_source_checkout_metadata_fails_without_package_fallback(tmp_path: Path) -> None:
    checkout = _write_valid_source_checkout(tmp_path / "checkout")
    metadata = validate_source_checkout(checkout, clock=lambda: _FIXED_NOW).to_metadata()
    (checkout / "docker/control-plane.Dockerfile").unlink()

    with pytest.raises(SourceCheckoutError) as exc_info:
        verified_source_from_metadata(metadata, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_ASSETS_STALE"
    assert error.root == checkout.resolve()
    assert error.missing_markers == ("docker/control-plane.Dockerfile",)


@pytest.mark.unit
def test_stale_source_checkout_metadata_reports_contract_drift(tmp_path: Path) -> None:
    checkout = _write_valid_source_checkout(tmp_path / "checkout")
    metadata = validate_source_checkout(checkout, clock=lambda: _FIXED_NOW).to_metadata()
    stale_metadata = metadata.model_copy(
        update={
            "markers": ("legacy-marker",),
            "asset_paths": source_assets.SourceCheckoutAssetPaths(
                compose_file="docker/compose/legacy.yml"
            ),
        }
    )

    with pytest.raises(SourceCheckoutError) as exc_info:
        verified_source_from_metadata(stale_metadata, clock=lambda: _FIXED_NOW)

    error = exc_info.value
    assert error.reason_code == "SOURCE_CHECKOUT_ASSETS_STALE"
    assert error.root == checkout.resolve()
    assert error.details["fallback_used"] is False
    assert error.details["expected_markers"] == list(SOURCE_CHECKOUT_REQUIRED_MARKER_PATHS)
    assert error.details["recorded_markers"] == ["legacy-marker"]
    assert error.details["expected_asset_paths"] == (
        source_assets.SourceCheckoutAssetPaths().model_dump(mode="json")
    )
    assert error.details["recorded_asset_paths"] == (
        stale_metadata.asset_paths.model_dump(mode="json")
    )


@pytest.mark.unit
def test_path_readable_handles_missing_non_posix_and_oserror(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing"
    readable_path = tmp_path / "readable.txt"
    unreadable_path = tmp_path / "unreadable.txt"
    readable_path.write_text("ok\n", encoding="utf-8")
    unreadable_path.write_text("ok\n", encoding="utf-8")

    assert source_assets._path_readable(missing_path) is False

    monkeypatch.setattr(source_assets.os, "name", "nt")
    assert source_assets._path_readable(readable_path) is True

    original_exists = Path.exists

    def _exists(self: Path) -> bool:
        if self == unreadable_path:
            raise OSError("permission denied")
        return original_exists(self)

    monkeypatch.setattr(Path, "exists", _exists)
    assert source_assets._path_readable(unreadable_path) is False


@pytest.mark.unit
def test_now_utc_returns_timezone_aware_timestamp() -> None:
    assert source_assets._now_utc().tzinfo is UTC


@pytest.mark.unit
def test_source_checkout_metadata_stale_detection_ignores_baseline_detail_count(
    tmp_path: Path,
) -> None:
    checkout = _write_valid_source_checkout(tmp_path / "checkout")
    metadata = validate_source_checkout(checkout, clock=lambda: _FIXED_NOW).to_metadata()

    is_stale, details = source_assets._source_checkout_contract_staleness(
        metadata,
        baseline_details={
            "fallback_used": False,
            "revalidated_at": _FIXED_NOW.isoformat(),
        },
    )

    assert is_stale is False
    assert details == {
        "fallback_used": False,
        "revalidated_at": _FIXED_NOW.isoformat(),
    }
    assert (
        verified_source_from_metadata(metadata, clock=lambda: _FIXED_NOW).root == checkout.resolve()
    )
