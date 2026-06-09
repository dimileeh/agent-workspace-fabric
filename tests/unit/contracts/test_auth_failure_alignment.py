"""Auth failure contract: REST 401/503 envelopes; CLI exit; MCP redaction.

REST control endpoints require ``Authorization: Bearer <AWF_API_TOKEN>``. With
no token configured, the dependency returns 503 ``API_TOKEN_NOT_CONFIGURED``;
with a wrong token, 401 ``UNAUTHORIZED`` and ``WWW-Authenticate: Bearer``.

CLI surfaces the REST body verbatim and exits non-zero on auth failure.

MCP runs over stdio in-process and is process-locally trusted by the parent
operator — it does **not** enforce ``Authorization``. The harness asserts that
the MCP server, when configured with an API token, never echoes the configured
token value in any read-only response (the existing redaction layer is the
operator-visible auth boundary on MCP responses).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from awf.api.app import configure_database, create_app
from awf.cli.main import app as cli_app
from awf.common.config import Settings, get_settings
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from tests.unit.contracts._capabilities import (
    CAPABILITIES_BY_NAME,
    ContractCapability,
    all_capabilities,
    mutating_capabilities,
    normalize_rest_error_body,
)
from tests.unit.contracts._introspection import cli_commands
from tests.unit.contracts._stack import ContractStack

_runner = CliRunner()


def _is_http_backed_cli_capability(capability: ContractCapability) -> bool:
    if capability.cli_tokens is None:
        return False
    command_info = cli_commands().get(capability.cli_tokens)
    return command_info is not None and "--base-url" in command_info.options


PROTECTED_REST_CAPABILITY_NAMES = tuple(
    sorted(capability.name for capability in all_capabilities() if capability.auth_required)
)

PROTECTED_CLI_CAPABILITY_NAMES = tuple(
    sorted(
        capability.name
        for capability in all_capabilities()
        if capability.auth_required and _is_http_backed_cli_capability(capability)
    )
)

_CLI_ARGUMENT_VALUES = {
    "workspace_id": "ws_auth_contract",
    "stream_id": "agent.stdout",
    "operation_id": "op_auth_contract",
}

_CLI_OPTION_VALUES_BY_CAPABILITY = {
    "adopt_pr_monitor": {
        "--repo": "owner/repo",
        "--pr": "1",
    },
    "create_workspace": {
        "--repo": "git@github.com:dimileeh/aira-agent.git",
        "--title": "Auth contract",
        "--prompt": "Create workspace auth contract.",
        "--agent": "codex",
    },
}


@pytest.mark.unit
async def test_rest_returns_401_envelope_on_wrong_bearer(
    contract_stack: ContractStack,
) -> None:
    response = await contract_stack.client.delete(
        "/v1/workspaces/ws_anything",
        headers={"Authorization": "Bearer not-the-real-token"},
    )
    assert response.status_code == 401
    envelope = normalize_rest_error_body(response.json())
    assert envelope["error_code"] == "UNAUTHORIZED"
    assert "AWF API token" in envelope["message"]
    assert response.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.unit
async def test_rest_returns_401_envelope_on_missing_bearer(
    contract_stack: ContractStack,
) -> None:
    response = await contract_stack.client.post(
        "/v1/workspaces/ws_anything/cancel",
        headers={"Idempotency-Key": "no-auth"},
        json={"reason": "no auth", "stop_stack": True},
    )
    assert response.status_code == 401
    envelope = normalize_rest_error_body(response.json())
    assert envelope["error_code"] == "UNAUTHORIZED"


@pytest.mark.unit
async def test_rest_retry_workspace_requires_bearer_when_token_configured(
    contract_stack: ContractStack,
) -> None:
    response = await contract_stack.client.post("/v1/workspaces/ws_anything/retry")

    assert response.status_code == 401
    envelope = normalize_rest_error_body(response.json())
    assert envelope["error_code"] == "UNAUTHORIZED"


@pytest.mark.unit
async def test_rest_returns_503_envelope_when_api_token_not_configured(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """503 ``API_TOKEN_NOT_CONFIGURED`` when the local service has no token set.

    Boots a fresh app whose settings dependency returns ``api_token=None``,
    then proves the bearer dependency surfaces the agreed-upon envelope.
    """
    work_dir = tmp_path / "awf-state"
    monkeypatch.setenv("AWF_WORK_DIR", str(work_dir))
    monkeypatch.delenv("AWF_API_TOKEN", raising=False)
    get_settings.cache_clear()

    factory = make_session_factory(engine)
    settings = Settings(
        _env_file=None,
        api_token=None,
        work_dir=str(work_dir),
        min_free_disk_bytes=700,
        worker_max_concurrent_provisions=5,
        worker_max_concurrent_executions=2,
    )
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(
            "/v1/workspaces/ws_any",
            headers={"Authorization": "Bearer anything", "Idempotency-Key": "k"},
        )

    assert response.status_code == 503
    envelope = normalize_rest_error_body(response.json())
    assert envelope["error_code"] == "API_TOKEN_NOT_CONFIGURED"
    assert "AWF_API_TOKEN" in envelope["message"]


@pytest.mark.unit
def test_cli_propagates_rest_unauthorized_envelope_and_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI surfaces the REST body verbatim and exits non-zero on 401."""
    body = json.dumps(
        {"detail": {"error_code": "UNAUTHORIZED", "message": "Invalid AWF API token."}}
    )

    def _capture(method: str, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            status_code=401,
            content=body.encode(),
            headers={"content-type": "application/json"},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("awf.cli.main.httpx.request", _capture)
    monkeypatch.setenv("AWF_API_TOKEN", "wrong")

    result = _runner.invoke(
        cli_app,
        [
            "workspace",
            "remonitor",
            "ws_target",
            "--idempotency-key",
            "cli-401",
        ],
    )
    assert result.exit_code != 0
    combined = (result.output or "") + (result.stderr or "")
    assert "UNAUTHORIZED" in combined


@pytest.mark.unit
async def test_mcp_redacts_configured_api_token_in_responses(
    contract_stack: ContractStack,
) -> None:
    """MCP responses must never echo the configured ``AWF_API_TOKEN`` literal."""
    tools = await contract_stack.mcp.list_tools()
    by_name = {tool.name: tool for tool in tools}

    health_tool = by_name.get("awf_get_service_health")
    assert health_tool is not None

    result = await contract_stack.mcp.call_tool("awf_get_service_health", {})
    serialized = json.dumps(result.structuredContent or {})
    assert contract_stack.api_token not in serialized
    text_blocks = [getattr(block, "text", "") for block in (result.content or [])]
    assert all(contract_stack.api_token not in text for text in text_blocks)


@pytest.mark.unit
async def test_mcp_does_not_expose_authorization_arg_on_mutating_tools(
    contract_stack: ContractStack,
) -> None:
    """MCP mutating tools don't take an authorization arg.

    MCP runs in-process with the parent operator's trust. Tools must not let an
    agent pass an arbitrary ``authorization`` / ``api_token`` field that could
    silently bypass any future auth boundary. The covered tools are derived from
    the contract registry so any newly added mutating tool is automatically
    swept up by this security check.
    """
    tools = {tool.name: tool for tool in await contract_stack.mcp.list_tools()}
    forbidden_keys = {"authorization", "api_token", "bearer", "token"}
    mutating_tool_names = [c.mcp_tool for c in mutating_capabilities() if c.mcp_tool]
    assert mutating_tool_names, "registry yielded no mutating MCP tools"
    for name in mutating_tool_names:
        properties = tools[name].inputSchema.get("properties", {})
        leak = forbidden_keys & set(properties)
        assert not leak, f"{name}: mutating tool exposes auth arg(s) {sorted(leak)}"


@pytest.mark.unit
async def test_rest_cancel_workspace_rejects_wrong_bearer_after_token_required(
    contract_stack: ContractStack,
) -> None:
    """``POST /v1/workspaces/{id}/cancel`` requires a bearer when token is configured.

    Sanity-check that the auth-protected control routes (cancel/stop/destroy/
    remonitor/refresh/validate/rebase) refuse cross-tenant or stale tokens.
    """
    async with contract_stack.factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@github.com:example/auth.git",
            branch_base="main",
            task_title="Auth contract",
            task_prompt="Exercise auth boundary.",
            agent="codex",
            test_commands=["pytest -q"],
        )
        await session.commit()
        workspace_id = ws.id

    # All control routes share the same dependency, so any one of them proves
    # the boundary. cancel is the smallest mutation surface to test.
    response = await contract_stack.client.post(
        f"/v1/workspaces/{workspace_id}/cancel",
        headers={"Authorization": "Bearer wrong", "Idempotency-Key": "wrong-token"},
        json={"reason": "wrong token", "stop_stack": True},
    )
    assert response.status_code == 401
    envelope = normalize_rest_error_body(response.json())
    assert envelope["error_code"] == "UNAUTHORIZED"


