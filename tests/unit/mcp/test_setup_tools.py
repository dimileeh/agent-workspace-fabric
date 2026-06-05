"""Focused tests for first-run setup/start/init/client MCP tools."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
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
    SETUP_PROVIDER_UNKNOWN,
    SETUP_READINESS_FAILED,
    first_run_failure_payload,
    first_run_success_payload,
)
from awf.host_setup.source_assets import (
    SOURCE_CHECKOUT_ASSETS_STALE,
    SourceCheckoutAssetMetadata,
)
from awf.host_setup.system_checks import SetupCheckError
from awf.mcp.server import build_mcp_server
from awf.service.bootstrap import ServiceBootstrapResult
from tests.unit.mcp.setup_tools_test_helpers import _json_text, _payload, _settings

SETUP_TOOL_NAMES = {
    "awf_get_setup_status",
    "awf_start_local_service",
    "awf_initialize_project_profile",
    "awf_get_client_integration_instructions",
}


@pytest.mark.unit
def test_client_instruction_reason_coded_next_step_preserves_backslash_digit_command() -> None:
    from awf.mcp import setup_tools

    step = "Fix GitHub auth, then re-run awf setup --dry-run --provider github."
    command = r"awf setup --source-checkout '/projects/model\1'"

    assert (
        setup_tools._client_instruction_reason_coded_next_step(step, command=command)
        == r"Fix GitHub auth, then re-run awf setup --source-checkout '/projects/model\1'."
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
        env_migration=None,
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
    assert payload["command"] == "awf setup --client claude --client codex"
    assert {client["client"] for client in payload["clients"]} == {"claude", "codex"}
    assert raw_token not in rendered


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
    assert payload["command"] == "awf setup --dry-run --provider github"
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
async def test_get_setup_status_setup_check_error_returns_matching_dry_run_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    checkout = tmp_path / "source checkout"

    def fail_run_setup(**kwargs: Any) -> Any:
        assert kwargs["providers"] == ["bogus"]
        assert kwargs["dry_run"] is True
        assert kwargs["source_checkout"] == checkout
        raise SetupCheckError(
            "Unsupported provider selector: 'bogus'.",
            reason_code=SETUP_PROVIDER_UNKNOWN,
            details={"provider": "bogus", "known_providers": ["codex", "github"]},
        )

    monkeypatch.setattr(setup_tools, "_run_setup", fail_run_setup)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_setup_status",
        {"providers": ["bogus"], "source_checkout": str(checkout)},
    )
    payload = _payload(result)
    expected_command = f"awf setup --dry-run --provider bogus --source-checkout '{checkout}'"

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["command"] == expected_command
    assert payload["next_steps"] == [
        f"Re-run awf setup --dry-run --source-checkout '{checkout}' with a supported "
        "--provider; the accepted names are listed under known_providers in the issue details.",
    ]
    assert payload["reason_code"] == SETUP_PROVIDER_UNKNOWN
    assert payload["issues"][0]["reason_code"] == SETUP_PROVIDER_UNKNOWN
    assert payload["issues"][0]["details"]["provider"] == "bogus"


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

    def fake_run_setup(**kwargs: Any) -> Any:
        assert kwargs["providers"] == ["github"]
        assert kwargs["dry_run"] is True
        return readiness

    monkeypatch.setattr(setup_tools, "_run_setup", fake_run_setup)
    monkeypatch.setattr(setup_tools, "read_host_setup_config", fail_read_config)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_get_setup_status", {"providers": ["github"]})
    payload = _payload(result)

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["command"] == "awf setup --dry-run --provider github"
    assert payload["reason_code"] == HOST_SETUP_CONFIG_CORRUPT
    assert payload["next_steps"] == [
        "Fix the reported issue above, then re-run awf setup --dry-run --provider github."
    ]
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

    checkout = tmp_path / "source checkout"
    raw_token = "sk-proj-" + "f" * 40
    leaked_detail = f"{tmp_path}/docker.sock contains {raw_token}"

    def fail_run_setup(**kwargs: Any) -> Any:
        assert kwargs["providers"] == ["github"]
        assert kwargs["dry_run"] is True
        assert kwargs["source_checkout"] == checkout
        raise OSError(leaked_detail)

    monkeypatch.setattr(setup_tools, "_run_setup", fail_run_setup)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_setup_status",
        {"providers": ["github"], "source_checkout": str(checkout)},
    )
    payload = _payload(result)
    rendered = _json_text(result)
    expected_command = f"awf setup --dry-run --provider github --source-checkout '{checkout}'"

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["command"] == expected_command
    assert payload["next_steps"] == [
        f"Fix the reported issue above, then re-run {expected_command}."
    ]
    assert payload["reason_code"] == SETUP_READINESS_FAILED
    assert payload["summary"] == "could not inspect local setup readiness"
    assert payload["issues"][0]["details"] == {"error_type": "OSError"}
    assert leaked_detail not in rendered
    assert raw_token not in rendered


@pytest.mark.unit
async def test_get_setup_status_success_transformation_failure_is_structured_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    checkout = tmp_path / "source checkout"
    raw_token = "sk-proj-" + "g" * 40
    readiness = first_run_success_payload(
        command="awf setup",
        summary="ready",
        details={"selected_providers": [], "checks": [{"name": "docker", "level": "ok"}]},
        next_steps=(),
    )

    def fail_setup_check_rendering(_value: Any) -> list[dict[str, str]]:
        raise RuntimeError(f"check rendering leaked {raw_token}")

    monkeypatch.setattr(setup_tools, "_run_setup", lambda **_kwargs: readiness)
    monkeypatch.setattr(setup_tools, "read_host_setup_config", HostSetupConfig)
    monkeypatch.setattr(setup_tools, "_safe_setup_checks", fail_setup_check_rendering)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_setup_status",
        {"providers": ["github"], "source_checkout": str(checkout)},
    )
    payload = _payload(result)
    rendered = _json_text(result)
    expected_command = f"awf setup --dry-run --provider github --source-checkout '{checkout}'"

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["command"] == expected_command
    assert payload["next_steps"] == [
        f"Fix the reported issue above, then re-run {expected_command}."
    ]
    assert payload["reason_code"] == SETUP_READINESS_FAILED
    assert payload["summary"] == "could not build setup status response"
    assert payload["issues"][0]["details"] == {"error_type": "RuntimeError"}
    assert raw_token not in rendered
