"""Transactional diagnostics while ordinary third-party admission stays off."""

import asyncio
from datetime import UTC, datetime

import pytest

from awf.db.repositories import WorkerHeartbeatRepository, WorkspaceRepository
from awf.db.session import make_session_factory

pytestmark = pytest.mark.usefixtures("mock_docker_cli_probe")


async def capable(engine):
    factory = make_session_factory(engine)
    async with factory() as session:
        now = datetime.now(UTC)
        await WorkerHeartbeatRepository(session).record_heartbeat(
            worker_id="diagnostics",
            node_id="local",
            started_at=now,
            last_heartbeat_at=now,
            poll_interval_seconds=5,
            execution_contract_version=2,
        )
        await session.commit()
    return factory


async def test_diagnostic_idempotency_conflicts_and_no_feature_gate(client, engine):
    factory = await capable(engine)
    body = {"agent": "codex", "provider": "ollama", "model": "candidate:cloud"}
    responses = await asyncio.gather(
        *(
            client.post(
                "/v1/providers/connection-tests", json=body, headers={"Idempotency-Key": "diag-one"}
            )
            for _ in range(2)
        )
    )
    assert [r.status_code for r in responses] == [202, 202]
    assert responses[0].json() == responses[1].json()
    data = responses[0].json()
    assert data["label"] == "provider_connection_test"
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(data["workspace_id"])
        assert ws.task_kind == "provider_connection_test"
        assert ws.repo_url.startswith("awf-internal:")
        assert ws.task_policy["agent_execution"]["provider"] == "ollama"
        assert ws.task_policy["provider_diagnostic"]["request"]["deadline_seconds"] == 180
    conflict = await client.post(
        "/v1/providers/connection-tests",
        json={**body, "deadline_seconds": 181},
        headers={"Idempotency-Key": "diag-one"},
    )
    assert conflict.status_code == 409


async def test_diagnostic_requires_capable_fleet_and_idempotency(client):
    body = {"agent": "claude_code", "provider": "openrouter", "model": "unknown"}
    assert (await client.post("/v1/providers/connection-tests", json=body)).status_code == 400
    denied = await client.post(
        "/v1/providers/connection-tests", json=body, headers={"Idempotency-Key": "new"}
    )
    assert denied.status_code == 409
    assert "WORKER_UNSUPPORTED" in denied.text


async def test_diagnostic_cannot_be_fabricated_by_generic_creation_or_retried(
    client, engine, monkeypatch
):
    await capable(engine)
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from awf.node.cleanup import WorkspaceCleanupResult

    monkeypatch.setattr(
        "awf.service.workspaces.default_cleaner",
        lambda: SimpleNamespace(cleanup=AsyncMock(return_value=WorkspaceCleanupResult.skipped())),
    )
    result = await client.post(
        "/v1/providers/connection-tests",
        json={"agent": "codex", "provider": "ollama", "model": "candidate:cloud"},
        headers={"Idempotency-Key": "retry-denied"},
    )
    ident = result.json()["workspace_id"]
    denied = await client.post(f"/v1/workspaces/{ident}/retry")
    assert denied.status_code == 409
    assert denied.json()["error_code"] == "PROVIDER_DIAGNOSTIC_REPLAY_FORBIDDEN"
    generic = await client.post(
        "/v1/workspaces",
        json={
            "repo": {"url": "https://github.com/example/project"},
            "task": {"title": "diag", "prompt": "arbitrary", "kind": "provider_connection_test"},
        },
    )
    assert generic.status_code == 422


async def test_diagnostic_uses_normal_create_admission_without_charging_replays(
    client, engine, monkeypatch
):
    from awf.common.config import get_settings

    await capable(engine)
    monkeypatch.setenv("AWF_WORKSPACE_CREATE_RATE_LIMIT_COUNT", "1")
    get_settings.cache_clear()
    body = {"agent": "codex", "provider": "ollama", "model": "candidate:cloud"}
    first = await client.post(
        "/v1/providers/connection-tests", json=body, headers={"Idempotency-Key": "limit-one"}
    )
    replay = await client.post(
        "/v1/providers/connection-tests", json=body, headers={"Idempotency-Key": "limit-one"}
    )
    limited = await client.post(
        "/v1/providers/connection-tests", json=body, headers={"Idempotency-Key": "limit-two"}
    )
    assert first.status_code == replay.status_code == 202
    assert first.json()["workspace_id"] == replay.json()["workspace_id"]
    assert limited.status_code == 429


async def test_idempotent_diagnostic_retains_original_operation_after_control_history(
    client, engine
):
    from awf.db.enums import OperationStatus, OperationType
    from awf.db.repositories import OperationRepository

    factory = await capable(engine)
    body = {"agent": "codex", "provider": "ollama", "model": "candidate:cloud"}
    first = await client.post(
        "/v1/providers/connection-tests", json=body, headers={"Idempotency-Key": "long-history"}
    )
    original = first.json()
    async with factory() as session:
        for _index in range(51):
            await OperationRepository(session).create(
                workspace_id=original["workspace_id"],
                operation_type=OperationType.stop,
                status=OperationStatus.succeeded,
            )
        await session.commit()
    replay = await client.post(
        "/v1/providers/connection-tests", json=body, headers={"Idempotency-Key": "long-history"}
    )
    assert replay.status_code == 202
    assert replay.json()["operation_id"] == original["operation_id"]


async def test_simultaneous_conflicting_diagnostic_requests_admit_only_one(client, engine):
    await capable(engine)
    body = {"agent": "codex", "provider": "ollama", "model": "candidate:cloud"}
    results = await asyncio.gather(
        *(
            client.post(
                "/v1/providers/connection-tests",
                json={**body, "deadline_seconds": deadline},
                headers={"Idempotency-Key": "conflicting"},
            )
            for deadline in (180, 181)
        )
    )
    assert sorted(result.status_code for result in results) == [202, 409]
    assert (
        next(result for result in results if result.status_code == 409).json()["error_code"]
        == "IDEMPOTENCY_CONFLICT"
    )


async def test_diagnostic_disk_denial_uses_normal_admission_and_creates_no_row(
    client, engine, monkeypatch
):
    from unittest.mock import AsyncMock

    from awf.service.disk import DiskCheck

    factory = await capable(engine)
    disk = DiskCheck(
        path=".awf",
        checked_path=".",
        total_bytes=10,
        used_bytes=10,
        free_bytes=0,
        percent_free=0,
        threshold_bytes=1,
        ok=False,
        status="blocked",
        reason="INSUFFICIENT_DISK",
    )
    monkeypatch.setattr(
        "awf.api.routes.workspaces._workspace_admission_disk_check", AsyncMock(return_value=disk)
    )
    response = await client.post(
        "/v1/providers/connection-tests",
        json={"agent": "codex", "provider": "ollama", "model": "candidate:cloud"},
        headers={"Idempotency-Key": "disk-denied"},
    )
    assert response.status_code == 503
    assert response.json()["error_code"] == "INSUFFICIENT_DISK"
    async with factory() as session:
        assert await WorkspaceRepository(session).get_by_idempotency_key("disk-denied") is None
