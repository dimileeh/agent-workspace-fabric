"""Repeated-boot attention and inert-history boundaries on actual HTTP/PostgreSQL."""

import asyncio
from copy import deepcopy

import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from awf.db.models import Operation, Task, TaskAttempt, Workspace, WorkspaceEvent
from tests.integration.test_retained_work_acceptance import (
    ACK,
    PREFIX,
    WS,
    fresh_bindings,
    replacement,
)
from tests.integration.test_retained_work_acceptance import retained as retained
from tests.unit.test_hosted_runtime_authority import authority as authority


@pytest.mark.parametrize(
    "retained",
    [
        {"kind": "terminal", "family": "execution_claim"},
        {"kind": "terminal", "family": "monitor_claim"},
    ],
    indirect=True,
)
async def test_quiescent_boot_still_counts_parked_terminal_attention(retained):
    client, runtime, factory, _, value, root = retained
    first = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert first.status_code == 200, first.text
    assert first.json()["recovery_acceptance"]["attention_workspaces"] == 1
    assert first.json()["database_quiescent"]
    async with factory() as session:
        workspace = await session.get(Workspace, WS)
        original_policy = deepcopy(workspace.task_policy)
        original_version = workspace.version
        assert workspace.status == "completed"
        assert workspace.execution_claimed_by is workspace.monitor_claimed_by is None

    assert (
        await client.post(PREFIX + "/maintenance/release", json={"generation": 7})
    ).status_code == 200
    async with factory.begin() as session:
        session.add_all(
            Workspace(
                id=f"ws_history_{index}",
                node_id=runtime.receiver.identity.owner,
                status="completed",
                repo_url="https://example.test/history",
                branch_base="main",
                task_title="Inert history",
                task_prompt="Synthetic",
                agent="codex",
            )
            for index in range(2048)
        )
        await session.flush()
        session.add_all(
            WorkspaceEvent(
                id=f"history-{index}",
                workspace_id=f"ws_history_{index}",
                event_type="workspace.completed",
            )
            for index in range(2048)
        )
        session.add_all(
            Operation(
                id=f"history-op-{index}",
                workspace_id=f"ws_history_{index}",
                type="execute",
                status="succeeded",
            )
            for index in range(2048)
        )
    async with replacement(
        (runtime, value, factory),
        root,
        maintenance_generation=8,
        incarnation="8063738394745155543",
    ) as third:
        loaded = []

        def record_load(_session, instance):
            loaded.append((type(instance).__name__, getattr(instance, "id", None)))

        event.listen(Session, "loaded_as_persistent", record_load)
        try:
            async with factory.begin() as historical_reader:
                await historical_reader.execute(
                    text("SELECT id FROM workspaces WHERE id LIKE 'ws_history_%' FOR UPDATE")
                )
                await historical_reader.execute(
                    text("SELECT id FROM workspace_events WHERE id LIKE 'history-%' FOR UPDATE")
                )
                response = await asyncio.wait_for(
                    third[0].post(PREFIX + "/recovery/acknowledge", json=ACK), timeout=5
                )
        finally:
            event.remove(Session, "loaded_as_persistent", record_load)
        assert response.status_code == 200, response.text
        assert response.json()["database_quiescent"]
        acceptance = response.json()["recovery_acceptance"]
        assert acceptance["previous_identity"] == runtime.receiver.identity.model_dump()
        assert acceptance["attention_workspaces"] == 1
        assert acceptance["queued_workspaces"] == acceptance["monitor_workspaces"] == 0
        assert (await third[0].get(PREFIX + "/readiness")).json()[
            "recovery_acceptance"
        ] == acceptance
        assert (await third[0].post(PREFIX + "/recovery/acknowledge", json=ACK)).json()[
            "recovery_acceptance"
        ] == acceptance
        assert ("Workspace", WS) in loaded
        assert not any(
            name in {"WorkspaceEvent", "Operation", "TaskAttempt", "Task"} for name, _ in loaded
        )
        assert not any(str(key).startswith("ws_history_") for _, key in loaded)
        async with factory() as session:
            workspace = await session.get(Workspace, WS)
            assert (
                workspace.task_policy == original_policy and workspace.version == original_version
            )
            assert workspace.status == "completed"
            assert workspace.execution_claimed_by is workspace.monitor_claimed_by is None
            assert (
                len(
                    list(
                        await session.scalars(
                            select(WorkspaceEvent.id).where(WorkspaceEvent.workspace_id == WS)
                        )
                    )
                )
                == 1
            )
        with pytest.raises(DBAPIError, match="RECOVERY_ATTENTION_REQUIRED"):
            async with factory.begin() as session:
                await session.execute(
                    update(Workspace)
                    .where(Workspace.id == WS)
                    .values(execution_claimed_by="new-worker")
                )


