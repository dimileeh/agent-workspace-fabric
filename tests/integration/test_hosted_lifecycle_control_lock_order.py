"""Real migrated admission triggers must not invert concurrent control locks."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from awf.db.enums import OperationType
from awf.hosted_node.runtime_authority import WorkspaceAuthority
from awf.hosted_node.runtime_schemas import HostedWorkspace
from awf.node.cleanup import WorkspaceCleanupResult
from awf.service import controls, controls_guide, pr_monitor_adoption, workspaces_retry
from awf.service.controls import WorkspaceControlService
from tests.integration.test_hosted_lifecycle_migrated import engine as engine
from tests.integration.test_hosted_lifecycle_parity import fresh_association, source_workspace
from tests.integration.test_hosted_lifecycle_reauthorization import install_pair, quiet_failed
from tests.unit.api.test_pr_monitor_adoption import _metadata
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_review_create import create_application as create_application
from tests.unit.test_hosted_runtime_review_create import create_client as create_client


async def wait_until_blocked(factory, pids):
    async with factory() as observer:
        while not await observer.scalar(
            text("select :first = any(pg_blocking_pids(:second))"), pids
        ):
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("first_action", ["cancel", "stop"])
@pytest.mark.parametrize("second_action", ["guide", "remonitor"])
@pytest.mark.parametrize("fresh_material", [False, True])
@pytest.mark.parametrize("same_key", [False, True])
async def test_controls_share_workspace_before_node_lock_order(
    authority,
    create_client,
    tmp_path,
    monkeypatch,
    first_action,
    second_action,
    fresh_material,
    same_key,
):
    await quiet_failed(authority, create_client, tmp_path)
    runtime, value, factory = authority
    client, headers = create_client
    body = {"hosted_authorization": {"namespace": "tenant", "actor": "caller"}}
    if second_action == "guide":
        body["directive"] = "Resume the existing review"
    if fresh_material:
        expected = HostedWorkspace.model_validate(value["hosted"]).model_dump(mode="json")
        replacement = await install_pair(client, headers, expected, runtime.receiver.identity)
        body["hosted_reauthorization"] = {
            "expected_association": expected,
            "replacement_association": replacement,
        }

    first_locked, second_entered, release_first = (asyncio.Event() for _ in range(3))
    pids = {}
    original_prepare = WorkspaceControlService._prepare_operation
    original_workspace = WorkspaceControlService._require_workspace_for_update
    original_authority = controls.control_authority

    async def prepare(self, *args, **kwargs):
        if kwargs["operation_type"] in {OperationType.cancel, OperationType.stop}:
            self._session.info["test_first_control"] = True
        return await original_prepare(self, *args, **kwargs)

    async def workspace(self, *args, **kwargs):
        result = await original_workspace(self, *args, **kwargs)
        if self._session.info.get("test_first_control"):
            pids["first"] = await self._session.scalar(text("select pg_backend_pid()"))
            first_locked.set()
            await release_first.wait()
        return result

    async def control_authority(session, *args, **kwargs):
        pids["second"] = await session.scalar(text("select pg_backend_pid()"))
        second_entered.set()
        return await original_authority(session, *args, **kwargs)

    monkeypatch.setattr(WorkspaceControlService, "_prepare_operation", prepare)
    monkeypatch.setattr(WorkspaceControlService, "_require_workspace_for_update", workspace)
    monkeypatch.setattr(controls, "control_authority", control_authority)
    monkeypatch.setattr(controls_guide, "control_authority", control_authority)
    monkeypatch.setattr(
        WorkspaceControlService,
        "_release_runtime_stack",
        AsyncMock(return_value=WorkspaceCleanupResult.skipped()),
    )

    first = asyncio.create_task(
        client.post(
            f"/v1/workspaces/ws_cloud_stable/{first_action}",
            json={"stop_stack": False} if first_action == "cancel" else {},
            headers={
                **headers,
                "Authorization": "Bearer synthetic-operator",
                "Idempotency-Key": "first-control",
            },
        )
    )
    second = None
    try:
        async with asyncio.timeout(15):
            await first_locked.wait()
            second = asyncio.create_task(
                client.post(
                    f"/v1/workspaces/ws_cloud_stable/{second_action}",
                    json=body,
                    headers={
                        **headers,
                        "Idempotency-Key": "first-control" if same_key else "second-control",
                    },
                )
            )
            await second_entered.wait()
            # Observe the real wait, rather than assuming a scheduler ordering.
            await wait_until_blocked(factory, pids)
            release_first.set()
            responses = await asyncio.gather(first, second)
        assert [response.status_code for response in responses] == [
            200,
            409 if same_key else 200,
        ], [response.text for response in responses]
    finally:
        release_first.set()
        pending = [task for task in (first, second) if task is not None]
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize("action", ["retry", "adopt-pr"])
async def test_cancel_and_successor_share_source_before_node_lock_order(
    authority, create_client, create_application, tmp_path, monkeypatch, action
):
    await source_workspace(authority, create_client, tmp_path, status="failed")
    runtime, value, factory = authority
    client, headers = create_client
    association = await fresh_association(runtime, value)
    if action == "retry":
        path, body = "/v1/workspaces/ws_cloud_stable/retry", {"hosted": association}
    else:
        create_application[0].state.pr_adoption_metadata_fetcher = AsyncMock(
            return_value=_metadata(number=42)
        )
        path, body = (
            "/v1/workspaces/adopt-pr",
            {
                "pr_url": "https://github.com/example/repo/pull/42",
                "repo_url": "https://github.com/example/repo.git",
                "source_workspace_id": "ws_cloud_stable",
                "hosted": association,
            },
        )
    first_locked, second_entered, release_first = (asyncio.Event() for _ in range(3))
    pids = {}
    original_workspace = WorkspaceControlService._require_workspace_for_update
    original_source = workspaces_retry.historical_source

    async def workspace(self, *args, **kwargs):
        result = await original_workspace(self, *args, **kwargs)
        pids["first"] = await self._session.scalar(text("select pg_backend_pid()"))
        first_locked.set()
        await release_first.wait()
        return result

    async def historical_source(session, *args, **kwargs):
        pids["second"] = await session.scalar(text("select pg_backend_pid()"))
        second_entered.set()
        return await original_source(session, *args, **kwargs)

    monkeypatch.setattr(WorkspaceControlService, "_require_workspace_for_update", workspace)
    monkeypatch.setattr(workspaces_retry, "historical_source", historical_source)
    monkeypatch.setattr(pr_monitor_adoption, "historical_source", historical_source)
    monkeypatch.setattr(
        WorkspaceControlService,
        "_release_runtime_stack",
        AsyncMock(return_value=WorkspaceCleanupResult.skipped()),
    )
    first = asyncio.create_task(
        client.post(
            "/v1/workspaces/ws_cloud_stable/cancel",
            json={"stop_stack": False},
            headers={
                **headers,
                "Authorization": "Bearer synthetic-operator",
                "Idempotency-Key": "first-control",
            },
        )
    )
    second = None
    try:
        async with asyncio.timeout(15):
            await first_locked.wait()
            second = asyncio.create_task(
                client.post(path, json=body, headers={**headers, "Idempotency-Key": "successor"})
            )
            await second_entered.wait()
            await wait_until_blocked(factory, pids)
            release_first.set()
            responses = await asyncio.gather(first, second)
        assert [response.status_code for response in responses] == [200, 202], [
            response.text for response in responses
        ]
    finally:
        release_first.set()
        pending = [task for task in (first, second) if task is not None]
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize("action", ["retry", "adopt-pr"])
async def test_create_and_successor_share_destination_before_node_lock_order(
    authority, create_client, create_application, tmp_path, monkeypatch, action
):
    await source_workspace(authority, create_client, tmp_path, status="failed")
    runtime, value, factory = authority
    client, headers = create_client
    association = await fresh_association(runtime, value)
    create = deepcopy(value)
    create["hosted"] = association
    if action == "retry":
        path, body = "/v1/workspaces/ws_cloud_stable/retry", {"hosted": association}
    else:
        create_application[0].state.pr_adoption_metadata_fetcher = AsyncMock(
            return_value=_metadata(number=42)
        )
        path, body = (
            "/v1/workspaces/adopt-pr",
            {
                "pr_url": "https://github.com/example/repo/pull/42",
                "repo_url": "https://github.com/example/repo.git",
                "source_workspace_id": "ws_cloud_stable",
                "hosted": association,
            },
        )
    first_locked, second_entered, release_first = (asyncio.Event() for _ in range(3))
    pids = {}
    original_guard = WorkspaceAuthority.guard_new_workspace
    original_destination = workspaces_retry.new_destination

    async def guard(self, session):
        has_destination = await session.scalar(
            text(
                "select exists(select 1 from pg_locks where pid=pg_backend_pid() "
                "and locktype='advisory' and granted and objsubid=2 "
                "and objid=(hashtext(:destination)::bigint & 4294967295))"
            ),
            {"destination": association["workspace_id"]},
        )
        if has_destination and not first_locked.is_set():
            pids["first"] = await session.scalar(text("select pg_backend_pid()"))
            first_locked.set()
            await release_first.wait()
        return await original_guard(self, session)

    async def destination(session, *args, **kwargs):
        pids["second"] = await session.scalar(text("select pg_backend_pid()"))
        second_entered.set()
        return await original_destination(session, *args, **kwargs)

    monkeypatch.setattr(WorkspaceAuthority, "guard_new_workspace", guard)
    monkeypatch.setattr(workspaces_retry, "new_destination", destination)
    monkeypatch.setattr(pr_monitor_adoption, "new_destination", destination)
    first = asyncio.create_task(
        client.post(
            "/v1/workspaces",
            json=create,
            headers={**headers, "Idempotency-Key": "destination-create"},
        )
    )
    second = None
    try:
        async with asyncio.timeout(15):
            await first_locked.wait()
            second = asyncio.create_task(
                client.post(path, json=body, headers={**headers, "Idempotency-Key": "successor"})
            )
            await second_entered.wait()
            await wait_until_blocked(factory, pids)
            release_first.set()
            responses = await asyncio.gather(first, second)
        assert [response.status_code for response in responses] == [202, 409], [
            response.text for response in responses
        ]
        assert responses[1].json()["error_code"] == "HOSTED_WORKSPACE_ID_CONFLICT"
    finally:
        release_first.set()
        pending = [task for task in (first, second) if task is not None]
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
