"""Typer CLI tests — each command is invoked with CliRunner + mocked httpx.

We mock ``httpx.request`` rather than spinning an API server because the
CLI's job is to produce the right HTTP call and format the response.
End-to-end testing against a real server lives in tests/e2e/ (future).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from awf.cli.main import app

_runner = CliRunner()


def _mock_response(*, status_code: int = 202, payload: object = None, text: str = "") -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.content = b"ok" if payload is not None or text else b""
    response.text = text or (json.dumps(payload) if payload is not None else "")
    response.json.return_value = payload
    return response


class TestWorkspaceCreate:
    @pytest.mark.unit
    def test_emits_json_body_to_post(self) -> None:
        response = _mock_response(status_code=202, payload={"workspace_id": "ws_abc"})
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "create",
                    "--repo",
                    "git@github.com:x/y.git",
                    "--title",
                    "Add docs",
                    "--prompt",
                    "Add docstrings everywhere.",
                    "--test",
                    "pytest -q",
                    "--test",
                    "ruff check .",
                    "--with-db",
                ],
            )

        assert result.exit_code == 0
        assert "ws_abc" in result.stdout

        args, kwargs = mock.call_args
        assert args[0] == "POST"
        assert args[1].endswith("/v2/workspaces")
        body = kwargs["json"]
        assert body["repo"]["url"] == "git@github.com:x/y.git"
        assert body["task"]["title"] == "Add docs"
        assert body["validation"]["commands"] == ["pytest -q", "ruff check ."]
        assert body["workspace"]["profile_ref"] == "aira"
        assert body["task"]["agent"] == "codex"
        assert body["task"]["auto_merge"] is True
        assert body["task"]["initial_review_grace_period_seconds"] is None

    @pytest.mark.unit
    def test_monitor_policy_flags_are_sent(self) -> None:
        response = _mock_response(status_code=202, payload={"workspace_id": "ws_manual"})
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "create",
                    "--repo",
                    "git@github.com:x/y.git",
                    "--title",
                    "Manual merge",
                    "--prompt",
                    "Open a PR and wait for human merge.",
                    "--no-auto-merge",
                    "--initial-review-grace-period-seconds",
                    "0",
                ],
            )

        assert result.exit_code == 0

        body = mock.call_args.kwargs["json"]
        assert body["task"]["auto_merge"] is False
        assert body["task"]["initial_review_grace_period_seconds"] == 0

    @pytest.mark.unit
    def test_initial_review_grace_period_rejects_values_above_one_day(self) -> None:
        with patch("awf.cli.main.httpx.request") as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "create",
                    "--repo",
                    "git@github.com:x/y.git",
                    "--title",
                    "Manual merge",
                    "--prompt",
                    "Open a PR and wait for human merge.",
                    "--initial-review-grace-period-seconds",
                    "86401",
                ],
            )

        assert result.exit_code != 0
        mock.assert_not_called()

    @pytest.mark.unit
    def test_idempotency_key_forwarded_as_header(self) -> None:
        response = _mock_response(status_code=202, payload={"workspace_id": "ws_idem"})
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "create",
                    "--repo",
                    "git@x:y.git",
                    "--title",
                    "t",
                    "--prompt",
                    "p",
                    "--idempotency-key",
                    "same-key-42",
                ],
            )

        assert result.exit_code == 0
        _, kwargs = mock.call_args
        assert kwargs["headers"]["Idempotency-Key"] == "same-key-42"

    @pytest.mark.unit
    def test_non_2xx_returns_nonzero_exit(self) -> None:
        response = _mock_response(
            status_code=422,
            payload={"detail": "invalid"},
            text='{"detail": "invalid"}',
        )
        with patch("awf.cli.main.httpx.request", return_value=response):
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "create",
                    "--repo",
                    "git@x:y.git",
                    "--title",
                    "t",
                    "--prompt",
                    "p",
                ],
            )
        assert result.exit_code == 1


class TestWorkspaceShow:
    @pytest.mark.unit
    def test_fetches_by_id_and_prints(self) -> None:
        response = _mock_response(
            status_code=200,
            payload={"id": "ws_xyz", "status": "ready", "version": 3},
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(app, ["workspace", "show", "ws_xyz"])

        assert result.exit_code == 0
        assert "ws_xyz" in result.stdout
        assert mock.call_args[0] == ("GET", "http://localhost:8000/v1/workspaces/ws_xyz")

    @pytest.mark.unit
    def test_pretty_format_emits_sorted_keys(self) -> None:
        response = _mock_response(
            status_code=200,
            payload={"status": "ready", "id": "ws_xyz"},
        )
        with patch("awf.cli.main.httpx.request", return_value=response):
            result = _runner.invoke(
                app,
                ["workspace", "show", "ws_xyz", "--format", "pretty"],
            )

        assert result.exit_code == 0
        # "id:" line appears before "status:" line because of sorted keys.
        id_pos = result.stdout.index("id:")
        status_pos = result.stdout.index("status:")
        assert id_pos < status_pos


class TestWorkspaceList:
    @pytest.mark.unit
    def test_passes_limit_as_query_param(self) -> None:
        response = _mock_response(status_code=200, payload=[])
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(app, ["workspace", "list", "--limit", "7"])

        assert result.exit_code == 0
        kwargs = mock.call_args.kwargs
        assert kwargs["params"] == {"limit": 7}

    @pytest.mark.unit
    def test_pretty_prints_separators_between_items(self) -> None:
        response = _mock_response(
            status_code=200,
            payload=[
                {"id": "ws_1", "status": "ready"},
                {"id": "ws_2", "status": "running"},
            ],
        )
        with patch("awf.cli.main.httpx.request", return_value=response):
            result = _runner.invoke(app, ["workspace", "list", "--format", "pretty"])

        assert result.exit_code == 0
        assert "--- #1 ---" in result.stdout
        assert "--- #2 ---" in result.stdout


class TestBaseUrlResolution:
    @pytest.mark.unit
    def test_cli_flag_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWF_CLI_BASE_URL", "http://from-env:9999")
        response = _mock_response(status_code=200, payload=[])
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            _runner.invoke(app, ["workspace", "list", "--base-url", "http://explicit:1234"])

        assert mock.call_args[0][1].startswith("http://explicit:1234")

    @pytest.mark.unit
    def test_env_used_when_no_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWF_CLI_BASE_URL", "http://from-env:9999")
        response = _mock_response(status_code=200, payload=[])
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            _runner.invoke(app, ["workspace", "list"])

        assert mock.call_args[0][1].startswith("http://from-env:9999")


class TestConnectionErrors:
    @pytest.mark.unit
    def test_request_error_exits_with_code_2(self) -> None:
        with patch(
            "awf.cli.main.httpx.request",
            side_effect=httpx.ConnectError("refused"),
        ):
            result = _runner.invoke(app, ["workspace", "list"])

        assert result.exit_code == 2
        assert "could not reach" in result.stderr
