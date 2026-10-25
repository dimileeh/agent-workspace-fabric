"""Different idempotency keys cannot race one actual hosted workspace identity."""

import asyncio
import base64
import subprocess
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.app import configure_database, create_app
from awf.api.routes import workspaces as workspace_routes
from awf.api.schemas import WorkspaceCreateRequest
from awf.common.config import Settings, get_settings
from awf.db.models import QueueDecision, ResourceReservation, Task, TaskAttempt, Workspace
from awf.db.repositories import WorkspaceRepository
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.schemas import Binding, Install, Preparation, Refresh
from tests.conftest import _ok_workspace_admission_disk_check
from tests.unit.test_hosted_runtime_authority import authority as authority


@pytest.fixture
async def create_client(authority, monkeypatch):
    from awf.service import provider_readiness

    monkeypatch.setattr(
        provider_readiness,
        "_run_subprocess",
        lambda args, **_kw: subprocess.CompletedProcess(args, 0, "synthetic-cli", ""),
    )
    runtime, _, factory = authority
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    app.state.hosted_receiver, app.state.hosted_runtime = runtime.receiver, runtime
    settings = Settings(
        _env_file=None, api_token="synthetic-operator", hosted_node=runtime.receiver.config
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.workspace_admission_disk_check = _ok_workspace_admission_disk_check
    factory.configure(info={**factory.kw.get("info", {}), "awf_hosted_runtime": runtime})
    headers = {
        **runtime.receiver.identity.headers(),
        "Authorization": "Bearer synthetic-controller",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        yield client, headers


def readable_view(runtime):
    return runtime.receiver.store.root / runtime.views._key("ws_cloud_stable") / "view"


def readable_material(runtime):
    path = readable_view(runtime)
    return {name: (path / name).read_bytes() for name in ("openai.api-key", "github.api-key")}


async def alternate_credentials(runtime, value):
    alternate = deepcopy(value)
    for role, binding in alternate["hosted"]["credentials"].items():
        for field in ("preparation_id", "grant_id", "idempotency_key"):
            binding[field] = f"conflicting-{role}-{field}"
        await runtime.receiver.prepare(
            Preparation.model_validate({key: binding[key] for key in Preparation.model_fields})
        )
        installed = await runtime.receiver.install(
            Install.model_validate(
                {
                    **binding,
                    "material": base64.b64encode(f"synthetic-conflicting-{role}".encode()).decode(),
                }
            )
        )
        assert installed.status == "current"
    return alternate


async def test_parallel_create_keys_conflict_without_partial_rows(
    authority, create_client, monkeypatch
):
    runtime, value, factory = authority
    client, headers = create_client
    alternate = await alternate_credentials(runtime, value)
    first_at_insert, release_first = asyncio.Event(), asyncio.Event()
    second_at_lock = asyncio.Event()
    original_create = WorkspaceRepository.create
    original_execute = AsyncSession.execute
    calls = 0
    lock_calls = 0

    async def create(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_at_insert.set()
            await release_first.wait()
        return await original_create(self, *args, **kwargs)

    async def execute(self, statement, *args, **kwargs):
        nonlocal lock_calls
        if "hashtext('awf:hosted-workspace')" in str(statement):
            lock_calls += 1
            if lock_calls == 2:
                second_at_lock.set()
        return await original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(WorkspaceRepository, "create", create)
    monkeypatch.setattr(AsyncSession, "execute", execute)
    try:
        first = asyncio.create_task(
            client.post(
                "/v1/workspaces", json=value, headers={**headers, "Idempotency-Key": "first"}
            )
        )
        await asyncio.wait_for(first_at_insert.wait(), 5)
        second = asyncio.create_task(
            client.post(
                "/v1/workspaces", json=alternate, headers={**headers, "Idempotency-Key": "second"}
            )
        )
        try:
            await asyncio.wait_for(second_at_lock.wait(), 5)
            assert not second.done()
        finally:
            release_first.set()
        responses = await asyncio.wait_for(asyncio.gather(first, second), 10)
        assert sorted(response.status_code for response in responses) == [202, 409], [
            r.text for r in responses
        ]
        conflict = next(response for response in responses if response.status_code == 409)
        assert conflict.json()["error_code"] == "HOSTED_WORKSPACE_ID_CONFLICT"
        async with factory() as session:
            for model in (Workspace, Task, TaskAttempt, ResourceReservation, QueueDecision):
                assert await session.scalar(select(func.count()).select_from(model)) == 1
        winner = "first" if responses[0].status_code == 202 else "second"
        replay = await client.post(
            "/v1/workspaces", json=value, headers={**headers, "Idempotency-Key": winner}
        )
        assert replay.status_code == 202
        assert replay.json()["workspace_id"] == "ws_cloud_stable"
        assert not readable_view(runtime).exists()
        await runtime.workspace("ws_cloud_stable")
        assert readable_material(runtime) == {
            "openai.api-key": b"synthetic-agent-key",
            "github.api-key": b"synthetic-github-key",
        }
    finally:
        release_first.set()


@pytest.mark.parametrize("same_key", [False, True])
async def test_rejected_create_preserves_incumbent_view_and_binding(
    authority, create_client, same_key
):
    runtime, value, factory = authority
    client, headers = create_client
    response = await client.post(
        "/v1/workspaces", json=value, headers={**headers, "Idempotency-Key": "first"}
    )
    assert response.status_code == 202
    await runtime.workspace("ws_cloud_stable")
    before = readable_material(runtime)
    alternate = await alternate_credentials(runtime, value)
    response = await client.post(
        "/v1/workspaces",
        json=alternate,
        headers={**headers, "Idempotency-Key": "first" if same_key else "second"},
    )
    assert response.status_code == 409
    async with factory() as session:
        workspace = await session.get(Workspace, "ws_cloud_stable")
        assert workspace.task_policy["hosted_node"] == WorkspaceCreateRequest.model_validate(
            value
        ).hosted.model_dump(mode="json")
    # Do not reauthorize the winner first: that would repair and hide corruption.
    assert readable_material(runtime) == before


@pytest.mark.parametrize("failure", ["disk", "commit"])
async def test_failed_initial_create_does_not_publish_readable_material(
    authority, create_client, monkeypatch, failure
):
    runtime, value, factory = authority
    client, headers = create_client
    commit_failed = False
    if failure == "disk":
        disk = _ok_workspace_admission_disk_check(Settings(_env_file=None))
        monkeypatch.setattr(
            workspace_routes,
            "_workspace_admission_disk_check",
            AsyncMock(return_value=replace(disk, ok=False, reason="INSUFFICIENT_DISK")),
        )
        expected_status = 503
    else:
        commit = AsyncSession.commit

        async def fail_commit(session):
            nonlocal commit_failed
            if await session.get(Workspace, "ws_cloud_stable") is not None:
                commit_failed = True
                raise SQLAlchemyError("synthetic commit failure")
            await commit(session)

        monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    response = await client.post(
        "/v1/workspaces", json=value, headers={**headers, "Idempotency-Key": "failed"}
    )
    if failure == "disk":
        assert response.status_code == expected_status, response.text
    else:
        # Request-scoped dependency teardown may follow response delivery.
        # Prove the injected commit failure and durable rollback, not acceptance.
        assert commit_failed
    async with factory() as session:
        for model in (Workspace, Task, TaskAttempt, ResourceReservation, QueueDecision):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    assert not readable_view(runtime).exists()


async def test_valid_refresh_replay_only_publishes_from_bound_consumer(authority, create_client):
    runtime, value, _ = authority
    client, headers = create_client
    headers = {**headers, "Idempotency-Key": "first"}
    assert (await client.post("/v1/workspaces", json=value, headers=headers)).status_code == 202
    await runtime.workspace("ws_cloud_stable")
    before = readable_material(runtime)
    for role, raw in value["hosted"]["credentials"].items():
        binding = Binding.model_validate(raw)
        receipt = await runtime.receiver.status(binding)
        refreshed = await runtime.receiver.install(
            Refresh(
                **{
                    **binding.model_dump(),
                    "idempotency_key": f"refresh-{role}",
                    "revision": 2,
                    "fence": 2,
                },
                predecessor=receipt.receipt_id,
                material=base64.b64encode(f"refreshed-{role}".encode()).decode(),
            ),
            refresh=True,
        )
        assert refreshed.status == "current"
    response = await client.post("/v1/workspaces", json=value, headers=headers)
    assert response.status_code == 202, response.text
    assert readable_material(runtime) == before
    await runtime.workspace("ws_cloud_stable")
    assert readable_material(runtime) == {
        "openai.api-key": b"refreshed-agent",
        "github.api-key": b"refreshed-github",
    }


@pytest.mark.parametrize("cancel", [False, True])
async def test_late_admission_failure_does_not_retract_incumbent_view(
    authority, create_client, monkeypatch, cancel
):
    runtime, value, _ = authority
    client, headers = create_client
    assert (
        await client.post(
            "/v1/workspaces", json=value, headers={**headers, "Idempotency-Key": "first"}
        )
    ).status_code == 202
    await runtime.workspace("ws_cloud_stable")
    before = readable_material(runtime)
    alternate = await alternate_credentials(runtime, value)
    admission = runtime.admission
    calls = 0

    async def deny_final_check():
        nonlocal calls
        calls += 1
        if calls == 3:
            if cancel:
                raise asyncio.CancelledError
            raise MaterialDeniedError("NODE_ADMISSION_CLOSED", 409)
        await admission()

    monkeypatch.setattr(runtime, "admission", deny_final_check)
    with pytest.raises(asyncio.CancelledError if cancel else MaterialDeniedError):
        await runtime.admit(WorkspaceCreateRequest.model_validate(alternate))
    assert readable_material(runtime) == before
