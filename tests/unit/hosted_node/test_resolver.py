import asyncio
import base64
import secrets
from pathlib import Path

import pytest
from sqlalchemy import text

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.resolver import ProtectedResolver
from awf.hosted_node.schemas import Binding
from awf.profiles.models import WorkspaceProfile

from .test_receiver_api import prepare


async def test_actual_resolver_and_revocation(hosted, engine, monkeypatch, tmp_path):
    client, receiver, _ = hosted
    binding = await prepare(hosted)
    canary = secrets.token_bytes(32)
    encoded = base64.b64encode(canary).decode()
    assert (
        await client.post(
            "/v1/hosted-node/materials/install", json={**binding, "material": encoded}
        )
    ).status_code == 200
    async with engine.connect() as connection:
        schema = await connection.scalar(text("select current_schema()"))
    url = engine.url.update_query_dict({"awf_search_path": schema}).render_as_string(
        hide_password=False
    )
    resolver = ProtectedResolver(
        url, receiver.config, receiver.identity, {"key": Binding.model_validate(binding)}
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "test",
            "secrets": [
                {
                    "name": "key",
                    "target": binding["target"],
                    "provider": "protected",
                    "kind": "mount",
                }
            ],
        }
    )
    monkeypatch.setenv("GH_TOKEN", encoded)
    try:
        result = await asyncio.to_thread(resolver.resolve, profile, workspace_id="ws")
        assert len(result.mounts) == 1
        assert Path(result.mounts[0].source).read_bytes() == canary
        assert result.mounts[0].mode == "ro"
        assert result.environment == ()
        assert encoded not in str(result.metadata)
        from awf.node.compose_manager import ComposeManager, WorkspaceComposeSpec

        manager = ComposeManager(
            work_dir=tmp_path,
            template_path=Path("docker/compose/workspace.base.yml.j2"),
        )
        rendered = manager.render(
            WorkspaceComposeSpec(
                workspace_id="ws",
                worktree_host_path=tmp_path,
                auth_mounts=result.mounts,
                agent_environment=result.environment,
            )
        )
        assert encoded not in rendered.compose_file.read_text()
        assert canary not in rendered.compose_file.read_bytes()
        with pytest.raises(MaterialDeniedError):
            await asyncio.to_thread(resolver.resolve, profile, workspace_id="other")
        revoke = {**binding, "fence": 2, "idempotency_key": "revoke"}
        assert (
            await client.post("/v1/hosted-node/materials/revoke", json=revoke)
        ).status_code == 200
        with pytest.raises(MaterialDeniedError):
            await asyncio.to_thread(resolver.resolve, profile, workspace_id="ws")
    finally:
        await asyncio.to_thread(resolver.close)
    with pytest.raises(MaterialDeniedError):
        resolver.resolve(profile, workspace_id="ws")


async def test_bridge_timeout_and_unavailable_database(hosted, engine, monkeypatch):
    _, receiver, _ = hosted
    async with engine.connect() as connection:
        schema = await connection.scalar(text("select current_schema()"))
    url = engine.url.update_query_dict({"awf_search_path": schema}).render_as_string(
        hide_password=False
    )
    resolver = ProtectedResolver(url, receiver.config, receiver.identity, {}, timeout=0.2)
    profile = WorkspaceProfile.model_validate({"name": "test"})
    original = resolver._resolve
    try:

        async def stalled(*args):
            await asyncio.sleep(2)

        monkeypatch.setattr(resolver, "_resolve", stalled)
        with pytest.raises(MaterialDeniedError, match="RESOLVER_UNAVAILABLE"):
            await asyncio.to_thread(resolver.resolve, profile, workspace_id="ws")
        monkeypatch.setattr(resolver, "_resolve", original)
        from sqlalchemy.exc import SQLAlchemyError

        async def unavailable(*args):
            raise SQLAlchemyError("sensitive-db-detail")

        monkeypatch.setattr(resolver, "_resolve", unavailable)
        with pytest.raises(MaterialDeniedError, match="RESOLVER_UNAVAILABLE"):
            await asyncio.to_thread(resolver.resolve, profile, workspace_id="ws")
    finally:
        await asyncio.to_thread(resolver.close)
        await asyncio.to_thread(resolver.close)


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "env"},
        {"mode": "rw"},
        {"provider": "env"},
        {"ref": "host-home"},
        {"target": "/run/awf/secrets/other"},
        {"name": "unknown"},
    ],
)
async def test_resolver_denies_unsupported_declarations(hosted, engine, change):
    _, receiver, _ = hosted
    binding = await prepare(hosted)
    async with engine.connect() as connection:
        schema = await connection.scalar(text("select current_schema()"))
    url = engine.url.update_query_dict({"awf_search_path": schema}).render_as_string(
        hide_password=False
    )
    resolver = ProtectedResolver(
        url, receiver.config, receiver.identity, {"key": Binding.model_validate(binding)}
    )
    try:
        profile = WorkspaceProfile.model_validate(
            {
                "name": "test",
                "secrets": [
                    {
                        "name": "key",
                        "kind": "mount",
                        "mode": "ro",
                        "provider": "protected",
                        "target": binding["target"],
                        **change,
                    }
                ],
            }
        )
        with pytest.raises(MaterialDeniedError, match="PROTECTED_DECLARATION_MISMATCH"):
            await asyncio.to_thread(resolver.resolve, profile, workspace_id="ws")
    finally:
        await asyncio.to_thread(resolver.close)


