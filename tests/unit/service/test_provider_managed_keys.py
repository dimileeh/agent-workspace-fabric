"""Managed-key broker lifecycle against Postgres and a fake management API."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from awf.adapters.managed_credentials import ProviderCredentialError
from awf.common.agent_execution import EXECUTION_POLICY_KEY, resolve_execution
from awf.db.models import ProviderManagedKey, WorkspaceEvent
from awf.db.repositories import ProviderManagedKeyRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.openrouter_management import OpenRouterManagementClient
from awf.service.provider_managed_keys import (
    PROVIDER_PARK_REASON_CODES,
    ManagedKeyBroker,
    managed_key_name,
    remaining_budget,
)
from tests.postgres import postgres_test_engine
from tests.shared.openrouter_management import FAKE_MANAGEMENT_KEY, FakeOpenRouterManagement

pytestmark = pytest.mark.unit

_SELECTION = resolve_execution(
    agent="codex",
    provider="openrouter",
    model="moonshotai/kimi-k2.7-code",
    allow_unverified=True,
    managed_openrouter=True,
)


async def _workspace(factory, *, budget: float = 10.0) -> str:
    async with factory() as session:
        ws = await WorkspaceRepository(session).create(
            repo_url="git@example.com:repo.git",
            branch_base="main",
            task_title="managed",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        ws.task_policy = {
            EXECUTION_POLICY_KEY: _SELECTION.model_dump(mode="json"),
            "provider_budget": {
                "version": 1,
                "provider": "openrouter",
                "managed": True,
                "limit_usd": budget,
                "source": "default",
            },
        }
        await session.commit()
        return ws.id


async def _rows(factory, workspace_id: str) -> list[ProviderManagedKey]:
    async with factory() as session:
        return await ProviderManagedKeyRepository(session).for_workspace(workspace_id)


def _broker(factory, fake: FakeOpenRouterManagement, **kwargs) -> ManagedKeyBroker:
    return ManagedKeyBroker(session_factory=factory, client=fake.client(), **kwargs)


async def test_mint_injects_only_the_selected_credential_and_persists_identity():
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = _broker(factory, fake)

        lease = await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")

        assert lease.env == {"OPENROUTER_API_KEY": fake.minted_values[0]}
        assert lease.secrets == (fake.minted_values[0],)
        assert fake.minted_values[0] not in repr(lease)
        post = fake.calls[-1]
        assert post[0] == "POST"
        assert post[2] == {
            "name": f"awf-{ws}-1",
            "limit": 10.0,
            "include_byok_in_limit": True,
        }
        (row,) = await _rows(factory, ws)
        assert (row.seq, row.key_name, row.status) == (1, f"awf-{ws}-1", "active")
        assert row.key_hash == "hash1" and row.limit_usd == Decimal("10")
        assert row.lease_expires_at is not None
        assert managed_key_name(ws, 1) == row.key_name


async def test_release_reads_final_usage_then_deletes_and_rotation_mints_the_remainder():
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = _broker(factory, fake)
        first = await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        fake.set_usage("hash1", 2.5, byok=0.5)

        outcome = await first.release(failed=False)

        methods = [(method, path) for method, path, _ in fake.calls[1:]]
        assert methods == [("GET", "/keys/hash1"), ("DELETE", "/keys/hash1")]
        assert outcome.exhausted is False
        assert outcome.details["spend_usd"] == "3.000000"
        assert outcome.details["remaining_usd"] == "7.000000"
        assert "hash1" not in fake.keys
        second = await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        assert fake.calls[-1][2]["name"] == f"awf-{ws}-2"
        assert fake.calls[-1][2]["limit"] == 7.0
        await second.release(failed=False)
        rows = await _rows(factory, ws)
        assert [(r.seq, r.status, r.final_usage_read) for r in rows] == [
            (1, "deleted", True),
            (2, "deleted", True),
        ]
        assert rows[0].usage_usd == Decimal("2.5") and rows[0].byok_usage_usd == Decimal("0.5")
        # Releasing twice is a no-op that returns the same verdict.
        assert (await first.release(failed=True)) == outcome


async def test_restart_finalizes_an_expired_stale_key_before_minting_a_new_one():
    fake = FakeOpenRouterManagement()
    now = datetime.now(UTC)
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        await _broker(factory, fake).acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        fake.set_usage("hash1", 4.0)
        # A worker restart lost the in-memory lease; its row lease expires.
        restarted = _broker(factory, fake, now=lambda: now + timedelta(hours=1))

        await restarted.acquire(workspace_id=ws, selection=_SELECTION, purpose="monitor")

        methods = [(m, p) for m, p, _ in fake.calls]
        assert methods[1:4] == [
            ("GET", "/keys/hash1"),
            ("DELETE", "/keys/hash1"),
            ("POST", "/keys"),
        ]
        assert fake.calls[3][2]["limit"] == 6.0
        rows = await _rows(factory, ws)
        assert [(r.seq, r.status, r.purpose) for r in rows] == [
            (1, "deleted", "agent"),
            (2, "active", "monitor"),
        ]


async def test_live_concurrent_lease_reserves_its_headroom_instead_of_being_deleted():
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = _broker(factory, fake)
        await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        fake.set_usage("hash1", 1.0)
        with pytest.raises(ProviderCredentialError) as raised:
            await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        assert raised.value.reason_code == "PROVIDER_BUDGET_EXHAUSTED"
        assert "hash1" in fake.keys


async def test_spend_is_summed_across_keys_and_delete_failures_reserve_headroom():
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = _broker(factory, fake)
        for seq, usage in ((1, 1.0), (2, 2.0)):
            lease = await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
            fake.set_usage(f"hash{seq}", usage)
            await lease.release(failed=False)
        lease = await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        fake.set_usage("hash3", 0.5)
        fake.fail[("DELETE", "key")] = 503
        outcome = await lease.release(failed=True)

        async with factory() as session:
            spend = (await ProviderManagedKeyRepository(session).spend_by_workspace_ids([ws]))[ws]
        assert spend.spend_usd == Decimal("3.5")
        assert spend.key_count == 3 and spend.live_key_count == 1
        rows = await _rows(factory, ws)
        assert rows[-1].status == "delete_failed"
        assert rows[-1].last_reason_code == "PROVIDER_MANAGEMENT_API_UNAVAILABLE"
        # The undeleted key keeps max(limit=7, usage=0.5) reserved.
        assert remaining_budget(Decimal("10"), rows) == Decimal("0")
        assert outcome.details["remaining_usd"] == "0.000000"
        # Exhaustion is the authoritative key read, not reserved headroom.
        assert outcome.exhausted is False


async def test_exhausted_budget_never_calls_the_management_api():
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory, budget=1.0)
        broker = _broker(factory, fake)
        lease = await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        fake.set_usage("hash1", 0.995)
        await lease.release(failed=True)
        calls = len(fake.calls)

        with pytest.raises(ProviderCredentialError) as raised:
            await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")

        assert raised.value.reason_code == "PROVIDER_BUDGET_EXHAUSTED"
        assert raised.value.reason_code in PROVIDER_PARK_REASON_CODES
        assert raised.value.details["provider_budget"]["budget_usd"] == "1.000000"
        assert raised.value.details["provider_budget"]["spend_usd"] == "0.995000"
        assert len(fake.calls) == calls


async def test_mint_failure_records_a_failed_intent_and_blocks_only_this_selection():
    fake = FakeOpenRouterManagement()
    fake.fail[("POST", "collection")] = 429
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)

        with pytest.raises(ProviderCredentialError) as raised:
            await _broker(factory, fake).acquire(
                workspace_id=ws, selection=_SELECTION, purpose="agent"
            )

        assert raised.value.reason_code == "PROVIDER_MANAGED_KEY_UNAVAILABLE"
        assert raised.value.details["management_reason_code"] == (
            "PROVIDER_MANAGEMENT_API_RATE_LIMITED"
        )
        (row,) = await _rows(factory, ws)
        assert (row.status, row.key_hash, row.last_reason_code) == (
            "mint_failed",
            None,
            "PROVIDER_MANAGEMENT_API_RATE_LIMITED",
        )
        async with factory() as session:
            spend = (await ProviderManagedKeyRepository(session).spend_by_workspace_ids([ws]))[ws]
        assert spend.key_count == 0


async def test_missing_management_key_or_budget_fails_closed():
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        with pytest.raises(ProviderCredentialError) as raised:
            await ManagedKeyBroker(session_factory=factory, client=None).acquire(
                workspace_id=ws, selection=_SELECTION, purpose="agent"
            )
        assert raised.value.reason_code == "PROVIDER_MANAGEMENT_KEY_MISSING"
        async with factory() as session:
            row = await WorkspaceRepository(session).get(ws)
            row.task_policy = {**row.task_policy, "provider_budget": {"managed": True}}
            await session.commit()
        fake = FakeOpenRouterManagement()
        with pytest.raises(ProviderCredentialError) as raised:
            await _broker(factory, fake).acquire(
                workspace_id=ws, selection=_SELECTION, purpose="agent"
            )
        assert raised.value.reason_code == "PROVIDER_BUDGET_INVALID"
        assert fake.calls == []


async def test_heartbeat_persists_usage_and_extends_the_lease_without_raising():
    fake = FakeOpenRouterManagement()
    clock = [datetime.now(UTC)]
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = _broker(factory, fake, now=lambda: clock[0], usage_poll_interval_seconds=60)
        assert broker.heartbeat_interval_seconds == 60
        lease = await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        (before,) = await _rows(factory, ws)
        fake.set_usage("hash1", 1.75)
        clock[0] += timedelta(seconds=60)

        await lease.heartbeat()

        (after,) = await _rows(factory, ws)
        assert after.usage_usd == Decimal("1.75")
        assert after.usage_read_at == clock[0]
        assert after.lease_expires_at > before.lease_expires_at
        fake.fail[("GET", "key")] = 500
        clock[0] += timedelta(seconds=60)
        await lease.heartbeat()
        (failed,) = await _rows(factory, ws)
        assert failed.last_reason_code == "PROVIDER_MANAGEMENT_API_UNAVAILABLE"
        assert failed.usage_usd == Decimal("1.75")
        assert failed.lease_expires_at > after.lease_expires_at


async def test_final_read_failure_keeps_the_key_for_a_later_finalize():
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = _broker(factory, fake)
        lease = await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        fake.fail[("GET", "key")] = 502

        await lease.release(failed=False)

        assert "hash1" in fake.keys
        (row,) = await _rows(factory, ws)
        assert (row.status, row.final_usage_read) == ("delete_failed", False)
        del fake.fail[("GET", "key")]
        fake.set_usage("hash1", 0.25)
        report = await broker.finalize_workspace_keys(ws)
        assert report == {"finalized": 1, "failed": 0}
        (row,) = await _rows(factory, ws)
        assert (row.status, row.final_usage_read, row.usage_usd) == (
            "deleted",
            True,
            Decimal("0.25"),
        )


async def test_terminal_finalize_deletes_live_keys_and_tolerates_already_deleted_ones():
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = _broker(factory, fake)
        await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        del fake.keys["hash1"]

        assert await broker.finalize_workspace_keys(ws) == {"finalized": 1, "failed": 0}

        (row,) = await _rows(factory, ws)
        assert (row.status, row.last_reason_code) == ("deleted", "PROVIDER_MANAGED_KEY_NOT_FOUND")
        assert await broker.finalize_workspace_keys(ws) == {"finalized": 0, "failed": 0}
        assert await ManagedKeyBroker(session_factory=factory, client=None).finalize_workspace_keys(
            ws
        ) == {"finalized": 0, "failed": 0}


async def test_release_completes_even_when_the_waiting_task_is_cancelled():
    gate = asyncio.Event()
    fake = FakeOpenRouterManagement()

    async def slow(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            await gate.wait()
        return fake.handler(request)

    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        client = OpenRouterManagementClient(
            FAKE_MANAGEMENT_KEY, transport=httpx.MockTransport(slow)
        )
        broker = ManagedKeyBroker(session_factory=factory, client=client)
        lease = await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        waiter = asyncio.create_task(lease.release(failed=True))
        await asyncio.sleep(0.05)
        waiter.cancel()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await lease.release(failed=True)

        assert "hash1" not in fake.keys
        (row,) = await _rows(factory, ws)
        assert row.status == "deleted"


async def test_no_key_value_or_management_key_is_persisted_in_rows_or_events():
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = _broker(factory, fake)
        lease = await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        await lease.release(failed=False)
        async with factory() as session:
            rows = (await session.execute(select(ProviderManagedKey))).scalars().all()
            events = (
                (
                    await session.execute(
                        select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == ws)
                    )
                )
                .scalars()
                .all()
            )
            dumped = json.dumps(
                [
                    {c.name: str(getattr(r, c.name)) for c in ProviderManagedKey.__table__.columns}
                    for r in rows
                ]
                + [{"type": e.event_type, "payload": e.payload} for e in events],
                default=str,
            )
        assert {e.event_type for e in events} >= {
            "provider.managed_key.minted",
            "provider.managed_key.released",
        }
        for secret in (*fake.minted_values, FAKE_MANAGEMENT_KEY):
            assert secret not in dumped


def test_budget_from_policy_rejects_absent_or_invalid_values():
    from awf.service.provider_managed_keys import budget_from_policy

    assert budget_from_policy({"provider_budget": {"limit_usd": 2.5}}) == Decimal("2.5")
    assert budget_from_policy({"provider_budget": {"limit_usd": "3"}}) == Decimal("3")
    for invalid in (None, True, "abc", "NaN", 0, -1, [1]):
        assert budget_from_policy({"provider_budget": {"limit_usd": invalid}}) is None
    assert budget_from_policy({"provider_budget": "corrupt"}) is None
    assert budget_from_policy(None) is None


def _db_error():
    from sqlalchemy.exc import OperationalError

    return OperationalError("SELECT 1", None, Exception("database unavailable"))


@pytest.mark.parametrize("delete_fails", [False, True])
async def test_unrecorded_minted_key_is_deleted_when_activation_cannot_persist(
    monkeypatch, delete_fails
):
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = _broker(factory, fake)

        async def _fail(*_args, **_kwargs):
            if delete_fails:
                fake.fail[("DELETE", "key")] = 500
            raise _db_error()

        monkeypatch.setattr(broker, "_activate", _fail)
        with pytest.raises(ProviderCredentialError) as raised:
            await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")

        assert raised.value.reason_code == "PROVIDER_MANAGED_KEY_UNAVAILABLE"
        assert raised.value.details == {
            "management_reason_code": "PROVIDER_MANAGED_KEY_PERSIST_FAILED"
        }
        assert ("DELETE", "/keys/hash1") in [(m, p) for m, p, _ in fake.calls]
        assert ("hash1" in fake.keys) is delete_fails


async def test_confirmed_delete_after_activation_failure_releases_the_intent(monkeypatch):
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = _broker(factory, fake)

        async def _fail(*_args, **_kwargs):
            raise _db_error()

        monkeypatch.setattr(broker, "_activate", _fail)
        with pytest.raises(ProviderCredentialError):
            await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")

        (intent,) = await _rows(factory, ws)
        assert (intent.status, intent.last_reason_code) == (
            "deleted",
            "PROVIDER_MANAGED_KEY_PERSIST_FAILED",
        )
        assert intent.deleted_at is not None and intent.lease_expires_at is None
        # The deleted key never served a call, so the next mint gets the whole budget.
        await _broker(factory, fake).acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        assert fake.calls[-1][2]["name"] == f"awf-{ws}-2"
        assert fake.calls[-1][2]["limit"] == 10.0


async def test_heartbeats_after_release_or_on_unhealthy_rows_change_nothing():
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = _broker(factory, fake)
        lease = await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        fake.fail[("DELETE", "key")] = 500
        await lease.release(failed=False)
        (failed,) = await _rows(factory, ws)
        assert failed.status == "delete_failed"
        await lease.heartbeat()
        (unchanged,) = await _rows(factory, ws)
        assert unchanged.lease_expires_at == failed.lease_expires_at is None
        del fake.fail[("DELETE", "key")]
        await broker.finalize_workspace_keys(ws)
        await lease.heartbeat()
        (final,) = await _rows(factory, ws)
        assert final.status == "deleted" and final.lease_expires_at is None


async def test_persistence_failures_are_logged_and_never_raise_into_the_run(monkeypatch):
    import structlog

    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = _broker(factory, fake)
        lease = await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")

        async def _broken(*_args, **_kwargs):
            raise _db_error()

        monkeypatch.setattr(ProviderManagedKeyRepository, "get", _broken)
        monkeypatch.setattr(ProviderManagedKeyRepository, "for_workspace", _broken)
        with structlog.testing.capture_logs() as logs:
            await lease.heartbeat()
            outcome = await lease.release(failed=True)

        assert outcome.exhausted is False
        assert "budget_usd" not in outcome.details
        # Without a committed final usage the key is kept for a later finalize.
        assert outcome.details["deleted"] is False
        assert outcome.details["management_reason_code"] == "PROVIDER_MANAGED_KEY_PERSIST_FAILED"
        assert "hash1" in fake.keys
        reasons = [
            log.get("reason_code")
            for log in logs
            if log["event"] == "provider.managed_key.persist_failed"
        ]
        assert reasons == ["PROVIDER_MANAGED_KEY_PERSIST_FAILED"] * 4


@pytest.mark.parametrize("outage_starts", ["before_delete", "after_delete"])
async def test_final_usage_is_committed_before_the_key_is_deleted(monkeypatch, outage_starts):
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = _broker(factory, fake)
        lease = await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        fake.set_usage("hash1", 6.0)
        get = ProviderManagedKeyRepository.get

        async def _outage(self, key_id):
            deleted = ("DELETE", "/keys/hash1") in [(m, p) for m, p, _ in fake.calls]
            if outage_starts == "before_delete" or deleted:
                raise _db_error()
            return await get(self, key_id)

        with monkeypatch.context() as patch:
            patch.setattr(ProviderManagedKeyRepository, "get", _outage)
            outcome = await lease.release(failed=False)

        assert outcome.details["deleted"] is (outage_starts == "after_delete")
        assert ("hash1" in fake.keys) is (outage_starts == "before_delete")
        # Once the database recovers, finalize keeps the real spend (even on a 404).
        assert await broker.finalize_workspace_keys(ws) == {"finalized": 1, "failed": 0}
        (row,) = await _rows(factory, ws)
        assert (row.status, row.final_usage_read, row.usage_usd) == (
            "deleted",
            True,
            Decimal("6"),
        )
        await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        assert fake.calls[-1][2]["limit"] == 4.0


async def test_release_without_a_budget_reports_no_budget_figures():
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        lease = await _broker(factory, fake).acquire(
            workspace_id=ws, selection=_SELECTION, purpose="agent"
        )
        async with factory() as session:
            row = await WorkspaceRepository(session).get(ws)
            row.task_policy = {k: v for k, v in row.task_policy.items() if k != "provider_budget"}
            await session.commit()

        outcome = await lease.release(failed=False)

        assert set(outcome.details) == {"key_name", "final_usage_read", "deleted"}


async def test_next_acquire_retries_deleting_a_key_whose_delete_failed():
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = _broker(factory, fake)
        lease = await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        fake.set_usage("hash1", 1.0)
        fake.fail[("DELETE", "key")] = 500
        await lease.release(failed=False)
        del fake.fail[("DELETE", "key")]

        await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")

        assert "hash1" not in fake.keys
        assert fake.calls[-1][2]["limit"] == 9.0
        assert [r.status for r in await _rows(factory, ws)] == ["deleted", "active"]


async def test_terminal_workspaces_with_leftover_keys_are_finalized_in_bounded_batches():
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        live = await _workspace(factory)
        done = await _workspace(factory)
        broker = _broker(factory, fake)
        await broker.acquire(workspace_id=live, selection=_SELECTION, purpose="agent")
        await broker.acquire(workspace_id=done, selection=_SELECTION, purpose="agent")
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(done)
            ws.status = "cancelled"
            await session.commit()

        assert await broker.finalize_terminal_workspaces(limit=10) == 1
        assert (
            await ManagedKeyBroker(
                session_factory=factory, client=None
            ).finalize_terminal_workspaces()
            == 0
        )

        assert [r.status for r in await _rows(factory, done)] == ["deleted"]
        assert [r.status for r in await _rows(factory, live)] == ["active"]
        assert set(fake.keys) == {"hash1"}
