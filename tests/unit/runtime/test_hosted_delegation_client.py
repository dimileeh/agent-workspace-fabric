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
from awf.db.enums import AgentRuntime
from awf.runtime.hosted_delegation import (
    HostedAgentRuntimeExecutor,
    HostedDelegationConfig,
    HostedDelegationProtocolError,
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
    "terminal_payload",
    [
        {"operation_id": "op_1", "workspace_id": "other", "state": "succeeded"},
        {"operation_id": "op_2", "workspace_id": "ws_hosted", "state": "succeeded"},
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
