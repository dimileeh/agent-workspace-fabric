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
from awf.host_setup import (
    ApiConfig,
    ClientIntegrationConfig,
    ConsentConfig,
    HostSetupConfig,
    HostSetupConfigError,
    InstallConfig,
    ProviderConfig,
    default_host_setup_config_path,
    read_host_setup_config,
    validate_source_checkout,
    write_host_setup_config,
)
from awf.host_setup.config import _ensure_no_secret_payload, _SecretPayloadError
from awf.host_setup.source_assets import SOURCE_CHECKOUT_MARKERS

_FIXED_NOW = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
_FAKE_OPENAI_SECRET_VALUE = "sk-proj-" + ("a" * 48)
_FAKE_OPENAI_SECRET_VALUE_UPPER = "SK-proj-" + ("A" * 48)


def _write_valid_source_checkout(root: Path) -> Path:
    """Create a checkout tree containing every required source marker."""
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
    """Verify host setup config round-trips with restrictive permissions."""
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
    """Verify explicit config parents keep their existing permissions."""
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
    """Verify explicit `.awf` parents keep their existing permissions."""
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
    """Verify symlinked explicit paths do not harden the parent directory."""
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
    """Verify the default config parent is secured even when passed explicitly."""
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
    """Verify repeated config writes use distinct atomic temp paths."""
    config_path = default_host_setup_config_path(home=tmp_path / "home")
    opened_paths: list[Path] = []
    original_open = os.open

    def _record_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
    ) -> int:
        """Record each opened temp path before delegating to `os.open`."""
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
    """Verify parent creation failures use the write-failed reason code."""
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
    """Verify low-level persistence failures use the write-failed reason code."""
    config_path = default_host_setup_config_path(home=tmp_path / "home")

    def _open_fails(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
    ) -> int:
        """Raise an `OSError` to simulate a failed atomic write."""
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
@pytest.mark.parametrize(
    ("config_path", "expected_path"),
    [(Path(), Path()), ("", Path()), (Path("/"), Path("/"))],
)
def test_host_setup_config_write_filename_less_paths_are_reason_coded(
    config_path: str | Path,
    expected_path: Path,
) -> None:
    """Verify filename-less config paths fail with structured details."""
    with pytest.raises(HostSetupConfigError) as exc_info:
        write_host_setup_config(HostSetupConfig(), path=config_path)

    error = exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_WRITE_FAILED"
    assert error.path == expected_path
    assert error.details == {"error_type": "ValueError"}
    assert "has an empty name" not in str(error.to_dict())


