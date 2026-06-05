"""Focused tests for first-run setup/start/init/client MCP tools."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CalledProcessError
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from awf.common.audit import REDACTION_MARKER
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
    START_HEALTH_TIMEOUT,
    first_run_failure_payload,
    first_run_success_payload,
)
from awf.host_setup.source_assets import (
    SOURCE_CHECKOUT_ASSETS_STALE,
    SOURCE_CHECKOUT_INVALID,
    SourceCheckoutAssetMetadata,
    SourceCheckoutError,
)
from awf.host_setup.system_checks import SetupCheckError
from awf.mcp.server import build_mcp_server
from awf.service.bootstrap import (
    SERVICE_BOOTSTRAP_TIMEOUT,
    ServiceBootstrapError,
    ServiceBootstrapResult,
)
from tests.unit.mcp.setup_tools_test_helpers import _json_text, _payload, _settings

SETUP_TOOL_NAMES = {
    "awf_get_setup_status",
    "awf_start_local_service",
    "awf_initialize_project_profile",
    "awf_get_client_integration_instructions",
}


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
    claude = next(client for client in payload["clients"] if client["client"] == "claude")
    assert claude["config_path"] == str(home / ".claude.json")
    assert claude["desired_entry"]["command"] == "awf"
    assert str(env_file) in claude["desired_entry"]["args"]
    assert "awf setup --client claude" in claude["apply_command"]
    assert raw_token not in rendered


@pytest.mark.unit
async def test_client_integration_instructions_source_checkout_failure_preserves_explicit_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    checkout = tmp_path / "source checkout"
    checkout.mkdir()

    def fail_env_file(source_path: Path | None, _require_existing: bool) -> Path:
        assert source_path == checkout
        raise SourceCheckoutError(
            reason_code=SOURCE_CHECKOUT_INVALID,
            message="AWF source checkout is missing required assets.",
            root=checkout,
            missing_markers=("pyproject.toml",),
        )

    monkeypatch.setattr(setup_tools, "_resolve_client_env_file", fail_env_file)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_client_integration_instructions",
        {"clients": ["claude", "codex"], "source_checkout": str(checkout)},
    )
    payload = _payload(result)
    expected_command = f"awf setup --client claude --client codex --source-checkout '{checkout}'"

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == SOURCE_CHECKOUT_INVALID
    assert payload["command"] == expected_command
    assert payload["next_steps"] == [
        f"Fix the reported --source-checkout path above, then re-run {expected_command}.",
    ]
    assert payload["issues"][0]["details"]["root"] == str(checkout)
    assert payload["issues"][0]["details"]["missing_markers"] == ["pyproject.toml"]


@pytest.mark.unit
async def test_client_integration_instructions_persisted_source_checkout_failure_preserves_selected_clients(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    checkout = tmp_path / "stale source checkout"
    checkout.mkdir()

    def fail_env_file(source_path: Path | None, _require_existing: bool) -> Path:
        assert source_path is None
        raise SourceCheckoutError(
            reason_code=SOURCE_CHECKOUT_ASSETS_STALE,
            message="Stored AWF source checkout metadata is no longer valid.",
            root=checkout,
            missing_markers=("uv.lock",),
        )

    monkeypatch.setattr(setup_tools, "_resolve_client_env_file", fail_env_file)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_client_integration_instructions",
        {"clients": ["claude", "codex"]},
    )
    payload = _payload(result)
    expected_command = "awf setup --client claude --client codex"

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == SOURCE_CHECKOUT_ASSETS_STALE
    assert payload["command"] == expected_command
    assert payload["next_steps"] == [
        f"Fix the reported --source-checkout path above, then re-run {expected_command}.",
    ]
    assert payload["issues"][0]["details"]["root"] == str(checkout)
    assert payload["issues"][0]["details"]["missing_markers"] == ["uv.lock"]


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
        env_migration=None,
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
        "Fix the reported issue above, then re-run awf setup --dry-run."
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


@pytest.mark.unit
async def test_start_local_service_reuses_bootstrap_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    verified = object()
    migration_payload = {
        "status": "migrated",
        "canonical_env_file": str(tmp_path / ".env"),
        "legacy_env_file": str(tmp_path / "docker" / "compose" / ".env"),
        "imported_keys": ["OPENAI_API_KEY"],
        "conflict_keys": [],
    }
    inputs = SimpleNamespace(
        settings=SimpleNamespace(
            api_base_url="http://localhost:8000",
            console_url="http://localhost:3000",
        ),
        compose_file=tmp_path / "compose.yml",
        compose_env_file=tmp_path / ".env",
        asset_root=tmp_path,
        service_env={"AWF_API_HOST_PORT": "8000"},
        env_migration=SimpleNamespace(to_dict=lambda: migration_payload),
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
    expected_command = f"awf start --rebuild --timeout-seconds 7.5 --source-checkout {tmp_path}"
    assert first["command"] == expected_command
    assert second["command"] == first["command"]
    assert first["details"]["env_migration"] == migration_payload
    assert second["details"]["env_migration"] == migration_payload
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
async def test_start_local_service_preserves_skip_agent_runtime_build_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    inputs = SimpleNamespace(
        settings=SimpleNamespace(api_base_url="http://localhost:8000", console_url=None),
        compose_file=tmp_path / "compose.yml",
        compose_env_file=None,
        asset_root=None,
        service_env={},
        env_migration=None,
    )
    calls: list[dict[str, Any]] = []

    async def fake_bootstrap(*args: Any, **kwargs: Any) -> ServiceBootstrapResult:
        calls.append({"args": args, "kwargs": kwargs})
        return ServiceBootstrapResult(stages=(), service_status={"status": "ok", "checks": {}})

    monkeypatch.setattr(setup_tools, "_resolve_start_source_checkout", lambda _path: None)
    monkeypatch.setattr(setup_tools, "_resolve_start_bootstrap_inputs", lambda _verified: inputs)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fake_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    payload = _payload(
        await mcp.call_tool("awf_start_local_service", {"skip_agent_runtime_build": True})
    )

    assert payload["status"] == "success"
    assert payload["command"] == "awf start --skip-agent-runtime-build"
    assert len(calls) == 1
    options = calls[0]["kwargs"]["options"]
    assert options.force_rebuild is False
    assert options.skip_agent_runtime_build is True


@pytest.mark.unit
async def test_start_local_service_preserves_explicit_source_checkout_success_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    checkout = tmp_path / "source checkout"
    checkout.mkdir()
    inputs = SimpleNamespace(
        settings=SimpleNamespace(api_base_url="http://localhost:8000", console_url=None),
        compose_file=checkout / "docker" / "compose" / "compose.yml",
        compose_env_file=None,
        asset_root=checkout,
        service_env={},
        env_migration=None,
    )

    async def fake_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        return ServiceBootstrapResult(stages=(), service_status={"status": "ok", "checks": {}})

    monkeypatch.setattr(setup_tools, "_resolve_start_source_checkout", lambda _path: object())
    monkeypatch.setattr(setup_tools, "_resolve_start_bootstrap_inputs", lambda _verified: inputs)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fake_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    payload = _payload(
        await mcp.call_tool("awf_start_local_service", {"source_checkout": str(checkout)})
    )

    assert payload["status"] == "success"
    assert payload["command"] == f"awf start --source-checkout '{checkout}'"


@pytest.mark.unit
async def test_start_local_service_preserves_explicit_source_checkout_validation_failure_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    checkout = tmp_path / "source checkout"
    checkout.mkdir()
    bootstrap_calls: list[bool] = []

    def fail_source_checkout(source_path: Path | None) -> object:
        assert source_path == checkout
        raise SourceCheckoutError(
            reason_code=SOURCE_CHECKOUT_INVALID,
            message="AWF source checkout is missing required assets.",
            root=checkout,
            missing_markers=("pyproject.toml",),
        )

    async def fake_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        bootstrap_calls.append(True)
        return ServiceBootstrapResult(stages=(), service_status={"status": "ok", "checks": {}})

    monkeypatch.setattr(setup_tools, "_resolve_start_source_checkout", fail_source_checkout)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fake_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_start_local_service", {"source_checkout": str(checkout)})
    payload = _payload(result)

    assert result.isError is True
    assert payload["status"] == "failed"
    assert payload["reason_code"] == SOURCE_CHECKOUT_INVALID
    assert payload["command"] == f"awf start --source-checkout '{checkout}'"
    assert payload["issues"][0]["details"]["missing_markers"] == ["pyproject.toml"]
    assert bootstrap_calls == []


@pytest.mark.unit
async def test_start_local_service_reports_structured_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    raw_token = "sk-proj-" + "b" * 40
    migration_payload = {
        "status": "migrated",
        "canonical_env_file": str(tmp_path / ".env"),
        "legacy_env_file": str(tmp_path / "docker" / "compose" / ".env"),
        "imported_keys": ["GITHUB_TOKEN"],
        "conflict_keys": [],
    }
    inputs = SimpleNamespace(
        settings=SimpleNamespace(api_base_url="http://localhost:8000", console_url=None),
        compose_file=tmp_path / "compose.yml",
        compose_env_file=None,
        asset_root=None,
        service_env={},
        env_migration=SimpleNamespace(to_dict=lambda: migration_payload),
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
    assert payload["issues"][0]["details"]["env_migration"] == migration_payload
    assert raw_token not in rendered
    assert REDACTION_MARKER in rendered


@pytest.mark.unit
async def test_start_local_service_preserves_explicit_source_checkout_bootstrap_failure_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    checkout = tmp_path / "source checkout"
    checkout.mkdir()
    inputs = SimpleNamespace(
        settings=SimpleNamespace(api_base_url="http://localhost:8000", console_url=None),
        compose_file=checkout / "docker" / "compose" / "compose.yml",
        compose_env_file=None,
        asset_root=checkout,
        service_env={},
        env_migration=None,
    )

    async def fail_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        raise ServiceBootstrapError(
            reason_code=SERVICE_BOOTSTRAP_TIMEOUT,
            message="timed out",
        )

    monkeypatch.setattr(setup_tools, "_resolve_start_source_checkout", lambda _path: object())
    monkeypatch.setattr(setup_tools, "_resolve_start_bootstrap_inputs", lambda _verified: inputs)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fail_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_start_local_service", {"source_checkout": str(checkout)})
    payload = _payload(result)

    assert result.isError is True
    assert payload["status"] == "failed"
    assert payload["command"] == f"awf start --source-checkout '{checkout}'"
    assert payload["issues"][0]["details"]["bootstrap"]["reason_code"] == SERVICE_BOOTSTRAP_TIMEOUT


@pytest.mark.unit
async def test_start_local_service_bootstrap_path_runtime_error_is_first_run_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    leaked_detail = "Could not determine home directory for ~nosuchuser/work"
    migration_payload = {
        "status": "migrated",
        "canonical_env_file": str(tmp_path / ".env"),
        "legacy_env_file": str(tmp_path / "docker" / "compose" / ".env"),
        "imported_keys": ["OPENAI_API_KEY"],
        "conflict_keys": [],
    }
    inputs = SimpleNamespace(
        settings=SimpleNamespace(api_base_url="http://localhost:8000", console_url=None),
        compose_file=tmp_path / "compose.yml",
        compose_env_file=None,
        asset_root=None,
        service_env={"AWF_HOST_WORK_DIR": "~nosuchuser/work"},
        env_migration=SimpleNamespace(to_dict=lambda: migration_payload),
    )

    async def fail_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        raise RuntimeError(leaked_detail)

    monkeypatch.setattr(setup_tools, "_resolve_start_source_checkout", lambda _path: None)
    monkeypatch.setattr(setup_tools, "_resolve_start_bootstrap_inputs", lambda _verified: inputs)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fail_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_start_local_service", {})
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["status"] == "failed"
    assert payload["command"] == "awf start"
    assert payload["reason_code"] == "START_BOOTSTRAP_EXECUTION_FAILED"
    assert payload["summary"] == "awf start failed: could not execute local service bootstrap"
    bootstrap = payload["issues"][0]["details"]["bootstrap"]
    assert bootstrap["reason_code"] == "START_BOOTSTRAP_EXECUTION_FAILED"
    assert bootstrap["message"] == "could not execute local service bootstrap"
    assert payload["issues"][0]["details"]["env_migration"] == migration_payload
    assert leaked_detail not in rendered


@pytest.mark.unit
async def test_start_local_service_preserves_explicit_source_checkout_bootstrap_path_failure_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    checkout = tmp_path / "source checkout"
    checkout.mkdir()
    inputs = SimpleNamespace(
        settings=SimpleNamespace(api_base_url="http://localhost:8000", console_url=None),
        compose_file=checkout / "docker" / "compose" / "compose.yml",
        compose_env_file=None,
        asset_root=checkout,
        service_env={},
        env_migration=None,
    )

    async def fail_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        raise RuntimeError("local bootstrap path failed")

    monkeypatch.setattr(setup_tools, "_resolve_start_source_checkout", lambda _path: object())
    monkeypatch.setattr(setup_tools, "_resolve_start_bootstrap_inputs", lambda _verified: inputs)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fail_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_start_local_service", {"source_checkout": str(checkout)})
    payload = _payload(result)

    assert result.isError is True
    assert payload["status"] == "failed"
    assert payload["command"] == f"awf start --source-checkout '{checkout}'"
    assert payload["reason_code"] == "START_BOOTSTRAP_EXECUTION_FAILED"


@pytest.mark.unit
async def test_start_local_service_bootstrap_called_process_error_is_structured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    raw_token = "sk-proj-" + "d" * 40
    inputs = SimpleNamespace(
        settings=SimpleNamespace(api_base_url="http://localhost:8000", console_url=None),
        compose_file=tmp_path / "compose.yml",
        compose_env_file=None,
        asset_root=None,
        service_env={},
        env_migration=None,
    )

    async def fail_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        raise CalledProcessError(17, ["docker", "compose", raw_token], stderr=raw_token)

    monkeypatch.setattr(setup_tools, "_resolve_start_source_checkout", lambda _path: None)
    monkeypatch.setattr(setup_tools, "_resolve_start_bootstrap_inputs", lambda _verified: inputs)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fail_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_start_local_service", {})
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["reason_code"] == "START_BOOTSTRAP_EXECUTION_FAILED"
    assert payload["issues"][0]["details"]["bootstrap"]["stderr"] == "error_type=CalledProcessError"
    assert raw_token not in rendered


@pytest.mark.unit
async def test_start_local_service_input_resolution_failure_is_structured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    leaked_detail = "/srv/awf/docker/compose/.env missing OPENAI_API_KEY"
    checkout = tmp_path / "source checkout"
    checkout.mkdir()

    def resolve_source_checkout(source_path: Path | None) -> object:
        assert source_path == checkout
        return object()

    def fail_bootstrap_inputs(_verified: object) -> SimpleNamespace:
        raise ValueError(leaked_detail)

    bootstrap_calls: list[bool] = []

    async def fake_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        bootstrap_calls.append(True)
        return ServiceBootstrapResult(stages=(), service_status={"status": "ok", "checks": {}})

    monkeypatch.setattr(setup_tools, "_resolve_start_source_checkout", resolve_source_checkout)
    monkeypatch.setattr(setup_tools, "_resolve_start_bootstrap_inputs", fail_bootstrap_inputs)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fake_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_start_local_service",
        {
            "rebuild": True,
            "timeout_seconds": 42.5,
            "source_checkout": str(checkout),
        },
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["command"] == (
        f"awf start --rebuild --timeout-seconds 42.5 --source-checkout '{checkout}'"
    )
    assert payload["summary"] == "could not resolve local service startup inputs"
    assert payload["reason_code"] == "START_INPUT_RESOLUTION_FAILED"
    assert payload["issues"][0]["details"] == {"error_type": "ValueError"}
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
    assert payload["status"] == "blocked"
    assert payload["command"] == "awf start"
    assert payload["summary"] == "could not resolve local service startup inputs"
    assert payload["reason_code"] == "START_INPUT_RESOLUTION_FAILED"
    assert payload["issues"][0]["details"] == {"error_type": "RuntimeError"}
    assert bootstrap_calls == []
    assert leaked_detail not in rendered


@pytest.mark.unit
async def test_start_local_service_setup_check_input_resolution_failure_is_reason_coded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    raw_token = "sk-proj-" + "e" * 40

    def fail_bootstrap_inputs(_verified: object) -> SimpleNamespace:
        raise SetupCheckError(
            f"startup readiness failed with provider secret {raw_token}",
            reason_code=SETUP_READINESS_FAILED,
            details={"check": "docker", "raw": raw_token},
        )

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
    assert payload["status"] == "blocked"
    assert payload["command"] == "awf start"
    assert payload["reason_code"] == SETUP_READINESS_FAILED
    assert payload["issues"][0]["reason_code"] == SETUP_READINESS_FAILED
    assert payload["issues"][0]["details"]["check"] == "docker"
    assert bootstrap_calls == []
    assert raw_token not in rendered
    assert REDACTION_MARKER in rendered


@pytest.mark.unit
async def test_start_local_service_preserves_explicit_source_checkout_setup_check_input_resolution_failure_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    checkout = tmp_path / "source checkout"
    checkout.mkdir()

    def resolve_source_checkout(source_path: Path | None) -> object:
        assert source_path == checkout
        return object()

    def fail_bootstrap_inputs(_verified: object) -> SimpleNamespace:
        raise SetupCheckError(
            "startup readiness failed",
            reason_code=SETUP_READINESS_FAILED,
            details={"check": "docker"},
        )

    bootstrap_calls: list[bool] = []

    async def fake_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        bootstrap_calls.append(True)
        return ServiceBootstrapResult(stages=(), service_status={"status": "ok", "checks": {}})

    monkeypatch.setattr(setup_tools, "_resolve_start_source_checkout", resolve_source_checkout)
    monkeypatch.setattr(setup_tools, "_resolve_start_bootstrap_inputs", fail_bootstrap_inputs)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fake_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_start_local_service", {"source_checkout": str(checkout)})
    payload = _payload(result)

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["command"] == f"awf start --source-checkout '{checkout}'"
    assert payload["reason_code"] == SETUP_READINESS_FAILED
    assert payload["issues"][0]["details"]["check"] == "docker"
    assert bootstrap_calls == []


@pytest.mark.unit
async def test_start_local_service_called_process_input_resolution_failure_is_structured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    raw_token = "sk-proj-" + "f" * 40

    def fail_bootstrap_inputs(_verified: object) -> SimpleNamespace:
        raise CalledProcessError(18, ["docker", "compose", raw_token], stderr=raw_token)

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
    assert payload["status"] == "blocked"
    assert payload["command"] == "awf start"
    assert payload["summary"] == "could not resolve local service startup inputs"
    assert payload["reason_code"] == "START_INPUT_RESOLUTION_FAILED"
    assert payload["issues"][0]["details"] == {"error_type": "CalledProcessError"}
    assert bootstrap_calls == []
    assert raw_token not in rendered


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
    assert payload["status"] == "blocked"
    assert payload["command"] == f"awf start --source-checkout '{expected_source_path}'"
    assert payload["summary"] == "could not resolve local service startup inputs"
    assert payload["reason_code"] == "START_INPUT_RESOLUTION_FAILED"
    assert payload["issues"][0]["details"] == {"error_type": "ValueError"}
    assert resolve_calls == [expected_source_path]
    assert bootstrap_calls == []
    assert leaked_detail not in rendered