@pytest.mark.parametrize("transition", ["missing", "expired", "restart", "refresh", "shutdown"])
async def test_resolver_revalidates_receiver_transitions(hosted, engine, transition):
    from datetime import UTC, datetime, timedelta

    from awf.hosted_node.receiver import Receiver

    client, receiver, _ = hosted
    binding = await prepare(hosted)
    if transition == "expired":
        binding["expires_at"] = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    response = await client.post(
        "/v1/hosted-node/materials/install", json={**binding, "material": "YQ=="}
    )
    assert response.json()["status"] == "current"
    async with engine.connect() as connection:
        schema = await connection.scalar(text("select current_schema()"))
    url = engine.url.update_query_dict({"awf_search_path": schema}).render_as_string(
        hide_password=False
    )
    resolver = ProtectedResolver(
        url, receiver.config, receiver.identity, {"key": Binding.model_validate(binding)}
    )
    profile = WorkspaceProfile.model_validate(
        {
            "name": "test",
            "secrets": [
                {
                    "name": "key",
                    "kind": "mount",
                    "mode": "ro",
                    "provider": "protected",
                    "target": binding["target"],
                }
            ],
        }
    )
    successor = None
    try:
        await asyncio.to_thread(resolver.resolve, profile, workspace_id="ws")
        if transition == "missing":
            for path in receiver.store.root.glob("*/*.material"):
                path.unlink()
        elif transition == "expired":
            await asyncio.sleep(1.05)
        elif transition == "restart":
            successor = await Receiver.start(receiver.config, receiver.factory)
        elif transition == "shutdown":
            await receiver.close()
        else:
            newer = {**binding, "idempotency_key": "refresh", "revision": 2, "fence": 2}
            refreshed = await client.post(
                "/v1/hosted-node/materials/refresh",
                json={
                    **newer,
                    "material": "Yg==",
                    "predecessor": response.json()["receipt_id"],
                },
            )
            assert refreshed.json()["status"] == "current"
            newer_resolver = ProtectedResolver(
                url, receiver.config, receiver.identity, {"key": Binding.model_validate(newer)}
            )
            try:
                mounts = (
                    await asyncio.to_thread(newer_resolver.resolve, profile, workspace_id="ws")
                ).mounts
                assert Path(mounts[0].source).read_bytes() == b"b"
            finally:
                await asyncio.to_thread(newer_resolver.close)
        with pytest.raises(MaterialDeniedError):
            await asyncio.to_thread(resolver.resolve, profile, workspace_id="ws")
    finally:
        await asyncio.to_thread(resolver.close)
        if successor is not None:
            await successor.close()


