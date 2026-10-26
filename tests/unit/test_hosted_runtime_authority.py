"""Real HTTP/PG authority; only the Linux tmpfs mount probe is synthetic here."""

import asyncio
import base64
import os
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from awf.api.app import configure_database, create_app
from awf.api.schemas import WorkspaceCreateRequest
from awf.common.config import Settings, get_settings
from awf.db.models import Workspace
from awf.db.session import make_engine, make_session_factory
from awf.hosted_node import runtime_views
from awf.hosted_node.config import HostedSettings, RuntimeCapacity
from awf.hosted_node.host_config import HostSettings
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_barrier import install_barrier
from awf.hosted_node.lifecycle_schemas import LifecycleSettings
from awf.hosted_node.lifecycle_store import locked
from awf.hosted_node.material_store import MaterialStore
from awf.hosted_node.receiver import Receiver
from awf.hosted_node.runtime_authority import WorkspaceAuthority
from awf.hosted_node.schemas import Binding, Install, Preparation
from tests.conftest import _ok_workspace_admission_disk_check
from tests.unit.test_hosted_runtime_contract import hosted_create


@pytest.fixture
async def authority(engine, tmp_path, monkeypatch):
    temporary = tempfile.TemporaryDirectory(dir="/dev/shm" if sys.platform == "linux" else tmp_path)
    root = Path(temporary.name)

    def simulated_tmpfs(self, path):
        self.root, self.closed = path, False
        fd = self._open_root()
        try:
            info = os.fstat(fd)
            self._device_inode = (info.st_dev, info.st_ino)
        finally:
            os.close(fd)

    if sys.platform != "linux":
        monkeypatch.setattr(MaterialStore, "__init__", simulated_tmpfs)
    # Portable unit runs use their available group. The isolated Linux
    # qualification runs as Core65532 with supplementary1000, exercising the
    # production default and real tmpfs without either override.
    if os.geteuid() != 0 and 1000 not in {os.getegid(), *os.getgroups()}:
        monkeypatch.setattr(runtime_views, "AGENT_GID", os.getegid())
    config = HostedSettings(
        owner="legacy-owner/nodea",
        incarnation="8063738394745155541",
        recovery_generation=1,
        controller_token="synthetic-controller",
        material_root=root,
        runtime_enabled=True,
        runtime_capacity=RuntimeCapacity(cpu_headroom_cores=4, memory_headroom_gb=8),
        lifecycle=LifecycleSettings(disk_ref="disk", database_ref="db"),
        host=HostSettings(
            state_root=tmp_path,
            device_path=Path("/dev/test"),
            filesystem_uuid="12345678-1234-1234-1234-123456789abc",
            workspaces_root=tmp_path / "work",
            docker_root=tmp_path / "docker",
            execution_cgroup=Path("/sys/fs/cgroup/test"),
            identity_file=tmp_path / "identity",
            docker_socket=tmp_path / "docker.sock",
            database_name="test",
            database_role="test",
        ),
    )
    async with engine.begin() as connection:
        await connection.run_sync(install_barrier)
        schema = await connection.scalar(text("select current_schema()"))
    # Exercise the production pool, not a fresh TCP connection for every
    # consumer authorization query; retain the disposable schema and barrier.
    runtime_engine = make_engine(
        engine.url.update_query_dict({"awf_search_path": schema}).render_as_string(
            hide_password=False
        )
    )
    factory = make_session_factory(runtime_engine)
    receiver = await Receiver.start(config, factory)
    await receiver.identity_status("accepted")
    # The host/recovery verifier is exercised separately; this fixture starts at
    # its accepted durable state, not by disabling the real admission trigger.
    async with factory.begin() as session:
        row = await locked(session, receiver.identity)
        row.phase, row.recovery_key, row.receipt_ref = "ready", "accepted", "proof"
    runtime = WorkspaceAuthority(receiver)
    value = hosted_create()
    for role, binding in value["hosted"]["credentials"].items():
        binding["identity"] = receiver.identity.model_dump()
        await receiver.prepare(
            Preparation.model_validate({key: binding[key] for key in Preparation.model_fields})
        )
        result = await receiver.install(
            Install.model_validate(
                {
                    **binding,
                    "material": base64.b64encode(f"synthetic-{role}-key".encode()).decode(),
                }
            )
        )
        assert result.status == "current"
    try:
        yield runtime, value, factory
    finally:
        try:
            if not receiver.closed:
                await receiver.close()
        finally:
            await runtime_engine.dispose()
            temporary.cleanup()


