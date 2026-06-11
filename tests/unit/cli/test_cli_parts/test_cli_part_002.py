"""Typer CLI tests — each command is invoked with CliRunner + mocked httpx.

We mock ``httpx.request`` rather than spinning an API server because the
CLI's job is to produce the right HTTP call and format the response.
End-to-end testing against a real server lives in tests/e2e/ (future).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import click
import httpx
import pytest
from typer.testing import CliRunner

import awf.cli.common as cli_common
from awf.cli import main as cli_main
from awf.cli.main import app

_runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_cli_local_service_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep developer root `.env` values out of mocked CLI HTTP tests."""
    monkeypatch.setattr(cli_common, "local_service_environ", lambda _environ: {})


def _mock_response(*, status_code: int = 202, payload: object = None, text: str = "") -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.content = b"ok" if payload is not None or text else b""
    response.text = text or (json.dumps(payload) if payload is not None else "")
    response.json.return_value = payload
    return response


def _assert_adopt_pr_help_exposes_model_and_effort(stdout: str) -> None:
    """Assert adopt-pr help exposes model and effort flags."""
    visible_help = click.unstyle(stdout)
    assert "--model" in visible_help
    assert "--effort" in visible_help
    assert "--owned-path" in visible_help


def _assert_workspace_create_help_exposes_model_and_effort(stdout: str) -> None:
    """Assert workspace create help exposes model and effort flags."""
    visible_help = click.unstyle(stdout)
    assert "--model" in visible_help
    assert "--effort" in visible_help


def _assert_current_first_path_guidance(stdout: str) -> None:
    visible_help = " ".join(click.unstyle(stdout).split()).lower()
    stale_help = visible_help.replace("`", "")
    assert "current runnable first path" in visible_help
    assert "awf service bootstrap" in visible_help
    assert "awf init <path>" in visible_help
    assert "recommended first path is awf setup" not in stale_help
    assert "awf setup, then awf start" not in stale_help


def _assert_control_headers(
    headers: dict[str, str],
    *,
    idempotency_key: str,
    if_match: str,
) -> None:
    assert headers["Idempotency-Key"] == idempotency_key
    assert headers["If-Match"] == if_match


