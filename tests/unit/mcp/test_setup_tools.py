"""Focused tests for first-run setup/start/init/client MCP tools."""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from mcp.types import CallToolResult

from awf.common.audit import REDACTION_MARKER
from awf.common.config import Settings
from awf.host_setup.config import (
    HOST_SETUP_CONFIG_CORRUPT,
    ClientIntegrationConfig,
    ConsentConfig,
    HostSetupConfig,
    HostSetupConfigError,
    ProviderConfig,
)
from awf.host_setup.rendering import (
    CLIENT_CONFIG_CONFLICT,
    SETUP_CLIENT_UNKNOWN,
    SETUP_READINESS_FAILED,
    START_HEALTH_TIMEOUT,
    first_run_failure_payload,
    first_run_success_payload,
)
from awf.host_setup.source_assets import SOURCE_CHECKOUT_ASSETS_STALE, SourceCheckoutAssetMetadata
from awf.host_setup.system_checks import SetupCheckError
from awf.mcp.server import build_mcp_server
from awf.service.bootstrap import (
    SERVICE_BOOTSTRAP_TIMEOUT,
    ServiceBootstrapError,
    ServiceBootstrapResult,
)

SETUP_TOOL_NAMES = {
    "awf_get_setup_status",
    "awf_start_local_service",
    "awf_initialize_project_profile",
    "awf_get_client_integration_instructions",
}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        AWF_DATABASE_URL="sqlite+aiosqlite:///unused.db",
        AWF_WORK_DIR=str(tmp_path / "work"),
        AWF_API_TOKEN="test-token-for-mcp-setup-tools",
    )


def _payload(result: CallToolResult) -> dict[str, Any]:
    assert result.structuredContent is not None
    assert isinstance(result.structuredContent, dict)
    return result.structuredContent


def _json_text(result: CallToolResult) -> str:
    return json.dumps(
        {
            "structured": result.structuredContent,
            "content": [getattr(item, "text", "") for item in result.content],
        },
        sort_keys=True,
        default=str,
    )


@pytest.mark.unit
async def test_setup_tools_are_registered(tmp_path: Path) -> None:
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    tools = {tool.name: tool for tool in await mcp.list_tools()}

    assert set(tools) >= SETUP_TOOL_NAMES
    for tool_name in SETUP_TOOL_NAMES:
        properties = tools[tool_name].inputSchema.get("properties", {})
        joined_property_names = " ".join(str(name).lower() for name in properties)
        assert "token" not in joined_property_names
        assert "password" not in joined_property_names
        assert "secret" not in joined_property_names
        assert "credential" not in joined_property_names