@pytest.mark.parametrize(
    "failure", ["engine", "factory", "store", "receiver", "timeout", "store_close", "dispose"]
)
def test_failed_initialization_cleans_up_on_bridge_thread(monkeypatch, failure):
    import threading
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from awf.hosted_node import resolver as module

    release = threading.Event()
    entered = threading.Event()
    engine = SimpleNamespace(
        dispose=AsyncMock(side_effect=OSError("dispose failed") if failure == "dispose" else None)
    )
    store = SimpleNamespace(
        close=Mock(side_effect=OSError("close failed") if failure == "store_close" else None)
    )
    bridge = ProtectedResolver.__new__(ProtectedResolver)

    def fail():
        raise OSError("private initialization detail")

    def make_store(root):
        if failure == "store":
            fail()
        if failure == "timeout":
            entered.set()
            assert release.wait(5)
        return store

    monkeypatch.setattr(
        module, "make_engine", lambda _url: fail() if failure == "engine" else engine
    )
    monkeypatch.setattr(
        module, "make_session_factory", lambda _value: fail() if failure == "factory" else object()
    )
    monkeypatch.setattr(module, "MaterialStore", make_store)
    monkeypatch.setattr(
        module,
        "Receiver",
        lambda *_args: fail() if failure in {"receiver", "store_close", "dispose"} else object(),
    )
    try:
        with pytest.raises(MaterialDeniedError, match="^RESOLVER_UNAVAILABLE$"):
            bridge.__init__(
                "unused", SimpleNamespace(material_root=None), object(), {}, timeout=0.1
            )
        if failure == "timeout":
            assert entered.is_set()
            assert bridge._thread.is_alive()
            assert not bridge._loop.is_closed()
    finally:
        release.set()
        bridge._thread.join(5)
    assert not bridge._thread.is_alive()
    assert bridge._loop.is_closed()
    if failure == "engine":
        engine.dispose.assert_not_awaited()
    else:
        engine.dispose.assert_awaited_once()
    if failure in {"receiver", "timeout", "store_close", "dispose"}:
        store.close.assert_called_once()
    else:
        store.close.assert_not_called()


def test_timeout_keeps_resolutions_serial_until_cancellation_finishes(monkeypatch):
    import threading
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from awf.hosted_node import resolver as module
    from awf.node.secret_mounts import LocalSecretLeaseResolution

    engine = SimpleNamespace(dispose=AsyncMock())
    receiver = SimpleNamespace(store=SimpleNamespace(close=Mock()), closed=False)
    monkeypatch.setattr(module, "make_engine", lambda _url: engine)
    monkeypatch.setattr(module, "make_session_factory", lambda _engine: object())
    monkeypatch.setattr(module, "MaterialStore", lambda _root: receiver.store)
    monkeypatch.setattr(module, "Receiver", lambda *_args: receiver)
    resolver = ProtectedResolver(
        "unused", SimpleNamespace(material_root=None), object(), {}, timeout=0.1
    )
    profile = WorkspaceProfile.model_validate({"name": "test"})
    cancelling = threading.Event()
    finished = threading.Event()
    release = asyncio.Event()
    calls = []
    expected = LocalSecretLeaseResolution(mounts=(), metadata={})

    async def resolve_with_cleanup(*args):
        calls.append(args)
        if len(calls) == 1:
            try:
                await asyncio.Future()
            finally:
                cancelling.set()
                await release.wait()
                finished.set()
        return expected

    monkeypatch.setattr(resolver, "_resolve", resolve_with_cleanup)
    try:
        with pytest.raises(MaterialDeniedError, match="^RESOLVER_UNAVAILABLE$"):
            resolver.resolve(profile, workspace_id="ws")
        assert cancelling.wait(5)
        with pytest.raises(MaterialDeniedError, match="^RESOLVER_UNAVAILABLE$"):
            resolver.resolve(profile, workspace_id="ws")
        assert len(calls) == 1
        assert not finished.is_set()
        resolver._loop.call_soon_threadsafe(release.set)
        assert finished.wait(5)
        assert resolver.resolve(profile, workspace_id="ws") == expected
        assert len(calls) == 2
    finally:
        resolver._loop.call_soon_threadsafe(release.set)
        assert finished.wait(5)
        resolver.close()