class TestWorkspaceAdoptPr:
    @pytest.mark.unit
    def test_posts_adoption_request_with_api_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWF_API_TOKEN", "env-secret")
        response = _mock_response(
            status_code=202,
            payload={
                "workspace_id": "ws_adopt",
                "status": "requested",
                "repo_slug": "dimileeh/aira-web",
                "pr_number": 277,
            },
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "adopt-pr",
                    "--repo",
                    "dimileeh/aira-web",
                    "--pr",
                    "277",
                    "--agent",
                    "codex",
                    "--no-auto-merge",
                    "--initial-review-grace-period-seconds",
                    "0",
                    "--reason",
                    "recover existing PR",
                ],
            )

        assert result.exit_code == 0
        assert "ws_adopt" in result.stdout
        assert "env-secret" not in result.stdout
        assert "env-secret" not in result.stderr
        assert mock.call_args[0] == (
            "POST",
            "http://localhost:8000/v1/workspaces/adopt-pr",
        )
        assert mock.call_args.kwargs["json"] == {
            "repo_url": None,
            "repo_slug": "dimileeh/aira-web",
            "pr_number": 277,
            "pr_url": None,
            "agent": "codex",
            "profile_ref": "auto",
            "profile": None,
            "auto_merge": False,
            "initial_review_grace_period_seconds": 0,
            "task_title": None,
            "task_prompt": None,
            "reason": "recover existing PR",
        }
        assert mock.call_args.kwargs["headers"] == {"Authorization": "Bearer env-secret"}

    @pytest.mark.unit
    def test_task_tag_is_injected_and_validated(self) -> None:
        """A valid --task-tag populates the adoption body; malformed values fail locally."""
        response = _mock_response(status_code=202, payload={"workspace_id": "ws_adopt"})
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "adopt-pr",
                    "--pr-url",
                    "https://github.com/dimileeh/aira-web/pull/277",
                    "--task-tag",
                    "PROJ-123",
                ],
            )
        assert result.exit_code == 0
        assert mock.call_args.kwargs["json"]["task_tag"] == "PROJ-123"

        with patch("awf.cli.main.httpx.request") as mock_bad:
            bad = _runner.invoke(
                app,
                [
                    "workspace",
                    "adopt-pr",
                    "--pr-url",
                    "https://github.com/dimileeh/aira-web/pull/277",
                    "--task-tag",
                    "bad-tag",
                ],
            )
        assert bad.exit_code != 0
        assert "task tag" in bad.output
        mock_bad.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("base_url",),
        (
            ("http://host:8000",),
            ("http://host:8000/",),
            ("http://host:8000/v1",),
            ("http://host:8000/v1/",),
        ),
    )
    def test_posts_adoption_request_to_normalized_v1_endpoint(self, base_url: str) -> None:
        response = _mock_response(status_code=202, payload={"workspace_id": "ws_adopt"})
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "adopt-pr",
                    "--base-url",
                    base_url,
                    "--repo",
                    "dimileeh/aira-web",
                    "--pr",
                    "277",
                ],
            )

        assert result.exit_code == 0
        assert mock.call_args[0] == ("POST", "http://host:8000/v1/workspaces/adopt-pr")
        assert not hasattr(cli_main, "_CALL_CONTEXT")

    @pytest.mark.unit
    def test_posts_adoption_request_to_normalized_v1_endpoint_from_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("AWF_BASE_URL", raising=False)
        monkeypatch.setattr(
            cli_common,
            "_cli_base_url_deprecation_notice_emitted",
            False,
            raising=False,
        )
        for base_url in ("http://host:8000/v1", "http://host:8000/v1/"):
            monkeypatch.setenv("AWF_CLI_BASE_URL", base_url)
            response = _mock_response(status_code=202, payload={"workspace_id": "ws_adopt"})
            with patch("awf.cli.main.httpx.request", return_value=response) as mock:
                result = _runner.invoke(
                    app,
                    [
                        "workspace",
                        "adopt-pr",
                        "--repo",
                        "dimileeh/aira-web",
                        "--pr",
                        "277",
                    ],
                )

            assert result.exit_code == 0
            assert mock.call_args[0] == ("POST", "http://host:8000/v1/workspaces/adopt-pr")
            assert not hasattr(cli_main, "_CALL_CONTEXT")

    @pytest.mark.unit
    def test_posts_model_and_effort_when_requested(self) -> None:
        response = _mock_response(status_code=202, payload={"workspace_id": "ws_adopt"})
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "adopt-pr",
                    "--repo",
                    "dimileeh/aira-web",
                    "--pr",
                    "277",
                    "--model",
                    "gpt-5.3-codex",
                    "--effort",
                    "high",
                ],
            )

        assert result.exit_code == 0
        body = mock.call_args.kwargs["json"]
        assert body["model"] == "gpt-5.3-codex"
        assert body["effort"] == "high"

    @pytest.mark.unit
    def test_posts_grok_agent_when_adopting_pr(self) -> None:
        response = _mock_response(status_code=202, payload={"workspace_id": "ws_adopt"})
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "adopt-pr",
                    "--repo",
                    "dimileeh/aira-web",
                    "--pr",
                    "277",
                    "--agent",
                    "grok",
                    "--model",
                    "grok-build",
                ],
            )

        assert result.exit_code == 0
        body = mock.call_args.kwargs["json"]
        assert body["agent"] == "grok"
        assert body["model"] == "grok-build"

    @pytest.mark.unit
    def test_posts_owned_paths_when_requested(self) -> None:
        response = _mock_response(status_code=202, payload={"workspace_id": "ws_adopt"})
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "adopt-pr",
                    "--repo",
                    "dimileeh/aira-web",
                    "--pr",
                    "277",
                    "--owned-path",
                    ".github/workflows/publish.yml",
                    "--owned-path",
                    "pyproject.toml",
                ],
            )

        assert result.exit_code == 0
        body = mock.call_args.kwargs["json"]
        assert body["owned_paths"] == [".github/workflows/publish.yml", "pyproject.toml"]

    @pytest.mark.unit
    def test_posts_model_without_effort_for_server_side_defaulting(self) -> None:
        response = _mock_response(status_code=202, payload={"workspace_id": "ws_adopt"})
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "adopt-pr",
                    "--repo",
                    "dimileeh/aira-web",
                    "--pr",
                    "277",
                    "--model",
                    "gpt-5.3-codex",
                ],
            )

        assert result.exit_code == 0
        body = mock.call_args.kwargs["json"]
        assert body["model"] == "gpt-5.3-codex"
        assert "effort" not in body

    @pytest.mark.unit
    def test_adopt_pr_help_exposes_model_and_effort_flags(self) -> None:
        result = _runner.invoke(app, ["workspace", "adopt-pr", "--help"])

        assert result.exit_code == 0
        _assert_adopt_pr_help_exposes_model_and_effort(result.stdout)

    @pytest.mark.unit
    def test_adopt_pr_help_exposes_model_and_effort_flags_when_color_is_forced(self) -> None:
        result = _runner.invoke(
            app,
            ["workspace", "adopt-pr", "--help"],
            env={
                "TERM": "xterm-256color",
                "FORCE_COLOR": "1",
                "CLICOLOR_FORCE": "1",
                "GITHUB_ACTIONS": "true",
                "CI": "true",
            },
        )

        assert result.exit_code == 0
        assert "\x1b[" in result.stdout
        _assert_adopt_pr_help_exposes_model_and_effort(result.stdout)

    @pytest.mark.unit
    def test_adopt_pr_help_exposes_model_and_effort_flags_when_terminal_is_narrow(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import typer.rich_utils as typer_rich_utils

        monkeypatch.setattr(typer_rich_utils, "MAX_WIDTH", 30)

        result = _runner.invoke(app, ["workspace", "adopt-pr", "--help"])

        assert result.exit_code == 0
        _assert_adopt_pr_help_exposes_model_and_effort(result.stdout)
        assert typer_rich_utils.MAX_WIDTH == 30

    @pytest.mark.unit
    def test_posts_pr_url_without_repo_fields(self) -> None:
        response = _mock_response(status_code=202, payload={"workspace_id": "ws_adopt"})
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "adopt-pr",
                    "--pr-url",
                    "https://github.com/dimileeh/aira-web/pull/277",
                    "--api-token",
                    "cli-secret",
                ],
            )

        assert result.exit_code == 0
        assert mock.call_args.kwargs["json"]["pr_url"] == (
            "https://github.com/dimileeh/aira-web/pull/277"
        )
        assert mock.call_args.kwargs["json"]["repo_slug"] is None
        assert mock.call_args.kwargs["json"]["pr_number"] is None
        assert mock.call_args.kwargs["headers"] == {"Authorization": "Bearer cli-secret"}

    @pytest.mark.unit
    def test_not_found_includes_request_context_and_does_not_emit_tokens(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AWF_API_TOKEN", "api-token-secret")
        response = _mock_response(status_code=404, payload={"message": "Not Found"})
        response.request = httpx.Request(
            "POST",
            "http://localhost:8000/v1/workspaces/adopt-pr",
        )
        with patch("awf.cli.main.httpx.request", return_value=response):
            result = _runner.invoke(
                app,
                ["workspace", "adopt-pr", "--repo", "dimileeh/aira-web", "--pr", "277"],
            )

        assert result.exit_code == 1
        assert "POST http://localhost:8000/v1/workspaces/adopt-pr" in result.stderr
        assert "404" in result.stderr
        assert "Not Found" in result.stderr
        assert "api-token-secret" not in result.stderr
        assert "Authorization" not in result.stderr

    @pytest.mark.unit
    def test_not_found_sanitizes_url_secret_query_params(self) -> None:
        response = _mock_response(status_code=404, payload={"message": "Not Found"})
        base_url = "http://host:8000/v1?access_token=top-secret-token"
        response.request = httpx.Request(
            "POST",
            "http://host:8000/v1/workspaces/adopt-pr?access_token=top-secret-token",
        )
        with patch("awf.cli.main.httpx.request", return_value=response):
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "adopt-pr",
                    "--base-url",
                    base_url,
                    "--repo",
                    "dimileeh/aira-web",
                    "--pr",
                    "277",
                ],
            )

        assert result.exit_code == 1
        assert (
            "POST http://host:8000/v1/workspaces/adopt-pr?access_token=%2A%2A%2A" in result.stderr
        )
        assert "top-secret-token" not in result.stderr

    @pytest.mark.unit
    def test_non_adopt_workspace_http_error_includes_request_context_and_does_not_emit_tokens(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AWF_API_TOKEN", "api-token-secret")
        response = _mock_response(status_code=404, payload={"message": "Not Found"})
        response.request = httpx.Request(
            "GET",
            "http://localhost:8000/v1/workspaces/ws_show",
        )
        with patch("awf.cli.main.httpx.request", return_value=response):
            result = _runner.invoke(app, ["workspace", "show", "ws_show"])

        assert result.exit_code == 1
        assert "GET http://localhost:8000/v1/workspaces/ws_show" in result.stderr
        assert "404" in result.stderr
        assert "Not Found" in result.stderr
        assert "api-token-secret" not in result.stderr
        assert "Authorization" not in result.stderr


class TestWorkspaceList:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("base_url", "expected_url"),
        (
            ("http://host:8000/awf", "http://host:8000/awf/v1/workspaces"),
            ("http://host:8000/awf/v1", "http://host:8000/awf/v1/workspaces"),
        ),
    )
    def test_list_uses_reversed_proxy_prefix_without_v1_duplication(
        self,
        base_url: str,
        expected_url: str,
    ) -> None:
        response = _mock_response(status_code=200, payload=[])
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(app, ["workspace", "list", "--base-url", base_url])

        assert result.exit_code == 0
        assert mock.call_args[0] == ("GET", expected_url)

    @pytest.mark.unit
    def test_passes_limit_as_query_param(self) -> None:
        response = _mock_response(status_code=200, payload=[])
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(app, ["workspace", "list", "--limit", "7"])

        assert result.exit_code == 0
        kwargs = mock.call_args.kwargs
        assert kwargs["params"] == [("limit", 7)]

    @pytest.mark.unit
    def test_injects_env_api_token_without_printing_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AWF_API_TOKEN", "env-secret")
        response = _mock_response(status_code=200, payload=[])
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(app, ["workspace", "list", "--limit", "7"])

        assert result.exit_code == 0
        assert "env-secret" not in result.stdout
        assert "env-secret" not in result.stderr
        assert mock.call_args.kwargs["headers"] == {"Authorization": "Bearer env-secret"}

    @pytest.mark.unit
    def test_api_token_option_overrides_env_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWF_API_TOKEN", "env-secret")
        response = _mock_response(status_code=200, payload=[])
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                ["workspace", "list", "--api-token", "cli-secret"],
            )

        assert result.exit_code == 0
        assert "cli-secret" not in result.stdout
        assert "cli-secret" not in result.stderr
        assert mock.call_args.kwargs["headers"] == {"Authorization": "Bearer cli-secret"}

    @pytest.mark.unit
    def test_forwards_fleet_filters_as_query_params(self) -> None:
        response = _mock_response(status_code=200, payload=[])
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "list",
                    "--status",
                    "ready",
                    "--agent",
                    "cursor",
                    "--repo-url",
                    "git@github.com:example/app.git",
                    "--limit",
                    "9",
                ],
            )

        assert result.exit_code == 0
        kwargs = mock.call_args.kwargs
        assert kwargs["params"] == [
            ("limit", 9),
            ("status", "ready"),
            ("agent", "cursor"),
            ("repo_url", "git@github.com:example/app.git"),
        ]

    @pytest.mark.unit
    def test_repeated_status_flags_passed(self) -> None:
        response = _mock_response(status_code=200, payload=[])
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "list",
                    "--status",
                    "running",
                    "--status",
                    "monitoring_pr",
                ],
            )

        assert result.exit_code == 0
        kwargs = mock.call_args.kwargs
        assert kwargs["params"] == [
            ("limit", 50),
            ("status", "running"),
            ("status", "monitoring_pr"),
        ]

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
    def test_runtime_uses_local_compose_token_for_default_local_target(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("AWF_API_TOKEN", raising=False)
        monkeypatch.setattr(
            cli_common,
            "local_service_environ",
            lambda _environ: {"AWF_API_TOKEN": "local-dev-token"},
        )
        response = _mock_response(
            status_code=200,
            payload={"workspace_id": "ws_obs", "stack_state": "running", "services": []},
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(app, ["workspace", "runtime", "ws_obs"])

        assert result.exit_code == 0
        assert "running" in result.stdout
        headers = mock.call_args.kwargs.get("headers", {})
        assert headers["Authorization"] == "Bearer local-dev-token"

    @pytest.mark.unit
    def test_runtime_does_not_send_local_compose_token_to_explicit_remote_target(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("AWF_API_TOKEN", raising=False)
        monkeypatch.setattr(
            cli_common,
            "local_service_environ",
            lambda _environ: {"AWF_API_TOKEN": "local-dev-token"},
        )
        response = _mock_response(
            status_code=200,
            payload={"workspace_id": "ws_obs", "stack_state": "running", "services": []},
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "runtime",
                    "ws_obs",
                    "--base-url",
                    "https://awf.example.test",
                ],
            )

        assert result.exit_code == 0
        assert mock.call_args[0][1] == "https://awf.example.test/v1/workspaces/ws_obs/runtime"
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
    @pytest.mark.parametrize("cursor_flag", ["--cursor", "--after"])
    def test_operations_forwards_pagination_cursor(self, cursor_flag: str) -> None:
        response = _mock_response(
            status_code=200,
            payload={
                "items": [{"id": "op_2", "type": "validate", "status": "running"}],
                "cursor": "eyJvIjoxfQ",
                "next_cursor": None,
                "has_more": False,
                "limit": 1,
            },
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "operations",
                    "ws_obs",
                    "--limit",
                    "1",
                    cursor_flag,
                    "eyJvIjoxfQ",
                ],
            )

        assert result.exit_code == 0, result.output
        assert mock.call_args[0] == (
            "GET",
            "http://localhost:8000/v1/workspaces/ws_obs/operations",
        )
        assert mock.call_args.kwargs["params"] == {
            "limit": 1,
            "cursor": "eyJvIjoxfQ",
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


class TestOperationCommands:
    @pytest.mark.unit
    def test_list_fetches_global_operations_with_filters(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AWF_API_TOKEN", "env-secret")
        response = _mock_response(
            status_code=200,
            payload={"items": [{"id": "op_1", "type": "validate", "status": "succeeded"}]},
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "operations",
                    "list",
                    "--workspace-id",
                    "ws_obs",
                    "--status",
                    "succeeded",
                    "--type",
                    "validate",
                    "--limit",
                    "5",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "validate" in result.stdout
        assert "env-secret" not in result.stdout
        assert "env-secret" not in result.stderr
        assert mock.call_args[0] == ("GET", "http://localhost:8000/v1/operations")
        assert mock.call_args.kwargs["params"] == {
            "limit": 5,
            "workspace_id": "ws_obs",
            "status": "succeeded",
            "type": "validate",
        }
        assert mock.call_args.kwargs["headers"] == {"Authorization": "Bearer env-secret"}

    @pytest.mark.unit
    @pytest.mark.parametrize("cursor_flag", ["--cursor", "--after"])
    def test_list_forwards_pagination_cursor(self, cursor_flag: str) -> None:
        response = _mock_response(
            status_code=200,
            payload={
                "items": [{"id": "op_2", "type": "validate", "status": "running"}],
                "cursor": "eyJvIjoxfQ",
                "next_cursor": None,
                "has_more": False,
                "limit": 1,
            },
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "operations",
                    "list",
                    "--limit",
                    "1",
                    cursor_flag,
                    "eyJvIjoxfQ",
                ],
            )

        assert result.exit_code == 0, result.output
        assert mock.call_args[0] == ("GET", "http://localhost:8000/v1/operations")
        assert mock.call_args.kwargs["params"] == {
            "limit": 1,
            "cursor": "eyJvIjoxfQ",
        }

    @pytest.mark.unit
    def test_show_fetches_operation_detail(self) -> None:
        response = _mock_response(
            status_code=200,
            payload={"id": "op_1", "workspace_id": "ws_obs", "type": "validate"},
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(app, ["operations", "show", "op_1"])

        assert result.exit_code == 0, result.output
        assert "op_1" in result.stdout
        assert mock.call_args[0] == ("GET", "http://localhost:8000/v1/operations/op_1")


class TestBaseUrlResolution:
    _DEPRECATION_NOTICE = "AWF_CLI_BASE_URL is deprecated; use AWF_BASE_URL"

    @staticmethod
    def _clear_base_url_env(monkeypatch: pytest.MonkeyPatch) -> None:
        """Reset CLI base URL environment state for a single test."""
        monkeypatch.delenv("AWF_BASE_URL", raising=False)
        monkeypatch.delenv("AWF_CLI_BASE_URL", raising=False)
        monkeypatch.delenv("AWF_API_HOST_PORT", raising=False)
        monkeypatch.setattr(cli_common, "local_service_environ", lambda _environ: {})
        monkeypatch.setattr(
            cli_common,
            "_cli_base_url_deprecation_notice_emitted",
            False,
            raising=False,
        )

    @pytest.mark.unit
    def test_cli_flag_overrides_env_without_deprecation_notice(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Prefer an explicit CLI base URL over all environment defaults."""
        self._clear_base_url_env(monkeypatch)
        monkeypatch.setenv("AWF_BASE_URL", "http://from-base-env:7777")
        monkeypatch.setenv("AWF_CLI_BASE_URL", "http://from-env:9999")
        monkeypatch.setenv("AWF_API_HOST_PORT", "8800")
        response = _mock_response(status_code=200, payload=[])
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                ["workspace", "list", "--base-url", "http://explicit:1234"],
            )

        assert result.exit_code == 0, result.output
        assert mock.call_args[0][1].startswith("http://explicit:1234")
        assert self._DEPRECATION_NOTICE not in result.stderr

    @pytest.mark.unit
    def test_awf_base_url_env_wins_over_deprecated_cli_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Prefer AWF_BASE_URL over the deprecated CLI-only environment URL."""
        self._clear_base_url_env(monkeypatch)
        monkeypatch.setenv("AWF_BASE_URL", "http://from-base-env:7777")
        monkeypatch.setenv("AWF_CLI_BASE_URL", "http://from-env:9999")
        response = _mock_response(status_code=200, payload=[])
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(app, ["workspace", "list"])

        assert result.exit_code == 0, result.output
        assert mock.call_args[0][1].startswith("http://from-base-env:7777")
        assert self._DEPRECATION_NOTICE not in result.stderr

    @pytest.mark.unit
    def test_deprecated_cli_env_used_when_no_base_url_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Use the deprecated CLI-only URL when no primary base URL is set."""
        self._clear_base_url_env(monkeypatch)
        monkeypatch.setenv("AWF_CLI_BASE_URL", "http://from-env:9999")
        response = _mock_response(status_code=200, payload=[])
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(app, ["workspace", "list"])

        assert result.exit_code == 0, result.output
        assert mock.call_args[0][1].startswith("http://from-env:9999")
        assert self._DEPRECATION_NOTICE in result.stderr

    @pytest.mark.unit
    def test_deprecated_cli_env_notice_is_one_time_per_process(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Warn once per process when the deprecated CLI URL variable is used."""
        self._clear_base_url_env(monkeypatch)
        monkeypatch.setenv("AWF_CLI_BASE_URL", "http://from-env:9999")

        assert cli_common._base_url(None) == "http://from-env:9999"  # noqa: SLF001
        assert cli_common._base_url(None) == "http://from-env:9999"  # noqa: SLF001

        assert capsys.readouterr().err.count(self._DEPRECATION_NOTICE) == 1

    @pytest.mark.unit
    def test_api_host_port_derives_default_host_cli_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Derive the default operator URL from the service host port override."""
        self._clear_base_url_env(monkeypatch)
        monkeypatch.setenv("AWF_API_HOST_PORT", "8800")
        response = _mock_response(status_code=200, payload=[])
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(app, ["workspace", "list"])

        assert result.exit_code == 0, result.output
        assert mock.call_args[0][1].startswith("http://localhost:8800")
        assert self._DEPRECATION_NOTICE not in result.stderr

    @pytest.mark.unit
    def test_compose_env_api_host_port_derives_default_host_cli_url(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Derive the default operator URL from the root Compose env file."""
        self._clear_base_url_env(monkeypatch)
        monkeypatch.setattr(
            cli_common,
            "local_service_environ",
            lambda _environ: {"AWF_API_HOST_PORT": "9100"},
        )
        response = _mock_response(status_code=200, payload=[])
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(app, ["workspace", "list"])

        assert result.exit_code == 0, result.output
        assert mock.call_args[0][1].startswith("http://localhost:9100")
        assert self._DEPRECATION_NOTICE not in result.stderr

    @pytest.mark.unit
    @pytest.mark.parametrize("host_port", ["not-a-port", "0", "65536"])
    def test_invalid_api_host_port_exits_before_request(
        self,
        monkeypatch: pytest.MonkeyPatch,
        host_port: str,
    ) -> None:
        """Reject invalid host port overrides before opening an HTTP request."""
        self._clear_base_url_env(monkeypatch)
        monkeypatch.setenv("AWF_API_HOST_PORT", host_port)
        with patch("awf.cli.main.httpx.request") as mock:
            result = _runner.invoke(app, ["workspace", "list"])

        assert result.exit_code == 2
        assert mock.call_count == 0
        assert "AWF_API_HOST_PORT must be an integer between 1 and 65535" in result.stderr
        assert repr(host_port) in result.stderr

    @pytest.mark.unit
    def test_no_base_url_env_uses_localhost_8000_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Use the localhost default when no CLI URL environment is configured."""
        self._clear_base_url_env(monkeypatch)
        response = _mock_response(status_code=200, payload=[])
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(app, ["workspace", "list"])

        assert result.exit_code == 0, result.output
        assert mock.call_args[0][1].startswith("http://localhost:8000")
        assert self._DEPRECATION_NOTICE not in result.stderr


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
    def test_pretty_output_surfaces_orphan_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from awf.service import config as config_mod
        from awf.service import status as status_mod

        settings = object()
        monkeypatch.setattr(
            config_mod, "resolve_service_settings", lambda *_args, **_kwargs: settings
        )

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
                        "action": ("Run docker compose -p <project> down -v --remove-orphans"),
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


class TestCliHelp:
    @pytest.mark.unit
    def test_main_help_contains_dx_guidance(self) -> None:
        result = _runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "Mutates:" in result.stdout
        _assert_current_first_path_guidance(result.stdout)

    @pytest.mark.unit
    def test_init_help_contains_dx_guidance(self) -> None:
        result = _runner.invoke(app, ["init", "--help"])
        assert result.exit_code == 0
        _assert_current_first_path_guidance(result.stdout)

    @pytest.mark.unit
    def test_service_bootstrap_help_contains_dx_guidance(self) -> None:
        result = _runner.invoke(app, ["service", "bootstrap", "--help"])
        assert result.exit_code == 0
        _assert_current_first_path_guidance(result.stdout)

    @pytest.mark.unit
    def test_workspace_help_contains_dx_guidance(self) -> None:
        result = _runner.invoke(app, ["workspace", "--help"])
        assert result.exit_code == 0
        _assert_current_first_path_guidance(result.stdout)


class TestServiceDoctorBundle:
    @pytest.mark.unit
    def test_cli_service_doctor_bundle_flag_writes_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from awf.service import support_bundle as bundle_mod

        out_dir = tmp_path / "bundles"
        out_dir.mkdir(parents=True, exist_ok=True)

        async def _collect_bundle(*args: object, **kwargs: object) -> dict[str, object]:
            return {
                "generated_at": "2025-01-01T00:00:00+00:00",
                "version": "0.1.0",
                "service_status": {"status": "ok"},
                "doctor_report": {"status": "ok"},
                "provider_readiness_summary": {"status": "ok"},
                "orphan_cleanup_posture": {},
                "recent_failure_summary": {},
                "config_fingerprint": {},
                "log_pointers": [],
                "issue_template_pointer": ".github/ISSUE_TEMPLATE/bug_report.yml",
            }

        monkeypatch.setattr(bundle_mod, "collect_support_bundle", _collect_bundle)

        original_write = bundle_mod.write_support_bundle

        def _write(bundle: dict[str, object], *, directory: Path | None = None) -> Path:
            return original_write(bundle, directory=out_dir)

        monkeypatch.setattr(bundle_mod, "write_support_bundle", _write)

        result = _runner.invoke(app, ["service", "doctor", "--bundle"])

        assert result.exit_code == 0, result.output
        assert "Support bundle written" in result.stdout
        bundle_path = next(out_dir.glob("awf-support-bundle-*.json"), None)
        assert bundle_path is not None
        bundle = json.loads(bundle_path.read_text())
        assert "doctor_report" in bundle
        assert "service_status" in bundle

    @pytest.mark.unit
    def test_cli_service_doctor_fail_path_points_to_bundle_and_issue_template(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from awf.service import config as config_mod
        from awf.service import doctor as doctor_mod

        monkeypatch.setattr(
            config_mod, "resolve_service_settings", lambda *_args, **_kwargs: object()
        )
        monkeypatch.setattr(config_mod, "local_service_environ", lambda **_kwargs: {})

        report = SimpleNamespace(
            status="fail",
            to_dict=lambda: {
                "service": "awf",
                "status": "fail",
                "summary": {"ok": 0, "warn": 0, "fail": 1},
                "diagnostics": [
                    {
                        "id": "docker",
                        "label": "Docker",
                        "status": "fail",
                        "reason": "DOCKER_DAEMON_UNREACHABLE",
                        "message": "Docker is not available.",
                        "action": "Start Docker Desktop.",
                        "source": "checks.docker",
                        "metadata": {},
                    }
                ],
            },
        )

        async def _collect(*args: object, **kwargs: object) -> SimpleNamespace:
            return report

        monkeypatch.setattr(doctor_mod, "collect_doctor_report", _collect)
        monkeypatch.setattr(
            doctor_mod,
            "render_doctor_pretty",
            lambda _report: (
                "AWF doctor: fail\n"
                "[fail] Docker: Docker is not available.\n"
                "       reason: DOCKER_DAEMON_UNREACHABLE\n"
                "       action: Start Docker Desktop.\n"
            ),
        )

        result = _runner.invoke(app, ["service", "doctor"])

        assert result.exit_code == 1
        output = result.stdout + result.stderr
        assert "awf service doctor --bundle" in output
        assert ".github/ISSUE_TEMPLATE/bug_report.yml" in output

    @pytest.mark.unit
    def test_cli_service_doctor_bundle_with_json_format(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from awf.service import support_bundle as bundle_mod

        out_dir = tmp_path / "bundles"
        out_dir.mkdir(parents=True, exist_ok=True)

        async def _collect_bundle(*args: object, **kwargs: object) -> dict[str, object]:
            return {
                "generated_at": "2025-01-01T00:00:00+00:00",
                "version": "0.1.0",
                "service_status": {"status": "ok"},
                "doctor_report": {"status": "ok"},
                "provider_readiness_summary": {"status": "ok"},
                "orphan_cleanup_posture": {},
                "recent_failure_summary": {},
                "config_fingerprint": {},
                "log_pointers": [],
                "issue_template_pointer": ".github/ISSUE_TEMPLATE/bug_report.yml",
            }

        monkeypatch.setattr(bundle_mod, "collect_support_bundle", _collect_bundle)

        original_write = bundle_mod.write_support_bundle

        def _write(bundle: dict[str, object], *, directory: Path | None = None) -> Path:
            return original_write(bundle, directory=out_dir)

        monkeypatch.setattr(bundle_mod, "write_support_bundle", _write)

        result = _runner.invoke(app, ["service", "doctor", "--bundle", "--format", "json"])
        assert result.exit_code == 0, result.output
        bundle_path = next(out_dir.glob("awf-support-bundle-*.json"), None)
        assert bundle_path is not None
        parsed = json.loads(result.stdout)
        assert parsed == {"support_bundle_path": str(bundle_path)}

    @pytest.mark.unit
    def test_cli_service_doctor_bundle_flag_ignores_failing_report_exit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from awf.service import support_bundle as bundle_mod

        out_dir = tmp_path / "bundles"
        out_dir.mkdir(parents=True, exist_ok=True)

        async def _collect_bundle(*args: object, **kwargs: object) -> dict[str, object]:
            return {
                "generated_at": "2025-01-01T00:00:00+00:00",
                "version": "0.1.0",
                "service_status": {"status": "fail"},
                "doctor_report": {"status": "fail"},
                "provider_readiness_summary": {"status": "fail"},
                "orphan_cleanup_posture": {},
                "recent_failure_summary": {},
                "config_fingerprint": {},
                "log_pointers": [],
                "issue_template_pointer": ".github/ISSUE_TEMPLATE/bug_report.yml",
            }

        monkeypatch.setattr(bundle_mod, "collect_support_bundle", _collect_bundle)

        original_write = bundle_mod.write_support_bundle

        def _write(bundle: dict[str, object], *, directory: Path | None = None) -> Path:
            return original_write(bundle, directory=out_dir)

        monkeypatch.setattr(bundle_mod, "write_support_bundle", _write)

        result = _runner.invoke(app, ["service", "doctor", "--bundle"])

        assert result.exit_code == 0, result.output
        assert "Support bundle written" in result.stdout
        bundle_path = next(out_dir.glob("awf-support-bundle-*.json"), None)
        assert bundle_path is not None
        bundle = json.loads(bundle_path.read_text())
        assert bundle.get("doctor_report", {}).get("status") == "fail"