@pytest.mark.unit
async def test_setup_status_init_and_client_tools_offload_blocking_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    event_loop_thread_id = threading.get_ident()
    helper_thread_ids: dict[str, int] = {}
    readiness = first_run_success_payload(
        command="awf setup",
        summary="ready",
        details={"selected_providers": [], "checks": []},
        next_steps=(),
    )
    project = tmp_path / "repo"
    project.mkdir()
    preview = SimpleNamespace(
        path=project,
        draft=SimpleNamespace(template="generic"),
        to_dict=lambda: {
            "path": str(project),
            "inspection": {"detected_template": "generic"},
            "draft": {"template": "generic", "yaml": "name: generic\n"},
            "diagnostics": {},
        },
    )
    env_file = tmp_path / ".env"
    home = tmp_path / "home"
    home.mkdir()

    def record_helper_thread(name: str) -> None:
        helper_thread_ids[name] = threading.get_ident()

    def fake_run_setup(**_kwargs: Any) -> Any:
        record_helper_thread("setup_status")
        return readiness

    def fake_preview(
        _path: Path,
        *,
        template: str,
        include_smoke_request: bool,
    ) -> Any:
        _ = (template, include_smoke_request)
        record_helper_thread("initialize_project_profile")
        return preview

    def fake_resolve_client_env_file(
        _source_checkout: Path | None,
        _require_existing: bool = False,
    ) -> Path:
        record_helper_thread("client_integration_instructions")
        return env_file

    monkeypatch.setattr(setup_tools, "_run_setup", fake_run_setup)
    monkeypatch.setattr(setup_tools, "read_host_setup_config", HostSetupConfig)
    monkeypatch.setattr(setup_tools, "preview_project_onboarding", fake_preview)
    monkeypatch.setattr(setup_tools, "_resolve_client_env_file", fake_resolve_client_env_file)
    monkeypatch.setattr(setup_tools, "_client_home", lambda: home)
    monkeypatch.setattr(setup_tools, "_client_which", lambda _binary: None)
    monkeypatch.setattr(setup_tools, "_client_now", lambda: datetime(2026, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(setup_tools, "_client_env", lambda: {})
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    setup_status = await mcp.call_tool("awf_get_setup_status", {})
    init_profile = await mcp.call_tool(
        "awf_initialize_project_profile",
        {"project_path": str(project)},
    )
    client_instructions = await mcp.call_tool(
        "awf_get_client_integration_instructions",
        {"clients": ["claude"]},
    )

    assert setup_status.isError is False
    assert init_profile.isError is False
    assert client_instructions.isError is False
    assert set(helper_thread_ids) == {
        "setup_status",
        "initialize_project_profile",
        "client_integration_instructions",
    }
    assert all(thread_id != event_loop_thread_id for thread_id in helper_thread_ids.values())


@pytest.mark.unit
async def test_start_local_service_offloads_sync_preparation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    event_loop_thread_id = threading.get_ident()
    helper_thread_ids: dict[str, int] = {}
    verified = object()
    inputs = SimpleNamespace(
        settings=SimpleNamespace(
            api_base_url="http://localhost:8000",
            console_url="http://localhost:3000",
        ),
        compose_file=tmp_path / "compose.yml",
        compose_env_file=tmp_path / ".env",
        asset_root=tmp_path,
        service_env={"AWF_API_HOST_PORT": "8000"},
    )

    def fake_resolve_start_source_checkout(source_checkout: Path | None) -> object:
        assert source_checkout == tmp_path
        helper_thread_ids["source_checkout"] = threading.get_ident()
        return verified

    def fake_resolve_start_bootstrap_inputs(item: object) -> SimpleNamespace:
        assert item is verified
        helper_thread_ids["bootstrap_inputs"] = threading.get_ident()
        return inputs

    async def fake_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        return ServiceBootstrapResult(stages=(), service_status={"status": "ok", "checks": {}})

    monkeypatch.setattr(
        setup_tools,
        "_resolve_start_source_checkout",
        fake_resolve_start_source_checkout,
    )
    monkeypatch.setattr(
        setup_tools,
        "_resolve_start_bootstrap_inputs",
        fake_resolve_start_bootstrap_inputs,
    )
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fake_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_start_local_service",
        {"source_checkout": str(tmp_path)},
    )

    assert result.isError is False
    assert set(helper_thread_ids) == {"source_checkout", "bootstrap_inputs"}
    assert all(thread_id != event_loop_thread_id for thread_id in helper_thread_ids.values())


@pytest.mark.unit
async def test_start_local_service_source_checkout_expanduser_failure_uses_guarded_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    source_checkout = "source checkout"
    expected_source_path = tmp_path / source_checkout
    verified = object()
    inputs = SimpleNamespace(
        settings=SimpleNamespace(
            api_base_url="http://localhost:8000",
            console_url="http://localhost:3000",
        ),
        compose_file=tmp_path / "compose.yml",
        compose_env_file=tmp_path / ".env",
        asset_root=tmp_path,
        service_env={"AWF_API_HOST_PORT": "8000"},
    )
    source_calls: list[Path | None] = []

    def fake_resolve_start_source_checkout(source_path: Path | None) -> object:
        source_calls.append(source_path)
        return verified

    original_expanduser = setup_tools.Path.expanduser

    def fail_expanduser(_path: Path) -> Path:
        if str(_path) != source_checkout:
            return original_expanduser(_path)
        raise RuntimeError("home directory unavailable")

    async def fake_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        return ServiceBootstrapResult(stages=(), service_status={"status": "ok", "checks": {}})

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        setup_tools,
        "_resolve_start_source_checkout",
        fake_resolve_start_source_checkout,
    )
    monkeypatch.setattr(setup_tools, "_resolve_start_bootstrap_inputs", lambda _item: inputs)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fake_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))
    monkeypatch.setattr(setup_tools.Path, "expanduser", fail_expanduser)

    result = await mcp.call_tool(
        "awf_start_local_service",
        {"source_checkout": source_checkout},
    )

    assert result.isError is False
    assert source_calls == [expected_source_path]