@pytest.mark.parametrize("transition", ["revoke", "refresh"])
async def test_resolver_rechecks_grant_after_selecting_mount(hosted, monkeypatch, transition):
    client, receiver, _ = hosted
    binding = await prepare(hosted)
    installed = await client.post(
        "/v1/hosted-node/materials/install", json={**binding, "material": "YQ=="}
    )
    assert installed.json()["status"] == "current"
    old_file = next(receiver.store.root.glob("*/*.material"))
    resolver = ProtectedResolver.__new__(ProtectedResolver)
    resolver._receiver = receiver
    resolver.bindings = {"key": Binding.model_validate(binding)}
    profile = WorkspaceProfile.model_validate(
        {
            "name": "test",
            "secrets": [
                {
                    "name": "key",
                    "target": binding["target"],
                    "provider": "protected",
                    "kind": "mount",
                }
            ],
        }
    )
    original_status = receiver.status
    changed = False

    async def status_then_transition(value):
        nonlocal changed
        status = await original_status(value)
        if not changed:
            changed = True
            assert status.status == "current"

            def cleanup_failed(*args):
                raise OSError("cleanup unavailable")

            # The durable grant change must dominate even when cleanup fails.
            with monkeypatch.context() as patch:
                patch.setattr(receiver.store, "remove", cleanup_failed)
                newer = {**binding, "idempotency_key": transition, "fence": 2}
                if transition == "refresh":
                    newer.update(
                        revision=2,
                        material="Yg==",
                        predecessor=installed.json()["receipt_id"],
                    )
                await client.post(f"/v1/hosted-node/materials/{transition}", json=newer)
            assert old_file.read_bytes() == b"a"
            assert (await original_status(value)).status != "current"
        return status

    monkeypatch.setattr(receiver, "status", status_then_transition)
    with pytest.raises(MaterialDeniedError, match="PROTECTED_MATERIAL_UNAVAILABLE"):
        await resolver._resolve(profile, "ws")
    assert changed


@pytest.mark.parametrize("dispose_fails", [False, True])
def test_close_waits_for_cancelled_resolution_cleanup(monkeypatch, dispose_fails):
    import threading
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from awf.hosted_node import resolver as module

    finished = threading.Event()

    async def dispose():
        assert finished.is_set()
        if dispose_fails:
            raise OSError("disposal failed")

    engine = SimpleNamespace(dispose=AsyncMock(side_effect=dispose))
    receiver = SimpleNamespace(store=SimpleNamespace(close=Mock()), closed=False)
    monkeypatch.setattr(module, "make_engine", lambda _url: engine)
    monkeypatch.setattr(module, "make_session_factory", lambda _engine: object())
    monkeypatch.setattr(module, "MaterialStore", lambda _root: receiver.store)
    monkeypatch.setattr(module, "Receiver", lambda *_args: receiver)
    resolver = ProtectedResolver(
        "unused", SimpleNamespace(material_root=None), object(), {}, timeout=0.1
    )
    cancelling = threading.Event()
    release = asyncio.Event()

    async def resolve_with_cleanup(*args):
        try:
            await asyncio.Future()
        finally:
            cancelling.set()
            await release.wait()
            finished.set()

    monkeypatch.setattr(resolver, "_resolve", resolve_with_cleanup)
    try:
        with pytest.raises(MaterialDeniedError, match="^RESOLVER_UNAVAILABLE$"):
            resolver.resolve(WorkspaceProfile(name="test"), workspace_id="ws")
        assert cancelling.wait(5)
        with pytest.raises(TimeoutError):
            resolver.close()
        assert resolver._thread.is_alive()
        assert not resolver._loop.is_closed()
        assert not receiver.closed
        receiver.store.close.assert_not_called()
        engine.dispose.assert_not_awaited()
        with pytest.raises(MaterialDeniedError, match="^RESOLVER_CLOSED$"):
            resolver.resolve(WorkspaceProfile(name="test"), workspace_id="ws")
    finally:
        if not resolver._loop.is_closed():
            resolver._loop.call_soon_threadsafe(release.set)
        resolver._thread.join(5)
    assert finished.is_set()
    assert receiver.closed
    receiver.store.close.assert_called_once()
    engine.dispose.assert_awaited_once()
    assert not resolver._thread.is_alive()
    assert resolver._loop.is_closed()
    resolver.close()
