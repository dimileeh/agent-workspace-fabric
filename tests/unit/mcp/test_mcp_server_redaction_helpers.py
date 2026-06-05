"""MCP server redaction helper tests."""

from __future__ import annotations

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