@pytest.mark.unit
async def test_get_setup_status_returns_only_status_and_safe_refs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    raw_token = "sk-proj-" + "a" * 40
    monkeypatch.setenv("OPENAI_API_KEY", raw_token)
    readiness = first_run_success_payload(
        command="awf setup",
        summary="ready",
        details={
            "selected_providers": ["github"],
            "checks": [{"name": "docker", "level": "ok"}],
            "ignored": raw_token,
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
            ),
            "codex": ProviderConfig(status="missing"),
        },
        clients={
            "claude": ClientIntegrationConfig(
                status="configured",
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        },
        consent=ConsentConfig(plain_file_secrets=True, source_checkout_assets=True),
        source_checkout=SourceCheckoutAssetMetadata(
            root=tmp_path / "stored-awf",
            verified_at=datetime(2026, 1, 2, tzinfo=UTC),
            markers=("pyproject.toml", "README.md"),
        ),
    )
    run_calls: list[dict[str, Any]] = []

    def fake_run_setup(**kwargs: Any) -> Any:
        run_calls.append(kwargs)
        return readiness

    monkeypatch.setattr(setup_tools, "_run_setup", fake_run_setup)
    monkeypatch.setattr(setup_tools, "read_host_setup_config", lambda: config)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_get_setup_status", {"providers": ["github"]})
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is False
    assert run_calls == [
        {
            "providers": ["github"],
            "dry_run": True,
            "non_interactive": True,
            "allow_plain_secrets": False,
            "source_checkout": None,
        }
    ]
    assert payload["status"] == "success"
    assert payload["command"] == "awf setup"
    assert payload["next_steps"] == ["Run awf start."]
    assert payload["setup"]["checks"] == [{"name": "docker", "level": "ok"}]
    assert payload["providers"]["github"] == {
        "status": "ready",
        "backend": "env_ref",
        "source": "env",
        "credential_ref": {"present": True, "scheme": "env"},
    }
    assert payload["providers"]["codex"] == {
        "status": "missing",
        "credential_ref": {"present": False},
    }
    assert payload["clients"]["claude"]["status"] == "configured"
    assert payload["source_checkout"] == {
        "present": True,
        "root": str(tmp_path / "stored-awf"),
        "verified_at": "2026-01-02T00:00:00+00:00",
        "marker_count": 2,
    }
    assert raw_token not in rendered
    assert "GITHUB_TOKEN" not in rendered
    assert "env://GITHUB_TOKEN" not in rendered


@pytest.mark.unit
@pytest.mark.parametrize("status", ["blocked", "failed"])
async def test_get_setup_status_marks_blocked_and_failed_readiness_as_mcp_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: str,
) -> None:
    from awf.mcp import setup_tools

    readiness = first_run_failure_payload(
        command="awf setup",
        reason_code=SETUP_READINESS_FAILED,
        summary=f"host readiness is {status}",
        status=status,
        details={"check": "docker"},
        next_steps=("Fix host readiness.",),
    )

    monkeypatch.setattr(setup_tools, "_run_setup", lambda **_kwargs: readiness)
    monkeypatch.setattr(setup_tools, "read_host_setup_config", HostSetupConfig)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_get_setup_status", {})
    payload = _payload(result)

    assert result.isError is True
    assert payload["status"] == status
    assert payload["reason_code"] == SETUP_READINESS_FAILED
    assert payload["issues"] == [
        {"reason_code": SETUP_READINESS_FAILED, "severity": status, "check": "docker"}
    ]


@pytest.mark.unit
async def test_get_setup_status_hides_stale_persisted_source_checkout_when_revalidation_blocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    stale_checkout = tmp_path / "stale-awf"
    readiness = first_run_failure_payload(
        command="awf setup",
        reason_code=SOURCE_CHECKOUT_ASSETS_STALE,
        summary="stored source checkout is stale",
        status="blocked",
        details={
            "check": "source_checkout",
            "root": str(stale_checkout),
            "missing_markers": ["pyproject.toml"],
        },
        next_steps=("Select a valid source checkout.",),
    )
    config = HostSetupConfig(
        source_checkout=SourceCheckoutAssetMetadata(
            root=stale_checkout,
            verified_at=datetime(2026, 1, 2, tzinfo=UTC),
            markers=("pyproject.toml",),
        )
    )

    monkeypatch.setattr(setup_tools, "_run_setup", lambda **_kwargs: readiness)
    monkeypatch.setattr(setup_tools, "read_host_setup_config", lambda: config)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_get_setup_status", {})
    payload = _payload(result)

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["issues"] == [
        {
            "reason_code": SOURCE_CHECKOUT_ASSETS_STALE,
            "severity": "blocked",
            "check": "source_checkout",
        }
    ]
    assert payload["source_checkout"] == {"present": False}


