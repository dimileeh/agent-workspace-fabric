"""Typer CLI tests — each command is invoked with CliRunner + mocked httpx.

We mock ``httpx.request`` rather than spinning an API server because the
CLI's job is to produce the right HTTP call and format the response.
End-to-end testing against a real server lives in tests/e2e/ (future).
"""

from __future__ import annotations

import json
import re
from importlib.metadata import PackageNotFoundError
from unittest.mock import MagicMock, patch

import click
import httpx
import pytest
from typer.testing import CliRunner

from awf import __version__
from awf.cli import common as cli_main
from awf.cli.main import app

_runner = CliRunner()


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


def _assert_control_headers(
    headers: dict[str, str],
    *,
    idempotency_key: str,
    if_match: str,
) -> None:
    assert headers["Idempotency-Key"] == idempotency_key
    assert headers["If-Match"] == if_match


@pytest.mark.unit
def test_handle_response_uses_response_request_without_global_context() -> None:
    response = _mock_response(status_code=200, payload={"ok": True})
    response.request = httpx.Request("GET", "http://localhost:8000/v1/workspaces")

    assert not hasattr(cli_main, "_CALL_CONTEXT")
    cli_main._handle_response(response, cli_main.OutputFormat.json)
    assert not hasattr(cli_main, "_CALL_CONTEXT")


@pytest.mark.unit
def test_root_version_option_reports_package_version() -> None:
    """``awf --version`` reports the installed package version."""
    with patch("awf.cli.main.importlib_metadata.version", return_value="9.8.7"):
        result = _runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert "awf 9.8.7" in result.stdout


