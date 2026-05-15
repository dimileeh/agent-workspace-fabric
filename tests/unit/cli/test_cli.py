"""Typer CLI tests — each command is invoked with CliRunner + mocked httpx.

We mock ``httpx.request`` rather than spinning an API server because the
CLI's job is to produce the right HTTP call and format the response.
End-to-end testing against a real server lives in tests/e2e/ (future).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
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


def _assert_control_headers(
    headers: dict[str, str],
    *,
    idempotency_key: str,
    if_match: str,
) -> None:
    assert headers["Idempotency-Key"] == idempotency_key
    assert headers["If-Match"] == if_match


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
                    "--task-class",
                    "docs_task",
                    "--priority",
                    "10",
                    "--human-boost",
                    "2",
                    "--owned-path",
                    "src/awf/**",
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
        assert kwargs["json"]["task"]["task_class"] == "docs_task"
        assert kwargs["json"]["task"]["priority"] == 10
        assert kwargs["json"]["task"]["human_boost"] == 2
        assert kwargs["json"]["task"]["owned_paths"] == ["src/awf/**"]
        assert kwargs["json"]["task"]["external_id"] == "ext_123"
        assert kwargs["json"]["resources"]["cpu"] == 2.5
        assert kwargs["json"]["resources"]["memory"] == "4GB"

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


class TestWorkspaceList:
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
                    "gemini",
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
            ("agent", "gemini"),
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
    def test_pretty_output_surfaces_orphan_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        assert "recommended first path" in result.stdout.lower()

    @pytest.mark.unit
    def test_init_help_contains_dx_guidance(self) -> None:
        result = _runner.invoke(app, ["init", "--help"])
        assert result.exit_code == 0
        assert "recommended first path" in result.stdout.lower()

    @pytest.mark.unit
    def test_service_bootstrap_help_contains_dx_guidance(self) -> None:
        result = _runner.invoke(app, ["service", "bootstrap", "--help"])
        assert result.exit_code == 0
        assert "recommended first path" in result.stdout.lower()

    @pytest.mark.unit
    def test_workspace_help_contains_dx_guidance(self) -> None:
        result = _runner.invoke(app, ["workspace", "--help"])
        assert result.exit_code == 0
        assert "recommended first path" in result.stdout.lower()


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

        monkeypatch.setattr(config_mod, "resolve_service_settings", lambda: object())
        monkeypatch.setattr(config_mod, "local_service_environ", lambda: {})

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
