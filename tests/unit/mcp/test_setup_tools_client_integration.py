"""Focused tests for MCP client integration setup instructions."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from awf.common.audit import REDACTION_MARKER
from awf.host_setup.rendering import (
    CLIENT_CONFIG_CONFLICT,
    SETUP_CLIENT_UNKNOWN,
    SETUP_READINESS_FAILED,
    START_COMPOSE_ASSETS_MISSING,
)
from awf.host_setup.source_assets import (
    SOURCE_CHECKOUT_ASSETS_STALE,
    SOURCE_CHECKOUT_INVALID,
    SOURCE_CHECKOUT_MARKERS,
    SourceCheckoutError,
)
from awf.host_setup.system_checks import SetupCheckError
from awf.mcp.server import build_mcp_server
from tests.unit.mcp.setup_tools_test_helpers import _json_text, _payload, _settings


@pytest.mark.unit
@pytest.mark.parametrize(
    ("step", "command", "expected"),
    [
        (
            "Run awf setup --dry-run, then compare awf setup --dry-run output.",
            "awf setup --dry-run --source-checkout '/tmp/source'",
            "Run awf setup --dry-run --source-checkout '/tmp/source', "
            "then compare awf setup --dry-run output.",
        ),
        (
            "Fix GitHub auth, then re-run awf setup --dry-run --provider github.",
            "awf setup --client claude --source-checkout '/tmp/source'",
            "Fix GitHub auth, then re-run awf setup --client claude "
            "--source-checkout '/tmp/source'.",
        ),
        (
            "Run awf setup --client once; awf setup --client remains as an example.",
            "awf setup --client claude --source-checkout '/tmp/source'",
            "Run awf setup --client claude --source-checkout '/tmp/source' once; "
            "awf setup --client remains as an example.",
        ),
        (
            "Run awf setup now; keep awf setup in the explanatory tail.",
            "awf setup --client codex",
            "Run awf setup --client codex now; keep awf setup in the explanatory tail.",
        ),
    ],
)
def test_client_instruction_reason_coded_next_step_rewrites_first_command_only(
    step: str,
    command: str,
    expected: str,
) -> None:
    from awf.mcp import setup_tools

    assert setup_tools._client_instruction_reason_coded_next_step(step, command=command) == expected


def _make_source_checkout(root: Path) -> Path:
    for marker in SOURCE_CHECKOUT_MARKERS:
        target = root / marker.path
        if marker.kind == "dir":
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")
    return root


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
    assert "command" not in payload
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
    assert resolve_calls == [(checkout, True)]
    assert payload["command"] == expected_command
    assert payload["clients"][0]["apply_command"] == expected_command
    assert payload["next_steps"] == [
        f"Run `{expected_command}` to apply the claude client integration."
    ]


@pytest.mark.unit
async def test_client_integration_instructions_missing_source_env_blocks_before_apply_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    checkout = _make_source_checkout(tmp_path / "source checkout")
    root_env = checkout / ".env"
    assert (checkout / ".env.example").is_file()
    assert not root_env.exists()
    home = tmp_path / "home"
    home.mkdir()

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
    rendered = _json_text(result)
    expected_command = f"awf setup --client claude --source-checkout '{checkout}'"

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == START_COMPOSE_ASSETS_MISSING
    assert payload["command"] == expected_command
    assert payload["summary"] == (
        "AWF could not register the MCP client: the env file does not exist yet."
    )
    assert payload["issues"][0]["details"] == {
        "check": "client_env_file",
        "env_file": str(root_env),
    }
    assert payload["next_steps"] == [
        f"Run awf service bootstrap to create the env file, then re-run {expected_command}.",
    ]
    assert payload["issues"][0]["remediation"]["related_command"] == (
        f"awf start --source-checkout '{checkout}'"
    )
    assert "clients" not in payload
    assert "apply_command" not in rendered


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
    assert resolve_calls == [(resolved_checkout, True)]
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
    assert payload["command"] == "awf setup --client missing-client"
    assert payload["next_steps"] == [
        "Re-run awf setup --client missing-client with a supported --client; "
        "the accepted names are listed under known_clients in the issue details."
    ]


@pytest.mark.unit
async def test_client_integration_instructions_planning_setup_error_is_structured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    raw_token = "sk-proj-" + "d" * 40
    checkout = tmp_path / "source checkout"
    env_file = checkout / "docker" / "compose" / ".env"

    def fail_plan(*_args: Any, **_kwargs: Any) -> Any:
        raise SetupCheckError(
            f"planner failed with provider secret {raw_token}",
            reason_code=CLIENT_CONFIG_CONFLICT,
            details={"client": "claude", "raw": raw_token},
        )

    monkeypatch.setattr(setup_tools, "_resolve_client_env_file", lambda *_args: env_file)
    monkeypatch.setattr(setup_tools, "build_client_config_plan", fail_plan)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_client_integration_instructions",
        {"clients": ["claude"], "source_checkout": str(checkout)},
    )
    payload = _payload(result)
    rendered = _json_text(result)
    expected_command = f"awf setup --client claude --source-checkout '{checkout}'"

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == CLIENT_CONFIG_CONFLICT
    assert payload["command"] == expected_command
    assert payload["next_steps"] == [
        f"Fix the reported issue above, then re-run {expected_command}.",
    ]
    assert raw_token not in rendered
    assert REDACTION_MARKER in rendered


@pytest.mark.unit
async def test_client_integration_instructions_planning_oserror_is_readiness_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    raw_token = "sk-proj-" + "e" * 40
    checkout = tmp_path / "source checkout"
    env_file = checkout / "docker" / "compose" / ".env"
    leaked_detail = f"{tmp_path}/config.json contains {raw_token}"

    def fail_plan(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(leaked_detail)

    monkeypatch.setattr(setup_tools, "_resolve_client_env_file", lambda *_args: env_file)
    monkeypatch.setattr(setup_tools, "build_client_config_plan", fail_plan)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_client_integration_instructions",
        {"clients": ["claude"], "source_checkout": str(checkout)},
    )
    payload = _payload(result)
    rendered = _json_text(result)
    expected_command = f"awf setup --client claude --source-checkout '{checkout}'"

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == SETUP_READINESS_FAILED
    assert payload["command"] == expected_command
    assert payload["summary"] == "could not inspect local client integration environment"
    assert payload["issues"][0]["details"] == {"error_type": "OSError"}
    assert payload["next_steps"] == [
        f"Fix the reported issue above, then re-run {expected_command}.",
    ]
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
    assert payload["reason_code"] == SETUP_READINESS_FAILED
    assert payload["summary"] == "could not inspect local client integration environment"
    assert payload["issues"][0]["details"] == {"error_type": "RuntimeError"}
    assert "Could not determine home directory" not in rendered


@pytest.mark.unit
async def test_client_integration_instructions_planning_value_error_is_readiness_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    raw_token = "sk-proj-" + "f" * 40
    env_file = tmp_path / ".env"
    leaked_detail = f"{tmp_path}/config.toml contains {raw_token}"

    def fail_plan(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError(leaked_detail)

    monkeypatch.setattr(setup_tools, "_resolve_client_env_file", lambda *_args: env_file)
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
    assert payload["reason_code"] == SETUP_READINESS_FAILED
    assert payload["summary"] == "could not inspect local client integration environment"
    assert payload["issues"][0]["details"] == {"error_type": "ValueError"}
    assert leaked_detail not in rendered
    assert raw_token not in rendered


@pytest.mark.unit
async def test_client_integration_instructions_planning_unexpected_exception_has_planning_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    raw_token = "sk-proj-" + "g" * 40
    env_file = tmp_path / ".env"
    leaked_detail = f"missing client descriptor {raw_token}"

    def fail_plan(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyError(leaked_detail)

    monkeypatch.setattr(setup_tools, "_resolve_client_env_file", lambda *_args: env_file)
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
    assert payload["reason_code"] == SETUP_READINESS_FAILED
    assert payload["summary"] == "could not plan client integration instructions"
    assert payload["issues"][0]["details"] == {"error_type": "KeyError"}
    assert leaked_detail not in rendered
    assert raw_token not in rendered


@pytest.mark.unit
async def test_client_integration_instructions_success_transformation_failure_is_structured_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    raw_token = "sk-proj-" + "h" * 40
    checkout = tmp_path / "source checkout"
    env_file = checkout / "docker" / "compose" / ".env"
    home = tmp_path / "home"
    home.mkdir()

    def fail_summary(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError(f"summary rendering leaked {raw_token}")

    monkeypatch.setattr(setup_tools, "_resolve_client_env_file", lambda *_args: env_file)
    monkeypatch.setattr(setup_tools, "_client_home", lambda: home)
    monkeypatch.setattr(setup_tools, "_client_which", lambda _binary: None)
    monkeypatch.setattr(setup_tools, "_client_now", lambda: datetime(2026, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(setup_tools, "_client_env", lambda: {})
    monkeypatch.setattr(setup_tools, "_client_instructions_summary", fail_summary)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool(
        "awf_get_client_integration_instructions",
        {"clients": ["claude"], "source_checkout": str(checkout)},
    )
    payload = _payload(result)
    rendered = _json_text(result)
    expected_command = f"awf setup --client claude --source-checkout '{checkout}'"

    assert result.isError is True
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == SETUP_READINESS_FAILED
    assert payload["command"] == expected_command
    assert payload["summary"] == "could not build client integration instructions"
    assert payload["issues"][0]["details"] == {"error_type": "RuntimeError"}
    assert payload["next_steps"] == [
        f"Fix the reported issue above, then re-run {expected_command}.",
    ]
    assert raw_token not in rendered