@pytest.mark.unit
def test_root_version_option_falls_back_when_package_metadata_is_missing() -> None:
    """Source checkout invocations still work without installed metadata."""
    with patch(
        "awf.cli.main.importlib_metadata.version",
        side_effect=PackageNotFoundError,
    ):
        result = _runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert f"awf {__version__}" in result.stdout


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
        assert args[1].endswith("/v1/workspaces")
        body = kwargs["json"]
        assert body["repo"]["url"] == "git@github.com:x/y.git"
        assert body["task"]["title"] == "Add docs"
        assert body["validation"]["commands"] == ["pytest -q", "ruff check ."]
        assert body["workspace"]["profile_ref"] == "aira"
        assert body["task"]["agent"] == "codex"
        assert body["task"]["auto_merge"] is True
        assert body["task"]["initial_review_grace_period_seconds"] is None
        assert body["repo"]["base_branch"] == "development"

    @pytest.mark.unit
    def test_workspace_create_accepts_grok_agent(self) -> None:
        response = _mock_response(status_code=202, payload={"workspace_id": "ws_grok"})
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "create",
                    "--repo",
                    "git@github.com:x/y.git",
                    "--title",
                    "Add Grok runtime",
                    "--prompt",
                    "Wire the grok adapter.",
                    "--agent",
                    "grok",
                    "--model",
                    "grok-build-0.1",
                ],
            )

        assert result.exit_code == 0
        body = mock.call_args.kwargs["json"]
        assert body["task"]["agent"] == "grok"
        assert body["task"]["model"] == "grok-build-0.1"

    @pytest.mark.unit
    def test_companion_json_can_include_compose_up_timeout(self) -> None:
        response = _mock_response(status_code=202, payload={"workspace_id": "ws_companion"})
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "create",
                    "--repo",
                    "git@github.com:x/y.git",
                    "--title",
                    "Companion timeout",
                    "--prompt",
                    "Exercise a slow companion build.",
                    "--companion-json",
                    (
                        '{"name":"backend","repo_url":"git@github.com:example/api.git",'
                        '"compose_up_timeout_seconds":900}'
                    ),
                ],
            )

        assert result.exit_code == 0
        body = mock.call_args.kwargs["json"]
        assert body["companions"] == [
            {
                "name": "backend",
                "repo_url": "git@github.com:example/api.git",
                "compose_up_timeout_seconds": 900,
            }
        ]

    @pytest.mark.unit
    def test_sync_release_pr_defaults_base_to_main(self) -> None:
        """Without --base, a sync_release_pr targets main so it syncs development → main."""
        response = _mock_response(status_code=202, payload={"workspace_id": "ws_rel"})
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "create",
                    "--repo",
                    "git@github.com:x/y.git",
                    "--title",
                    "Release sync",
                    "--prompt",
                    "Open the release PR.",
                    "--task-kind",
                    "sync_release_pr",
                ],
            )

        assert result.exit_code == 0
        body = mock.call_args.kwargs["json"]
        assert body["task"]["kind"] == "sync_release_pr"
        assert body["repo"]["base_branch"] == "main"
        assert "source_branch" not in body["repo"]

    @pytest.mark.unit
    def test_sync_release_pr_respects_explicit_base(self) -> None:
        """An explicit --base always wins over the task-kind default."""
        response = _mock_response(status_code=202, payload={"workspace_id": "ws_rel2"})
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "create",
                    "--repo",
                    "git@github.com:x/y.git",
                    "--title",
                    "Release sync",
                    "--prompt",
                    "Open the release PR.",
                    "--task-kind",
                    "sync_release_pr",
                    "--base",
                    "release",
                ],
            )

        assert result.exit_code == 0
        body = mock.call_args.kwargs["json"]
        assert body["repo"]["base_branch"] == "release"

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
    def test_emits_new_v2_flags_to_post(self) -> None:
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
                    "--model",
                    "gemini-test",
                    "--effort",
                    "xhigh",
                    "--task-class",
                    "docs_task",
                    "--priority",
                    "10",
                    "--human-boost",
                    "2",
                    "--owned-path",
                    "src/awf/**",
                    "--owned-path",
                    ".github/workflows/publish.yml",
                    "--owned-path",
                    "pyproject.toml",
                    "--external-id",
                    "ext_123",
                    "--cpu",
                    "2.5",
                    "--memory",
                    "4GB",
                ],
            )

        assert result.exit_code == 0
        kwargs = mock.call_args.kwargs
        assert kwargs["json"]["task"]["model"] == "gemini-test"
        assert kwargs["json"]["task"]["effort"] == "xhigh"
        assert kwargs["json"]["task"]["task_class"] == "docs_task"
        assert kwargs["json"]["task"]["priority"] == 10
        assert kwargs["json"]["task"]["human_boost"] == 2
        assert kwargs["json"]["task"]["owned_paths"] == [
            "src/awf/**",
            ".github/workflows/publish.yml",
            "pyproject.toml",
        ]
        assert kwargs["json"]["task"]["external_id"] == "ext_123"
        assert kwargs["json"]["resources"]["cpu"] == 2.5
        assert kwargs["json"]["resources"]["memory"] == "4GB"

    @pytest.mark.unit
    def test_create_help_exposes_model_and_effort_flags(self) -> None:
        """Verify CLI help advertises model and effort for workspace create."""
        result = _runner.invoke(app, ["workspace", "create", "--help"])

        assert result.exit_code == 0
        _assert_workspace_create_help_exposes_model_and_effort(result.stdout)

    @pytest.mark.unit
    def test_emits_json_policy_flags_to_post(self) -> None:
        response = _mock_response(status_code=202, payload={"workspace_id": "ws_policy"})
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "create",
                    "--repo",
                    "git@github.com:x/y.git",
                    "--title",
                    "Policy create",
                    "--prompt",
                    "Verify policy flags.",
                    "--out-of-scope-changes-json",
                    '{"mode":"block","allowlist_patterns":["docs/**"]}',
                    "--provider-recovery-json",
                    '{"max_fallback_attempts":1,"fallbacks":[{"agent":"codex","provider":"openai","model":"gpt-5.5"}]}',
                ],
            )

        assert result.exit_code == 0
        body = mock.call_args.kwargs["json"]
        assert body["task"]["out_of_scope_changes"] == {
            "mode": "block",
            "allowlist_patterns": ["docs/**"],
        }
        assert body["task"]["provider_recovery"] == {
            "max_fallback_attempts": 1,
            "fallbacks": [
                {"agent": "codex", "provider": "openai", "model": "gpt-5.5"},
            ],
        }

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "flag, value",
        (
            ("--out-of-scope-changes-json", "{mode:block}"),
            ("--out-of-scope-changes-json", "[1,2,3]"),
            ("--provider-recovery-json", "{fallbacks:[{agent:codex}]}"),
            ("--provider-recovery-json", "null"),
        ),
    )
    def test_invalid_json_policy_flag_values_do_not_request(
        self,
        flag: str,
        value: str,
    ) -> None:
        with patch("awf.cli.main.httpx.request") as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "create",
                    "--repo",
                    "git@github.com:x/y.git",
                    "--title",
                    "Policy create",
                    "--prompt",
                    "Verify policy validation.",
                    flag,
                    value,
                ],
            )

        assert result.exit_code == 2
        assert flag in result.stderr
        assert "json" in result.stderr.lower()
        assert not mock.called

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
    def test_api_token_header_forwarded_without_printing_secret(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AWF_API_TOKEN", "env-secret")
        response = _mock_response(status_code=202, payload={"workspace_id": "ws_auth"})
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
        assert "env-secret" not in result.stdout
        assert "env-secret" not in result.stderr
        assert mock.call_args.kwargs["headers"] == {
            "Authorization": "Bearer env-secret",
            "Idempotency-Key": "same-key-42",
        }

    @pytest.mark.unit
    def test_api_token_option_overrides_env_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWF_API_TOKEN", "env-secret")
        response = _mock_response(status_code=202, payload={"workspace_id": "ws_auth"})
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
                    "--api-token",
                    "cli-secret",
                ],
            )

        assert result.exit_code == 0
        assert "cli-secret" not in result.stdout
        assert "cli-secret" not in result.stderr
        assert mock.call_args.kwargs["headers"] == {"Authorization": "Bearer cli-secret"}

    @pytest.mark.unit
    def test_provider_readiness_override_flag_is_sent(self) -> None:
        response = _mock_response(status_code=202, payload={"workspace_id": "ws_override"})
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
                    "--provider-readiness-override",
                    "--provider-readiness-override-reason",
                    "operator verified auth",
                ],
            )

        assert result.exit_code == 0
        body = mock.call_args.kwargs["json"]
        assert body["preflight"] == {
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "operator verified auth",
        }

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
    def test_injects_env_api_token_without_printing_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AWF_API_TOKEN", "env-secret")
        response = _mock_response(
            status_code=200,
            payload={"id": "ws_xyz", "status": "ready", "version": 3},
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(app, ["workspace", "show", "ws_xyz"])

        assert result.exit_code == 0
        assert "env-secret" not in result.stdout
        assert "env-secret" not in result.stderr
        assert mock.call_args.kwargs["headers"] == {"Authorization": "Bearer env-secret"}

    @pytest.mark.unit
    def test_api_token_option_overrides_env_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWF_API_TOKEN", "env-secret")
        response = _mock_response(
            status_code=200,
            payload={"id": "ws_xyz", "status": "ready", "version": 3},
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                ["workspace", "show", "ws_xyz", "--api-token", "cli-secret"],
            )

        assert result.exit_code == 0
        assert "cli-secret" not in result.stdout
        assert "cli-secret" not in result.stderr
        assert mock.call_args.kwargs["headers"] == {"Authorization": "Bearer cli-secret"}

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

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("base_url", "expected_url"),
        (
            ("http://host:8000/awf", "http://host:8000/awf/v1/workspaces/ws_xyz"),
            ("http://host:8000/awf/v1", "http://host:8000/awf/v1/workspaces/ws_xyz"),
        ),
    )
    def test_show_with_reversed_proxy_prefix_normalizes_v1(
        self,
        base_url: str,
        expected_url: str,
    ) -> None:
        response = _mock_response(status_code=200, payload={"id": "ws_xyz", "status": "ready"})
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                ["workspace", "show", "ws_xyz", "--base-url", base_url],
            )

        assert result.exit_code == 0
        assert mock.call_args[0] == ("GET", expected_url)


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

    @pytest.mark.unit
    def test_retry_provider_readiness_override_flag_is_sent(self) -> None:
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
    def test_remonitor_cli_generates_idempotency_key_before_http_call(self) -> None:
        response = _mock_response(status_code=202, payload={"ok": True})
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(app, ["workspace", "remonitor", "ws_monitor"])

        assert result.exit_code == 0, result.output
        generated_key = mock.call_args.kwargs["headers"]["Idempotency-Key"]
        assert re.fullmatch(r"awf-cli-remonitor-[0-9a-f]{32}", generated_key)
        assert f"Generated Idempotency-Key: {generated_key}" in result.stderr


