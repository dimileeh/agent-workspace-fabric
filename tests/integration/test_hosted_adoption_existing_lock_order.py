"""Re-adoption locks both distinct predecessors before the admission mutex."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.hosted_node.runtime_authority import WorkspaceAuthority
from awf.hosted_node.runtime_schemas import HostedWorkspace
from awf.node.cleanup import WorkspaceCleanupResult
from awf.service import pr_monitor_adoption
from awf.service.controls import WorkspaceControlService
from tests.integration.test_hosted_lifecycle_migrated import engine as engine
from tests.integration.test_hosted_lifecycle_parity import fresh_association, source_workspace
from tests.unit.api.test_pr_monitor_adoption import _metadata
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client


@pytest.mark.parametrize("action", ["cancel", "stop"])
async def test_control_existing_adoption_and_distinct_source_successor(
    authority, create_client, create_application, tmp_path, monkeypatch, action
):
    await source_workspace(authority, create_client, tmp_path, status="failed")
    runtime, value, factory = authority
    client, headers = create_client
    source_id, existing_id, destination_id = "ws_cloud_stable", "ws_adoption_b", "ws_adoption_c"
    source_association = HostedWorkspace.model_validate(value["hosted"]).model_dump(mode="json")
    create_application[0].state.pr_adoption_metadata_fetcher = AsyncMock(
        return_value=replace(
            _metadata(number=42),
            head_repo_slug="example/repo",
            url="https://github.com/example/repo/pull/42",
        )
    )
    body = {
        "pr_url": "https://github.com/example/repo/pull/42",
        "repo_url": "https://github.com/example/repo.git",
        "source_workspace_id": source_id,
        "hosted": await fresh_association(runtime, value, workspace_id=existing_id),
    }
    initial = await client.post("/v1/workspaces/adopt-pr", json=body, headers=headers)
    assert initial.status_code == 202, initial.text
    assert initial.json()["workspace_id"] == existing_id
    assert initial.json()["attached_existing"] is False
    async with factory.begin() as session:
        existing = await session.get(Workspace, existing_id)
        assert existing is not None
        original_key = existing.idempotency_key
        assert original_key is not None
        existing.status = "failed"
        existing.node_id = runtime.receiver.identity.owner
        await WorkspaceRepository(session).add_event(
            existing,
            event_type="workspace.pre_launch_failed",
            reason_code="SYNTHETIC_PRELAUNCH_FAILURE",
        )
    association = await fresh_association(runtime, value, workspace_id=destination_id)
    body = {**body, "hosted": association}

    first_locked, source_locked, release_first = (asyncio.Event() for _ in range(3))
    pids: dict[str, int] = {}
    source_checks = 0
    original_workspace = WorkspaceControlService._require_workspace_for_update
    original_source = pr_monitor_adoption.historical_source

    async def workspace(
        self: WorkspaceControlService, repo: WorkspaceRepository, workspace_id: str
    ) -> Workspace:
        assert workspace_id == existing_id
        result = await original_workspace(self, repo, workspace_id)
        pid = await self._session.scalar(text("select pg_backend_pid()"))
        assert isinstance(pid, int)
        pids["first"] = pid
        first_locked.set()
        await release_first.wait()
        return result

    async def historical_source(
        session: AsyncSession,
        authority: WorkspaceAuthority,
        row: Workspace | None,
        namespace: str,
    ) -> HostedWorkspace:
        nonlocal source_checks
        result = await original_source(session, authority, row, namespace)
        if row is not None and row.id == source_id:
            source_checks += 1
            # The first check is the unlocked preview; the second follows A's
            # FOR UPDATE, immediately before the separate existing-B row lock.
            if source_checks == 2:
                pid = await session.scalar(text("select pg_backend_pid()"))
                assert isinstance(pid, int)
                pids["second"] = pid
                source_locked.set()
        return result

    monkeypatch.setattr(WorkspaceControlService, "_require_workspace_for_update", workspace)
    monkeypatch.setattr(pr_monitor_adoption, "historical_source", historical_source)
    monkeypatch.setattr(
        WorkspaceControlService,
        "_release_runtime_stack",
        AsyncMock(return_value=WorkspaceCleanupResult.skipped()),
    )
    first = asyncio.create_task(
        client.post(
            f"/v1/workspaces/{existing_id}/{action}",
            json={"stop_stack": False} if action == "cancel" else {},
            headers={
                **headers,
                "Authorization": "Bearer synthetic-operator",
                "Idempotency-Key": "control-existing-adoption",
            },
        )
    )
    second = None
    try:
        async with asyncio.timeout(15):
            await first_locked.wait()
            second = asyncio.create_task(
                client.post("/v1/workspaces/adopt-pr", json=body, headers=headers)
            )
            await source_locked.wait()
            async with factory() as observer:
                while not await observer.scalar(
                    text("select :first = any(pg_blocking_pids(:second))"), pids
                ):
                    await asyncio.sleep(0.01)
            release_first.set()
            responses = await asyncio.gather(first, second)
        assert [response.status_code for response in responses] == [200, 202], [
            response.text for response in responses
        ]
        assert source_checks == 2
        assert responses[1].json()["workspace_id"] == destination_id
        assert responses[1].json()["attached_existing"] is False
        replay = await client.post("/v1/workspaces/adopt-pr", json=body, headers=headers)
        assert replay.status_code == 202, replay.text
        assert replay.json()["workspace_id"] == destination_id
        assert replay.json()["attached_existing"] is True
        async with factory() as session:
            source = await session.get(Workspace, source_id)
            existing = await session.get(Workspace, existing_id)
            destination = await session.get(Workspace, destination_id)
            assert source is not None and existing is not None and destination is not None
            assert source.status == "failed"
            assert source.task_policy["hosted_node"] == source_association
            assert existing.idempotency_key != original_key
            assert destination.idempotency_key == original_key
            assert destination.task_policy["hosted_source_workspace_id"] == source_id
            assert destination.task_policy["hosted_node"] == association
            assert await session.scalar(select(func.count()).select_from(Workspace)) == 3
    finally:
        release_first.set()
        pending = [task for task in (first, second) if task is not None]
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
