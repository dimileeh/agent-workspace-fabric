"""Focused tests for MCP client integration setup instructions."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from awf.common.audit import REDACTION_MARKER
from awf.host_setup.rendering import CLIENT_CONFIG_CONFLICT, SETUP_CLIENT_UNKNOWN
from awf.host_setup.system_checks import SetupCheckError
from awf.mcp.server import build_mcp_server
from tests.unit.mcp.setup_tools_test_helpers import _json_text, _payload, _settings


@pytest.mark.unit
async def test_client_integration_instructions_preserves_explicit_empty_clients(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    def fail_source_checkout_resolution(_source_checkout: str | None) -> Path | None:
        pytest.fail("source checkout must not be resolved for explicit empty clients")

    def fail_env_file_resolution(_source_checkout: Path | None, _require_existing: bool) -> Path:
        pytest.fail("env file must not be resolved for explicit empty clients")

    monkeypatch.setattr(
        setup_tools,
        "_resolve_client_source_checkout_path",
        fail_source_checkout_resolution,
    )
    monkeypatch.setattr(setup_tools, "_resolve_client_env_file", fail_env_file_resolution)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_client_integration_instructions",
        {"clients": [], "source_checkout": str(tmp_path / "stale-checkout")},
    )
    payload = _payload(result)

    assert result.isError is False
    assert payload["status"] == "success"
    assert payload["clients"] == []
    assert "env_file" not in payload
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


@pytest.mark.unit
async def test_client_integration_instructions_codex_invalid_home_override_is_structured(
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
    monkeypatch.setattr(setup_tools, "_client_env", lambda: {"CODEX_HOME": "~nosuchuser"})
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_client_integration_instructions",
        {"clients": ["codex"]},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == CLIENT_CONFIG_CONFLICT
    assert payload["summary"] == "could not inspect existing client MCP configuration"
    assert payload["issues"][0]["details"] == {"error_type": "RuntimeError"}
    assert "Could not determine home directory" not in rendered


@pytest.mark.unit
async def test_client_integration_instructions_planning_value_error_is_generic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    raw_token = "sk-proj-" + "f" * 40
    leaked_detail = f"{tmp_path}/config.toml contains {raw_token}"

    def fail_plan(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError(leaked_detail)

    monkeypatch.setattr(setup_tools, "build_client_config_plan", fail_plan)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_client_integration_instructions",
        {"clients": ["codex"]},
    )
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == CLIENT_CONFIG_CONFLICT
    assert payload["summary"] == "could not inspect existing client MCP configuration"
    assert payload["issues"][0]["details"] == {"error_type": "ValueError"}
    assert leaked_detail not in rendered
    assert raw_token not in rendered