class TestWorkspaceControlCommandsPresence:
    @pytest.mark.unit
    def test_workspace_help_lists_control_commands(self) -> None:
        result = _runner.invoke(app, ["workspace", "--help"])

        assert result.exit_code == 0
        for command in ("cancel", "stop", "destroy", "refresh", "validate", "rebase"):
            assert command in result.stdout


@pytest.mark.unit
@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("cancel", ["ws_cancel", "--reason", "operator requested"]),
        ("stop", ["ws_stop", "--reason", "stack unstable"]),
        ("destroy", ["ws_destroy"]),
        ("refresh", ["ws_refresh", "--reason", "stale branch"]),
        ("validate", ["ws_validate", "--requested-tier", "2"]),
        ("rebase", ["ws_rebase", "--reason", "recover merge conflicts"]),
    ],
)
def test_workspace_control_commands_generate_idempotency_key_when_omitted(
    command: str,
    args: list[str],
) -> None:
    response = _mock_response(status_code=202, payload={"ok": True})
    with patch("awf.cli.main.httpx.request", return_value=response) as mock:
        result = _runner.invoke(app, ["workspace", command, *args])

    assert result.exit_code == 0, result.output
    generated_key = mock.call_args.kwargs["headers"]["Idempotency-Key"]
    assert re.fullmatch(rf"awf-cli-{command}-[0-9a-f]{{32}}", generated_key)
    assert f"Generated Idempotency-Key: {generated_key}" in result.stderr