@pytest.mark.unit
@pytest.mark.parametrize("capability_name", PROTECTED_REST_CAPABILITY_NAMES)
async def test_every_registry_protected_rest_route_rejects_wrong_bearer(
    contract_stack: ContractStack,
    capability_name: str,
) -> None:
    capability = CAPABILITIES_BY_NAME[capability_name]
    response = await contract_stack.client.request(
        capability.rest_method,
        _protected_rest_path(capability_name),
        headers=_protected_rest_headers(capability_name),
        params=_protected_rest_params(capability_name),
        json=_protected_rest_body(capability_name),
    )

    assert response.status_code == 401, (capability_name, response.text)
    envelope = normalize_rest_error_body(response.json())
    assert envelope["error_code"] == "UNAUTHORIZED"
    assert response.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.unit
@pytest.mark.parametrize("capability_name", PROTECTED_CLI_CAPABILITY_NAMES)
def test_every_registry_protected_cli_command_propagates_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
    capability_name: str,
) -> None:
    captured: dict[str, Any] = {}
    body = {"detail": {"error_code": "UNAUTHORIZED", "message": "Invalid AWF API token."}}

    def _capture(method: str, url: str, **kwargs: Any) -> httpx.Response:
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        return httpx.Response(
            status_code=401,
            content=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr("awf.cli.main.httpx.request", _capture)
    _seed_protected_cli_auth(monkeypatch, CAPABILITIES_BY_NAME[capability_name])

    result = _runner.invoke(cli_app, _protected_cli_args(capability_name))

    assert result.exit_code != 0
    assert "Authorization" in captured["headers"]
    assert captured["headers"]["Authorization"] == "Bearer wrong-token"
    combined = (result.output or "") + (result.stderr or "")
    assert "UNAUTHORIZED" in combined


def _protected_rest_path(capability_name: str) -> str:
    capability = CAPABILITIES_BY_NAME[capability_name]
    return capability.rest_path.format(
        workspace_id="ws_auth_contract",
        task_ref="task_auth_contract",
        stream_id="agent.stdout",
        operation_id="op_auth_contract",
    )


def _protected_rest_headers(capability_name: str) -> dict[str, str]:
    headers = {"Authorization": "Bearer wrong-token"}
    capability = CAPABILITIES_BY_NAME[capability_name]
    if capability.supports_idempotency_key:
        headers["Idempotency-Key"] = f"{capability_name}-auth"
    return headers


def _protected_rest_params(capability_name: str) -> dict[str, object] | None:
    if capability_name == "destroy_workspace":
        return {"force": True, "remove_volumes": True, "remove_worktree": False}
    if capability_name == "workspace_artifact_download":
        return {"path": "logs/stdout.txt"}
    if capability_name == "read_workspace_log":
        return {"offset": 0, "limit_bytes": 16}
    return None


def _protected_rest_body(capability_name: str) -> dict[str, object] | None:
    if capability_name == "create_workspace":
        return {
            "repo": {
                "url": "git@github.com:dimileeh/aira-agent.git",
                "base_branch": "main",
            },
            "task": {
                "title": "Auth contract",
                "prompt": "Create a workspace under auth-protected route.",
                "agent": "codex",
                "kind": "feature_branch_pr",
            },
            "workspace": {"profile_ref": "auto", "profile": None},
            "validation": {"commands": ["pytest -q"], "requested_tier": 1},
            "resources": {},
        }
    if capability_name == "cancel_workspace":
        return {"reason": "auth contract", "stop_stack": True}
    if capability_name in {
        "stop_workspace",
        "remonitor_workspace",
        "refresh_workspace",
        "rebase_workspace",
    }:
        return {"reason": "auth contract"}
    if capability_name == "request_validation":
        return {"reason": "auth contract", "requested_tier": 1}
    if capability_name == "adopt_pr_monitor":
        return {
            "repo_url": None,
            "repo_slug": "owner/repo",
            "pr_number": 1,
            "pr_url": None,
            "agent": "codex",
            "profile_ref": "auto",
            "profile": None,
            "auto_merge": True,
            "initial_review_grace_period_seconds": None,
            "task_title": None,
            "task_prompt": None,
            "reason": None,
        }
    return None


def _seed_protected_cli_auth(
    monkeypatch: pytest.MonkeyPatch,
    capability: ContractCapability,
) -> None:
    if "--api-token" not in capability.cli_options:
        monkeypatch.setenv("AWF_API_TOKEN", "wrong-token")


def _protected_cli_args(capability_name: str) -> list[str]:
    capability = CAPABILITIES_BY_NAME[capability_name]
    if capability.cli_tokens is None:
        raise AssertionError(f"{capability_name} has no registered CLI command")
    command = list(capability.cli_tokens)
    command.extend(_protected_cli_argument_tokens(capability))
    command.extend(_protected_cli_option_tokens(capability))
    return command


def _protected_cli_argument_tokens(capability: ContractCapability) -> list[str]:
    command_info = cli_commands().get(capability.cli_tokens or ())
    if command_info is None:
        raise AssertionError(
            f"{capability.name}: missing registered CLI command {capability.cli_tokens}"
        )
    values: list[str] = []
    for argument in command_info.argument_order:
        try:
            values.append(_CLI_ARGUMENT_VALUES[argument])
        except KeyError as exc:
            raise AssertionError(
                f"{capability.name}: no auth-contract CLI fixture for argument {argument!r}"
            ) from exc
    return values


def _protected_cli_option_tokens(capability: ContractCapability) -> list[str]:
    values: list[str] = []
    option_values = _CLI_OPTION_VALUES_BY_CAPABILITY.get(capability.name, {})
    for option in sorted(option_values):
        values.extend([option, option_values[option]])
    if capability.requires_idempotency_key and "--idempotency-key" in capability.cli_options:
        values.extend(["--idempotency-key", f"{capability.name}-cli-auth"])
    if "--api-token" in capability.cli_options:
        values.extend(["--api-token", "wrong-token"])
    return values