async def test_authority_resolves_both_current_credentials_and_actual_workspace(authority):
    runtime, value, _ = authority
    payload = WorkspaceCreateRequest.model_validate(value)
    material = await runtime.admit(payload)
    assert material.environment["CODEX_API_KEY"] == "synthetic-agent-key"
    assert "OPENAI_API_KEY" not in material.environment
    assert material.environment["GH_TOKEN"] == "synthetic-github-key"
    assert "synthetic-agent-key" not in repr(material)
    assert not (
        runtime.receiver.store.root / runtime.views._key("ws_cloud_stable") / "view"
    ).exists()


@pytest.fixture
async def bound_workspace(authority):
    runtime, value, factory = authority
    async with factory.begin() as session:
        session.add(
            Workspace(
                id="ws_cloud_stable",
                status="requested",
                repo_url="https://github.com/example/repo.git",
                branch_base="main",
                task_title="Synthetic",
                task_prompt="Synthetic",
                agent="codex",
                task_policy={
                    "hosted_node": WorkspaceCreateRequest.model_validate(value).hosted.model_dump(
                        mode="json"
                    )
                },
            )
        )
    return runtime, value, factory


async def test_runtime_profile_uses_only_readable_workspace_view(bound_workspace):
    from awf.profiles.models import WorkspaceProfile

    runtime, _, _ = bound_workspace
    resolved = await runtime.profile_mounts(WorkspaceProfile(name="synthetic"), "ws_cloud_stable")
    assert len(resolved.mounts) == 1
    mount = resolved.mounts[0]
    assert mount.target == "/run/awf/secrets" and mount.mode == "ro"
    assert Path(mount.source).name == "view"
    assert Path(mount.source).stat().st_gid == runtime.views.gid
    assert (Path(mount.source) / "openai.api-key").read_text() == "synthetic-agent-key"
    assert not resolved.environment


async def test_expiry_during_final_database_checks_denies_material(authority, monkeypatch):
    from awf.hosted_node import runtime_authority

    runtime, value, _ = authority
    payload = WorkspaceCreateRequest.model_validate(value)
    expiry = payload.hosted.credentials.agent.expires_at
    original = runtime.admission
    calls = 0

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp((expiry + timedelta(seconds=1)).timestamp(), tz=tz)

    async def admission():
        nonlocal calls
        await original()
        calls += 1
        if calls == 3:
            monkeypatch.setattr(runtime_authority, "datetime", Clock)

    monkeypatch.setattr(runtime, "admission", admission)
    with pytest.raises(MaterialDeniedError, match="PROTECTED_MATERIAL_UNAVAILABLE"):
        await runtime.admit(payload)


async def test_oauth_json_is_not_converted_to_an_api_key(authority):
    from awf.hosted_node.schemas import Refresh

    runtime, value, _ = authority
    binding = Binding.model_validate(value["hosted"]["credentials"]["agent"])
    receipt = await runtime.receiver.status(binding)
    updated = {**binding.model_dump(), "idempotency_key": "refresh", "revision": 2, "fence": 2}
    await runtime.receiver.install(
        Refresh(
            **updated,
            predecessor=receipt.receipt_id,
            material=base64.b64encode(b'{"tokens":{"access_token":"synthetic"}}').decode(),
        ),
        refresh=True,
    )
    value["hosted"]["credentials"]["agent"] = Binding.model_validate(updated).model_dump(
        mode="json"
    )
    with pytest.raises(MaterialDeniedError, match="INVALID_API_KEY_MATERIAL"):
        await runtime.admit(WorkspaceCreateRequest.model_validate(value))