@pytest.mark.unit
def test_workspace_control_generated_idempotency_key_survives_request_failure() -> None:
    with patch(
        "awf.cli.main.httpx.request",
        side_effect=httpx.ReadTimeout("response dropped"),
    ) as mock:
        result = _runner.invoke(
            app,
            ["workspace", "cancel", "ws_cancel", "--reason", "operator requested"],
        )

    assert result.exit_code == 2
    generated_key = mock.call_args.kwargs["headers"]["Idempotency-Key"]
    assert re.fullmatch(r"awf-cli-cancel-[0-9a-f]{32}", generated_key)
    assert f"Generated Idempotency-Key: {generated_key}" in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    ("command", "args"),
    [
        (
            "cancel",
            ["ws_cancel", "--reason", "operator requested", "--idempotency-key", "cancel-key"],
        ),
        (
            "stop",
            ["ws_stop", "--reason", "stack unstable", "--idempotency-key", "stop-key"],
        ),
        (
            "destroy",
            ["ws_destroy", "--idempotency-key", "destroy-key", "--if-match", "1"],
        ),
        (
            "refresh",
            ["ws_refresh", "--reason", "stale branch", "--idempotency-key", "refresh-key"],
        ),
        (
            "validate",
            [
                "ws_validate",
                "--requested-tier",
                "2",
                "--idempotency-key",
                "validate-key",
            ],
        ),
        (
            "rebase",
            ["ws_rebase", "--reason", "recover merge conflicts", "--idempotency-key", "rebase-key"],
        ),
    ],
)
def test_workspace_control_commands_emit_structured_api_error(
    command: str,
    args: list[str],
) -> None:
    response = _mock_response(
        status_code=409,
        payload={
            "error_code": "VERSION_CONFLICT",
            "message": "Workspace version changed while processing control request.",
            "detail": {"workspace_id": "ws_control"},
        },
    )
    with patch("awf.cli.main.httpx.request", return_value=response):
        result = _runner.invoke(app, ["workspace", command, *args])

    assert result.exit_code == 1
    assert "VERSION_CONFLICT" in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    ("command", "args"),
    [
        (
            "cancel",
            [
                "ws_cancel",
                "--reason",
                "operator requested",
                "--idempotency-key",
                "cancel-key",
                "--if-match",
                "bad",
            ],
        ),
        (
            "stop",
            [
                "ws_stop",
                "--reason",
                "stack unstable",
                "--idempotency-key",
                "stop-key",
                "--if-match",
                "bad",
            ],
        ),
        ("destroy", ["ws_destroy", "--idempotency-key", "destroy-key", "--if-match", "bad"]),
        (
            "refresh",
            [
                "ws_refresh",
                "--reason",
                "stale branch",
                "--idempotency-key",
                "refresh-key",
                "--if-match",
                "bad",
            ],
        ),
        (
            "validate",
            [
                "ws_validate",
                "--requested-tier",
                "2",
                "--idempotency-key",
                "validate-key",
                "--if-match",
                "bad",
            ],
        ),
        (
            "rebase",
            [
                "ws_rebase",
                "--reason",
                "recover merge conflicts",
                "--idempotency-key",
                "rebase-key",
                "--if-match",
                "bad",
            ],
        ),
    ],
)
def test_workspace_control_commands_surface_invalid_if_match_api_error(
    command: str,
    args: list[str],
) -> None:
    response = _mock_response(
        status_code=400,
        payload={
            "error_code": "INVALID_REQUEST",
            "message": "If-Match must be a workspace version integer.",
        },
    )
    with patch("awf.cli.main.httpx.request", return_value=response) as mock:
        result = _runner.invoke(app, ["workspace", command, *args])

    assert result.exit_code == 1
    assert "INVALID_REQUEST" in result.stderr
    assert mock.call_args.kwargs["headers"]["If-Match"] == "bad"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("command", "args"),
    [
        (
            "remonitor",
            ["ws_monitor", "--reason", "operator recovery", "--idempotency-key", "remonitor-key"],
        ),
        (
            "cancel",
            ["ws_cancel", "--reason", "operator requested", "--idempotency-key", "cancel-key"],
        ),
        ("stop", ["ws_stop", "--reason", "stack unstable", "--idempotency-key", "stop-key"]),
        ("destroy", ["ws_destroy", "--idempotency-key", "destroy-key"]),
        (
            "refresh",
            ["ws_refresh", "--reason", "stale branch", "--idempotency-key", "refresh-key"],
        ),
        (
            "validate",
            ["ws_validate", "--requested-tier", "2", "--idempotency-key", "validate-key"],
        ),
        (
            "rebase",
            ["ws_rebase", "--reason", "recover merge conflicts", "--idempotency-key", "rebase-key"],
        ),
    ],
)
@pytest.mark.parametrize("if_match", ['"7"', 'W/"7"'])
def test_workspace_control_commands_forward_etag_if_match_syntax(
    command: str,
    args: list[str],
    if_match: str,
) -> None:
    response = _mock_response(status_code=202, payload={"ok": True})
    with patch("awf.cli.main.httpx.request", return_value=response) as mock:
        result = _runner.invoke(app, ["workspace", command, *args, "--if-match", if_match])

    assert result.exit_code == 0, result.output
    assert mock.call_args.kwargs["headers"]["If-Match"] == if_match


