"""Real HTTP paging and service-level GC remain usable around contained work."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from awf.common.ids import new_event_id
from awf.db.models import Operation, Workspace, WorkspaceEvent
from awf.db.repositories.service_gc_request_repo import ServiceGCRequestRepository
from awf.service.gc import plan_terminal_workspace_gc
from tests.integration.test_retained_work_acceptance import ACK, PREFIX, WS, fresh_bindings
from tests.integration.test_retained_work_acceptance import authority as authority
from tests.integration.test_retained_work_acceptance import retained as retained


@pytest.mark.parametrize("retained", ["queued", "uncertain"], indirect=True)
async def test_recovery_event_is_a_usable_http_pagination_boundary(retained):
    client, _, factory, _, _, _ = retained
    await fresh_bindings(retained)
    accepted = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert accepted.status_code == 200, accepted.text
    older_id = new_event_id()
    async with factory.begin() as session:
        session.add(
            WorkspaceEvent(
                id=older_id,
                workspace_id=WS,
                event_type="workspace.phase_started",
                occurred_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
    path = f"/v1/workspaces/{WS}/events"
    headers = {"Authorization": "Bearer synthetic-operator"}
    first = await client.get(path, params={"limit": 1}, headers=headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["items"][0]["event_type"] == "workspace.recovery_accepted"
    assert body["has_more"] and body["next_cursor"]
    second = await client.get(
        path, params={"limit": 1, "cursor": body["next_cursor"]}, headers=headers
    )
    assert second.status_code == 200, second.text
    assert [event["id"] for event in second.json()["items"]] == [older_id]
    assert not second.json()["has_more"]
    assert (await client.post(PREFIX + "/recovery/acknowledge", json=ACK)).status_code == 200
    async with factory() as session:
        events = list(
            await session.scalars(
                select(WorkspaceEvent.id).where(
                    WorkspaceEvent.event_type == "workspace.recovery_accepted"
                )
            )
        )
        assert events == [body["items"][0]["id"]]


@pytest.mark.parametrize("retained", ["terminal_uncertain"], indirect=True)
async def test_service_gc_request_lifecycle_preserves_attention_and_safe_terminal_gc(retained):
    client, runtime, factory, _, _, root = retained
    assert (await client.post(PREFIX + "/recovery/acknowledge", json=ACK)).status_code == 200
    assert (
        await client.post(PREFIX + "/maintenance/release", json={"generation": 7})
    ).status_code == 200
    async with factory.begin() as session:
        session.add(
            Workspace(
                id="safe-terminal",
                status="completed",
                node_id=runtime.receiver.identity.owner,
                repo_url="https://example.test/history.git",
                branch_base="main",
                task_title="Safe terminal history",
                task_prompt="Never execute",
                agent="codex",
                task_policy={},
                updated_at=datetime.now(UTC) - timedelta(days=3),
            )
        )
    plan = await plan_terminal_workspace_gc(
        factory, work_dir=root, min_age_hours=0, include_statuses=["completed"]
    )
    assert [candidate.workspace_id for candidate in plan.candidates] == ["safe-terminal"]
    now = datetime.now(UTC)
    async with factory.begin() as session:
        request = await ServiceGCRequestRepository(session).create_pending(
            node_id=runtime.receiver.identity.owner,
            requested_at=now,
            deadline_at=now + timedelta(minutes=1),
        )
        request_id = request.id
    async with factory.begin() as session:
        claimed = await ServiceGCRequestRepository(session).claim_oldest_pending(
            node_id=runtime.receiver.identity.owner, now=now
        )
        assert claimed is not None and claimed.id == request_id
    async with factory.begin() as session:
        result = await ServiceGCRequestRepository(session).mark_completed(
            request_id=request_id, result={"candidate_ids": ["safe-terminal"]}, now=now
        )
        assert result.status == "completed"
    for model in (Workspace, Operation):
        with pytest.raises(DBAPIError, match="RECOVERY_ATTENTION_REQUIRED"):
            async with factory.begin() as session:
                predicate = model.id == WS if model is Workspace else model.workspace_id == WS
                await session.execute(update(model).where(predicate).values(status="failed"))
    async with factory() as session:
        assert (await session.get(Workspace, WS)).status == "completed"
        assert (await session.get(Operation, "uncertain")).status == "running"