@pytest.mark.unit
async def test_get_setup_status_host_config_error_without_source_checkout_is_structured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    readiness = first_run_success_payload(
        command="awf setup",
        summary="ready",
        details={"selected_providers": [], "checks": []},
        next_steps=("Run awf start.",),
    )

    def fail_read_config() -> HostSetupConfig:
        raise HostSetupConfigError(
            reason_code=HOST_SETUP_CONFIG_CORRUPT,
            message="Host setup config is corrupt or unsupported.",
            path=tmp_path / ".awf" / "config.yml",
            details={"error_type": "ParserError"},
        )

    monkeypatch.setattr(setup_tools, "_run_setup", lambda **_kwargs: readiness)
    monkeypatch.setattr(setup_tools, "read_host_setup_config", fail_read_config)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_get_setup_status", {})
    payload = _payload(result)

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == HOST_SETUP_CONFIG_CORRUPT
    assert payload["issues"][0]["reason_code"] == HOST_SETUP_CONFIG_CORRUPT
    assert payload["issues"][0]["severity"] == "blocked"
    assert payload["issues"][0]["details"] == {
        "error_type": "ParserError",
        "path": str(tmp_path / ".awf" / "config.yml"),
    }


@pytest.mark.unit
async def test_get_setup_status_run_setup_oserror_is_structured_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    raw_token = "sk-proj-" + "f" * 40
    leaked_detail = f"{tmp_path}/docker.sock contains {raw_token}"

    def fail_run_setup(**_kwargs: Any) -> Any:
        raise OSError(leaked_detail)

    monkeypatch.setattr(setup_tools, "_run_setup", fail_run_setup)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_get_setup_status", {})
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == SETUP_READINESS_FAILED
    assert payload["summary"] == "could not inspect local setup readiness"
    assert payload["issues"][0]["details"] == {"error_type": "OSError"}
    assert leaked_detail not in rendered
    assert raw_token not in rendered


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
        next_steps=("Run awf start to start local AWF Core.",),
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
        f"Run {expected_start_command} to start local AWF Core.",
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
        next_steps=("Fix the reported blockers above, then re-run awf setup --dry-run.",),
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
        f"Fix the reported blockers above, then re-run {expected_setup_command}.",
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


@pytest.mark.unit
async def test_start_local_service_reuses_bootstrap_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    verified = object()
    inputs = SimpleNamespace(
        settings=SimpleNamespace(
            api_base_url="http://localhost:8000",
            console_url="http://localhost:3000",
        ),
        compose_file=tmp_path / "compose.yml",
        compose_env_file=tmp_path / ".env",
        asset_root=tmp_path,
        service_env={"AWF_API_HOST_PORT": "8000"},
    )
    calls: list[dict[str, Any]] = []

    async def fake_bootstrap(*args: Any, **kwargs: Any) -> ServiceBootstrapResult:
        calls.append({"args": args, "kwargs": kwargs})
        return ServiceBootstrapResult(stages=(), service_status={"status": "ok", "checks": {}})

    monkeypatch.setattr(
        setup_tools,
        "_resolve_start_source_checkout",
        lambda _source_checkout: verified,
    )
    monkeypatch.setattr(setup_tools, "_resolve_start_bootstrap_inputs", lambda _item: inputs)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fake_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    args = {
        "rebuild": True,
        "skip_agent_runtime_build": False,
        "timeout_seconds": 7.5,
        "source_checkout": str(tmp_path),
    }
    first = _payload(await mcp.call_tool("awf_start_local_service", args))
    second = _payload(await mcp.call_tool("awf_start_local_service", args))

    assert first["status"] == "success"
    assert second["status"] == "success"
    assert len(calls) == 2
    for call in calls:
        options = call["kwargs"]["options"]
        assert options.timeout_seconds == 7.5
        assert options.force_rebuild is True
        assert options.skip_agent_runtime_build is False
        assert call["kwargs"]["compose_file"] == inputs.compose_file
        assert call["kwargs"]["env_file"] == inputs.compose_env_file
        assert call["kwargs"]["asset_root"] == inputs.asset_root
        assert call["kwargs"]["service_environ"] == inputs.service_env


@pytest.mark.unit
async def test_start_local_service_reports_structured_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    raw_token = "sk-proj-" + "b" * 40
    inputs = SimpleNamespace(
        settings=SimpleNamespace(api_base_url="http://localhost:8000", console_url=None),
        compose_file=tmp_path / "compose.yml",
        compose_env_file=None,
        asset_root=None,
        service_env={},
    )

    async def fail_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        raise ServiceBootstrapError(
            reason_code=SERVICE_BOOTSTRAP_TIMEOUT,
            message="timed out",
            stderr=f"provider output {raw_token}",
        )

    monkeypatch.setattr(setup_tools, "_resolve_start_source_checkout", lambda _path: None)
    monkeypatch.setattr(setup_tools, "_resolve_start_bootstrap_inputs", lambda _verified: inputs)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fail_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_start_local_service", {})
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["status"] == "failed"
    assert payload["reason_code"] == START_HEALTH_TIMEOUT
    assert payload["issues"][0]["details"]["bootstrap"]["reason_code"] == SERVICE_BOOTSTRAP_TIMEOUT
    assert raw_token not in rendered
    assert REDACTION_MARKER in rendered


