"""Workspace retry CLI tests split out for the first-party line limit."""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from awf.cli import common as cli_main
from awf.cli.main import app

_runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_cli_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AWF_BASE_URL", "AWF_CLI_BASE_URL", "AWF_API_HOST_PORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(cli_main, "local_service_environ", lambda _environ: {})


def _mock_response(*, status_code: int = 202, payload: object = None) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.content = b"ok" if payload is not None else b""
    response.text = json.dumps(payload) if payload is not None else ""
    response.json.return_value = payload
    return response


class TestWorkspaceRetry:
    """Workspace retry command tests."""

    @pytest.mark.unit
    def test_posts_retry_request_and_prints_new_workspace(self) -> None:
        """Post a retry request and print the replacement workspace id."""
        response = _mock_response(
            status_code=202,
            payload={
                "source_workspace_id": "ws_old",
                "new_workspace_id": "ws_new",
                "operation_id": "op_retry",
                "status": "requested",
                "attempt_number": 2,
            },
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(app, ["workspace", "retry", "ws_old"])

        assert result.exit_code == 0
        assert "ws_new" in result.stdout
        assert mock.call_args[0] == (
            "POST",
            "http://localhost:8000/v1/workspaces/ws_old/retry",
        )
        generated_key = mock.call_args.kwargs["headers"]["Idempotency-Key"]
        assert re.fullmatch(r"awf-cli-retry-[0-9a-f]{32}", generated_key)
        assert f"Generated Idempotency-Key: {generated_key}" in result.stderr

    @pytest.mark.unit
    def test_retry_preserves_explicit_idempotency_key_and_api_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Send explicit retry keys and API tokens without replacement."""
        monkeypatch.setenv("AWF_API_TOKEN", "env-secret")
        response = _mock_response(
            status_code=202,
            payload={
                "source_workspace_id": "ws_old",
                "new_workspace_id": "ws_new",
                "operation_id": "op_retry",
                "status": "requested",
                "attempt_number": 2,
            },
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "retry",
                    "ws_old",
                    "--idempotency-key",
                    "retry-cli-key",
                ],
            )

        assert result.exit_code == 0
        assert mock.call_args.kwargs["headers"] == {
            "Authorization": "Bearer env-secret",
            "Idempotency-Key": "retry-cli-key",
        }
        assert "Generated Idempotency-Key" not in result.stderr

    @pytest.mark.unit
    def test_retry_provider_readiness_override_flag_is_sent(self) -> None:
        """Send provider readiness override query parameters on retry."""
        response = _mock_response(
            status_code=202,
            payload={
                "source_workspace_id": "ws_old",
                "new_workspace_id": "ws_new",
                "operation_id": "op_retry",
                "status": "requested",
                "attempt_number": 2,
            },
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "retry",
                    "ws_old",
                    "--provider-readiness-override",
                    "--provider-readiness-override-reason",
                    "operator verified auth",
                ],
            )

        assert result.exit_code == 0
        assert mock.call_args.kwargs["params"] == {
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "operator verified auth",
        }

    @pytest.mark.unit
    def test_retry_blocked_provider_readiness_prints_structured_error(self) -> None:
        """Print structured provider readiness errors from retry responses."""
        response = _mock_response(
            status_code=409,
            payload={
                "error_code": "PROVIDER_READINESS_PRECHECK_FAILED",
                "message": "Selected provider readiness blocked workspace launch.",
                "detail": {
                    "provider_readiness_preflight": {
                        "provider": "codex",
                        "model": "gpt-5.5",
                        "auth_status": "fail",
                        "auth_source": "not_observed",
                    }
                },
            },
        )
        with patch("awf.cli.main.httpx.request", return_value=response):
            result = _runner.invoke(app, ["workspace", "retry", "ws_old"])

        assert result.exit_code == 1
        assert "PROVIDER_READINESS_PRECHECK_FAILED" in result.stderr
        assert "gpt-5.5" in result.stderr