@pytest.mark.parametrize(
    "retained", [{"kind": "terminal", "family": "execution_claim"}], indirect=True
)
async def test_quiescent_parked_attention_still_obeys_inventory_limit(retained, monkeypatch):
    from awf.hosted_node import retained_work

    client, runtime, factory, _, value, root = retained
    first = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert first.status_code == 200, first.text
    assert first.json()["database_quiescent"]
    assert first.json()["recovery_acceptance"]["attention_workspaces"] == 1
    monkeypatch.setattr(retained_work, "MAX_RETAINED_WORKSPACES", 0)
    async with replacement(
        (runtime, value, factory),
        root,
        maintenance_generation=8,
        incarnation="8063738394745155543",
    ) as third:
        response = await third[0].post(PREFIX + "/recovery/acknowledge", json=ACK)
        assert response.status_code == 409, response.text
        assert response.json()["error_code"] == "RECOVERY_OUTCOME_UNRESOLVED"
        state = (await third[0].get(PREFIX + "/readiness")).json()
        assert state["state"] == "recovering" and state["database_quiescent"]
        assert state["recovery_acceptance"] is None
        assert state["admission_closed"] and not state["node_ready"]


@pytest.mark.parametrize("retained", ["terminal"], indirect=True)
async def test_idle_boot_history_is_not_next_boot_resumption_authority(retained):
    client, runtime, factory, boot1, value, root = retained
    idle = await client.post(PREFIX + "/recovery/acknowledge", json=ACK)
    assert idle.status_code == 200, idle.text
    assert idle.json()["database_quiescent"]
    assert (
        await client.post(PREFIX + "/maintenance/release", json={"generation": 7})
    ).status_code == 200
    active_value = deepcopy(value)
    active_value["hosted"]["workspace_id"] = "ws_active"
    for binding in active_value["hosted"]["credentials"].values():
        binding["workspace_id"] = "ws_active"
    active_value["hosted"]["credentials"] = await fresh_bindings(
        (*retained[:4], active_value, root), prefix="boot2-"
    )
    async with factory.begin() as session:
        history = await session.get(Workspace, WS)
        original_policy, original_version = deepcopy(history.task_policy), history.version
        session.add(
            Workspace(
                id="ws_active",
                node_id=runtime.receiver.identity.owner,
                status="requested",
                repo_url=history.repo_url,
                branch_base="main",
                task_title="New",
                task_prompt="Synthetic",
                agent="codex",
                task_policy={"hosted_node": active_value["hosted"]},
            )
        )
        session.add(
            Task(
                id="active-task",
                repo_url=history.repo_url,
                base_branch="main",
                title="New",
                prompt="Synthetic",
            )
        )
        await session.flush()
        session.add(
            TaskAttempt(
                id="active-attempt",
                task_id="active-task",
                workspace_id="ws_active",
                attempt_number=1,
                agent="codex",
                status="requested",
                title="New",
                repo_url=history.repo_url,
                base_branch="main",
            )
        )
        for index in range(80):
            session.add(
                WorkspaceEvent(
                    id=f"history-{index}", workspace_id=WS, event_type="workspace.completed"
                )
            )
            session.add(
                Operation(
                    id=f"history-op-{index}", workspace_id=WS, type="execute", status="succeeded"
                )
            )
    async with replacement(
        (runtime, active_value, factory), root, maintenance_generation=8
    ) as third:
        await fresh_bindings(third, prefix="boot3-")
        loaded = []

        def record_load(_session, instance):
            loaded.append((type(instance).__name__, getattr(instance, "id", None)))

        event.listen(Session, "loaded_as_persistent", record_load)
        try:
            async with factory.begin() as historical_reader:
                await historical_reader.execute(
                    text("SELECT id FROM workspaces WHERE id=:id FOR UPDATE"), {"id": WS}
                )
                await historical_reader.execute(
                    text("SELECT id FROM workspace_events WHERE workspace_id=:id FOR UPDATE"),
                    {"id": WS},
                )
                response = await asyncio.wait_for(
                    third[0].post(PREFIX + "/recovery/acknowledge", json=ACK), timeout=5
                )
        finally:
            event.remove(Session, "loaded_as_persistent", record_load)
        assert response.status_code == 200, response.text
        assert response.json()["recovery_acceptance"]["queued_workspaces"] == 1
        assert response.json()["recovery_acceptance"]["attention_workspaces"] == 0
        assert ("Workspace", WS) not in loaded
        assert not any(name in {"WorkspaceEvent", "TaskAttempt", "Task"} for name, _ in loaded)
        assert not any(str(key).startswith("history-") for _, key in loaded)
        async with factory() as session:
            history = await session.get(Workspace, WS)
            assert history.task_policy == original_policy and history.version == original_version
            assert (
                history.task_policy["hosted_node"]["credentials"]["agent"]["identity"]
                == boot1.model_dump()
            )
            assert (
                len(
                    list(
                        await session.scalars(
                            select(WorkspaceEvent.id).where(WorkspaceEvent.workspace_id == WS)
                        )
                    )
                )
                == 80
            )
            assert (
                len(
                    list(
                        await session.scalars(
                            select(Operation.id).where(
                                Operation.workspace_id == WS, Operation.status == "succeeded"
                            )
                        )
                    )
                )
                == 80
            )