async def test_cancelled_publication_cannot_restore_a_readable_view(bound_workspace, monkeypatch):
    runtime, _, _ = bound_workspace
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    publish = runtime.views.publish

    def delayed(*args):
        entered.set()
        assert release.wait(timeout=5)
        try:
            return publish(*args)
        finally:
            finished.set()

    monkeypatch.setattr(runtime.views, "publish", delayed)
    pending = asyncio.create_task(runtime.workspace("ws_cloud_stable"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert await asyncio.to_thread(finished.wait, 5)
    assert not (
        runtime.receiver.store.root / runtime.views._key("ws_cloud_stable") / "view"
    ).exists()


async def test_final_admission_denial_removes_already_published_view(bound_workspace, monkeypatch):
    runtime, _, _ = bound_workspace
    original = runtime.admission
    calls = 0

    async def admission():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise MaterialDeniedError("NODE_ADMISSION_CLOSED", 409)
        await original()

    monkeypatch.setattr(runtime, "admission", admission)
    with pytest.raises(MaterialDeniedError, match="NODE_ADMISSION_CLOSED"):
        await runtime.workspace("ws_cloud_stable")
    assert not (
        runtime.receiver.store.root / runtime.views._key("ws_cloud_stable") / "view"
    ).exists()


@pytest.mark.parametrize(
    "failure", ["missing", "revoked", "expired", "owner", "provider", "override"]
)
async def test_authority_fails_closed_before_consumer(authority, failure):
    runtime, value, _ = authority
    binding = value["hosted"]["credentials"]["agent"]
    if failure == "missing":
        value.pop("hosted")
    elif failure == "revoked":
        await runtime.receiver.revoke(
            Binding.model_validate(
                {
                    **binding,
                    "idempotency_key": "revoke",
                    "fence": 2,
                }
            )
        )
    elif failure == "expired":
        value["hosted"]["credentials"]["agent"]["expires_at"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
    elif failure == "owner":
        for item in value["hosted"]["credentials"].values():
            item["identity"]["owner"] = "other"
    elif failure == "provider":
        value["task"]["agent"] = "claude_code"
    else:
        value["preflight"] = {
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "no",
        }
    with pytest.raises(MaterialDeniedError):
        await runtime.admit(WorkspaceCreateRequest.model_validate(value))


async def test_http_create_uses_actual_bound_id_and_idempotency(authority, monkeypatch):
    from awf.service import provider_readiness

    def probe(args, **kwargs):
        assert args[0] == "docker"
        return subprocess.CompletedProcess(args, 0, "synthetic-cli", "")

    monkeypatch.setattr(provider_readiness, "_run_subprocess", probe)
    runtime, value, factory = authority
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    app.state.hosted_receiver = runtime.receiver
    app.state.hosted_runtime = runtime
    settings = Settings(
        _env_file=None, api_token="synthetic-operator", hosted_node=runtime.receiver.config
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.workspace_admission_disk_check = _ok_workspace_admission_disk_check
    factory.configure(info={**factory.kw.get("info", {}), "awf_hosted_runtime": runtime})
    headers = {
        "Authorization": "Bearer synthetic-controller",
        "Idempotency-Key": "cloud-create",
        **runtime.receiver.identity.headers(),
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/v1/workspaces", json=value, headers=headers)
        assert first.status_code == 202, first.text
        again = await client.post("/v1/workspaces", json=value, headers=headers)
        assert again.status_code == 202, again.text
        assert first.json()["workspace_id"] == again.json()["workspace_id"] == "ws_cloud_stable"
        async with factory() as session:
            rows = list(await session.scalars(select(Workspace)))
            assert len(rows) == 1
            assert rows[0].task_policy["hosted_node"] == WorkspaceCreateRequest.model_validate(
                value
            ).hosted.model_dump(mode="json")
        wrong = {**headers, "Authorization": "Bearer synthetic-operator"}
        assert (await client.post("/v1/workspaces", json=value, headers=wrong)).status_code == 403
