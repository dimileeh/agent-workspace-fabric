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


class TestWorkspaceRetry:
    @pytest.mark.unit
    def test_posts_retry_request_and_prints_new_workspace(self) -> None:
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


class TestWorkspaceRemonitor:
    @pytest.mark.unit
    def test_posts_remonitor_request_with_sensitive_control_headers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AWF_API_TOKEN", "env-secret")
        response = _mock_response(
            status_code=200,
            payload={
                "workspace_id": "ws_monitor",
                "operation_id": "op_remonitor",
                "status": "monitoring_pr",
                "message": "workspace PR monitor recovery requested",
            },
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "remonitor",
                    "ws_monitor",
                    "--reason",
                    "operator recovery",
                    "--idempotency-key",
                    "remonitor-cli-key",
                    "--if-match",
                    "7",
                ],
            )

        assert result.exit_code == 0
        assert "op_remonitor" in result.stdout
        assert "env-secret" not in result.stdout
        assert "env-secret" not in result.stderr
        assert mock.call_args[0] == (
            "POST",
            "http://localhost:8000/v1/workspaces/ws_monitor/remonitor",
        )
        assert mock.call_args.kwargs["json"] == {"reason": "operator recovery"}
        assert mock.call_args.kwargs["headers"] == {
            "Authorization": "Bearer env-secret",
            "Idempotency-Key": "remonitor-cli-key",
            "If-Match": "7",
        }

    @pytest.mark.unit
    def test_remonitor_cli_requires_idempotency_key_before_http_call(self) -> None:
        with patch("awf.cli.main.httpx.request") as mock:
            result = _runner.invoke(app, ["workspace", "remonitor", "ws_monitor"])

        assert result.exit_code != 0
        mock.assert_not_called()


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