@pytest.mark.unit
async def test_start_local_service_input_resolution_failure_is_structured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    leaked_detail = "/srv/awf/docker/compose/.env missing OPENAI_API_KEY"

    def fail_bootstrap_inputs(_verified: object) -> SimpleNamespace:
        raise ValueError(leaked_detail)

    bootstrap_calls: list[bool] = []

    async def fake_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        bootstrap_calls.append(True)
        return ServiceBootstrapResult(stages=(), service_status={"status": "ok", "checks": {}})

    monkeypatch.setattr(setup_tools, "_resolve_start_source_checkout", lambda _path: object())
    monkeypatch.setattr(setup_tools, "_resolve_start_bootstrap_inputs", fail_bootstrap_inputs)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fake_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_start_local_service", {})
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["error_code"] == "START_INPUT_RESOLUTION_FAILED"
    assert payload["message"] == "could not resolve local service startup inputs"
    assert payload["detail"] == {"error_type": "ValueError"}
    assert bootstrap_calls == []
    assert leaked_detail not in rendered


@pytest.mark.unit
async def test_start_local_service_runtime_input_resolution_failure_is_structured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    leaked_detail = "home directory unavailable while building service environment"

    def fail_bootstrap_inputs(_verified: object) -> SimpleNamespace:
        raise RuntimeError(leaked_detail)

    bootstrap_calls: list[bool] = []

    async def fake_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        bootstrap_calls.append(True)
        return ServiceBootstrapResult(stages=(), service_status={"status": "ok", "checks": {}})

    monkeypatch.setattr(setup_tools, "_resolve_start_source_checkout", lambda _path: object())
    monkeypatch.setattr(setup_tools, "_resolve_start_bootstrap_inputs", fail_bootstrap_inputs)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fake_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_start_local_service", {})
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["error_code"] == "START_INPUT_RESOLUTION_FAILED"
    assert payload["message"] == "could not resolve local service startup inputs"
    assert payload["detail"] == {"error_type": "RuntimeError"}
    assert bootstrap_calls == []
    assert leaked_detail not in rendered


@pytest.mark.unit
async def test_start_local_service_source_checkout_value_error_is_structured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    source_checkout = "bad\0path"
    expected_source_path = tmp_path / source_checkout
    leaked_detail = "lstat: embedded null character in path"
    resolve_calls: list[Path | None] = []
    bootstrap_calls: list[bool] = []

    def fail_source_checkout(source_path: Path | None) -> object:
        resolve_calls.append(source_path)
        raise ValueError(leaked_detail)

    async def fake_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        bootstrap_calls.append(True)
        return ServiceBootstrapResult(stages=(), service_status={"status": "ok", "checks": {}})

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setup_tools, "_resolve_start_source_checkout", fail_source_checkout)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fake_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_start_local_service",
        {"source_checkout": source_checkout},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["error_code"] == "START_INPUT_RESOLUTION_FAILED"
    assert payload["message"] == "could not resolve local service startup inputs"
    assert payload["detail"] == {"error_type": "ValueError"}
    assert resolve_calls == [expected_source_path]
    assert bootstrap_calls == []
    assert leaked_detail not in rendered


@pytest.mark.unit
async def test_initialize_project_profile_uses_onboarding_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    project = tmp_path / "repo"
    project.mkdir()
    preview = SimpleNamespace(
        path=project,
        draft=SimpleNamespace(template="python"),
        to_dict=lambda: {
            "path": str(project),
            "inspection": {"detected_template": "python"},
            "draft": {"template": "python", "yaml": "name: python\n"},
            "diagnostics": {},
        },
    )
    writes: list[tuple[Any, bool]] = []

    def fake_preview(path: Path, *, template: str, include_smoke_request: bool) -> Any:
        assert path == project.resolve()
        assert template == "python"
        assert include_smoke_request is True
        return preview

    def fake_write(item: Any, *, force: bool) -> Path:
        writes.append((item, force))
        return project / ".awf" / "workspace.yml"

    monkeypatch.setattr(setup_tools, "preview_project_onboarding", fake_preview)
    monkeypatch.setattr(setup_tools, "write_workspace_profile", fake_write)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    preview_result = _payload(
        await mcp.call_tool(
            "awf_initialize_project_profile",
            {
                "project_path": str(project),
                "template": "python",
                "include_smoke_request": True,
            },
        )
    )
    write_result = _payload(
        await mcp.call_tool(
            "awf_initialize_project_profile",
            {
                "project_path": str(project),
                "template": "python",
                "include_smoke_request": True,
                "write_profile": True,
                "force": True,
            },
        )
    )

    assert preview_result["mode"] == "preview"
    assert "written_path" not in preview_result
    assert writes == [(preview, True)]
    assert write_result["mode"] == "write"
    assert write_result["written_path"].endswith(".awf/workspace.yml")


