"""MCP server redaction helper tests."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

import pytest

from awf.common.config import Settings
from awf.mcp import server as mcp_server_mod
from awf.service import config as service_config


@pytest.mark.unit
def test_redact_exact_secret_bytes_includes_service_api_token_without_extra_secret(
    tmp_path: Path,
) -> None:
    """Redact the service API token even when extra secrets are incomplete."""
    service_api_token = "service-api-token-123456"
    settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=None)
    service_settings = service_config.resolve_service_settings(
        settings,
        environ={"AWF_API_TOKEN": service_api_token},
    )
    content = f"prefix {service_api_token} suffix".encode()

    redacted = mcp_server_mod._redact_exact_secret_bytes(  # noqa: SLF001
        content,
        settings,
        service_settings,
        extra_secrets=(),
    )

    assert service_api_token.encode() not in redacted
    assert redacted == b"prefix <redacted> suffix"


@pytest.mark.unit
def test_redact_exact_secret_bytes_merges_overlapping_configured_secrets(
    tmp_path: Path,
) -> None:
    """Redact overlapping configured byte secrets as one marker."""
    settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token="abcdef")
    service_settings = service_config.resolve_service_settings(settings, environ={})

    redacted = mcp_server_mod._redact_exact_secret_bytes(  # noqa: SLF001
        b"prefix abcdefgh suffix",
        settings,
        service_settings,
        extra_secrets=("cdefgh",),
    )

    assert redacted == b"prefix <redacted> suffix"


@pytest.mark.unit
def test_redact_exact_secret_bytes_preserves_content_without_secret_matches(
    tmp_path: Path,
) -> None:
    """Leave empty content and nonmatching byte content unchanged."""
    settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=None)
    service_settings = service_config.resolve_service_settings(settings, environ={})

    assert (
        mcp_server_mod._redact_exact_secret_bytes(  # noqa: SLF001
            b"",
            settings,
            service_settings,
            extra_secrets=("configured-secret",),
        )
        == b""
    )
    assert (
        mcp_server_mod._redact_exact_secret_bytes(  # noqa: SLF001
            b"public content",
            settings,
            service_settings,
            extra_secrets=("configured-secret",),
        )
        == b"public content"
    )


@pytest.mark.unit
def test_redact_exact_secret_bytes_delegates_to_common_byte_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keep MCP exact byte redaction on the shared redaction implementation."""
    settings_api_token = "settings-api-token-123456"
    settings_github_token = "settings-github-token-123456"
    service_api_token = "service-api-token-123456"
    service_github_token = "service-github-token-123456"
    extra_secret = "extra-token-123456"
    settings = Settings(
        _env_file=None,
        work_dir=str(tmp_path),
        api_token=settings_api_token,
        github_token=settings_github_token,
    )
    service_settings = replace(
        service_config.resolve_service_settings(
            settings,
            environ={
                "AWF_API_TOKEN": service_api_token,
                "AWF_GITHUB_TOKEN": service_github_token,
            },
        ),
        api_token=service_api_token,
        github_token=service_github_token,
    )
    calls: list[tuple[bytes, tuple[str, ...]]] = []

    def fake_redact_exact_secret_bytes(
        content: bytes,
        *,
        extra_secrets: Iterable[str],
    ) -> bytes:
        """Record the MCP delegate call and return a recognizable payload."""
        calls.append((content, tuple(extra_secrets)))
        return b"common-redacted"

    monkeypatch.setattr(
        mcp_server_mod,
        "redact_exact_secret_bytes",
        fake_redact_exact_secret_bytes,
    )

    redacted = mcp_server_mod._redact_exact_secret_bytes(  # noqa: SLF001
        b"raw content",
        settings,
        service_settings,
        extra_secrets=(extra_secret,),
    )

    assert redacted == b"common-redacted"
    assert calls and calls[0][0] == b"raw content"
    assert set(calls[0][1]) == {
        settings_api_token,
        settings_github_token,
        service_api_token,
        service_github_token,
        extra_secret,
    }


@pytest.mark.unit
def test_validation_error_message_strips_dynamic_toolchain_keys_and_values() -> None:
    """Ensure custom validator error messages for toolchains strip dynamic keys and secrets."""
    from pydantic import ValidationError

    from awf.profiles.models import ProfileRuntime

    raw_secret_key = "plainsecret48729"
    raw_secret_val = "arbitrary_custom_secret_value_123"

    # Test invalid version value
    with pytest.raises(ValidationError) as exc_info:
        ProfileRuntime.model_validate({"toolchains": {raw_secret_key: [raw_secret_val]}})

    msg = mcp_server_mod._validation_error_message(exc_info.value)  # noqa: SLF001
    assert raw_secret_key not in msg
    assert raw_secret_val not in msg
    assert msg == "toolchains: Value error"

    # Test wrong value type
    with pytest.raises(ValidationError) as exc_info2:
        ProfileRuntime.model_validate({"toolchains": {raw_secret_key: "bad"}})

    msg2 = mcp_server_mod._validation_error_message(exc_info2.value)  # noqa: SLF001
    assert raw_secret_key not in msg2
    assert "bad" not in msg2
    assert msg2 == "toolchains: Value error"


