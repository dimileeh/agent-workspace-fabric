"""Focused tests for awf_start_local_service MCP behavior."""

from __future__ import annotations

from pathlib import Path
from subprocess import CalledProcessError
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from awf.common.audit import REDACTION_MARKER
from awf.common.redaction import REDACTION_MARKER as TEXT_REDACTION_MARKER
from awf.host_setup.rendering import (
    SETUP_READINESS_FAILED,
    START_COMPOSE_ASSETS_MISSING,
    START_HEALTH_TIMEOUT,
    START_PORT_CONFLICT,
)
from awf.host_setup.source_assets import (
    SOURCE_CHECKOUT_ASSETS_STALE,
    SOURCE_CHECKOUT_INVALID,
    SourceCheckoutError,
)
from awf.host_setup.system_checks import SetupCheckError
from awf.mcp.server import build_mcp_server
from awf.service.bootstrap import (
    SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND,
    SERVICE_BOOTSTRAP_STAGE_FAILED,
    SERVICE_BOOTSTRAP_TIMEOUT,
    ServiceBootstrapError,
    ServiceBootstrapResult,
)
from tests.unit.mcp.setup_tools_test_helpers import _json_text, _payload, _settings


@pytest.mark.unit
@pytest.mark.parametrize(
    ("step", "command", "expected"),
    [
        (
            "Re-run awf setup --client claude after correcting config; keep awf setup as reference.",
            "awf start --rebuild",
            "Re-run awf start --rebuild after correcting config; keep awf setup as reference.",
        ),
        (
            "Fix GitHub auth, then re-run awf setup --dry-run --provider github.",
            "awf start --timeout-seconds 42",
            "Fix GitHub auth, then re-run awf start --timeout-seconds 42.",
        ),
        (
            "Re-run awf setup --client without --allow-plain-secrets; keep awf setup as reference.",
            "awf start",
            "Re-run awf start without --allow-plain-secrets; keep awf setup as reference.",
        ),
    ],
)
def test_start_reason_coded_next_step_strips_setup_only_selectors(
    step: str,
    command: str,
    expected: str,
) -> None:
    from awf.mcp import setup_tools

    assert setup_tools._start_reason_coded_next_step(step, command=command) == expected


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
    assert (
        payload["issues"][0]["remediation"]["related_command"]
        == f"awf setup --source-checkout '{checkout}'"
    )
    assert payload["issues"][0]["details"]["missing_markers"] == ["pyproject.toml"]
    assert bootstrap_calls == []


@pytest.mark.unit
async def test_start_local_service_persisted_source_checkout_failure_uses_persisted_remediation_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    checkout = tmp_path / "stale source checkout"
    checkout.mkdir()
    bootstrap_calls: list[bool] = []

    def fail_source_checkout(source_path: Path | None) -> object:
        assert source_path is None
        raise SourceCheckoutError(
            reason_code=SOURCE_CHECKOUT_ASSETS_STALE,
            message="Stored AWF source checkout metadata is no longer valid.",
            root=checkout,
            missing_markers=("uv.lock",),
        )

    async def fake_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        bootstrap_calls.append(True)
        return ServiceBootstrapResult(stages=(), service_status={"status": "ok", "checks": {}})

    monkeypatch.setattr(setup_tools, "_resolve_start_source_checkout", fail_source_checkout)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fake_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_start_local_service", {})
    payload = _payload(result)

    assert result.isError is True
    assert payload["status"] == "failed"
    assert payload["reason_code"] == SOURCE_CHECKOUT_ASSETS_STALE
    assert payload["command"] == "awf start"
    assert (
        payload["issues"][0]["remediation"]["related_command"]
        == f"awf setup --source-checkout '{checkout}'"
    )
    assert payload["issues"][0]["details"]["root"] == str(checkout)
    assert payload["issues"][0]["details"]["missing_markers"] == ["uv.lock"]
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
    assert payload["issues"][0]["remediation"]["related_command"] == "awf service status"
    assert payload["issues"][0]["details"]["env_migration"] == migration_payload
    assert raw_token not in rendered
    assert REDACTION_MARKER in rendered


