"""Hosted delegation HTTP client tests."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import httpx
import pytest

from awf.adapters.runtime_executor import AgentRuntimeExecRequest
from awf.common.commands import COMMAND_TIMEOUT_REASON
from awf.common.config import Settings
from awf.db.enums import AgentRuntime
from awf.runtime.hosted_delegation import (
    HostedAgentRuntimeExecutor,
    HostedDelegationConfig,
    HostedDelegationConfigError,
    HostedDelegationProtocolError,
    hosted_delegation_config_from_settings,
    hosted_delegation_config_from_values,
)


def _config(**overrides: object) -> HostedDelegationConfig:
    values: dict[str, object] = {
        "base_url": "https://hosted.example.test",
        "bearer_token": "secret-token",
        "poll_interval_seconds": 0.001,
        "operation_timeout_seconds": 1.0,
        "request_timeout_seconds": 1.0,
        "cancel_timeout_seconds": 1.0,
        "max_output_bytes": 100_000,
    }
    values.update(overrides)
    return HostedDelegationConfig(**values)  # type: ignore[arg-type]


def _agent_request(**overrides: object) -> AgentRuntimeExecRequest:
    values: dict[str, object] = {
        "workspace_id": "ws_hosted",
        "agent_runtime": AgentRuntime.codex,
        "cli_args": ("codex", "exec", "-"),
        "prompt_stdin": b"repair prompt",
        "log_source": "monitor.repair",
        "model": "gpt-5",
        "effort": "high",
        "repo_url": "git@github.com:dimileeh/aira-web.git",
        "pr_url": "https://github.com/dimileeh/aira-web/pull/277",
        "pr_number": 277,
        "base_ref": "development",
        "head_ref": "feature/ready",
        "head_repo_url": "git@github.com:dimileeh/aira-web.git",
        "head_repo_slug": "dimileeh/aira-web",
        "owned_paths": ("src/**",),
        "expected_head_sha": "a" * 40,
    }
    values.update(overrides)
    return AgentRuntimeExecRequest(**values)  # type: ignore[arg-type]


@pytest.mark.unit
def test_hosted_delegation_config_resolves_env_token_and_redacts_secret() -> None:
    config = hosted_delegation_config_from_values(
        base_url=" https://hosted.example.test/ ",
        bearer_token=None,
        bearer_token_env=" HOSTED_DELEGATION_TOKEN ",
        environ={"HOSTED_DELEGATION_TOKEN": " env-secret-token "},
        poll_interval_seconds=2.0,
        operation_timeout_seconds=30.0,
        request_timeout_seconds=5.0,
        cancel_timeout_seconds=3.0,
        max_output_bytes=1024,
    )

    assert config.base_url == "https://hosted.example.test"
    assert config.bearer_token == "env-secret-token"
    assert config.redacted_payload() == {
        "base_url": "https://hosted.example.test",
        "bearer_token": "<redacted>",
        "poll_interval_seconds": 2.0,
        "operation_timeout_seconds": 30.0,
        "request_timeout_seconds": 5.0,
        "cancel_timeout_seconds": 3.0,
        "max_output_bytes": 1024,
    }


@pytest.mark.unit
def test_hosted_delegation_config_reports_blank_base_and_missing_env_token() -> None:
    with pytest.raises(HostedDelegationConfigError) as excinfo:
        hosted_delegation_config_from_values(
            base_url="   ",
            bearer_token=None,
            bearer_token_env="HOSTED_DELEGATION_TOKEN",
            environ={},
            poll_interval_seconds=2.0,
            operation_timeout_seconds=30.0,
            request_timeout_seconds=5.0,
            cancel_timeout_seconds=3.0,
            max_output_bytes=1024,
        )

    assert excinfo.value.detail() == {
        "missing": [
            "AWF_HOSTED_DELEGATION_BASE_URL",
            "AWF_HOSTED_DELEGATION_BEARER_TOKEN or AWF_HOSTED_DELEGATION_BEARER_TOKEN_ENV",
        ],
    }


@pytest.mark.unit
def test_hosted_delegation_config_accepts_direct_token_without_env_lookup() -> None:
    config = hosted_delegation_config_from_values(
        base_url="https://hosted.example.test",
        bearer_token=" direct-token ",
        bearer_token_env=None,
        environ={"HOSTED_DELEGATION_TOKEN": "ignored-env-token"},
        poll_interval_seconds=2.0,
        operation_timeout_seconds=30.0,
        request_timeout_seconds=5.0,
        cancel_timeout_seconds=3.0,
        max_output_bytes=1024,
    )

    assert config.bearer_token == "direct-token"


@pytest.mark.unit
def test_hosted_delegation_config_from_settings_uses_settings_values() -> None:
    settings = Settings(
        _env_file=None,
        hosted_delegation_base_url="https://hosted.example.test/",
        hosted_delegation_bearer_token="settings-token",
        hosted_delegation_poll_interval_seconds=4.0,
        hosted_delegation_operation_timeout_seconds=40.0,
        hosted_delegation_request_timeout_seconds=6.0,
        hosted_delegation_cancel_timeout_seconds=2.0,
        hosted_delegation_max_output_bytes=2048,
    )

    config = hosted_delegation_config_from_settings(settings, environ={})

    assert config.base_url == "https://hosted.example.test"
    assert config.bearer_token == "settings-token"
    assert config.poll_interval_seconds == 4.0
    assert config.operation_timeout_seconds == 40.0
    assert config.request_timeout_seconds == 6.0
    assert config.cancel_timeout_seconds == 2.0
    assert config.max_output_bytes == 2048


@pytest.mark.unit
def test_hosted_delegation_config_rejects_non_https_base_url() -> None:
    with pytest.raises(HostedDelegationConfigError) as excinfo:
        hosted_delegation_config_from_values(
            base_url="http://hosted.example.test",
            bearer_token="secret-token",
            bearer_token_env=None,
            environ={},
            poll_interval_seconds=2.0,
            operation_timeout_seconds=30.0,
            request_timeout_seconds=5.0,
            cancel_timeout_seconds=3.0,
            max_output_bytes=1024,
        )

    assert excinfo.value.detail() == {
        "missing": ["AWF_HOSTED_DELEGATION_BASE_URL"],
    }


@pytest.mark.unit
def test_hosted_delegation_config_reports_unset_base_url() -> None:
    with pytest.raises(HostedDelegationConfigError) as excinfo:
        hosted_delegation_config_from_values(
            base_url=None,
            bearer_token="secret-token",
            bearer_token_env=None,
            environ={},
            poll_interval_seconds=2.0,
            operation_timeout_seconds=30.0,
            request_timeout_seconds=5.0,
            cancel_timeout_seconds=3.0,
            max_output_bytes=1024,
        )

    assert excinfo.value.detail() == {
        "missing": ["AWF_HOSTED_DELEGATION_BASE_URL"],
    }


@pytest.mark.unit
async def test_agent_delegation_posts_secret_free_body_and_maps_terminal_head_sha() -> None:
    seen: dict[str, Any] = {}
    terminal_head_sha = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/agent-runs":
            seen["headers"] = dict(request.headers)
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                202,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/op_1",
                    "state": "running",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/op_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "returncode": 0,
                    "stdout": "ok",
                    "stderr": "",
                    "terminal_head_sha": terminal_head_sha,
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        result = await HostedAgentRuntimeExecutor(_config(), client=client).execute(
            _agent_request()
        )

    assert result.returncode == 0
    assert result.stdout == "ok"
    assert result.terminal_head_sha == terminal_head_sha
    assert seen["headers"]["authorization"] == "Bearer secret-token"
    assert "secret-token" not in seen["url"]
    body_blob = json.dumps(seen["body"], sort_keys=True)
    assert "secret-token" not in body_blob
    assert "repair prompt" not in body_blob
    assert seen["body"]["cli_args"] == ["codex", "exec", "-"]
    assert base64.b64decode(seen["body"]["prompt_stdin_base64"]) == b"repair prompt"
    assert seen["body"]["pr_identity"] == {
        "repo_url": "git@github.com:dimileeh/aira-web.git",
        "pr_url": "https://github.com/dimileeh/aira-web/pull/277",
        "pr_number": 277,
        "base_ref": "development",
        "head_ref": "feature/ready",
        "head_repo_url": "git@github.com:dimileeh/aira-web.git",
        "head_repo_slug": "dimileeh/aira-web",
        "owned_paths": ["src/**"],
        "expected_head_sha": "a" * 40,
    }


@pytest.mark.unit
async def test_agent_delegation_omits_pr_identity_when_request_has_no_pr_metadata() -> None:
    seen: dict[str, Any] = {}
    terminal_head_sha = "b" * 40

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/agent-runs":
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                202,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "https://hosted.example.test/v1/operations/op_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/op_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "returncode": 0,
                    "stdout": "ok",
                    "stderr": "",
                    "terminal_head_sha": terminal_head_sha,
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    request = _agent_request(
        repo_url=None,
        pr_url=None,
        pr_number=None,
        base_ref=None,
        head_ref=None,
        head_repo_url=None,
        head_repo_slug=None,
        owned_paths=(),
        expected_head_sha=None,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        result = await HostedAgentRuntimeExecutor(_config(), client=client).execute(request)

    assert result.terminal_head_sha == terminal_head_sha
    assert "pr_identity" not in seen["body"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("state", "returncode", "timeout_reason"),
    [
        ("failed", 1, ""),
        ("cancelled", 130, ""),
        ("timed_out", 124, COMMAND_TIMEOUT_REASON),
    ],
)
async def test_agent_delegation_maps_unsuccessful_terminal_without_head_sha(
    state: str,
    returncode: int,
    timeout_reason: str,
) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/agent-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/op_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/op_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "state": state,
                    "returncode": returncode,
                    "stdout": "agent stdout",
                    "stderr": "agent stderr",
                    "timeout_reason": timeout_reason,
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        result = await HostedAgentRuntimeExecutor(_config(), client=client).execute(
            _agent_request()
        )

    assert result.returncode == returncode
    assert result.stdout == "agent stdout"
    assert result.stderr == "agent stderr"
    assert result.timeout_reason == timeout_reason
    assert result.terminal_head_sha is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("state", "expected_returncode", "expected_timeout_reason"),
    [
        ("failed", 1, ""),
        ("cancelled", 130, ""),
        ("timed_out", 124, COMMAND_TIMEOUT_REASON),
    ],
)
async def test_agent_delegation_terminal_failure_state_overrides_stale_success_payload(
    state: str,
    expected_returncode: int,
    expected_timeout_reason: str,
) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/agent-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/op_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/op_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "state": state,
                    "returncode": 0,
                    "stdout": "partial agent stdout",
                    "stderr": "hosted operation did not complete successfully",
                    "terminal_head_sha": "b" * 40,
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        result = await HostedAgentRuntimeExecutor(_config(), client=client).execute(
            _agent_request()
        )

    assert result.returncode == expected_returncode
    assert result.stdout == "partial agent stdout"
    assert result.stderr == "hosted operation did not complete successfully"
    assert result.timeout_reason == expected_timeout_reason
    assert result.terminal_head_sha is None


@pytest.mark.unit
async def test_agent_delegation_rejects_oversized_poll_response_before_json_parse() -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/agent-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/op_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/op_1":
            return httpx.Response(
                200,
                headers={"Content-Length": "70000"},
                content=b"{",
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        with pytest.raises(HostedDelegationProtocolError, match="response exceeds"):
            await HostedAgentRuntimeExecutor(_config(max_output_bytes=16), client=client).execute(
                _agent_request()
            )


@pytest.mark.unit
async def test_agent_delegation_rejects_oversized_poll_response_without_valid_length() -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/agent-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/op_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/op_1":
            return httpx.Response(
                200,
                headers={"Content-Length": "not-an-int"},
                content=b"{" + (b"x" * 70000),
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        with pytest.raises(HostedDelegationProtocolError, match="response exceeds"):
            await HostedAgentRuntimeExecutor(_config(max_output_bytes=16), client=client).execute(
                _agent_request()
            )


@pytest.mark.unit
async def test_agent_delegation_rejects_terminal_output_over_max_output_bytes() -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/agent-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/op_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/op_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "state": "failed",
                    "returncode": 1,
                    "stdout": "12345",
                    "stderr": "",
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        with pytest.raises(HostedDelegationProtocolError, match="output exceeds"):
            await HostedAgentRuntimeExecutor(_config(max_output_bytes=4), client=client).execute(
                _agent_request()
            )


@pytest.mark.unit
@pytest.mark.parametrize(
    "terminal_payload",
    [
        {"operation_id": "op_1", "workspace_id": "other", "state": "succeeded"},
        {"operation_id": "op_2", "workspace_id": "ws_hosted", "state": "succeeded"},
        {"operation_id": "op_1", "workspace_id": "ws_hosted", "state": "paused"},
        {"operation_id": "op_1", "workspace_id": "ws_hosted", "state": "succeeded"},
        {
            "operation_id": "op_1",
            "workspace_id": "ws_hosted",
            "state": "succeeded",
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
        },
    ],
)
async def test_agent_delegation_rejects_malformed_or_cross_workspace_terminal_response(
    terminal_payload: dict[str, object],
) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                202,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/op_1",
                },
            )
        return httpx.Response(200, json=terminal_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        with pytest.raises(HostedDelegationProtocolError) as excinfo:
            await HostedAgentRuntimeExecutor(_config(), client=client).execute(
                _agent_request(prompt_stdin=b"do not leak")
            )

    assert "secret-token" not in str(excinfo.value)
    assert "do not leak" not in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("terminal_payload", "match"),
    [
        (
            {
                "operation_id": "op_1",
                "workspace_id": "ws_hosted",
                "state": "succeeded",
                "returncode": 0,
                "stderr": "",
                "terminal_head_sha": "b" * 40,
            },
            "missing stdout",
        ),
        (
            {
                "operation_id": "op_1",
                "workspace_id": "ws_hosted",
                "state": "succeeded",
                "stdout": "ok",
                "stderr": "",
                "terminal_head_sha": "b" * 40,
            },
            "missing returncode",
        ),
    ],
)
async def test_agent_delegation_rejects_malformed_success_result(
    terminal_payload: dict[str, object],
    match: str,
) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/agent-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/op_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/op_1":
            return httpx.Response(200, json=terminal_payload)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        with pytest.raises(HostedDelegationProtocolError, match=match):
            await HostedAgentRuntimeExecutor(_config(), client=client).execute(_agent_request())


@pytest.mark.unit
@pytest.mark.parametrize(
    ("start_payload", "match"),
    [
        (
            {
                "operation_id": "op_1",
                "workspace_id": "other_workspace",
                "operation_url": "/v1/operations/op_1",
            },
            "workspace mismatch",
        ),
        (
            {
                "operation_id": "op_1",
                "workspace_id": "ws_hosted",
                "operation_url": "https://evil.example.test/v1/operations/op_1",
            },
            "origin mismatch",
        ),
        (
            {
                "operation_id": "op_1",
                "workspace_id": "ws_hosted",
                "operation_url": "v1/operations/op_1",
            },
            "absolute path",
        ),
        (
            {
                "workspace_id": "ws_hosted",
                "operation_url": "/v1/operations/op_1",
            },
            "missing operation_id",
        ),
    ],
)
async def test_agent_delegation_rejects_malformed_start_operation(
    start_payload: dict[str, object],
    match: str,
) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/agent-runs"
        return httpx.Response(202, json=start_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        with pytest.raises(HostedDelegationProtocolError, match=match):
            await HostedAgentRuntimeExecutor(_config(), client=client).execute(_agent_request())


@pytest.mark.unit
@pytest.mark.parametrize(
    "start_response",
    [
        httpx.Response(202, content=b"not-json"),
        httpx.Response(202, json=[]),
    ],
)
async def test_agent_delegation_rejects_non_object_start_response(
    start_response: httpx.Response,
) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/agent-runs"
        return start_response

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        with pytest.raises(HostedDelegationProtocolError):
            await HostedAgentRuntimeExecutor(_config(), client=client).execute(_agent_request())


@pytest.mark.unit
@pytest.mark.parametrize(
    ("poll_content", "match"),
    [
        (b"not-json", "non-json"),
        (b"[]", "non-object"),
    ],
)
async def test_agent_delegation_rejects_non_object_poll_response(
    poll_content: bytes,
    match: str,
) -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/agent-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/op_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/op_1":
            return httpx.Response(200, content=poll_content)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        with pytest.raises(HostedDelegationProtocolError, match=match):
            await HostedAgentRuntimeExecutor(_config(), client=client).execute(_agent_request())


@pytest.mark.unit
async def test_agent_delegation_accepts_streamed_poll_response_without_content_length() -> None:
    terminal_head_sha = "c" * 40

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/agent-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/op_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/op_1":
            payload = json.dumps(
                {
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "returncode": 0,
                    "stdout": "streamed ok",
                    "stderr": "",
                    "terminal_head_sha": terminal_head_sha,
                }
            ).encode()
            return httpx.Response(200, stream=httpx.ByteStream(payload))
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        result = await HostedAgentRuntimeExecutor(_config(), client=client).execute(
            _agent_request()
        )

    assert result.returncode == 0
    assert result.stdout == "streamed ok"
    assert result.terminal_head_sha == terminal_head_sha


@pytest.mark.unit
async def test_agent_delegation_terminal_failure_synthesizes_stderr_when_host_omits_it() -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/agent-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/op_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/op_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "state": "failed",
                    "stdout": "partial output",
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        result = await HostedAgentRuntimeExecutor(_config(), client=client).execute(
            _agent_request()
        )

    assert result.returncode == 1
    assert result.stdout == "partial output"
    assert result.stderr == "hosted agent operation failed\n"
    assert result.terminal_head_sha is None


@pytest.mark.unit
async def test_agent_delegation_operation_timeout_posts_cancel_and_maps_timeout() -> None:
    cancel_paths: list[str] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/agent-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/op_1",
                },
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"operation_id": "op_1", "workspace_id": "ws_hosted", "state": "running"},
            )
        if request.method == "POST" and request.url.path == "/v1/operations/op_1/cancel":
            cancel_paths.append(request.url.path)
            return httpx.Response(202, json={"state": "cancelled"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        result = await HostedAgentRuntimeExecutor(
            _config(operation_timeout_seconds=0.003),
            client=client,
        ).execute(_agent_request())

    assert cancel_paths == ["/v1/operations/op_1/cancel"]
    assert result.returncode == 124
    assert result.timeout_reason == COMMAND_TIMEOUT_REASON
    assert result.terminal_head_sha is None


@pytest.mark.unit
async def test_agent_delegation_operation_timeout_ignores_cancel_failure() -> None:
    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/agent-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/op_1",
                },
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"operation_id": "op_1", "workspace_id": "ws_hosted", "state": "running"},
            )
        if request.method == "POST" and request.url.path == "/v1/operations/op_1/cancel":
            raise httpx.ConnectError("cancel endpoint unavailable", request=request)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        result = await HostedAgentRuntimeExecutor(
            _config(operation_timeout_seconds=0.003),
            client=client,
        ).execute(_agent_request())

    assert result.returncode == 124
    assert result.timeout_reason == COMMAND_TIMEOUT_REASON
    assert result.terminal_head_sha is None


@pytest.mark.unit
async def test_agent_delegation_cancellation_posts_cancel_without_leaking_prompt_or_token() -> None:
    started_poll = asyncio.Event()
    cancel_paths: list[str] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/agent-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "op_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/op_1",
                },
            )
        if request.method == "GET":
            started_poll.set()
            await asyncio.sleep(10)
        if request.method == "POST" and request.url.path == "/v1/operations/op_1/cancel":
            cancel_paths.append(request.url.path)
            return httpx.Response(202, json={"state": "cancelled"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        task = asyncio.create_task(
            HostedAgentRuntimeExecutor(_config(), client=client).execute(
                _agent_request(prompt_stdin=b"cancel prompt")
            )
        )
        await started_poll.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert cancel_paths == ["/v1/operations/op_1/cancel"]