class TestWorkspaceCancel:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("stop_stack",),
        [("true",), ("false",)],
    )
    def test_posts_cancel_request_with_control_shape(self, stop_stack: str) -> None:
        response = _mock_response(
            status_code=200,
            payload={
                "workspace_id": "ws_cancel",
                "operation_id": "op_cancel",
                "operation_status": "requested",
                "status": "cancelling",
                "message": "workspace cancellation requested",
            },
        )
        flags: list[str] = [
            "ws_cancel",
            "--reason",
            "operator requested",
            "--idempotency-key",
            "cancel-key",
            "--if-match",
            "7",
        ]
        if stop_stack == "true":
            flags.append("--stop-stack")
        elif stop_stack == "false":
            flags.append("--no-stop-stack")

        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(app, ["workspace", "cancel", *flags])

        assert result.exit_code == 0
        assert "op_cancel" in result.stdout
        assert mock.call_args[0] == (
            "POST",
            "http://localhost:8000/v1/workspaces/ws_cancel/cancel",
        )
        assert mock.call_args.kwargs["json"] == {
            "reason": "operator requested",
            "stop_stack": stop_stack == "true",
        }
        _assert_control_headers(
            mock.call_args.kwargs["headers"],
            idempotency_key="cancel-key",
            if_match="7",
        )


class TestWorkspaceStop:
    @pytest.mark.unit
    def test_posts_stop_request_and_output_shape(self) -> None:
        response = _mock_response(
            status_code=200,
            payload={
                "workspace_id": "ws_stop",
                "operation_id": "op_stop",
                "operation_status": "requested",
                "status": "stopping",
                "message": "workspace stop requested",
            },
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "stop",
                    "ws_stop",
                    "--reason",
                    "stack unstable",
                    "--idempotency-key",
                    "stop-key",
                    "--if-match",
                    "13",
                ],
            )

        assert result.exit_code == 0
        assert "op_stop" in result.stdout
        assert mock.call_args[0] == (
            "POST",
            "http://localhost:8000/v1/workspaces/ws_stop/stop",
        )
        assert mock.call_args.kwargs["json"] == {"reason": "stack unstable"}
        _assert_control_headers(
            mock.call_args.kwargs["headers"],
            idempotency_key="stop-key",
            if_match="13",
        )