class TestWorkspaceObservability:
    @pytest.mark.unit
    def test_events_fetches_workspace_timeline_with_filters(self) -> None:
        response = _mock_response(
            status_code=200,
            payload={"items": [{"event_type": "workspace.created", "workspace_id": "ws_obs"}]},
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "events",
                    "ws_obs",
                    "--limit",
                    "12",
                    "--event-type",
                    "workspace.created",
                ],
            )

        assert result.exit_code == 0
        assert "workspace.created" in result.stdout
        assert mock.call_args[0] == ("GET", "http://localhost:8000/v1/workspaces/ws_obs/events")
        assert mock.call_args.kwargs["params"] == {
            "limit": 12,
            "event_type": "workspace.created",
        }

    @pytest.mark.unit
    def test_runtime_fetches_without_token_header_when_unset(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("AWF_API_TOKEN", raising=False)
        response = _mock_response(
            status_code=200,
            payload={"workspace_id": "ws_obs", "stack_state": "running", "services": []},
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(app, ["workspace", "runtime", "ws_obs"])

        assert result.exit_code == 0
        assert "running" in result.stdout
        headers = mock.call_args.kwargs.get("headers", {})
        assert "Authorization" not in headers

    @pytest.mark.unit
    def test_operations_fetches_workspace_operations_with_limit(self) -> None:
        response = _mock_response(
            status_code=200,
            payload={"items": [{"id": "op_1", "type": "validate", "status": "succeeded"}]},
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(app, ["workspace", "operations", "ws_obs", "--limit", "5"])

        assert result.exit_code == 0
        assert "validate" in result.stdout
        assert mock.call_args[0] == (
            "GET",
            "http://localhost:8000/v1/workspaces/ws_obs/operations",
        )
        assert mock.call_args.kwargs["params"] == {"limit": 5}

    @pytest.mark.unit
    def test_operations_forwards_status_and_type_filters(self) -> None:
        response = _mock_response(
            status_code=200,
            payload={"items": [{"id": "op_1", "type": "validate", "status": "succeeded"}]},
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "operations",
                    "ws_obs",
                    "--status",
                    "succeeded",
                    "--type",
                    "validate",
                    "--limit",
                    "5",
                ],
            )

        assert result.exit_code == 0
        assert "validate" in result.stdout
        assert mock.call_args.kwargs["params"] == {
            "limit": 5,
            "status": "succeeded",
            "type": "validate",
        }

    @pytest.mark.unit
    def test_logs_injects_env_api_token_without_printing_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AWF_API_TOKEN", "env-secret")
        response = _mock_response(
            status_code=200,
            payload={"items": [{"stream_id": "agent.stdout", "size_bytes": 42}]},
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(app, ["workspace", "logs", "ws_obs"])

        assert result.exit_code == 0
        assert "agent.stdout" in result.stdout
        assert "env-secret" not in result.stdout
        assert "env-secret" not in result.stderr
        assert mock.call_args[0] == ("GET", "http://localhost:8000/v1/workspaces/ws_obs/logs")
        assert mock.call_args.kwargs["headers"] == {"Authorization": "Bearer env-secret"}

    @pytest.mark.unit
    def test_log_reads_stream_with_cli_token_override(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AWF_API_TOKEN", "env-secret")
        response = _mock_response(
            status_code=200,
            payload={"stream_id": "agent.stdout", "offset": 64, "data": "tail"},
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "log",
                    "ws_obs",
                    "agent.stdout",
                    "--offset",
                    "64",
                    "--limit-bytes",
                    "1024",
                    "--api-token",
                    "cli-secret",
                ],
            )

        assert result.exit_code == 0
        assert "tail" in result.stdout
        assert "cli-secret" not in result.stdout
        assert "cli-secret" not in result.stderr
        assert mock.call_args[0] == (
            "GET",
            "http://localhost:8000/v1/workspaces/ws_obs/logs/agent.stdout",
        )
        assert mock.call_args.kwargs["params"] == {"offset": 64, "limit_bytes": 1024}
        assert mock.call_args.kwargs["headers"] == {"Authorization": "Bearer cli-secret"}

    @pytest.mark.unit
    def test_empty_cli_token_suppresses_env_api_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AWF_API_TOKEN", "env-secret")
        response = _mock_response(
            status_code=200,
            payload={"stream_id": "agent.stdout", "offset": 0, "data": "tail"},
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "log",
                    "ws_obs",
                    "agent.stdout",
                    "--api-token",
                    "",
                ],
            )

        assert result.exit_code == 0
        assert mock.call_args.kwargs["headers"] == {}

    @pytest.mark.unit
    def test_log_encodes_stream_id_path_segment(self) -> None:
        response = _mock_response(
            status_code=200,
            payload={
                "stream_id": "namespace/agent.stdout?tail#chunk",
                "offset": 0,
                "data": "tail",
            },
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "log",
                    "ws_obs",
                    "namespace/agent.stdout?tail#chunk",
                ],
            )

        assert result.exit_code == 0
        assert mock.call_args[0] == (
            "GET",
            "http://localhost:8000/v1/workspaces/ws_obs/logs/namespace%2Fagent.stdout%3Ftail%23chunk",
        )

    @pytest.mark.unit
    def test_non_json_upstream_errors_are_printed_as_text(self) -> None:
        response = _mock_response(status_code=502, text="upstream failed")
        response.json.side_effect = ValueError("not json")
        with patch("awf.cli.main.httpx.request", return_value=response):
            result = _runner.invoke(app, ["workspace", "operations", "ws_obs"])

        assert result.exit_code == 1
        assert "upstream failed" in result.stderr


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


class TestServiceStatusOrphanReporting:
    @pytest.mark.unit
    def test_pretty_output_surfaces_orphan_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from awf.service import config as config_mod
        from awf.service import status as status_mod

        settings = object()
        monkeypatch.setattr(config_mod, "resolve_service_settings", lambda: settings)

        async def _collect(received: object, **_kwargs: object) -> dict[str, object]:
            assert received is settings
            return {
                "service": "awf",
                "status": "fail",
                "checks": {
                    "orphan_workspaces": {
                        "ok": False,
                        "status": "fail",
                        "reason": "ORPHANS_PRESENT",
                        "orphan_count": 2,
                        "active_count": 1,
                        "examples": [
                            {
                                "workspace_id": "ws_dead",
                                "compose_project": "awf_ws_dead",
                                "classification": "terminal",
                                "reason": "WORKSPACE_TERMINAL",
                            },
                            {
                                "workspace_id": "ws_ghost",
                                "compose_project": "awf-ws_ghost",
                                "classification": "missing",
                                "reason": "WORKSPACE_MISSING",
                            },
                        ],
                        "action": (
                            "Run docker compose -p <project> down -v --remove-orphans"
                        ),
                    }
                },
            }

        monkeypatch.setattr(status_mod, "collect_service_status", _collect)

        result = _runner.invoke(app, ["service", "status", "--format", "pretty"])

        assert result.exit_code == 1, result.output
        assert "checks.orphan_workspaces.orphan_count: 2" in result.stdout
        assert "checks.orphan_workspaces.active_count: 1" in result.stdout
        assert "ORPHANS_PRESENT" in result.stdout
        assert "ws_dead" in result.stdout
        assert "ws_ghost" in result.stdout
