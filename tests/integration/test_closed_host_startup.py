from contextlib import contextmanager
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import text

from awf.api import app as application
from awf.common.config import Settings
from awf.hosted_node import host_startup
from awf.hosted_node.config import HostedSettings
from awf.hosted_node.host_linux import LinuxObservation, LinuxSnapshot
from tests.integration.test_closed_host_database import runtime_database as runtime_database
from tests.integration.test_node_idle_gate import PREFIX
from tests.unit.hosted_node.test_host_linux import BOOT


@pytest.fixture
def startup_config(runtime_database, monkeypatch):
    _, host, _, engine = runtime_database
    config = HostedSettings(
        owner="org/node",
        incarnation="8063738394745155541",
        recovery_generation=1,
        controller_token="synthetic-controller",
        material_root="/unused-materials",
        lifecycle={"disk_ref": "disk", "database_ref": "db"},
        host=host,
    )
    events = []

    @contextmanager
    def lease(*args):
        events.append("lease")
        try:
            yield
        finally:
            events.append("release")

    monkeypatch.setattr(host_startup, "host_lease", lease)
    monkeypatch.setattr(host_startup, "validate_socket", lambda _: None)
    monkeypatch.setattr("awf.hosted_node.receiver.MaterialStore", lambda _: Mock())
    monkeypatch.setattr(LinuxObservation, "sample", lambda _: LinuxSnapshot(BOOT, 1, 2, 3, True))
    monkeypatch.setattr(
        LinuxObservation, "bootstrap_approved", lambda _, receipt: receipt == "first-node"
    )

    def docker(request):
        if request.url.path.endswith("/info"):
            return httpx.Response(
                200,
                json={
                    "DockerRootDir": str(host.docker_root),
                    "Swarm": {"LocalNodeState": "inactive"},
                },
            )
        return httpx.Response(200, json=[])

    monkeypatch.setattr(
        host_startup,
        "docker_client",
        lambda _: httpx.AsyncClient(
            transport=httpx.MockTransport(docker), base_url="http://docker"
        ),
    )
    monkeypatch.setattr(application, "make_engine", lambda _: engine)
    monkeypatch.setattr(
        application,
        "get_settings",
        lambda: Settings(_env_file=None, hosted_node=config, api_token="synthetic-operator"),
    )
    return config, events


async def test_actual_lifespan_wires_closed_host_and_shuts_down(runtime_database, startup_config):
    config, events = startup_config
    app = application.create_app()
    async with app.router.lifespan_context(app):
        receiver = app.state.hosted_receiver
        assert events == ["lease"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://core.test",
            headers={"Authorization": "Bearer synthetic-controller", **receiver.identity.headers()},
        ) as client:
            state = (await client.get(PREFIX + "/readiness")).json()
            assert state["state"] == "recovering" and state["admission_closed"]
            result = await client.post(
                PREFIX + "/recovery/acknowledge",
                json={"idempotency_key": "ack", "receipt_ref": "first-node"},
            )
            assert result.status_code == 200, result.text
            assert result.json()["node_ready"] and not result.json()["runtime_ready"]
            assert (await client.post(PREFIX + "/drain", json={"generation": 1})).json()[
                "state"
            ] == "drained"
    assert receiver.closed and events == ["lease", "release"]
    _, _, migration, _ = runtime_database
    async with migration.connect() as connection:
        assert (
            await connection.scalar(text("SELECT phase FROM hosted_node_lifecycle")) == "recovering"
        )


async def test_startup_rejects_migration_privilege_before_owner_boot(
    runtime_database, startup_config
):
    _, host, migration, _ = runtime_database
    async with migration.begin() as connection:
        await connection.execute(text(f'GRANT CREATE ON SCHEMA public TO "{host.database_role}"'))
    app = application.create_app()
    with pytest.raises(ValueError, match="HOST_DATABASE_UNSAFE"):
        async with app.router.lifespan_context(app):
            pytest.fail("must not serve with DDL rights")
    async with migration.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM hosted_owners")) == 0


async def test_startup_identity_failure_never_boots_or_serves(
    runtime_database, startup_config, monkeypatch
):
    def bad_host(_):
        raise ValueError("HOST_BOOT_IDENTITY_MISMATCH")

    monkeypatch.setattr(LinuxObservation, "sample", bad_host)
    app = application.create_app()
    with pytest.raises(ValueError):
        async with app.router.lifespan_context(app):
            pytest.fail("must not serve stale host identity")