@pytest.mark.unit
async def test_initialize_project_profile_file_exists_is_structured_mcp_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    project = tmp_path / "repo"
    project.mkdir()
    preview = SimpleNamespace(
        path=project,
        draft=SimpleNamespace(template="generic"),
        to_dict=lambda: {
            "path": str(project),
            "inspection": {"detected_template": "generic"},
            "draft": {"template": "generic", "yaml": "name: generic\n"},
            "diagnostics": {},
        },
    )
    monkeypatch.setattr(setup_tools, "preview_project_onboarding", lambda *_a, **_k: preview)

    def fail_write(_preview: Any, *, force: bool) -> Path:
        raise FileExistsError(17, "File exists", project / ".awf" / "workspace.yml")

    monkeypatch.setattr(setup_tools, "write_workspace_profile", fail_write)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_initialize_project_profile",
        {"project_path": str(project), "write_profile": True},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["error_code"] == "PROJECT_PROFILE_EXISTS"
    assert payload["message"] == "project profile already exists; pass force=true to overwrite"
    assert payload["detail"]["project_path"] == str(project.resolve())
    assert "[Errno" not in rendered


@pytest.mark.unit
async def test_initialize_project_profile_path_expanduser_failure_returns_structured_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    project_path = "missing project"
    expected_project_path = tmp_path / project_path
    leaked_detail = "home directory unavailable"
    original_expanduser = setup_tools.Path.expanduser

    def fail_expanduser(path: Path) -> Path:
        if str(path) == project_path:
            raise RuntimeError(leaked_detail)
        return original_expanduser(path)

    monkeypatch.chdir(tmp_path)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))
    monkeypatch.setattr(setup_tools.Path, "expanduser", fail_expanduser)

    result = await mcp.call_tool(
        "awf_initialize_project_profile",
        {"project_path": project_path, "template": "generic"},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["error_code"] == "PROJECT_INIT_INVALID_PATH"
    assert payload["message"] == "project path does not exist"
    assert payload["detail"] == {"project_path": str(expected_project_path)}
    assert leaked_detail not in rendered


@pytest.mark.unit
async def test_initialize_project_profile_path_resolve_failure_returns_structured_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    project_path = "missing project"
    expected_project_path = tmp_path / project_path
    leaked_detail = "path resolution unavailable"
    original_resolve = setup_tools.Path.resolve

    def fail_resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
        if str(path) == project_path:
            raise OSError(leaked_detail)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.chdir(tmp_path)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))
    monkeypatch.setattr(setup_tools.Path, "resolve", fail_resolve)

    result = await mcp.call_tool(
        "awf_initialize_project_profile",
        {"project_path": project_path, "template": "generic"},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["error_code"] == "PROJECT_INIT_INVALID_PATH"
    assert payload["message"] == "project path does not exist"
    assert payload["detail"] == {"project_path": str(expected_project_path)}
    assert leaked_detail not in rendered


@pytest.mark.unit
async def test_initialize_project_profile_path_value_error_returns_structured_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project_path = "bad\0path"
    expected_project_path = tmp_path / project_path

    monkeypatch.chdir(tmp_path)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_initialize_project_profile",
        {"project_path": project_path, "template": "generic"},
    )
    payload = _payload(result)

    assert result.isError is True
    assert payload["error_code"] == "PROJECT_INIT_INVALID_PATH"
    assert payload["message"] == "project path does not exist"
    assert payload["detail"] == {"project_path": str(expected_project_path)}


