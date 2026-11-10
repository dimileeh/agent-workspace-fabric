"""Exact provider spend replaces unknown cost for managed OpenRouter workspaces."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from awf.common.agent_execution import EXECUTION_POLICY_KEY, resolve_execution
from awf.db.repositories import ProviderSpendSummary, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.provider_managed_keys import ManagedKeyBroker
from awf.service.workspace_provider_spend import (
    apply_exact_spend,
    prefetched_provider_spend,
    provider_spend_payload,
    provider_spend_scope,
)
from awf.service.workspaces import WorkspaceService
from tests.shared.openrouter_management import FakeOpenRouterManagement

pytestmark = pytest.mark.unit

_SELECTION = resolve_execution(
    agent="codex",
    provider="openrouter",
    model="m/x",
    allow_unverified=True,
    managed_openrouter=True,
)
_MANAGED_POLICY = {
    EXECUTION_POLICY_KEY: _SELECTION.model_dump(mode="json"),
    "provider_budget": {"version": 1, "managed": True, "limit_usd": 4.0, "source": "default"},
}
_READ_AT = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def _ws(policy, workspace_id: str = "ws_a"):
    return SimpleNamespace(id=workspace_id, task_policy=policy)


def test_unmanaged_workspaces_have_no_provider_spend():
    assert provider_spend_payload(_ws({})) is None
    assert provider_spend_payload(_ws(None)) is None
    usage = {"cost_estimate": None, "status": "unavailable", "source": "none", "reason": "x"}
    assert apply_exact_spend(usage, None, {}) == usage


def test_unloaded_spend_degrades_to_unavailable_without_inventing_cost():
    payload = provider_spend_payload(_ws(_MANAGED_POLICY))

    assert payload is not None
    assert payload["status"] == "unavailable"
    assert payload["reason"] == "provider_spend_not_loaded"
    assert payload["budget_usd"] == 4.0 and payload["spend_usd"] is None
    usage = {"cost_estimate": None, "status": "unavailable", "source": "none", "reason": "x"}
    assert apply_exact_spend(usage, payload, _MANAGED_POLICY) == usage


def test_prefetched_spend_is_exact_and_labelled():
    summary = ProviderSpendSummary(
        spend_usd=Decimal("1.25"), key_count=3, live_key_count=1, last_read_at=_READ_AT
    )
    with prefetched_provider_spend({"ws_a": summary, "ws_b": None}):
        loaded = provider_spend_payload(_ws(_MANAGED_POLICY))
        unspent = provider_spend_payload(_ws(_MANAGED_POLICY, "ws_b"))
        over = provider_spend_payload(
            _ws({**_MANAGED_POLICY, "provider_budget": {"managed": True, "limit_usd": 1.0}})
        )

    assert loaded == {
        "kind": "exact_provider_spend",
        "provider": "openrouter",
        "status": "available",
        "spend_usd": 1.25,
        "budget_usd": 4.0,
        "remaining_usd": 2.75,
        "currency": "USD",
        "key_count": 3,
        "live_key_count": 1,
        "last_read_at": _READ_AT,
        "source": "openrouter_key_usage",
        "reason": None,
    }
    assert unspent["spend_usd"] == 0.0 and unspent["key_count"] == 0
    assert over["remaining_usd"] == 0.0
    usage = {
        "input_tokens": None,
        "cost_estimate": None,
        "currency": None,
        "status": "unavailable",
        "source": "none",
        "reason": "usage_not_reported",
    }
    assert apply_exact_spend(usage, loaded, _MANAGED_POLICY) == {
        "input_tokens": None,
        "cost_estimate": 1.25,
        "currency": "USD",
        "status": "available",
        "source": "openrouter_key_usage",
        "reason": None,
    }


def test_managed_fallback_does_not_replace_a_native_primary_cost():
    fallback = {"agent": "codex", "provider": "openrouter", "model": "m/x"}
    fallback[EXECUTION_POLICY_KEY] = _SELECTION.model_dump(mode="json")
    native = resolve_execution(agent="codex", model="gpt-5.1-codex")
    policy = {
        EXECUTION_POLICY_KEY: native.model_dump(mode="json"),
        "provider_recovery": {"fallbacks": [fallback]},
        "provider_budget": _MANAGED_POLICY["provider_budget"],
    }
    usage = {"cost_estimate": 3.4, "currency": "USD", "status": "available", "source": "ccusage"}
    with prefetched_provider_spend({"ws_a": None}):
        spend = provider_spend_payload(_ws(policy))

    assert spend is not None and spend["status"] == "available" and spend["spend_usd"] == 0.0
    assert apply_exact_spend(usage, spend, policy) == usage
    promoted = {**policy, EXECUTION_POLICY_KEY: fallback[EXECUTION_POLICY_KEY]}
    assert apply_exact_spend(usage, spend, promoted)["cost_estimate"] == 0.0
    assert apply_exact_spend(usage, spend, {EXECUTION_POLICY_KEY: {"version": 0}}) == usage


async def _managed_workspace(factory, fake: FakeOpenRouterManagement) -> str:
    async with factory() as session:
        ws = await WorkspaceRepository(session).create(
            repo_url="git@example.com:repo.git",
            branch_base="main",
            task_title="managed",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        ws.task_policy = dict(_MANAGED_POLICY)
        await session.commit()
        workspace_id = ws.id
    broker = ManagedKeyBroker(session_factory=factory, client=fake.client())
    for usage in (0.5, 0.75):
        lease = await broker.acquire(workspace_id=workspace_id, selection=_SELECTION, purpose="a")
        fake.set_usage(lease.key_hash, usage)
        await lease.release(failed=False)
    return workspace_id


async def test_scope_loads_every_workspace_in_one_grouped_query(engine):
    fake = FakeOpenRouterManagement()
    factory = make_session_factory(engine)
    workspace_id = await _managed_workspace(factory, fake)
    statements: list[str] = []

    def _record(_conn, _cursor, statement, *_args):
        if "provider_managed_keys" in statement:
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        async with factory() as session, provider_spend_scope(session, [workspace_id, "ws_x"]):
            payload = provider_spend_payload(_ws(_MANAGED_POLICY, workspace_id))
            missing = provider_spend_payload(_ws(_MANAGED_POLICY, "ws_x"))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)

    assert len(statements) == 1
    assert payload["spend_usd"] == 1.25 and payload["key_count"] == 2
    assert missing["status"] == "available" and missing["spend_usd"] == 0.0


async def test_rest_surfaces_expose_exact_spend(client, engine):
    fake = FakeOpenRouterManagement()
    factory = make_session_factory(engine)
    workspace_id = await _managed_workspace(factory, fake)
    async with factory() as session:
        static = await WorkspaceRepository(session).create(
            repo_url="git@example.com:repo.git",
            branch_base="main",
            task_title="static",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await session.commit()
        static_id = static.id

    show = (await client.get(f"/v1/workspaces/{workspace_id}")).json()
    listed = {w["id"]: w for w in (await client.get("/v1/workspaces")).json()}
    overview_items = (await client.get("/v1/workspaces/overview")).json()["items"]
    overview = {w["workspace_id"]: w for w in overview_items}
    batch = (
        await client.post(
            "/v1/workspaces/overview/batch", json={"workspace_ids": [workspace_id, static_id]}
        )
    ).json()["items"]
    tasks = {t["workspace_id"]: t for t in (await client.get("/v1/tasks")).json()["items"]}

    for item in (show, listed[workspace_id], overview[workspace_id], batch[0], tasks[workspace_id]):
        assert item["provider_spend"]["spend_usd"] == 1.25
        assert item["provider_spend"]["remaining_usd"] == 2.75
        assert item["llm_usage"]["cost_estimate"] == 1.25
        assert item["llm_usage"]["source"] == "openrouter_key_usage"
    for item in (listed[static_id], overview[static_id], batch[1], tasks[static_id]):
        assert item["provider_spend"] is None
        assert item["llm_usage"]["source"] != "openrouter_key_usage"


async def test_in_process_service_exposes_exact_spend(engine):
    fake = FakeOpenRouterManagement()
    factory = make_session_factory(engine)
    workspace_id = await _managed_workspace(factory, fake)
    service = WorkspaceService(factory)

    shown = await service.get(workspace_id)
    listed = await service.list()

    assert shown.provider_spend.spend_usd == 1.25
    assert listed[0].provider_spend.spend_usd == 1.25
    assert shown.llm_usage.source == "openrouter_key_usage"
