"""MCP create forwards expected_base_sha into the persisted policy snapshot."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from mcp.types import CallToolResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.config import get_settings
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.mcp.server import WorkspaceService, build_mcp_server
from tests.postgres import postgres_test_engine

pytestmark = pytest.mark.usefixtures("mock_docker_cli_probe")

_SHA = "b" * 40


@pytest.fixture(autouse=True)
def _provider_auth_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CODEX_AUTH_TOKEN", "unit-test-provider-token")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _args(*, pin: str | None) -> dict[str, object]:
    args: dict[str, object] = {
        "repo_url": "git@github.com:example/expected-base.git",
        "base_branch": "development",
        "task_title": "Pin development",
        "task_prompt": "Implement against the inspected commit.",
        "agent": "codex",
        "provider_readiness_override": True,
        "provider_readiness_override_reason": "mcp expected base sha",
    }
    if pin is not None:
        args["expected_base_sha"] = pin
    return args


async def _policy(factory: async_sessionmaker[AsyncSession], payload: object) -> dict[str, object]:
    assert isinstance(payload, dict)
    async with factory() as session:
        row = await WorkspaceRepository(session).get(str(payload["workspace_id"]))
    assert row is not None
    assert isinstance(row.task_policy, dict)
    return row.task_policy


@pytest.mark.unit
async def test_mcp_create_persists_expected_base_sha_and_omits_it_when_absent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    mcp = build_mcp_server(service=WorkspaceService(factory))
    pinned = await mcp.call_tool("awf_create_workspace", _args(pin=_SHA))
    omitted = await mcp.call_tool("awf_create_workspace", _args(pin=None))
    pinned_payload = pinned.structuredContent if isinstance(pinned, CallToolResult) else pinned[1]
    omitted_payload = (
        omitted.structuredContent if isinstance(omitted, CallToolResult) else omitted[1]
    )

    assert (await _policy(factory, pinned_payload))["expected_base_sha"] == _SHA
    assert "expected_base_sha" not in await _policy(factory, omitted_payload)


async def test_mcp_provider_selection_reports_disabled_admission(factory):
    mcp = build_mcp_server(service=WorkspaceService(factory))
    result = await mcp.call_tool(
        "awf_create_workspace",
        {**_args(pin=None), "provider": "ollama", "model": "coder", "allow_unverified_model": True},
    )
    payload = result.structuredContent if isinstance(result, CallToolResult) else result[1]
    assert payload["error_code"] == "PROVIDER_READINESS_PRECHECK_FAILED"
    assert (
        payload["detail"]["provider_readiness_preflight"]["reason_code"]
        == "THIRD_PARTY_AGENT_PROVIDER_ADMISSION_DISABLED"
    )


async def test_mcp_create_forwards_the_managed_provider_budget(factory, monkeypatch):
    from datetime import UTC, datetime

    from awf.common.config import Settings
    from awf.db.repositories import WorkerHeartbeatRepository

    now = datetime.now(UTC)
    async with factory() as session:
        await WorkerHeartbeatRepository(session).record_heartbeat(
            worker_id="managed",
            node_id="local",
            started_at=now,
            last_heartbeat_at=now,
            poll_interval_seconds=5,
            execution_contract_version=3,
        )
        await session.commit()
    settings = Settings(
        _env_file=None,
        third_party_agent_providers_enabled=True,
        openrouter_management_key="sk-or-mgmt-mcp-sentinel",
    )
    mcp = build_mcp_server(service=WorkspaceService(factory, settings=settings), settings=settings)
    args = {
        **_args(pin=None),
        "provider": "openrouter",
        "model": "m/x",
        "allow_unverified_model": True,
        "provider_budget_usd": 4.5,
    }
    args.pop("provider_readiness_override")
    args.pop("provider_readiness_override_reason")

    result = await mcp.call_tool("awf_create_workspace", args)

    payload = result.structuredContent if isinstance(result, CallToolResult) else result[1]
    budget = (await _policy(factory, payload))["provider_budget"]
    assert budget["limit_usd"] == 4.5 and budget["source"] == "task"
