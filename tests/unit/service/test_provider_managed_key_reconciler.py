"""The periodic reconciler deletes orphaned ``awf-*`` keys and keeps live ones."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from awf.adapters.managed_credentials import ProviderCredentialError
from awf.common.agent_execution import EXECUTION_POLICY_KEY, resolve_execution
from awf.db.enums import WorkspaceStatus
from awf.db.models import WorkspaceEvent
from awf.db.repositories import ProviderManagedKeyRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.openrouter_management import OpenRouterManagementError
from awf.service.provider_managed_key_reconciler import reconcile_openrouter_keys
from awf.service.provider_managed_keys import ManagedKeyBroker
from tests.postgres import postgres_test_engine
from tests.shared.openrouter_management import FakeOpenRouterManagement

pytestmark = pytest.mark.unit

_SELECTION = resolve_execution(
    agent="codex",
    provider="openrouter",
    model="m/x",
    allow_unverified=True,
    managed_openrouter=True,
)


async def _workspace(factory, *, status: WorkspaceStatus = WorkspaceStatus.running) -> str:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.create(
            repo_url="git@example.com:repo.git",
            branch_base="main",
            task_title="managed",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        ws.task_policy = {
            EXECUTION_POLICY_KEY: _SELECTION.model_dump(mode="json"),
            "provider_budget": {"managed": True, "limit_usd": 10.0},
        }
        steps = {
            WorkspaceStatus.running: ("provisioning", "ready", "running"),
            WorkspaceStatus.cancelled: ("cancelled",),
        }[status]
        for step in steps:
            await repo.transition(ws, to=WorkspaceStatus(step), reason_code="test")
        await session.commit()
        return ws.id


async def _rows(factory, workspace_id: str):
    async with factory() as session:
        return await ProviderManagedKeyRepository(session).for_workspace(workspace_id)


async def test_orphans_are_usage_recorded_then_deleted_while_live_keys_stay():
    fake = FakeOpenRouterManagement()
    now = datetime.now(UTC)
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        live_ws = await _workspace(factory)
        dead_ws = await _workspace(factory, status=WorkspaceStatus.cancelled)
        broker = ManagedKeyBroker(session_factory=factory, client=fake.client())
        live = await broker.acquire(workspace_id=live_ws, selection=_SELECTION, purpose="agent")
        await broker.acquire(workspace_id=dead_ws, selection=_SELECTION, purpose="agent")
        fake.set_usage("hash2", 0.4)
        # A crash between mint and hash persist leaves an unbound named key.
        unbound = fake.add_key(f"awf-{live_ws}-7", usage=0.1)
        # Another control plane's key and an unrelated key are never touched.
        foreign = fake.add_key("awf-ws_0123456789abcdef01234567-1")
        unrelated = fake.add_key("team-production")

        report = await reconcile_openrouter_keys(
            session_factory=factory, client=fake.client(), now=lambda: now
        )

        assert report == {
            "listed": 5,
            "live": 1,
            "deleted": 2,
            "foreign_skipped": 1,
            "failed": 0,
            "truncated": False,
        }
        assert set(fake.keys) == {live.key_hash, foreign, unrelated}
        (dead_row,) = await _rows(factory, dead_ws)
        assert dead_row.status == "orphan_deleted"
        assert dead_row.usage_usd == Decimal("0.4") and dead_row.final_usage_read
        orphan = next(r for r in await _rows(factory, live_ws) if r.seq == 7)
        assert (orphan.status, orphan.key_hash, orphan.usage_usd) == (
            "orphan_deleted",
            unbound,
            Decimal("0.1"),
        )
        async with factory() as session:
            events = (
                (
                    await session.execute(
                        select(WorkspaceEvent).where(
                            WorkspaceEvent.event_type == "provider.managed_key.orphan_deleted"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert {e.workspace_id for e in events} == {live_ws, dead_ws}
        assert {e.reason_code for e in events} == {"PROVIDER_MANAGED_KEY_ORPHAN_DELETED"}


@pytest.mark.parametrize(
    ("release_delete_fails", "status"), [(False, "deleted"), (True, "orphan_deleted")]
)
async def test_a_final_read_newer_than_the_listing_is_never_lowered(release_delete_fails, status):
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = ManagedKeyBroker(session_factory=factory, client=fake.client())
        lease = await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        fake.set_usage(lease.key_hash, 0.2)
        client = fake.client()
        list_keys = client.list_keys

        async def list_then_release(**kwargs):
            listing = await list_keys(**kwargs)
            # The invocation finishes after the listing: its final read is newer.
            fake.set_usage(lease.key_hash, 0.7)
            if release_delete_fails:
                fake.fail[("DELETE", "key")] = 503
            await lease.release(failed=False)
            fake.fail.clear()
            return listing

        client.list_keys = list_then_release  # type: ignore[method-assign]
        await reconcile_openrouter_keys(session_factory=factory, client=client)

        (row,) = await _rows(factory, ws)
        assert (row.status, row.usage_usd, row.final_usage_read) == (
            status,
            Decimal("0.7"),
            True,
        )
        assert fake.keys == {}


async def test_a_heartbeat_read_older_than_the_listing_never_lowers_recorded_usage():
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker_client = fake.client()
        broker = ManagedKeyBroker(session_factory=factory, client=broker_client)
        lease = await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        fake.set_usage(lease.key_hash, 0.2)
        stale = await broker_client.get_key(lease.key_hash)
        fake.set_usage(lease.key_hash, 0.7)

        async def stale_read(key_hash):
            return stale

        broker_client.get_key = stale_read  # type: ignore[method-assign]
        client = fake.client()
        delete_key = client.delete_key

        async def heartbeat_then_delete(key_hash):
            # A heartbeat read before the listing lands after its usage is recorded.
            await lease.heartbeat()
            await delete_key(key_hash)

        client.delete_key = heartbeat_then_delete  # type: ignore[method-assign]
        await reconcile_openrouter_keys(
            session_factory=factory,
            client=client,
            now=lambda: datetime.now(UTC) + timedelta(hours=1),
        )

        (row,) = await _rows(factory, ws)
        assert (row.status, row.usage_usd) == ("orphan_deleted", Decimal("0.7"))
        assert fake.keys == {}


async def test_spend_after_the_listing_is_read_before_the_orphan_is_deleted():
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = ManagedKeyBroker(session_factory=factory, client=fake.client())
        lease = await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        fake.set_usage(lease.key_hash, 0.2)
        client = fake.client()
        list_keys = client.list_keys

        async def list_then_spend(**kwargs):
            listing = await list_keys(**kwargs)
            # The lease-expired invocation keeps spending without a heartbeat.
            fake.set_usage(lease.key_hash, 0.5)
            return listing

        client.list_keys = list_then_spend  # type: ignore[method-assign]
        report = await reconcile_openrouter_keys(
            session_factory=factory,
            client=client,
            now=lambda: datetime.now(UTC) + timedelta(hours=1),
        )

        assert report["deleted"] == 1
        (row,) = await _rows(factory, ws)
        assert (row.status, row.usage_usd, row.final_usage_read) == (
            "orphan_deleted",
            Decimal("0.5"),
            True,
        )
        assert fake.keys == {}


async def test_an_orphan_whose_usage_cannot_be_read_is_kept_for_the_next_sweep():
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory, status=WorkspaceStatus.cancelled)
        key_hash = fake.add_key(f"awf-{ws}-1", usage=0.3)
        fake.fail[("GET", "key")] = 503

        unread = await reconcile_openrouter_keys(session_factory=factory, client=fake.client())

        assert (unread["failed"], unread["deleted"]) == (1, 0)
        assert key_hash in fake.keys
        assert await _rows(factory, ws) == []
        fake.fail.clear()
        read = await reconcile_openrouter_keys(session_factory=factory, client=fake.client())
        assert (read["failed"], read["deleted"]) == (0, 1)
        (row,) = await _rows(factory, ws)
        assert (row.status, row.usage_usd) == ("orphan_deleted", Decimal("0.3"))


async def test_a_lease_renewed_after_the_bindings_load_keeps_its_key(monkeypatch):
    from awf.service import provider_managed_key_reconciler as reconciler

    fake = FakeOpenRouterManagement()
    clock = [datetime.now(UTC)]
    sweep_at = clock[0] + timedelta(hours=1)
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = ManagedKeyBroker(
            session_factory=factory, client=fake.client(), now=lambda: clock[0]
        )
        lease = await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        load_bindings = reconciler._load_bindings

        async def load_then_heartbeat(*args):
            loaded = await load_bindings(*args)
            # A delayed heartbeat renews the expired lease after the snapshot.
            clock[0] = sweep_at
            await lease.heartbeat()
            return loaded

        monkeypatch.setattr(reconciler, "_load_bindings", load_then_heartbeat)
        report = await reconcile_openrouter_keys(
            session_factory=factory, client=fake.client(), now=lambda: sweep_at
        )

        assert (report["deleted"], report["failed"]) == (0, 0)
        assert lease.key_hash in fake.keys
        (row,) = await _rows(factory, ws)
        assert (row.status, row.final_usage_read) == ("active", False)
        assert row.lease_expires_at is not None and row.lease_expires_at > sweep_at


async def test_a_gone_key_is_finalized_even_if_a_heartbeat_renewed_its_lease(monkeypatch):
    from awf.service import provider_managed_key_reconciler as reconciler

    fake = FakeOpenRouterManagement()
    clock = [datetime.now(UTC)]
    sweep_at = clock[0] + timedelta(hours=1)
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = ManagedKeyBroker(
            session_factory=factory, client=fake.client(), now=lambda: clock[0]
        )
        lease = await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        fake.set_usage(lease.key_hash, 0.4)
        load_bindings = reconciler._load_bindings

        async def load_then_vanish_then_heartbeat(*args):
            loaded = await load_bindings(*args)
            # The key vanishes after the listing; a delayed heartbeat (whose read
            # 404s too) still renews the expired lease after the snapshot.
            del fake.keys[lease.key_hash]
            clock[0] = sweep_at
            await lease.heartbeat()
            return loaded

        monkeypatch.setattr(reconciler, "_load_bindings", load_then_vanish_then_heartbeat)
        report = await reconcile_openrouter_keys(
            session_factory=factory, client=fake.client(), now=lambda: sweep_at
        )

        assert (report["deleted"], report["failed"]) == (1, 0)
        (row,) = await _rows(factory, ws)
        # Later listings omit the gone key: this sweep is its only finalize.
        assert (row.status, row.usage_usd, row.final_usage_read, row.lease_expires_at) == (
            "orphan_deleted",
            Decimal("0.4"),
            True,
            None,
        )


async def test_a_heartbeat_after_the_usage_is_recorded_cannot_renew_a_deleting_key():
    fake = FakeOpenRouterManagement()
    clock = [datetime.now(UTC)]
    sweep_at = clock[0] + timedelta(hours=1)
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = ManagedKeyBroker(
            session_factory=factory, client=fake.client(), now=lambda: clock[0]
        )
        lease = await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        client = fake.client()
        delete_key = client.delete_key

        async def heartbeat_then_delete(key_hash):
            # The usage row is committed; the delete is still in flight.
            clock[0] = sweep_at
            await lease.heartbeat()
            return await delete_key(key_hash)

        client.delete_key = heartbeat_then_delete  # type: ignore[method-assign]
        report = await reconcile_openrouter_keys(
            session_factory=factory, client=client, now=lambda: sweep_at
        )

        assert report["deleted"] == 1
        (row,) = await _rows(factory, ws)
        assert (row.status, row.lease_expires_at) == ("orphan_deleted", None)


@pytest.mark.parametrize("reconciler_delete_status", [503, None])
async def test_a_broker_finalize_during_the_orphan_delete_is_never_clobbered(
    reconciler_delete_status,
):
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = ManagedKeyBroker(session_factory=factory, client=fake.client())
        await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        client = fake.client()
        delete_key = client.delete_key

        async def finalize_then_delete(key_hash):
            # The usage row is committed; the broker finalizes the key first.
            await broker.finalize_workspace_keys(ws)
            if reconciler_delete_status is not None:
                fake.fail[("DELETE", "key")] = reconciler_delete_status
            return await delete_key(key_hash)  # 404 when nothing is injected

        client.delete_key = finalize_then_delete  # type: ignore[method-assign]
        await reconcile_openrouter_keys(
            session_factory=factory,
            client=client,
            now=lambda: datetime.now(UTC) + timedelta(hours=1),
        )

        (row,) = await _rows(factory, ws)
        assert (row.status, row.last_reason_code) == ("deleted", None)
        async with factory() as session:
            orphan_events = (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.event_type == "provider.managed_key.orphan_deleted"
                    )
                )
            ).all()
        assert orphan_events == []


async def test_expired_leases_and_old_intents_are_orphans_but_young_mints_are_not():
    fake = FakeOpenRouterManagement()
    now = datetime.now(UTC)
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = ManagedKeyBroker(session_factory=factory, client=fake.client())
        await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        # A minting intent committed moments ago: its POST may still be in flight.
        async with factory() as session:
            row = (await ProviderManagedKeyRepository(session).for_workspace(ws))[0]
            row.status = "minting"
            row.key_hash = None
            await session.commit()

        young = await reconcile_openrouter_keys(
            session_factory=factory, client=fake.client(), now=lambda: now
        )
        old = await reconcile_openrouter_keys(
            session_factory=factory,
            client=fake.client(),
            now=lambda: now + timedelta(minutes=30),
        )

        assert (young["live"], young["deleted"]) == (1, 0)
        assert (old["live"], old["deleted"]) == (0, 1)
        assert fake.keys == {}


async def test_old_hashless_intents_without_a_remote_key_release_their_reservation():
    fake = FakeOpenRouterManagement()
    now = datetime.now(UTC)
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = ManagedKeyBroker(session_factory=factory, client=fake.client())
        # The intent's key was deleted unused (or never minted) while the database
        # was down: no hash is recorded, yet it reserves the whole budget.
        await broker._reserve(ws, provider="openrouter", purpose="agent")
        with pytest.raises(ProviderCredentialError) as blocked:
            await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        # Nothing was spent: the reservation, not the budget, blocks the mint.
        assert blocked.value.reason_code == "PROVIDER_MANAGED_KEY_UNAVAILABLE"

        # An incomplete listing cannot prove the key absent; a young intent may
        # still have its POST in flight.
        await reconcile_openrouter_keys(
            session_factory=factory,
            client=fake.client(),
            now=lambda: now + timedelta(minutes=30),
            max_pages=0,
        )
        await reconcile_openrouter_keys(
            session_factory=factory, client=fake.client(), now=lambda: now
        )
        assert [r.status for r in await _rows(factory, ws)] == ["minting"]

        await reconcile_openrouter_keys(
            session_factory=factory,
            client=fake.client(),
            now=lambda: now + timedelta(minutes=30),
        )

        (intent,) = await _rows(factory, ws)
        assert (intent.status, intent.last_reason_code, intent.lease_expires_at) == (
            "mint_failed",
            "PROVIDER_MANAGED_KEY_NOT_FOUND",
            None,
        )
        await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        assert fake.calls[-1][2]["name"] == f"awf-{ws}-2"
        assert fake.calls[-1][2]["limit"] == 10.0


async def test_a_listed_intent_left_by_the_deletion_cap_keeps_its_reservation():
    fake = FakeOpenRouterManagement()
    now = datetime.now(UTC)
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = ManagedKeyBroker(session_factory=factory, client=fake.client())
        # An unbound orphan listed first uses up the sweep's only deletion; the
        # intent's key was minted but its hash was never recorded.
        fake.add_key(f"awf-{ws}-9")
        _row_id, key_name, _limit, _budget = await broker._reserve(
            ws, provider="openrouter", purpose="agent"
        )
        fake.add_key(key_name)

        report = await reconcile_openrouter_keys(
            session_factory=factory,
            client=fake.client(),
            now=lambda: now + timedelta(minutes=30),
            max_deletions=1,
        )

        assert (report["deleted"], report["truncated"]) == (1, True)
        # The intent's key still exists, so it must keep reserving its headroom.
        assert [(r.seq, r.status) for r in await _rows(factory, ws)] == [
            (1, "minting"),
            (9, "orphan_deleted"),
        ]
        assert [key["name"] for key in fake.keys.values()] == [key_name]


async def test_pagination_bounds_and_per_key_failure_isolation():
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory, status=WorkspaceStatus.cancelled)
        for seq in range(1, 131):
            fake.add_key(f"awf-{ws}-{seq}")
        original = fake.handler

        def flaky(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE" and request.url.path.endswith("/hash3"):
                return httpx.Response(503)
            return original(request)

        fake.handler = flaky  # type: ignore[method-assign]
        report = await reconcile_openrouter_keys(
            session_factory=factory, client=fake.client(), max_deletions=5
        )

        assert report["listed"] == 130
        assert report["failed"] == 1
        assert report["deleted"] == 5
        assert report["truncated"] is True
        assert len(fake.keys) == 125
        rows = await _rows(factory, ws)
        failed = next(r for r in rows if r.key_hash == "hash3")
        assert failed.status == "delete_failed"
        assert failed.last_reason_code == "PROVIDER_MANAGEMENT_API_UNAVAILABLE"


async def test_listing_failures_and_page_truncation_are_reported():
    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory, status=WorkspaceStatus.cancelled)
        for seq in range(1, 101):
            fake.add_key(f"awf-{ws}-{seq}")

        truncated = await reconcile_openrouter_keys(
            session_factory=factory, client=fake.client(), max_pages=1, max_deletions=0
        )
        assert truncated["truncated"] is True and truncated["deleted"] == 0

        fake.fail[("GET", "collection")] = 401
        with pytest.raises(OpenRouterManagementError) as raised:
            await reconcile_openrouter_keys(session_factory=factory, client=fake.client())
        assert raised.value.reason_code == "PROVIDER_MANAGEMENT_API_UNAUTHORIZED"


async def test_worker_maintenance_is_wired_only_with_a_management_key(monkeypatch):
    from awf.service import provider_managed_keys
    from awf.service.provider_managed_keys import provider_key_maintenance

    fake = FakeOpenRouterManagement()
    monkeypatch.setattr(
        provider_managed_keys, "OpenRouterManagementClient", lambda _key: fake.client()
    )
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        broker = ManagedKeyBroker(session_factory=factory, client=fake.client())
        assert (
            provider_key_maintenance(session_factory=factory, broker=broker, management_key=None)
            == {}
        )
        ws = await _workspace(factory, status=WorkspaceStatus.cancelled)
        fake.add_key(f"awf-{ws}-1")

        hooks = provider_key_maintenance(
            session_factory=factory, broker=broker, management_key="sk-or-mgmt-x"
        )
        report = await hooks["provider_key_reconciler"]()

        assert report["deleted"] == 1
        assert report["terminal_workspaces_finalized"] == 0
        assert hooks["provider_key_finalizer"] == broker.finalize_workspace_keys


async def test_an_account_without_awf_keys_is_a_quiet_no_op():
    fake = FakeOpenRouterManagement()
    fake.add_key("team-production")
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)

        report = await reconcile_openrouter_keys(session_factory=factory, client=fake.client())

    assert report == {
        "listed": 1,
        "live": 0,
        "deleted": 0,
        "foreign_skipped": 0,
        "failed": 0,
        "truncated": False,
    }
    assert set(fake.keys) == {"hash1"}


@pytest.mark.parametrize("failing", ["_record_usage", "_mark"])
async def test_a_database_error_on_one_key_never_aborts_the_sweep(monkeypatch, failing):
    from sqlalchemy.exc import IntegrityError

    from awf.service import provider_managed_key_reconciler as reconciler

    fake = FakeOpenRouterManagement()
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory, status=WorkspaceStatus.cancelled)
        first = fake.add_key(f"awf-{ws}-1")
        second = fake.add_key(f"awf-{ws}-2")
        original = getattr(reconciler, failing)
        calls: list[object] = []

        async def _fails_once(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise IntegrityError("INSERT", None, Exception("duplicate key"))
            return await original(*args, **kwargs)

        monkeypatch.setattr(reconciler, failing, _fails_once)
        report = await reconcile_openrouter_keys(session_factory=factory, client=fake.client())

        assert report["failed"] == 1
        assert second not in fake.keys
        # Usage is recorded before a delete: an unrecorded key is never deleted.
        assert (first in fake.keys) is (failing == "_record_usage")
        assert any(
            r.key_hash == second and r.status == "orphan_deleted" for r in await _rows(factory, ws)
        )


@pytest.mark.parametrize("claim", ["execution", "monitor"])
async def test_a_key_is_kept_while_its_workspace_claim_is_live_even_past_its_lease(claim):
    fake = FakeOpenRouterManagement()
    now = datetime.now(UTC)
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        ws = await _workspace(factory)
        broker = ManagedKeyBroker(session_factory=factory, client=fake.client())
        await broker.acquire(workspace_id=ws, selection=_SELECTION, purpose="agent")
        async with factory() as session:
            row = await WorkspaceRepository(session).get(ws)
            setattr(row, f"{claim}_claimed_by", "worker-a")
            setattr(row, f"{claim}_claim_expires_at", now + timedelta(hours=2))
            await session.commit()

        # The key's heartbeat died silently, but the worker still holds the claim.
        claimed = await reconcile_openrouter_keys(
            session_factory=factory, client=fake.client(), now=lambda: now + timedelta(hours=1)
        )
        lapsed = await reconcile_openrouter_keys(
            session_factory=factory, client=fake.client(), now=lambda: now + timedelta(hours=3)
        )

        assert (claimed["live"], claimed["deleted"]) == (1, 0)
        assert (lapsed["live"], lapsed["deleted"]) == (0, 1)
        assert fake.keys == {}
