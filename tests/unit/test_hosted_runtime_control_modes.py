"""Runtime-disabled hosted observation preserves existing safe controls."""

import os
import sys
import tempfile
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from awf.api.app import configure_database, create_app
from awf.common.config import Settings, get_settings
from awf.db.models import Operation, Workspace, WorkspaceEvent
from awf.db.session import make_session_factory
from awf.hosted_node.config import HostedSettings
from awf.hosted_node.lifecycle_barrier import install_barrier
from awf.hosted_node.material_store import MaterialStore
from awf.hosted_node.receiver import Receiver
from awf.runtime.inspection import inspection_environment
from tests.integration.test_node_idle_gate import PREFIX, evidence
from tests.unit.hosted_node.test_host_config import host_config
from tests.unit.test_hosted_runtime_control_endpoint import (
    CONTROL,
    EXECUTION,
    calls,
    request_control,
)
from tests.unit.test_hosted_runtime_control_endpoint import (
    docker_control_probe as docker_control_probe,
)


@asynccontextmanager
async def observer_app(engine, tmp_path, monkeypatch, kind, *, runtime_enabled=False):
    def portable_tmpfs(self, path):
        self.root, self.closed = path, False
        fd = self._open_root()
        try:
            info = os.fstat(fd)
            self._device_inode = (info.st_dev, info.st_ino)
        finally:
            os.close(fd)

    if sys.platform != "linux":
        monkeypatch.setattr(MaterialStore, "__init__", portable_tmpfs)
    with tempfile.TemporaryDirectory(
        dir="/dev/shm" if sys.platform == "linux" else tmp_path
    ) as temporary:
        config = HostedSettings(
            owner="synthetic-owner/node",
            incarnation="123",
            recovery_generation=1,
            controller_token="synthetic-controller-only",
            material_root=Path(temporary),
            lifecycle=None
            if kind == "material"
            else {"disk_ref": "disk-123", "database_ref": "node-database"},
            host=host_config(docker_socket=CONTROL.removeprefix("unix://"))
            if kind == "observer"
            else None,
            runtime_enabled=runtime_enabled,
        )
        factory = make_session_factory(engine)
        if config.lifecycle is not None:
            async with engine.begin() as connection:
                await connection.run_sync(install_barrier)
        receiver = await Receiver.start(config, factory)
        settings = Settings(
            _env_file=None,
            api_token="synthetic-operator",
            hosted_node=config,
            docker_host=CONTROL,
            work_dir=str(tmp_path),
        )
        app = create_app(use_lifespan=False)
        configure_database(app, factory)
        app.state.hosted_receiver = receiver
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                if receiver.lifecycle is not None:
                    # Synthetic host verifier; real HTTP recovery and DB barrier.
                    evidence(receiver)
                    acknowledged = await client.post(
                        PREFIX + "/recovery/acknowledge",
                        headers={
                            "Authorization": "Bearer synthetic-controller-only",
                            **receiver.identity.headers(),
                        },
                        json={
                            "idempotency_key": "synthetic-recovery",
                            "receipt_ref": "verified-recovery-receipt",
                        },
                    )
                    assert acknowledged.status_code == 200, acknowledged.text
                async with factory.begin() as session:
                    session.add(
                        Workspace(
                            id="ws_cloud_stable",
                            status="blocked",
                            node_id=config.owner,
                            repo_url="https://example.invalid/repo",
                            branch_base="main",
                            task_title="Synthetic",
                            task_prompt="Never executed",
                            agent="codex",
                        )
                    )
                yield (
                    client,
                    factory,
                    settings,
                    {
                        "Authorization": "Bearer synthetic-operator",
                        "Idempotency-Key": "synthetic-control-key",
                        **receiver.identity.headers(),
                    },
                )
        finally:
            await receiver.close()


