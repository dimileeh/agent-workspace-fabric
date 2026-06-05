"""Setup-status source-checkout MCP tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from awf.host_setup.config import (
    HOST_SETUP_CONFIG_CORRUPT,
    ClientIntegrationConfig,
    ConsentConfig,
    HostSetupConfig,
    HostSetupConfigError,
    ProviderConfig,
)
from awf.host_setup.rendering import (
    SETUP_READINESS_FAILED,
    first_run_failure_payload,
    first_run_success_payload,
)
from awf.host_setup.source_assets import SourceCheckoutAssetMetadata
from awf.mcp.server import build_mcp_server
from tests.unit.mcp.setup_tools_test_helpers import _payload, _settings


@pytest.mark.unit
async def test_get_setup_status_source_checkout_reads_host_config_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    checkout = tmp_path / "source checkout"
    verified_at = datetime(2026, 2, 3, tzinfo=UTC).isoformat()
    readiness = first_run_success_payload(
        command="awf setup",
        summary="source checkout ready",
        details={
            "selected_providers": ["github"],
            "checks": [{"name": "docker", "level": "ok"}],
            "source_checkout": {"root": str(checkout), "verified_at": verified_at},
        },
        next_steps=("Run awf start: start local AWF Core.",),
    )
    config = HostSetupConfig(
        providers={
            "github": ProviderConfig(
                credential_ref="env://GITHUB_TOKEN",
                backend="env_ref",
                source="env",
                status="ready",
            )
        },
        clients={
            "claude": ClientIntegrationConfig(
                status="configured",
                updated_at=datetime(2026, 2, 4, tzinfo=UTC),
            )
        },
        consent=ConsentConfig(plain_file_secrets=True, source_checkout_assets=True),
        source_checkout=SourceCheckoutAssetMetadata(
            root=tmp_path / "stored-awf",
            verified_at=datetime(2026, 2, 5, tzinfo=UTC),
            markers=("pyproject.toml",),
        ),
    )
    run_calls: list[dict[str, Any]] = []
    read_calls: list[bool] = []

    def fake_run_setup(**kwargs: Any) -> Any:
        run_calls.append(kwargs)
        return readiness

    def fake_read_config() -> HostSetupConfig:
        read_calls.append(True)
        return config

    monkeypatch.setattr(setup_tools, "_run_setup", fake_run_setup)
    monkeypatch.setattr(setup_tools, "read_host_setup_config", fake_read_config)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_setup_status",
        {"providers": ["github"], "source_checkout": str(checkout)},
    )
    payload = _payload(result)
    expected_setup_command = f"awf setup --dry-run --provider github --source-checkout '{checkout}'"
    expected_start_command = f"awf start --source-checkout '{checkout}'"

    assert result.isError is False
    assert read_calls == [True]
    assert run_calls == [
        {
            "providers": ["github"],
            "dry_run": True,
            "non_interactive": True,
            "allow_plain_secrets": False,
            "source_checkout": checkout,
        }
    ]
    assert payload["status"] == "success"
    assert payload["command"] == expected_setup_command
    assert payload["next_steps"] == [
        f"Run {expected_start_command}: start local AWF Core.",
    ]
    assert payload["setup"]["plain_file_consent"] is True
    assert payload["setup"]["source_checkout_assets_consent"] is True
    assert payload["providers"]["github"] == {
        "status": "ready",
        "backend": "env_ref",
        "source": "env",
        "credential_ref": {"present": True, "scheme": "env"},
    }
    assert payload["clients"]["claude"] == {
        "status": "configured",
        "updated_at": "2026-02-04T00:00:00+00:00",
    }
    assert payload["source_checkout"] == {
        "present": True,
        "root": str(checkout),
        "verified_at": verified_at,
        "marker_count": None,
    }


@pytest.mark.unit
async def test_get_setup_status_source_checkout_preserves_host_config_when_config_path_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    checkout = tmp_path / "source checkout"
    verified_at = datetime(2026, 2, 3, tzinfo=UTC).isoformat()
    readiness = first_run_success_payload(
        command="awf setup",
        summary="source checkout ready",
        details={
            "selected_providers": ["github"],
            "checks": [{"name": "docker", "level": "ok"}],
            "source_checkout": {"root": str(checkout), "verified_at": verified_at},
        },
        next_steps=("Run awf start.",),
    )
    config = HostSetupConfig(
        providers={
            "github": ProviderConfig(
                credential_ref="env://GITHUB_TOKEN",
                backend="env_ref",
                source="env",
                status="ready",
            )
        },
        clients={
            "codex": ClientIntegrationConfig(
                status="configured",
                updated_at=datetime(2026, 2, 4, tzinfo=UTC),
            )
        },
        consent=ConsentConfig(plain_file_secrets=True, source_checkout_assets=True),
    )

    def fail_config_path() -> Path:
        raise HostSetupConfigError(
            reason_code=HOST_SETUP_CONFIG_CORRUPT,
            message="Host setup config path could not be resolved.",
            path=Path("~/.awf/config.yml"),
            details={"error_type": "RuntimeError"},
        )

    monkeypatch.setattr(setup_tools, "_run_setup", lambda **_kwargs: readiness)
    monkeypatch.setattr(setup_tools, "read_host_setup_config", lambda: config)
    monkeypatch.setattr(setup_tools, "default_host_setup_config_path", fail_config_path)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_setup_status",
        {"providers": ["github"], "source_checkout": str(checkout)},
    )
    payload = _payload(result)

    assert result.isError is False
    assert "config_path" not in payload["setup"]
    assert payload["setup"]["plain_file_consent"] is True
    assert payload["setup"]["source_checkout_assets_consent"] is True
    assert payload["providers"]["github"] == {
        "status": "ready",
        "backend": "env_ref",
        "source": "env",
        "credential_ref": {"present": True, "scheme": "env"},
    }
    assert payload["clients"]["codex"] == {
        "status": "configured",
        "updated_at": "2026-02-04T00:00:00+00:00",
    }
    assert payload["source_checkout"] == {
        "present": True,
        "root": str(checkout),
        "verified_at": verified_at,
        "marker_count": None,
    }


@pytest.mark.unit
async def test_get_setup_status_source_checkout_next_steps_do_not_duplicate_existing_start_flags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    checkout = tmp_path / "source checkout"
    upstream_checkout = tmp_path / "upstream checkout"
    readiness = first_run_success_payload(
        command="awf setup",
        summary="source checkout ready",
        details={
            "selected_providers": [],
            "checks": [{"name": "docker", "level": "ok"}],
        },
        next_steps=(f"Run awf start --source-checkout '{upstream_checkout}'.",),
    )

    monkeypatch.setattr(setup_tools, "_run_setup", lambda **_kwargs: readiness)
    monkeypatch.setattr(setup_tools, "read_host_setup_config", HostSetupConfig)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_setup_status",
        {"source_checkout": str(checkout)},
    )
    payload = _payload(result)

    assert result.isError is False
    expected_step = f"Run awf start --source-checkout '{checkout}'."
    assert payload["next_steps"] == [expected_step]
    assert payload["next_steps"][0].count("--source-checkout") == 1


@pytest.mark.unit
async def test_get_setup_status_source_checkout_next_steps_do_not_rewrite_inserted_start_command_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    checkout = tmp_path / "awf start to source"
    readiness = first_run_success_payload(
        command="awf setup",
        summary="source checkout ready",
        details={
            "selected_providers": [],
            "checks": [{"name": "docker", "level": "ok"}],
        },
        next_steps=("Run awf start --source-checkout '/old/checkout' to start local AWF Core.",),
    )

    monkeypatch.setattr(setup_tools, "_run_setup", lambda **_kwargs: readiness)
    monkeypatch.setattr(setup_tools, "read_host_setup_config", HostSetupConfig)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_setup_status",
        {"source_checkout": str(checkout)},
    )
    payload = _payload(result)
    expected_start_command = f"awf start --source-checkout '{checkout}'"

    assert result.isError is False
    assert payload["next_steps"] == [
        f"Run {expected_start_command} to start local AWF Core.",
    ]
    assert payload["next_steps"][0].count("--source-checkout") == 1


@pytest.mark.unit
async def test_get_setup_status_source_checkout_blocked_next_steps_preserve_explicit_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    checkout = tmp_path / "source checkout"
    readiness = first_run_failure_payload(
        command="awf setup",
        reason_code=SETUP_READINESS_FAILED,
        status="blocked",
        summary="source checkout blocked",
        details={"check": "docker"},
        next_steps=(
            "Fix the reported blockers above, then re-run awf setup --dry-run: inspect again.",
        ),
    )

    monkeypatch.setattr(setup_tools, "_run_setup", lambda **_kwargs: readiness)
    monkeypatch.setattr(setup_tools, "read_host_setup_config", HostSetupConfig)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_setup_status",
        {"source_checkout": str(checkout)},
    )
    payload = _payload(result)
    expected_setup_command = f"awf setup --dry-run --source-checkout '{checkout}'"

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["command"] == expected_setup_command
    assert payload["next_steps"] == [
        f"Fix the reported blockers above, then re-run {expected_setup_command}: inspect again.",
    ]


@pytest.mark.unit
async def test_get_setup_status_source_checkout_falls_back_when_host_config_read_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    checkout = tmp_path / "awf"
    verified_at = datetime(2026, 2, 3, tzinfo=UTC).isoformat()
    readiness = first_run_success_payload(
        command="awf setup",
        summary="source checkout ready",
        details={
            "selected_providers": [],
            "checks": [{"name": "docker", "level": "ok"}],
            "source_checkout": {"root": str(checkout), "verified_at": verified_at},
        },
        next_steps=("Run awf start.",),
    )
    run_calls: list[dict[str, Any]] = []
    read_calls: list[bool] = []

    def fake_run_setup(**kwargs: Any) -> Any:
        run_calls.append(kwargs)
        return readiness

    def fail_read_config() -> HostSetupConfig:
        read_calls.append(True)
        raise HostSetupConfigError(
            reason_code=HOST_SETUP_CONFIG_CORRUPT,
            message="Host setup config is corrupt or unsupported.",
            path=tmp_path / ".awf" / "config.yml",
            details={"error_type": "ParserError"},
        )

    monkeypatch.setattr(setup_tools, "_run_setup", fake_run_setup)
    monkeypatch.setattr(setup_tools, "read_host_setup_config", fail_read_config)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_setup_status",
        {"source_checkout": str(checkout)},
    )
    payload = _payload(result)

    assert result.isError is False
    assert read_calls == [True]
    assert run_calls == [
        {
            "providers": [],
            "dry_run": True,
            "non_interactive": True,
            "allow_plain_secrets": False,
            "source_checkout": checkout,
        }
    ]
    assert payload["status"] == "success"
    assert payload["setup"]["plain_file_consent"] is False
    assert payload["setup"]["source_checkout_assets_consent"] is False
    assert payload["providers"] == {}
    assert payload["clients"] == {}
    assert payload["source_checkout"] == {
        "present": True,
        "root": str(checkout),
        "verified_at": verified_at,
        "marker_count": None,
    }


@pytest.mark.unit
async def test_get_setup_status_source_checkout_omits_unresolvable_config_path_on_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    checkout = tmp_path / "awf"
    verified_at = datetime(2026, 2, 3, tzinfo=UTC).isoformat()
    readiness = first_run_success_payload(
        command="awf setup",
        summary="source checkout ready",
        details={
            "selected_providers": [],
            "checks": [{"name": "docker", "level": "ok"}],
            "source_checkout": {"root": str(checkout), "verified_at": verified_at},
        },
        next_steps=("Run awf start.",),
    )

    def fail_config_path() -> Path:
        raise HostSetupConfigError(
            reason_code=HOST_SETUP_CONFIG_CORRUPT,
            message="Host setup config path could not be resolved.",
            path=Path("~/.awf/config.yml"),
            details={"error_type": "RuntimeError"},
        )

    monkeypatch.setattr(setup_tools, "_run_setup", lambda **_kwargs: readiness)
    monkeypatch.setattr(setup_tools, "read_host_setup_config", fail_config_path)
    monkeypatch.setattr(setup_tools, "default_host_setup_config_path", fail_config_path)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_setup_status",
        {"source_checkout": str(checkout)},
    )
    payload = _payload(result)

    assert result.isError is False
    assert payload["status"] == "success"
    assert "config_path" not in payload["setup"]
    assert payload["source_checkout"] == {
        "present": True,
        "root": str(checkout),
        "verified_at": verified_at,
        "marker_count": None,
    }


@pytest.mark.unit
async def test_get_setup_status_source_checkout_expanduser_failure_uses_guarded_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    source_checkout = "source checkout"
    expected_source_path = tmp_path / source_checkout
    readiness = first_run_success_payload(
        command="awf setup",
        summary="source checkout ready",
        details={"selected_providers": [], "checks": []},
        next_steps=("Run awf start.",),
    )
    run_calls: list[dict[str, Any]] = []

    def fake_run_setup(**kwargs: Any) -> Any:
        run_calls.append(kwargs)
        return readiness

    original_expanduser = setup_tools.Path.expanduser

    def fail_expanduser(_path: Path) -> Path:
        if str(_path) != source_checkout:
            return original_expanduser(_path)
        raise RuntimeError("home directory unavailable")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setup_tools, "_run_setup", fake_run_setup)
    monkeypatch.setattr(setup_tools, "read_host_setup_config", HostSetupConfig)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))
    monkeypatch.setattr(setup_tools.Path, "expanduser", fail_expanduser)

    result = await mcp.call_tool(
        "awf_get_setup_status",
        {"source_checkout": source_checkout},
    )

    assert result.isError is False
    assert run_calls == [
        {
            "providers": [],
            "dry_run": True,
            "non_interactive": True,
            "allow_plain_secrets": False,
            "source_checkout": expected_source_path,
        }
    ]


@pytest.mark.unit
async def test_get_setup_status_source_checkout_value_error_uses_guarded_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    source_checkout = "bad\0path"
    expected_source_path = tmp_path / source_checkout
    readiness = first_run_success_payload(
        command="awf setup",
        summary="source checkout ready",
        details={"selected_providers": [], "checks": []},
        next_steps=("Run awf start.",),
    )
    run_calls: list[dict[str, Any]] = []

    def fake_run_setup(**kwargs: Any) -> Any:
        run_calls.append(kwargs)
        return readiness

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setup_tools, "_run_setup", fake_run_setup)
    monkeypatch.setattr(setup_tools, "read_host_setup_config", HostSetupConfig)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_setup_status",
        {"source_checkout": source_checkout},
    )

    assert result.isError is False
    assert run_calls == [
        {
            "providers": [],
            "dry_run": True,
            "non_interactive": True,
            "allow_plain_secrets": False,
            "source_checkout": expected_source_path,
        }
    ]