@pytest.mark.unit
async def test_start_local_service_redacts_selected_start_environment_secret_from_bootstrap_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    settings_secret = "opaque-selected-settings-secret"
    env_secret = "opaque-selected-env-secret"
    inputs = SimpleNamespace(
        settings=SimpleNamespace(
            api_base_url="http://localhost:8000",
            console_url=None,
            api_token=settings_secret,
            github_token=None,
        ),
        compose_file=tmp_path / "compose.yml",
        compose_env_file=tmp_path / ".env",
        asset_root=tmp_path,
        service_env={"CUSTOM_API_TOKEN": env_secret},
        env_migration=None,
    )

    async def fail_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        raise ServiceBootstrapError(
            reason_code=SERVICE_BOOTSTRAP_TIMEOUT,
            message="timed out",
            stderr=f"compose echoed {settings_secret} and {env_secret}",
        )

    monkeypatch.setattr(setup_tools, "_resolve_start_source_checkout", lambda _path: object())
    monkeypatch.setattr(setup_tools, "_resolve_start_bootstrap_inputs", lambda _verified: inputs)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fail_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_start_local_service", {"source_checkout": str(tmp_path)})
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["status"] == "failed"
    assert settings_secret not in rendered
    assert env_secret not in rendered
    assert (
        payload["issues"][0]["details"]["bootstrap"]["stderr"]
        == f"compose echoed {TEXT_REDACTION_MARKER} and {TEXT_REDACTION_MARKER}"
    )


@pytest.mark.unit
async def test_start_local_service_redacts_path_failure_migration_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    settings_secret = "opaque-path-settings-secret"
    env_secret = "opaque-path-env-secret"

    class EnvMigration:
        def to_dict(self) -> dict[str, str]:
            return {
                "status": "migrated",
                "diagnostic": f"moved {settings_secret} and {env_secret}",
            }

    inputs = SimpleNamespace(
        settings=SimpleNamespace(
            api_base_url="http://localhost:8000",
            console_url=None,
            api_token=settings_secret,
            github_token=None,
        ),
        compose_file=tmp_path / "compose.yml",
        compose_env_file=tmp_path / ".env",
        asset_root=tmp_path,
        service_env={"CUSTOM_API_TOKEN": env_secret},
        env_migration=EnvMigration(),
    )

    async def fail_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        raise RuntimeError("bootstrap path failed")

    monkeypatch.setattr(setup_tools, "_resolve_start_source_checkout", lambda _path: object())
    monkeypatch.setattr(setup_tools, "_resolve_start_bootstrap_inputs", lambda _verified: inputs)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fail_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_start_local_service", {"source_checkout": str(tmp_path)})
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is True
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "START_BOOTSTRAP_EXECUTION_FAILED"
    assert settings_secret not in rendered
    assert env_secret not in rendered
    assert payload["issues"][0]["details"]["env_migration"]["diagnostic"] == (
        f"moved {TEXT_REDACTION_MARKER} and {TEXT_REDACTION_MARKER}"
    )


@pytest.mark.unit
async def test_start_local_service_redacts_selected_start_environment_secret_from_success_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    settings_secret = "opaque-selected-settings-secret"
    env_secret = "opaque-selected-env-secret"
    inputs = SimpleNamespace(
        settings=SimpleNamespace(
            api_base_url="http://localhost:8000",
            console_url=None,
            api_token=settings_secret,
            github_token=None,
        ),
        compose_file=tmp_path / "compose.yml",
        compose_env_file=tmp_path / ".env",
        asset_root=tmp_path,
        service_env={"CUSTOM_API_TOKEN": env_secret},
        env_migration=None,
    )

    async def fake_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        return ServiceBootstrapResult(
            stages=(),
            service_status={
                "status": f"ready {settings_secret} {env_secret}",
                "checks": {},
            },
        )

    monkeypatch.setattr(setup_tools, "_resolve_start_source_checkout", lambda _path: object())
    monkeypatch.setattr(setup_tools, "_resolve_start_bootstrap_inputs", lambda _verified: inputs)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fake_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_start_local_service", {"source_checkout": str(tmp_path)})
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is False
    assert payload["status"] == "success"
    assert settings_secret not in rendered
    assert env_secret not in rendered
    assert payload["details"]["health"] == (
        f"ready {TEXT_REDACTION_MARKER} {TEXT_REDACTION_MARKER}"
    )