async def configure_cleanup(factory, tmp_path, path):
    async with factory.begin() as session:
        row = await session.get(Workspace, "ws_cloud_stable")
        if path == "no-compose":
            row.task_policy = {"pr_adoption": {"execution": {"mode": "hosted"}}}
        else:
            row.compose_project_name = "awf_ws_cloud_stable"
            if path == "down":
                compose = tmp_path / "controlled-compose.yml"
                compose.write_text("services: {}\n")
                row.compose_file_path = str(compose)


@pytest.mark.parametrize("kind", ["material", "lifecycle", "observer"])
@pytest.mark.parametrize("action", ["cancel", "stop", "destroy"])
@pytest.mark.parametrize("path", ["no-compose", "labels", "down"])
@pytest.mark.parametrize("task_bound", [False, True], ids=["ambient", "task-bound"])
async def test_disabled_hosted_modes_keep_controls_without_enabling_execution(
    engine, tmp_path, monkeypatch, docker_control_probe, kind, action, path, task_bound
):
    log, _ = docker_control_probe
    async with observer_app(engine, tmp_path, monkeypatch, kind) as (
        client,
        factory,
        settings,
        headers,
    ):
        assert not settings.hosted_node.runtime_enabled
        await configure_cleanup(factory, tmp_path, path)
        before = dict(os.environ)
        context = (
            inspection_environment({"PATH": os.environ["PATH"], "DOCKER_HOST": EXECUTION})
            if task_bound
            else nullcontext()
        )
        with context:
            denied = await client.post(
                "/v1/workspaces",
                json={},
                headers={**headers, "Authorization": "Bearer synthetic-controller-only"},
            )
            assert denied.status_code == 403
            assert denied.json()["error_code"] == "HOSTED_RUNTIME_INTEGRATION_REQUIRED"
            assert not calls(log)
            response = await request_control(client, action, headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["operation_status"] == "succeeded"
        async with factory() as session:
            row = await session.get(Workspace, "ws_cloud_stable")
            operation = await session.get(Operation, body["operation_id"])
            assert operation.status == "succeeded"
            assert row.status != "destroying"
            assert row.execution_claimed_by is None
            assert row.monitor_claimed_by is None
            assert await session.scalar(select(func.count()).select_from(Workspace)) == 1
        observed = calls(log)
        if path == "no-compose":
            assert not observed
        else:
            assert observed
            assert {call["host"] for call in observed} == {EXECUTION if task_bound else CONTROL}
            assert all(
                call["context"] == (None if task_bound else "synthetic-control-context")
                for call in observed
            )
            assert any(
                call["args"][0] == ("compose" if path == "down" else "ps") for call in observed
            )
        assert dict(os.environ) == before


@pytest.mark.parametrize("action", ["cancel", "stop", "destroy"])
@pytest.mark.parametrize("path", ["no-compose", "labels", "down"])
async def test_enabled_observer_rejects_control_endpoint_and_rolls_back(
    engine, tmp_path, monkeypatch, docker_control_probe, action, path
):
    log, _ = docker_control_probe
    async with observer_app(engine, tmp_path, monkeypatch, "observer", runtime_enabled=True) as (
        client,
        factory,
        _,
        headers,
    ):
        await configure_cleanup(factory, tmp_path, path)
        async with factory() as session:
            row = await session.get(Workspace, "ws_cloud_stable")
            before = (
                row.status,
                row.version,
                row.execution_claimed_by,
                row.execution_claim_epoch,
                row.monitor_claimed_by,
            )
        response = await request_control(client, action, headers)
        assert response.status_code == 409, response.text
        assert "EXECUTION_DOCKER_ENDPOINT_REQUIRED" in response.text
        assert not calls(log)
        async with factory() as session:
            row = await session.get(Workspace, "ws_cloud_stable")
            assert (
                row.status,
                row.version,
                row.execution_claimed_by,
                row.execution_claim_epoch,
                row.monitor_claimed_by,
            ) == before
            assert await session.scalar(select(func.count()).select_from(Operation)) == 0
            assert await session.scalar(select(func.count()).select_from(WorkspaceEvent)) == 0
