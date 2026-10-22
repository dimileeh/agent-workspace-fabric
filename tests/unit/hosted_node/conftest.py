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