@pytest.mark.unit
async def test_start_local_service_redacts_future_settings_secret_field_from_success_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    settings_secret = "opaque-future-settings-secret"

    class FutureSettings:
        model_fields = {
            "api_base_url": object(),
            "console_url": object(),
            "registry_token": object(),
        }

        api_base_url = "http://localhost:8000"
        console_url = None
        registry_token = settings_secret

    inputs = SimpleNamespace(
        settings=FutureSettings(),
        compose_file=tmp_path / "compose.yml",
        compose_env_file=tmp_path / ".env",
        asset_root=tmp_path,
        service_env={},
        env_migration=None,
    )

    async def fake_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        return ServiceBootstrapResult(
            stages=(),
            service_status={
                "status": f"ready {settings_secret}",
                "checks": {},
            },
        )

    monkeypatch.setattr(setup_tools, "_resolve_start_source_checkout", lambda _path: object())
    monkeypatch.setattr(setup_tools, "_resolve_start_bootstrap_inputs", lambda _verified: inputs)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fake_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_start_local_service", {"source_checkout": str(tmp_path)})
    payload = _payload(result)
    rendered = _json_text(result)

    assert result.isError is False
    assert payload["status"] == "success"
    assert settings_secret not in rendered
    assert payload["details"]["health"] == f"ready {TEXT_REDACTION_MARKER}"


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
async def test_start_local_service_rewrites_reason_coded_bootstrap_remediation_command(
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
            reason_code=SERVICE_BOOTSTRAP_STAGE_FAILED,
            message="compose up failed",
            stage="compose_up",
            stderr="Bind for 0.0.0.0:8000 failed: port is already allocated",
        )

    monkeypatch.setattr(setup_tools, "_resolve_start_source_checkout", lambda _path: object())
    monkeypatch.setattr(setup_tools, "_resolve_start_bootstrap_inputs", lambda _verified: inputs)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fail_bootstrap)
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
    expected_command = f"awf start --rebuild --timeout-seconds 42.5 --source-checkout '{checkout}'"

    assert result.isError is True
    assert payload["status"] == "failed"
    assert payload["reason_code"] == START_PORT_CONFLICT
    assert payload["command"] == expected_command
    assert payload["issues"][0]["remediation"]["related_command"] == expected_command


@pytest.mark.unit
async def test_start_local_service_preserves_asset_missing_source_checkout_remediation_without_source_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    inputs = SimpleNamespace(
        settings=SimpleNamespace(api_base_url="http://localhost:8000", console_url=None),
        compose_file=tmp_path / "missing-compose.yml",
        compose_env_file=None,
        asset_root=None,
        service_env={},
        env_migration=None,
    )

    async def fail_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        raise ServiceBootstrapError(
            reason_code=SERVICE_BOOTSTRAP_ASSETS_NOT_FOUND,
            message="compose assets missing",
        )

    monkeypatch.setattr(setup_tools, "_resolve_start_source_checkout", lambda _path: None)
    monkeypatch.setattr(setup_tools, "_resolve_start_bootstrap_inputs", lambda _verified: inputs)
    monkeypatch.setattr(setup_tools, "run_service_bootstrap", fail_bootstrap)
    mcp = build_mcp_server(service=MagicMock(), settings=_settings(tmp_path))

    result = await mcp.call_tool("awf_start_local_service", {"rebuild": True})
    payload = _payload(result)

    assert result.isError is True
    assert payload["status"] == "failed"
    assert payload["reason_code"] == START_COMPOSE_ASSETS_MISSING
    assert payload["command"] == "awf start --rebuild"
    assert payload["issues"][0]["remediation"]["related_command"] == (
        "awf start --source-checkout ."
    )


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
async def test_start_local_service_unexpected_bootstrap_exception_is_structured_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    raw_token = "sk-proj-" + "j" * 40
    settings_secret = "opaque-unexpected-settings-secret"
    env_secret = "opaque-unexpected-env-secret"

    class EnvMigration:
        def to_dict(self) -> dict[str, str]:
            return {
                "status": "migrated",
                "diagnostic": f"moved {settings_secret} and {env_secret}",
            }

    inputs = SimpleNamespace(
        settings=SimpleNamespace(
            api_base_url="http://localhost:8000",
            console_url=None,
            api_token=settings_secret,
            github_token=None,
        ),
        compose_file=tmp_path / "compose.yml",
        compose_env_file=None,
        asset_root=None,
        service_env={"CUSTOM_API_TOKEN": env_secret},
        env_migration=EnvMigration(),
    )

    async def fail_bootstrap(*_args: Any, **_kwargs: Any) -> ServiceBootstrapResult:
        raise KeyError(raw_token)

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
    assert payload["issues"][0]["details"]["bootstrap"]["stderr"] == "error_type=KeyError"
    assert payload["issues"][0]["details"]["env_migration"]["diagnostic"] == (
        f"moved {TEXT_REDACTION_MARKER} and {TEXT_REDACTION_MARKER}"
    )
    assert raw_token not in rendered
    assert settings_secret not in rendered
    assert env_secret not in rendered


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
async def test_start_local_service_unexpected_input_resolution_failure_is_structured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.mcp import setup_tools

    raw_token = "sk-proj-" + "i" * 40

    def fail_bootstrap_inputs(_verified: object) -> SimpleNamespace:
        raise KeyError(raw_token)

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
    assert payload["issues"][0]["details"] == {"error_type": "KeyError"}
    assert bootstrap_calls == []
    assert raw_token not in rendered


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
    assert payload["next_steps"] == [
        "Fix the reported issue above, then re-run awf start.",
    ]
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

    checkout = tmp_path / "model\\1"
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
    assert payload["next_steps"] == [
        f"Fix the reported issue above, then re-run awf start --source-checkout '{checkout}'.",
    ]
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
