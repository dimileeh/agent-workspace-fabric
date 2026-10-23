"""Real application and PostgreSQL fixtures for the closed hosted foundation."""

import tempfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from awf.api.app import configure_database, create_app
from awf.common.config import Settings, get_settings
from awf.db.session import make_session_factory
from awf.hosted_node.config import HostedSettings
from awf.hosted_node.receiver import Receiver


@pytest.fixture
async def hosted(engine):
    with tempfile.TemporaryDirectory(dir="/dev/shm") as root:
        config = HostedSettings(
            owner="owner",
            incarnation="vm",
            recovery_generation=1,
            controller_token="controller-test-only",
            material_root=Path(root),
        )
        factory = make_session_factory(engine)
        receiver = await Receiver.start(config, factory)
        app = create_app(use_lifespan=False)
        configure_database(app, factory)
        app.state.hosted_receiver = receiver
        app.dependency_overrides[get_settings] = lambda: Settings(
            _env_file=None,
            api_token="operator-test-only",
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            client.headers.update(
                {
                    "Authorization": "Bearer controller-test-only",
                    **receiver.identity.headers(),
                }
            )
            yield client, receiver, app
        await receiver.close()


@pytest.fixture
async def peer_engine(engine):
    from sqlalchemy import text

    from awf.db.session import make_engine

    async with engine.connect() as connection:
        schema = await connection.scalar(text("select current_schema()"))
    peer = make_engine(
        engine.url.update_query_dict({"awf_search_path": schema}).render_as_string(
            hide_password=False
        )
    )
    try:
        yield peer
    finally:
        await peer.dispose()


@pytest.fixture
async def peer(hosted, peer_engine):
    """Independent request handler for the same boot; startup races allocate new boots."""
    from awf.hosted_node.material_store import MaterialStore

    _, original, _ = hosted
    factory = make_session_factory(peer_engine)
    receiver = Receiver(
        original.config, factory, original.identity, MaterialStore(original.config.material_root)
    )
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    app.state.hosted_receiver = receiver
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, api_token="operator-test-only"
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://peer") as client:
            client.headers.update(
                {"Authorization": "Bearer controller-test-only", **receiver.identity.headers()}
            )
            yield client, receiver, app
    finally:
        # A request handler does not own boot shutdown; hosted owns the shared boot.
        receiver.store.close()


@pytest.fixture
async def protected_resolver(hosted, peer_engine):
    import asyncio

    from sqlalchemy import text

    from awf.hosted_node.resolver import ProtectedResolver
    from awf.hosted_node.schemas import Binding
    from awf.profiles.models import WorkspaceProfile

    _, receiver, _ = hosted
    async with peer_engine.connect() as connection:
        schema = await connection.scalar(text("select current_schema()"))
    url = peer_engine.url.update_query_dict({"awf_search_path": schema}).render_as_string(
        hide_password=False
    )
    resolvers = []

    async def resolve(binding):
        resolver = ProtectedResolver(
            url,
            receiver.config,
            receiver.identity,
            {"key": Binding.model_validate(binding)},
        )
        resolvers.append(resolver)
        profile = WorkspaceProfile.model_validate(
            {
                "name": "test",
                "secrets": [
                    {
                        "name": "key",
                        "kind": "mount",
                        "provider": "protected",
                        "target": binding["target"],
                    }
                ],
            }
        )
        return await asyncio.to_thread(resolver.resolve, profile, workspace_id="ws")

    try:
        yield resolve
    finally:
        for resolver in resolvers:
            await asyncio.to_thread(resolver.close)


@pytest.fixture
def interrupt_publication(monkeypatch):
    """Emulate crash leftovers at real syscalls, including skipped finally cleanup."""
    import os
    from contextlib import contextmanager

    @contextmanager
    def interrupt(boundary):
        link, unlink = os.link, os.unlink

        def publish(source, destination, **kwargs):
            assert os.stat(source, dir_fd=kwargs["src_dir_fd"]).st_size > 0
            if boundary == "staged":
                raise OSError("interrupted before publication")
            return link(source, destination, **kwargs)

        def cleanup(path, **kwargs):
            if str(path).endswith(".pending"):
                raise OSError("interrupted before staging unlink")
            return unlink(path, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(os, "link", publish)
            patch.setattr(os, "unlink", cleanup)
            yield

    return interrupt
