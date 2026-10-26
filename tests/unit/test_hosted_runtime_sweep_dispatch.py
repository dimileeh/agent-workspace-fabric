"""Retained cleanup fairness and denied recovery in the actual worker poll."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, text

from awf.common.commands import CommandResult
from awf.db.models import Workspace, WorkspaceEvent
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.runtime_consumers import WorkspaceConsumers
from awf.service import worker
from tests.unit.control.test_worker_parts.test_worker_part_025 import (
    _BlockingMonitorExecutor,
    _live_agent_snapshot,
    _RecordingRuntimeInspector,
)
from tests.unit.service.test_worker import _settings
from tests.unit.test_hosted_runtime_authority import authority as authority
from tests.unit.test_hosted_runtime_owned_sweep import deny
from tests.unit.test_hosted_runtime_review_retained import retained


@pytest.mark.parametrize(
    "failure,when,stop_failure",
    [
        ("revoke", "before_poll", None),
        ("expiry", "before_poll", None),
        ("revoke", "before_recovery", None),
        ("expiry", "before_recovery", None),
        ("revoke", "before_recovery", "material"),
        ("revoke", "before_recovery", "os"),
        ("revoke", "watcher", None),
        ("expiry", "watcher", None),
        ("revoke", "final_check", None),
        ("expiry", "final_check", None),
        ("revoke", "watcher", "material"),
        ("revoke", "watcher", "os"),
        ("revoke", "final_check", "material"),
        ("revoke", "final_check", "os"),
    ],
)
async def test_denied_preserved_recovery_does_not_block_healthy_dispatch(
    authority, tmp_path, monkeypatch, failure, when, stop_failure
):
    runtime, value, factory = authority
    denied = await retained(runtime, value, factory, "ws_cloud_stable", status="pushing")
    healthy = await retained(runtime, value, factory, "ws_healthy", status="pushing", other=True)
    async with factory.begin() as session:
        for workspace_id in ("ws_cloud_stable", "ws_healthy"):
            row = await session.get(Workspace, workspace_id)
            row.branch_name = row.remote_push_branch = f"awf/{workspace_id}"
            row.base_commit = "a" * 40
    calls = []

    class Commands:
        async def run(self, args, **kwargs):
            assert args[:3] == ["gh", "pr", "list"]
            workspace_id = consumers.workspace_id.get()
            assert workspace_id in {"ws_cloud_stable", "ws_healthy"}
            assert kwargs["env"]["GH_TOKEN"] == (
                "synthetic-github-key"
                if workspace_id == "ws_cloud_stable"
                else "synthetic-other-github"
            )
            async with factory.begin() as session:
                assert (
                    await session.scalar(
                        select(Workspace)
                        .where(Workspace.id == workspace_id)
                        .with_for_update(nowait=True)
                    )
                    is not None
                )
            calls.append(workspace_id)
            if workspace_id == "ws_cloud_stable":
                assert when in {"watcher", "final_check"}
                await deny(runtime, value, failure, monkeypatch)
                if when == "watcher":
                    await asyncio.wait_for(asyncio.Event().wait(), 5)
            return CommandResult(
                0,
                json.dumps(
                    [
                        {
                            "url": "https://github.com/example/repo/pull/270",
                            "number": 270,
                            "headRefName": f"awf/{workspace_id}",
                            "headRefOid": "e" * 40,
                            "headRepository": {"name": "repo"},
                            "headRepositoryOwner": {"login": "example"},
                        }
                    ]
                ),
                "",
            )

    monkeypatch.setattr(worker, "AsyncioSubprocessRunner", Commands)
    engine = factory.kw["bind"]
    async with engine.connect() as connection:
        schema = await connection.scalar(text("select current_schema()"))
    url = engine.url.update_query_dict({"awf_search_path": schema}).render_as_string(
        hide_password=False
    )
    settings = replace(
        _settings(tmp_path, database_url=url), node_id=runtime.receiver.identity.owner
    )
    consumers = WorkspaceConsumers(runtime, Path(settings.work_dir))
    if when == "watcher":
        consumers.poll_seconds = 0.01
    built = worker.build_worker_runtime(settings, hosted=consumers)
    executor = _BlockingMonitorExecutor()
    built.worker._executor = executor
    built.worker._runtime_inspector = _RecordingRuntimeInspector(
        {
            f"awf_{workspace_id}": _live_agent_snapshot()
            for workspace_id in ("ws_cloud_stable", "ws_healthy")
        }
    )
    consumers.stop_workspace = AsyncMock()
    if stop_failure:
        error = MaterialDeniedError if stop_failure == "material" else OSError
        consumers.stop_workspace.side_effect = error("synthetic stop failure")
    if when == "before_poll":
        await deny(runtime, value, failure, monkeypatch)
    elif when == "before_recovery":
        recover = built.worker._maybe_recover_stale_active_executions

        async def deny_before_recovery():
            await deny(runtime, value, failure, monkeypatch)
            await recover()

        monkeypatch.setattr(
            built.worker, "_maybe_recover_stale_active_executions", deny_before_recovery
        )
    try:
        if stop_failure:
            with pytest.raises(error, match="synthetic stop failure"):
                await built.worker.run_once()
            assert not executor.started.is_set()
            assert not denied.exists() and healthy.is_dir()
            assert consumers.workspace_id.get() is None
            return
        assert await built.worker.run_once() == 1
        await asyncio.wait_for(executor.started.wait(), 5)
        assert executor.resume_calls == ["ws_healthy"]
        assert calls == (
            ["ws_cloud_stable", "ws_healthy"]
            if when in {"watcher", "final_check"}
            else ["ws_healthy"]
        )
        assert not denied.exists() and healthy.is_dir()
        assert {call.args[0] for call in consumers.stop_workspace.await_args_list} == {
            "ws_cloud_stable"
        }
        assert consumers.workspace_id.get() is None
        async with factory() as session:
            bad = await session.get(Workspace, "ws_cloud_stable")
            good = await session.get(Workspace, "ws_healthy")
            assert bad.status == "pushing" and bad.pr_number is None
            assert good.status == "monitoring_pr" and good.pr_number == 270
            version = bad.version
            events = await session.scalar(
                select(func.count())
                .select_from(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == "ws_cloud_stable")
            )
        for _ in range(2):
            built.worker._next_stale_active_execution_scan_at = 0
            assert await built.worker.run_once() == 0
            async with factory() as session:
                bad = await session.get(Workspace, "ws_cloud_stable")
                assert bad.version == version and bad.pr_number is None
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(WorkspaceEvent)
                        .where(WorkspaceEvent.workspace_id == "ws_cloud_stable")
                    )
                    == events
                )
    finally:
        executor.release.set()
        await built.worker.wait_for_execution_tasks()
        await built.engine.dispose()


@pytest.mark.parametrize("history_count", [2, 130, 1300])
@pytest.mark.parametrize("failure", ["revoke", "expiry"])
@pytest.mark.parametrize("history_kind", ["inactive", "terminal"])
async def test_inactive_history_cannot_delay_live_authority_or_exceed_page_budget(
    authority, tmp_path, monkeypatch, history_count, failure, history_kind
):
    runtime, value, factory = authority
    current = await retained(runtime, value, factory, "ws_cloud_stable")
    healthy = await retained(runtime, value, factory, "ws_healthy", other=True)
    inactive = await retained(runtime, value, factory, "ws_00000", other=True)
    later_index = min(history_count - 1, 64)
    later_id = f"ws_{later_index:05}"
    later = await retained(runtime, value, factory, later_id, other=True)
    async with factory.begin() as session:
        row = await session.get(Workspace, "ws_00000")
        row.status = "destroyed" if history_kind == "inactive" else "failed"
        for index in range(1, history_count):
            workspace_id = f"ws_{index:05}"
            if workspace_id == later_id:
                row = await session.get(Workspace, workspace_id)
                row.status = "completed"
                row.filesystem_reclaimed_at = (
                    datetime.now(UTC) if history_kind == "inactive" else None
                )
                continue
            association = deepcopy(value["hosted"])
            association["workspace_id"] = workspace_id
            for binding in association["credentials"].values():
                binding["workspace_id"] = workspace_id
            session.add(
                Workspace(
                    id=workspace_id,
                    status=("destroyed" if history_kind == "inactive" else "failed")
                    if index % 2
                    else "completed",
                    filesystem_reclaimed_at=datetime.now(UTC)
                    if history_kind == "inactive" and not index % 2
                    else None,
                    repo_url="https://github.com/example/repo.git",
                    branch_base="main",
                    task_title="Synthetic",
                    task_prompt="Synthetic",
                    agent="codex",
                    node_id=runtime.receiver.identity.owner,
                    task_policy={"hosted_node": association},
                )
            )
    if history_kind == "terminal":
        for workspace_id in ("ws_00000", later_id):
            association, _ = await runtime.association(workspace_id)
            await runtime.receiver.revoke(
                association.credentials.agent.model_copy(
                    update={"idempotency_key": f"historical-revoke-{workspace_id}", "fence": 2}
                )
            )
    await deny(runtime, value, failure, monkeypatch)
    scope = WorkspaceConsumers(runtime, tmp_path)
    scope.stop_workspace = AsyncMock()
    reconcile = runtime.reconcile_workspace
    checked = []

    async def observe(workspace_id):
        checked.append(workspace_id)
        await reconcile(workspace_id)

    monkeypatch.setattr(runtime, "reconcile_workspace", observe)
    assert await scope.admission_check()
    assert not current.exists(), "inactive history must not delay the live revocation page"
    assert not inactive.exists(), "inactive views still need bounded cleanup"
    assert healthy.is_dir()
    stopped = {call.args[0] for call in scope.stop_workspace.await_args_list}
    assert "ws_cloud_stable" in stopped and "ws_healthy" not in stopped
    if history_kind == "inactive":
        assert stopped == {"ws_cloud_stable"}
    assert len(checked) <= 64
    assert {"ws_cloud_stable", "ws_healthy", "ws_00000"} <= set(checked)
    history_seen = set(checked) - {"ws_cloud_stable", "ws_healthy"}
    assert later.exists() is (history_count > 64)
    for _ in range(2):
        checked.clear()
        assert await scope.admission_check()
        assert len(checked) <= 64
        assert {"ws_cloud_stable", "ws_healthy"} <= set(checked)
        historical = set(checked) - {"ws_cloud_stable", "ws_healthy"}
        if history_count > 64:
            assert len(historical) == 32 and historical.isdisjoint(history_seen)
        else:
            assert historical == history_seen
        history_seen.update(historical)
        assert not current.exists() and healthy.is_dir()
    assert not later.exists(), "live cursor wrap must not restart historical cleanup"


@pytest.mark.parametrize("when", ["watcher", "final_check"])
async def test_scope_infrastructure_failure_is_not_classified_as_authority_denial(
    authority, tmp_path, monkeypatch, when
):
    runtime, value, factory = authority
    path = await retained(runtime, value, factory, "ws_cloud_stable")
    consumers = WorkspaceConsumers(runtime, tmp_path)
    consumers.poll_seconds = 0.01
    consumers.stop_workspace = AsyncMock()
    workspace = runtime.workspace
    broken = False

    async def unavailable(workspace_id):
        if broken:
            raise OSError("synthetic authority transport failure")
        return await workspace(workspace_id)

    monkeypatch.setattr(runtime, "workspace", unavailable)
    with pytest.raises(OSError, match="synthetic authority transport failure"):
        async with consumers.scope("ws_cloud_stable"):
            broken = True
            if when == "watcher":
                await asyncio.wait_for(asyncio.Event().wait(), 5)
    consumers.stop_workspace.assert_awaited_once_with("ws_cloud_stable")
    assert not path.exists()
    assert consumers.workspace_id.get() is None
