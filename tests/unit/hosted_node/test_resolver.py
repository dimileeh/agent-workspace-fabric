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