@pytest.mark.unit
@pytest.mark.parametrize("operation", ["read", "write"])
def test_host_setup_config_path_resolution_failure_for_default_home_is_reason_coded(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """Verify default-home path resolution failures are reason-coded."""

    def _home_unavailable() -> Path:
        """Raise the same home lookup error produced by pathlib."""
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
    """Verify explicit path expansion failures are reason-coded."""
    config_path = Path("~/awf-config.yml")

    def _expanduser_unavailable(self: Path) -> Path:
        """Raise the same expanduser error produced by pathlib."""
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
    """Verify explicit home expansion failures are reason-coded."""
    home = Path("~/awf-home")

    def _expanduser_unavailable(self: Path) -> Path:
        """Raise the same expanduser error produced by pathlib."""
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
    """Verify raw secret-looking values are rejected and redacted."""
    with pytest.raises(ValidationError):
        ProviderConfig(credential_ref="ghp_abcdefghijklmnopqrstuvwxyz123456")
    with pytest.raises(ValidationError):
        HostSetupConfig.model_validate(
            {"providers": {"openai": {"credential_ref": _FAKE_OPENAI_SECRET_VALUE}}}
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
def test_host_setup_config_allows_non_secret_sk_prefixed_status_and_channel(
    tmp_path: Path,
) -> None:
    """Verify non-secret status/channel values may start with `sk`."""
    config = HostSetupConfig(
        install=InstallConfig(channel="sk-beta"),
        providers={"openai": ProviderConfig(status="sk-active")},
        clients={"codex": ClientIntegrationConfig(status="sk-configured")},
    )
    config_path = default_host_setup_config_path(home=tmp_path / "home")

    write_host_setup_config(config, path=config_path)

    loaded = read_host_setup_config(path=config_path)
    assert loaded.install.channel == "sk-beta"
    assert loaded.providers["openai"].status == "sk-active"
    assert loaded.clients["codex"].status == "sk-configured"


@pytest.mark.unit
def test_host_setup_config_rejects_duplicate_yaml_keys_before_secret_scan(
    tmp_path: Path,
) -> None:
    """Verify duplicate YAML keys are classified before secret scanning."""
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
def test_duplicate_key_loader_constructs_each_key_node_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify duplicate-key detection constructs each key only once."""
    loader = host_setup_config._DuplicateKeyRejectingSafeLoader("version: 1\n")
    node = loader.get_single_node()
    assert node is not None
    key_node, value_node = node.value[0]
    constructed_nodes: list[object] = []

    def _construct_once(_loader: object, yaml_node: object, *, deep: bool) -> object:
        """Construct only the known key/value nodes for this loader test."""
        del deep
        constructed_nodes.append(yaml_node)
        if yaml_node is key_node:
            return "version"
        if yaml_node is value_node:
            return 1
        raise AssertionError("unexpected YAML node")

    monkeypatch.setattr(host_setup_config, "_construct_yaml_object", _construct_once)

    assert host_setup_config._construct_mapping_without_duplicate_keys(loader, node) == {
        "version": 1
    }
    assert sum(1 for constructed in constructed_nodes if constructed is key_node) == 1


@pytest.mark.unit
def test_duplicate_key_loader_rejects_non_mapping_nodes() -> None:
    """Verify the mapping constructor rejects non-mapping YAML nodes."""
    loader = host_setup_config._DuplicateKeyRejectingSafeLoader("version\n")
    node = loader.get_single_node()
    assert node is not None

    with pytest.raises(yaml.constructor.ConstructorError, match="expected a mapping node"):
        host_setup_config._construct_mapping_without_duplicate_keys(loader, node)


@pytest.mark.unit
def test_duplicate_key_loader_rejects_unhashable_constructed_keys() -> None:
    """Verify YAML keys that construct to lists are rejected safely."""
    with pytest.raises(yaml.constructor.ConstructorError, match="found unhashable key"):
        yaml.load(
            "? [github, openai]\n: configured\n",
            Loader=host_setup_config._DuplicateKeyRejectingSafeLoader,
        )


@pytest.mark.unit
def test_host_setup_work_dir_documents_expanduser_contract() -> None:
    """Verify the work-dir field documents `expanduser` handling."""
    description = HostSetupConfig.model_fields["work_dir"].description

    assert description is not None
    assert "expanduser" in description
    assert "~" in description


@pytest.mark.unit
def test_host_setup_config_reads_missing_and_empty_yaml_as_defaults(tmp_path: Path) -> None:
    """Verify missing and empty config files load default settings."""
    config_path = default_host_setup_config_path(home=tmp_path / "home")

    assert read_host_setup_config(path=config_path) == HostSetupConfig()

    config_path.parent.mkdir(parents=True)
    config_path.write_text("", encoding="utf-8")

    assert read_host_setup_config(path=config_path) == HostSetupConfig()


@pytest.mark.unit
def test_host_setup_config_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    """Verify non-mapping YAML files are reported as corrupt config."""
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
    """Verify provider refs allow omission and reject unsafe literals."""
    assert ProviderConfig(credential_ref=None).credential_ref is None

    with pytest.raises(ValidationError) as exc_info:
        ProviderConfig(credential_ref="literal-token-file")

    assert "literal-token-file" not in str(exc_info.value)


@pytest.mark.unit
def test_host_setup_config_read_wraps_unsupported_version(tmp_path: Path) -> None:
    """Verify unsupported config versions are wrapped as corrupt config."""
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
    """Verify read-time validation errors preserve secret-value classification."""
    config_path = default_host_setup_config_path(home=tmp_path / "home")
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "version: 1\nproviders:\n  github:\n    credential_ref: ghp_raw_secret\n",
        encoding="utf-8",
    )

    def _allow_secret_payload(value: object) -> None:
        """Bypass pre-validation scanning so model validation owns the failure."""
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
    """Verify secret-like provider names are rejected and redacted."""
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
def test_host_setup_config_write_revalidates_model_copy_updates(tmp_path: Path) -> None:
    """Verify copied model updates are revalidated before writing."""
    config_path = default_host_setup_config_path(home=tmp_path / "home")
    copied_config = HostSetupConfig().model_copy(
        update={"providers": {"github": {"credential_ref": "literal-token-file"}}}
    )

    with pytest.raises(HostSetupConfigError) as exc_info:
        write_host_setup_config(copied_config, path=config_path)

    error = exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_CORRUPT"
    assert error.path == config_path
    assert error.details == {
        "error_count": 1,
        "error_types": ["value_error"],
        "locations": ["providers.github.credential_ref"],
    }
    assert "literal-token-file" not in str(error.to_dict())
    assert not config_path.exists()


@pytest.mark.unit
def test_host_setup_config_write_wraps_validation_secret_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify write-time validation classifies raw credential refs as secrets."""
    config_path = default_host_setup_config_path(home=tmp_path / "home")
    copied_config = HostSetupConfig().model_copy(
        update={"providers": {"github": {"credential_ref": "ghp_raw_secret"}}}
    )

    def _allow_secret_payload(value: object) -> None:
        """Bypass pre-validation scanning so model validation owns the failure."""
        del value

    monkeypatch.setattr(host_setup_config, "_ensure_no_secret_payload", _allow_secret_payload)

    with pytest.raises(HostSetupConfigError) as exc_info:
        write_host_setup_config(copied_config, path=config_path)

    error = exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_SECRET_VALUE"
    assert error.path == config_path
    assert error.details == {
        "error_count": 1,
        "error_types": ["value_error"],
        "locations": ["providers.github.credential_ref"],
    }
    assert "ghp_raw_secret" not in str(error.to_dict())
    assert not config_path.exists()


@pytest.mark.unit
def test_host_setup_config_write_wraps_non_serializable_model_copy_updates(
    tmp_path: Path,
) -> None:
    """Verify non-serializable copied provider updates are wrapped."""
    config_path = default_host_setup_config_path(home=tmp_path / "home")
    copied_config = HostSetupConfig().model_copy(update={"providers": {"github": object()}})

    with pytest.raises(HostSetupConfigError) as exc_info:
        write_host_setup_config(copied_config, path=config_path)

    error = exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_CORRUPT"
    assert error.path == config_path
    assert error.details == {
        "error_count": 1,
        "error_types": ["model_type"],
        "locations": ["providers.github"],
    }
    assert not config_path.exists()


@pytest.mark.unit
@pytest.mark.parametrize("field_name", ["providers", "clients"])
def test_host_setup_config_write_wraps_non_mapping_copied_mapping_fields(
    field_name: str,
    tmp_path: Path,
) -> None:
    """Verify copied mapping fields must remain mappings at write time."""
    config_path = default_host_setup_config_path(home=tmp_path / "home")
    copied_config = HostSetupConfig().model_copy(update={field_name: object()})

    with pytest.raises(HostSetupConfigError) as exc_info:
        write_host_setup_config(copied_config, path=config_path)

    error = exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_CORRUPT"
    assert error.path == config_path
    assert error.details == {
        "error_count": 1,
        "error_types": ["dict_type"],
        "locations": [field_name],
    }
    assert not config_path.exists()


@pytest.mark.unit
def test_host_setup_config_write_wraps_recursive_payload_scan_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify recursive payload scan failures are wrapped as corrupt config."""
    config_path = default_host_setup_config_path(home=tmp_path / "home")
    config = HostSetupConfig()

    def _raise_recursive_payload_error(value: object) -> None:
        """Raise the recursive payload error from the secret scan hook."""
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
    """Verify frozen provider mappings reject in-place mutation."""
    empty_config = HostSetupConfig()

    with pytest.raises(TypeError):
        empty_config.providers["github"] = {"token": "ghp_raw"}

    assert "github" not in empty_config.providers

    configured = HostSetupConfig(
        providers={"github": ProviderConfig(credential_ref="env://GITHUB_TOKEN")}
    )

    with pytest.raises(TypeError):
        configured.providers["openai"] = {"credential_ref": _FAKE_OPENAI_SECRET_VALUE}

    assert set(configured.providers) == {"github"}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw_config", "expected_issue", "expected_path"),
    [
        (
            f"version: 1\naudit:\n  - {_FAKE_OPENAI_SECRET_VALUE}\n",
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
    """Verify list-contained raw secret payloads are rejected and redacted."""
    config_path = default_host_setup_config_path(home=tmp_path / "home")
    config_path.parent.mkdir(parents=True)
    config_path.write_text(raw_config, encoding="utf-8")

    with pytest.raises(HostSetupConfigError) as exc_info:
        read_host_setup_config(path=config_path)

    error = exc_info.value
    assert error.reason_code == "HOST_SETUP_CONFIG_SECRET_VALUE"
    assert error.path == config_path
    assert error.details == {"issue": expected_issue, "path": expected_path}
    assert _FAKE_OPENAI_SECRET_VALUE not in str(error.to_dict())
    assert "ghp_raw_secret" not in str(error.to_dict())


@pytest.mark.unit
def test_host_setup_config_treats_recursive_yaml_alias_as_corrupt(tmp_path: Path) -> None:
    """Verify recursive YAML aliases are classified as corrupt config."""
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
    """Verify tuple-contained secret payloads are rejected."""
    with pytest.raises(_SecretPayloadError) as exc_info:
        _ensure_no_secret_payload({"audit": ({"token": "ghp_raw_secret"},)})

    assert exc_info.value.details() == {
        "issue": "secret-bearing key",
        "path": "audit.[0].token",
    }


@pytest.mark.unit
def test_secret_payload_scan_rejects_sequence_container_secret_payloads() -> None:
    """Verify non-list sequence containers are scanned for secrets."""
    with pytest.raises(_SecretPayloadError) as exc_info:
        _ensure_no_secret_payload({"audit": UserList([_FAKE_OPENAI_SECRET_VALUE])})

    assert exc_info.value.details() == {
        "issue": "secret-like value",
        "path": "audit.[0]",
    }


@pytest.mark.unit
def test_secret_payload_scan_rejects_recursive_mappings() -> None:
    """Verify recursive mappings fail with recursive-alias details."""
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
    """Verify secret scan locations include non-string mapping keys."""
    with pytest.raises(_SecretPayloadError) as exc_info:
        _ensure_no_secret_payload({1: [_FAKE_OPENAI_SECRET_VALUE]})

    assert exc_info.value.details() == {
        "issue": "secret-like value",
        "path": "1.[0]",
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw_secret",
    [
        _FAKE_OPENAI_SECRET_VALUE_UPPER,
        "GHP_raw_secret_value",
        "XOXB-raw-secret-value",
    ],
)
def test_secret_payload_scan_rejects_uppercase_token_prefixes(raw_secret: str) -> None:
    """Verify uppercase token-like prefixes are still treated as secrets."""
    with pytest.raises(_SecretPayloadError) as exc_info:
        _ensure_no_secret_payload({"audit": [raw_secret]})

    assert exc_info.value.details() == {
        "issue": "secret-like value",
        "path": "audit.[0]",
    }
    assert raw_secret not in str(exc_info.value.details())


@pytest.mark.unit
def test_corrupt_config_has_reason_code_and_path_details(tmp_path: Path) -> None:
    """Verify malformed YAML errors expose reason code and path details."""
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
    """Verify unreadable config existence checks are reason-coded."""
    config_path = default_host_setup_config_path(home=tmp_path / "home")
    original_exists = Path.exists

    def _permission_denied_for_config(self: Path) -> bool:
        """Raise permission denial only for the target config path."""
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
    """Verify host setup errors omit empty details from dictionaries."""
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
def test_config_corrupt_error_accepts_default_details(tmp_path: Path) -> None:
    """Verify corrupt config helper works without explicit details."""
    config_path = tmp_path / "config.yml"

    error = host_setup_config._config_corrupt_error(config_path)

    assert error.to_dict() == {
        "status": "failed",
        "reason_code": "HOST_SETUP_CONFIG_CORRUPT",
        "message": "Host setup config is corrupt or unsupported.",
        "path": str(config_path),
    }


@pytest.mark.unit
def test_config_path_helpers_handle_resolution_and_platform_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify config path helpers handle fallback branches."""
    config_path = tmp_path / "config.yml"

    def _default_path_unavailable() -> Path:
        """Raise a default-path resolution error for helper fallback coverage."""
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
        """Raise during absolute path resolution for fallback coverage."""
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "absolute", _absolute_unavailable)
    assert host_setup_config._normalized_config_path(config_path) == config_path

    monkeypatch.setattr(host_setup_config.os, "name", "nt")
    host_setup_config._chmod_best_effort(tmp_path / "missing", 0o600)


@pytest.mark.unit
def test_validation_location_formatter_handles_non_tuple_and_root_locations() -> None:
    """Verify validation locations format scalars, tuples, and root paths."""
    assert host_setup_config._format_validation_location("version") == "version"
    assert (
        host_setup_config._format_validation_location(("providers", "github", "credential_ref"))
        == "providers.github.credential_ref"
    )
    assert host_setup_config._format_validation_location(()) == "<root>"
