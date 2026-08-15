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
def test_validation_error_message_returns_allowlisted_message_for_custom_validator() -> None:
    """Ensure custom validator error messages with arbitrary values return allowlisted messages."""
    from pydantic import ValidationError

    from awf.profiles.models import ProfileRuntime

    raw_secret = "arbitrary_custom_secret_value_123"
    with pytest.raises(ValidationError) as exc_info:
        ProfileRuntime.model_validate({"toolchains": {"python": [raw_secret]}})

    msg = mcp_server_mod._validation_error_message(exc_info.value)  # noqa: SLF001
    assert raw_secret not in msg
    assert "toolchains" in msg
    assert "invalid toolchain version for 'python'" in msg


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
    assert msg == "secret_field: Value error"
