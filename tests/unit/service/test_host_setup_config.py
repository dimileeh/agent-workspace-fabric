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
from awf.host_setup.source_assets import SOURCE_CHECKOUT_REQUIRED_MARKER_PATHS

_FIXED_NOW = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)


def _write_valid_source_checkout(root: Path) -> Path:
    for marker in SOURCE_CHECKOUT_REQUIRED_MARKER_PATHS:
        path = root / marker
        if marker == "docs":
            path.mkdir(parents=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{marker}\n", encoding="utf-8")
    return root


@pytest.mark.unit
def test_host_setup_config_round_trips_with_conservative_permissions(tmp_path: Path) -> None:
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
    config_path = default_host_setup_config_path(home=tmp_path / "home")

    write_host_setup_config(config, path=config_path)

    if os.name == "posix":
        assert stat.S_IMODE(config_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert read_host_setup_config(path=config_path) == config

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert raw["providers"]["github"]["credential_ref"] == "keyring://awf/github/token"
    assert "token:" not in config_path.read_text(encoding="utf-8")


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
    assert error.reason_code == "HOST_SETUP_CONFIG_CORRUPT"
    assert error.path == config_path
    assert error.details == {"error_type": "FileExistsError"}
    assert "not a directory" not in str(error.to_dict())


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