class TestWorkspaceDestroy:
    @pytest.mark.unit
    def test_posts_destroy_request_with_query_shape(self) -> None:
        response = _mock_response(
            status_code=200,
            payload={
                "workspace_id": "ws_destroy",
                "operation_id": "op_destroy",
                "operation_status": "requested",
                "status": "destroying",
                "message": "workspace destruction requested",
            },
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "destroy",
                    "ws_destroy",
                    "--force",
                    "--no-remove-volumes",
                    "--no-remove-worktree",
                    "--idempotency-key",
                    "destroy-key",
                    "--if-match",
                    "19",
                ],
            )

        assert result.exit_code == 0
        assert "op_destroy" in result.stdout
        assert mock.call_args[0] == (
            "DELETE",
            "http://localhost:8000/v1/workspaces/ws_destroy",
        )
        assert mock.call_args.kwargs["params"] == {
            "force": True,
            "remove_volumes": False,
            "remove_worktree": False,
        }
        _assert_control_headers(
            mock.call_args.kwargs["headers"],
            idempotency_key="destroy-key",
            if_match="19",
        )


class TestWorkspaceRefresh:
    @pytest.mark.unit
    def test_posts_refresh_request_with_reason_and_version_headers(self) -> None:
        response = _mock_response(
            status_code=202,
            payload={
                "operation_id": "op_refresh",
                "operation_status": "requested",
                "status": "requested",
                "workspace_id": "ws_refresh",
                "message": "workspace refresh requested",
            },
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "refresh",
                    "ws_refresh",
                    "--reason",
                    "stale branch",
                    "--idempotency-key",
                    "refresh-key",
                    "--if-match",
                    "33",
                ],
            )

        assert result.exit_code == 0
        assert "op_refresh" in result.stdout
        assert mock.call_args[0] == (
            "POST",
            "http://localhost:8000/v1/workspaces/ws_refresh/refresh",
        )
        assert mock.call_args.kwargs["json"] == {"reason": "stale branch"}
        _assert_control_headers(
            mock.call_args.kwargs["headers"],
            idempotency_key="refresh-key",
            if_match="33",
        )


class TestWorkspaceValidate:
    @pytest.mark.unit
    def test_posts_validate_request_with_requested_tier(self) -> None:
        response = _mock_response(
            status_code=202,
            payload={
                "operation_id": "op_validate",
                "operation_status": "requested",
                "status": "requested",
                "workspace_id": "ws_validate",
                "message": "workspace validation requested",
            },
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "validate",
                    "ws_validate",
                    "--requested-tier",
                    "2",
                    "--reason",
                    "manual revalidation",
                    "--idempotency-key",
                    "validate-key",
                    "--if-match",
                    "2",
                ],
            )

        assert result.exit_code == 0
        assert "op_validate" in result.stdout
        assert mock.call_args[0] == (
            "POST",
            "http://localhost:8000/v1/workspaces/ws_validate/validate",
        )
        assert mock.call_args.kwargs["json"] == {
            "reason": "manual revalidation",
            "requested_tier": 2,
        }
        _assert_control_headers(
            mock.call_args.kwargs["headers"],
            idempotency_key="validate-key",
            if_match="2",
        )


class TestWorkspaceRebase:
    @pytest.mark.unit
    def test_posts_rebase_request_with_reason(self) -> None:
        response = _mock_response(
            status_code=202,
            payload={
                "operation_id": "op_rebase",
                "operation_status": "requested",
                "status": "rebasing",
                "workspace_id": "ws_rebase",
                "message": "workspace rebase requested",
            },
        )
        with patch("awf.cli.main.httpx.request", return_value=response) as mock:
            result = _runner.invoke(
                app,
                [
                    "workspace",
                    "rebase",
                    "ws_rebase",
                    "--reason",
                    "recover merge conflicts",
                    "--idempotency-key",
                    "rebase-key",
                    "--if-match",
                    "11",
                ],
            )

        assert result.exit_code == 0
        assert "op_rebase" in result.stdout
        assert mock.call_args[0] == (
            "POST",
            "http://localhost:8000/v1/workspaces/ws_rebase/rebase",
        )
        assert mock.call_args.kwargs["json"] == {"reason": "recover merge conflicts"}
        _assert_control_headers(
            mock.call_args.kwargs["headers"],
            idempotency_key="rebase-key",
            if_match="11",
        )