@pytest.mark.unit
def test_validation_error_message_replaces_unallowlisted_custom_msg_with_generic() -> None:
    """Ensure non-allowlisted custom validator msg is replaced with generic Value error."""

    class FakeError(Exception):
        pass

    class DummyValidationError:
        def errors(
            self, *, include_input: bool = True, include_url: bool = True
        ) -> list[dict[str, object]]:
            return [
                {
                    "type": "value_error",
                    "loc": ("secret_field",),
                    "msg": "Value error, custom message containing sensitive_data_xyz",
                }
            ]

    msg = mcp_server_mod._validation_error_message(DummyValidationError())  # type: ignore[arg-type] # noqa: SLF001
    assert "sensitive_data_xyz" not in msg
    assert msg == "<key>: Value error"


@pytest.mark.unit
def test_validation_error_message_sanitizes_dynamic_mapping_key_locations() -> None:
    """Ensure dynamic dict key names in runtime.environment are sanitized in validation locations."""
    from pydantic import ValidationError

    from awf.profiles.models import ProfileRuntime

    raw_secret_key = "plainsecret48729"

    with pytest.raises(ValidationError) as exc_info:
        ProfileRuntime.model_validate({"environment": {raw_secret_key: 123}})

    msg = mcp_server_mod._validation_error_message(exc_info.value)  # noqa: SLF001
    assert raw_secret_key not in msg
    assert "123" not in msg
    assert msg == "environment.<key>: Input should be a valid string"


@pytest.mark.unit
def test_validation_error_message_handles_empty_errors_list() -> None:
    """Return 'Validation error' when ValidationError errors() returns an empty list."""

    class DummyEmptyValidationError:
        def errors(
            self, *, include_input: bool = True, include_url: bool = True
        ) -> list[dict[str, object]]:
            return []

    msg = mcp_server_mod._validation_error_message(DummyEmptyValidationError())  # type: ignore[arg-type] # noqa: SLF001
    assert msg == "Validation error"


@pytest.mark.unit
def test_validation_error_message_handles_empty_location() -> None:
    """Format message without location prefix when loc is empty."""

    class DummyNoLocValidationError:
        def errors(
            self, *, include_input: bool = True, include_url: bool = True
        ) -> list[dict[str, object]]:
            return [
                {
                    "type": "value_error",
                    "loc": (),
                    "msg": "Input should be a valid string",
                }
            ]

    msg = mcp_server_mod._validation_error_message(DummyNoLocValidationError())  # type: ignore[arg-type] # noqa: SLF001
    assert msg == "Value error"


@pytest.mark.unit
def test_resolve_mcp_compose_env_secret_file_none() -> None:
    """Return None when compose_env_file is None."""
    assert mcp_server_mod._resolve_mcp_compose_env_secret_file(None) is None  # noqa: SLF001


@pytest.mark.unit
def test_contains_secret_bytes_env_and_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detect known secret env keys and regex token patterns in content."""
    settings = Settings(_env_file=None, work_dir=str(tmp_path), api_token=None)
    service_settings = service_config.resolve_service_settings(settings, environ={})

    monkeypatch.setenv("GITHUB_TOKEN", "my_secret_github_token_123")
    content_env = b"data my_secret_github_token_123 data"
    assert mcp_server_mod._contains_secret_bytes(  # noqa: SLF001
        content_env,
        settings,
        service_settings=service_settings,
        extra_secrets=(),
    )

    token_content = b"token_prefix_ghp_123456789012345678901234567890123456_suffix"
    assert mcp_server_mod._contains_secret_bytes(  # noqa: SLF001
        token_content,
        settings,
        service_settings=service_settings,
        extra_secrets=(),
    )


@pytest.mark.unit
def test_resolve_settings_default_fallback() -> None:
    """Resolve default settings when None is passed to _resolve_settings."""
    from awf.mcp.control_tools import _resolve_settings

    resolved = _resolve_settings(None)
    assert resolved is not None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_control_tools_workspace_control_error_handling() -> None:
    """Handle WorkspaceControlError in cancel_workspace gracefully."""
    from unittest.mock import AsyncMock, MagicMock

    from mcp.types import CallToolResult

    from awf.mcp.server import build_mcp_server
    from awf.service.controls import WorkspaceControlError

    service = MagicMock()
    service.cancel_workspace = AsyncMock(
        side_effect=WorkspaceControlError(
            error_code="INVALID_STATE",
            message="Control action failed",
        )
    )

    mcp = build_mcp_server(service=service)

    res = await mcp.call_tool(
        "awf_cancel_workspace",
        {"workspace_id": "ws-123", "reason": "testing", "idempotency_key": "ik-123"},
    )
    assert isinstance(res, CallToolResult)
    assert res.isError is True