@pytest.mark.unit
async def test_initialize_project_profile_preview_failure_does_not_surface_exception_text(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    project = tmp_path / "repo"
    project.mkdir()
    leaked_detail = "/srv/awf/internal/config.yml traceback frame"

    def fail_preview(
        _path: Path,
        *,
        template: str,
        include_smoke_request: bool,
    ) -> Any:
        _ = (template, include_smoke_request)
        raise RuntimeError(leaked_detail)

    monkeypatch.setattr(setup_tools, "preview_project_onboarding", fail_preview)
    caplog.set_level(logging.ERROR, logger="awf.mcp.setup_tools")
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_initialize_project_profile",
        {"project_path": str(project), "template": "generic"},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["error_code"] == "PROJECT_INIT_FAILED"
    assert payload["message"] == "could not build onboarding preview"
    assert payload["detail"] == {
        "project_path": str(project.resolve()),
        "template": "generic",
    }
    assert leaked_detail not in rendered

    records = [
        record
        for record in caplog.records
        if record.name == "awf.mcp.setup_tools"
        and record.message == "could not build onboarding preview for MCP project initialization"
    ]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert records[0].project_path == str(project.resolve())
    assert records[0].template == "generic"


@pytest.mark.unit
async def test_initialize_project_profile_value_error_preview_failure_does_not_surface_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    project = tmp_path / "repo"
    project.mkdir()
    leaked_detail = "/srv/awf/internal/template.yml unsupported onboarding template"

    def fail_preview(
        _path: Path,
        *,
        template: str,
        include_smoke_request: bool,
    ) -> Any:
        _ = (template, include_smoke_request)
        raise ValueError(leaked_detail)

    monkeypatch.setattr(setup_tools, "preview_project_onboarding", fail_preview)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_initialize_project_profile",
        {"project_path": str(project), "template": "generic"},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["error_code"] == "PROJECT_INIT_FAILED"
    assert payload["message"] == "could not build onboarding preview"
    assert payload["detail"] == {
        "project_path": str(project.resolve()),
        "template": "generic",
    }
    assert leaked_detail not in rendered


@pytest.mark.unit
async def test_initialize_project_profile_existing_profile_probe_failure_is_structured(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    project = tmp_path / "repo"
    project.mkdir()
    leaked_detail = "/srv/awf/internal/.awf/workspace.yml permission denied"

    def fail_existing_profile_probe(_repository: Path) -> Path | None:
        raise PermissionError(leaked_detail)

    monkeypatch.setattr(
        setup_tools,
        "_existing_project_profile_path",
        fail_existing_profile_probe,
    )
    caplog.set_level(logging.ERROR, logger="awf.mcp.setup_tools")
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_initialize_project_profile",
        {"project_path": str(project), "template": "generic"},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["error_code"] == "PROJECT_INIT_FAILED"
    assert payload["message"] == "could not build onboarding preview"
    assert payload["detail"] == {
        "project_path": str(project.resolve()),
        "template": "generic",
    }
    assert leaked_detail not in rendered

    records = [
        record
        for record in caplog.records
        if record.name == "awf.mcp.setup_tools"
        and record.message == "could not build onboarding preview for MCP project initialization"
    ]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert records[0].project_path == str(project.resolve())
    assert records[0].template == "generic"


@pytest.mark.unit
async def test_client_integration_instructions_are_secret_free(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    raw_token = "sk-proj-" + "c" * 40
    env_file = tmp_path / ".env"
    env_file.write_text(f"OPENAI_API_KEY={raw_token}\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.setattr(setup_tools, "_resolve_client_env_file", lambda *_args: env_file)
    monkeypatch.setattr(setup_tools, "_client_home", lambda: home)
    monkeypatch.setattr(setup_tools, "_client_which", lambda _binary: None)
    monkeypatch.setattr(setup_tools, "_client_now", lambda: datetime(2026, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(setup_tools, "_client_env", lambda: {})
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_client_integration_instructions",
        {"clients": ["claude", "codex"]},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is False
    assert payload["status"] == "success"
    assert {client["client"] for client in payload["clients"]} == {"claude", "codex"}
    claude = next(client for client in payload["clients"] if client["client"] == "claude")
    assert claude["config_path"] == str(home / ".claude.json")
    assert claude["desired_entry"]["command"] == "awf"
    assert str(env_file) in claude["desired_entry"]["args"]
    assert "awf setup --client claude" in claude["apply_command"]
    assert raw_token not in rendered


@pytest.mark.unit
async def test_client_integration_instructions_preserves_explicit_empty_clients(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    env_file = tmp_path / ".env"
    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.setattr(setup_tools, "_resolve_client_env_file", lambda *_args: env_file)
    monkeypatch.setattr(setup_tools, "_client_home", lambda: home)
    monkeypatch.setattr(setup_tools, "_client_which", lambda _binary: None)
    monkeypatch.setattr(setup_tools, "_client_now", lambda: datetime(2026, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(setup_tools, "_client_env", lambda: {})
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_client_integration_instructions",
        {"clients": []},
    )
    payload = _payload(result)

    assert result.isError is False
    assert payload["status"] == "success"
    assert payload["clients"] == []
    assert payload["next_steps"] == ["No client config changes are needed."]


@pytest.mark.unit
async def test_client_integration_instructions_preserve_explicit_source_checkout_apply_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    checkout = tmp_path / "source checkout"
    env_file = checkout / "docker" / "compose" / ".env"
    home = tmp_path / "home"
    home.mkdir()
    resolve_calls: list[tuple[Path | None, bool]] = []

    def fake_resolve_client_env_file(
        source_checkout: Path | None,
        require_existing: bool = False,
    ) -> Path:
        resolve_calls.append((source_checkout, require_existing))
        return env_file

    monkeypatch.setattr(setup_tools, "_resolve_client_env_file", fake_resolve_client_env_file)
    monkeypatch.setattr(setup_tools, "_client_home", lambda: home)
    monkeypatch.setattr(setup_tools, "_client_which", lambda _binary: None)
    monkeypatch.setattr(setup_tools, "_client_now", lambda: datetime(2026, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(setup_tools, "_client_env", lambda: {})
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_client_integration_instructions",
        {"clients": ["claude"], "source_checkout": str(checkout)},
    )
    payload = _payload(result)

    expected_command = f"awf setup --client claude --source-checkout '{checkout}'"
    assert result.isError is False
    assert resolve_calls == [(checkout, False)]
    assert payload["clients"][0]["apply_command"] == expected_command
    assert payload["next_steps"] == [
        f"Run `{expected_command}` to apply the claude client integration."
    ]


@pytest.mark.unit
async def test_client_integration_instructions_resolves_relative_source_checkout_apply_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    checkout = tmp_path / "source checkout"
    env_file = checkout / "docker" / "compose" / ".env"
    home = tmp_path / "home"
    home.mkdir()
    resolve_calls: list[tuple[Path | None, bool]] = []

    def fake_resolve_client_env_file(
        source_checkout: Path | None,
        require_existing: bool = False,
    ) -> Path:
        resolve_calls.append((source_checkout, require_existing))
        return env_file

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(setup_tools, "_resolve_client_env_file", fake_resolve_client_env_file)
    monkeypatch.setattr(setup_tools, "_client_home", lambda: home)
    monkeypatch.setattr(setup_tools, "_client_which", lambda _binary: None)
    monkeypatch.setattr(setup_tools, "_client_now", lambda: datetime(2026, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(setup_tools, "_client_env", lambda: {})
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_client_integration_instructions",
        {"clients": ["claude"], "source_checkout": "source checkout"},
    )
    payload = _payload(result)

    resolved_checkout = checkout.resolve()
    expected_command = f"awf setup --client claude --source-checkout '{resolved_checkout}'"
    assert result.isError is False
    assert resolve_calls == [(resolved_checkout, False)]
    assert payload["clients"][0]["apply_command"] == expected_command
    assert payload["next_steps"] == [
        f"Run `{expected_command}` to apply the claude client integration."
    ]


@pytest.mark.unit
async def test_client_integration_instructions_unknown_client_is_structured_error(
    tmp_path: Path,
) -> None:
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_client_integration_instructions",
        {"clients": ["missing-client"]},
    )
    payload = _payload(result)

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == SETUP_CLIENT_UNKNOWN


@pytest.mark.unit
async def test_client_integration_instructions_planning_setup_error_is_structured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    raw_token = "sk-proj-" + "d" * 40

    def fail_plan(*_args: Any, **_kwargs: Any) -> Any:
        raise SetupCheckError(
            f"planner failed with provider secret {raw_token}",
            reason_code=CLIENT_CONFIG_CONFLICT,
            details={"client": "claude", "raw": raw_token},
        )

    monkeypatch.setattr(setup_tools, "build_client_config_plan", fail_plan)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_client_integration_instructions",
        {"clients": ["claude"]},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == CLIENT_CONFIG_CONFLICT
    assert raw_token not in rendered
    assert REDACTION_MARKER in rendered


@pytest.mark.unit
async def test_client_integration_instructions_planning_oserror_is_generic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    raw_token = "sk-proj-" + "e" * 40
    leaked_detail = f"{tmp_path}/config.json contains {raw_token}"

    def fail_plan(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(leaked_detail)

    monkeypatch.setattr(setup_tools, "build_client_config_plan", fail_plan)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_client_integration_instructions",
        {"clients": ["claude"]},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == CLIENT_CONFIG_CONFLICT
    assert payload["summary"] == "could not inspect existing client MCP configuration"
    assert payload["issues"][0]["details"] == {"error_type": "OSError"}
    assert leaked_detail not in rendered
    assert raw_token not in rendered
