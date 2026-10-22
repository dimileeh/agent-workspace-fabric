"""Durable GC completion, bounded progress, and stale-plan regressions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.common.config import DEFAULT_WORKSPACE_CLEANUP_BATCH_LIMIT
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service import gc
from awf.service.gc_worktrees import WorkspaceGCWorktreeRemoveTargetResult

NOW = datetime(2026, 10, 22, tzinfo=UTC)
OLD = NOW - timedelta(days=60)


@pytest.fixture
async def factory(engine: AsyncEngine):
    return make_session_factory(engine)


@pytest.fixture(autouse=True)
def worktree_success(monkeypatch):
    monkeypatch.setattr(
        gc,
        "_default_worktree_remover",
        AsyncMock(
            return_value=gc.WorkspaceGCWorktreeRemoveResult(
                status="succeeded", reason_code="WORKTREE_REMOVE_SUCCEEDED"
            )
        ),
    )


async def seed(factory, *, count=1, status="completed", marked=False, age=OLD):
    ids = []
    async with factory() as session:
        for i in range(count):
            ws = await WorkspaceRepository(session).create(
                repo_url="https://github.com/example/repo.git",
                branch_base="main",
                task_title="GC backlog",
                task_prompt="task",
                agent="codex",
                test_commands=[],
            )
            ws.status = status
            ws.pr_number = 1
            ws.pr_merge_sha = "merged"
            ws.filesystem_reclaimed_at = OLD if marked else None
            ws.updated_at = age + timedelta(seconds=i)
            ids.append(ws.id)
        await session.commit()
    return ids


async def rows(factory):
    async with factory() as session:
        return {ws.id: ws for ws in (await session.scalars(select(Workspace))).all()}


async def run(factory, tmp_path, *, single=False, workspace_id=None, **kwargs):
    if single:
        return await gc.run_workspace_filesystem_gc(
            factory, work_dir=tmp_path, workspace_id=workspace_id, now=NOW, **kwargs
        )
    return await gc.run_terminal_workspace_gc(factory, work_dir=tmp_path, now=NOW, **kwargs)


async def test_default_batch_advances_past_sixty_empty_rows_within_two_runs(factory, tmp_path):
    empty = await seed(factory, count=60)
    [data_id] = await seed(factory, age=OLD + timedelta(days=1))
    data = tmp_path / "auth" / data_id / "data"
    data.parent.mkdir(parents=True)
    data.write_bytes(b"reclaim me")
    before = await rows(factory)
    limit = DEFAULT_WORKSPACE_CLEANUP_BATCH_LIMIT
    assert limit == 50
    first = await run(factory, tmp_path, execute=True, limit=limit)
    assert len(first.plan.candidates) == 50
    assert first.deleted_paths == []
    after_first = await rows(factory)
    assert sum(ws.filesystem_reclaimed_at is not None for ws in after_first.values()) == 50
    assert data.exists()
    second = await run(factory, tmp_path, execute=True, limit=limit)
    assert len(second.plan.candidates) == 11
    assert data_id in {c.workspace_id for c in second.plan.candidates}
    assert not data.exists()
    after = await rows(factory)
    for ws_id in [*empty, data_id]:
        assert after[ws_id].filesystem_reclaimed_at == NOW
        assert after[ws_id].status == before[ws_id].status
        assert after[ws_id].updated_at == before[ws_id].updated_at
    assert not (await run(factory, tmp_path, limit=limit)).plan.candidates


@pytest.mark.parametrize("single", [False, True])
async def test_dry_run_never_marks_or_changes_existing_markers(factory, tmp_path, single):
    [ws_id] = await seed(factory)
    [marked_id] = await seed(factory, marked=True)
    for _ in range(2):
        await run(factory, tmp_path, single=single, workspace_id=ws_id)
        await run(factory, tmp_path, single=single, workspace_id=marked_id)
    after = await rows(factory)
    assert after[ws_id].filesystem_reclaimed_at is None
    assert after[marked_id].filesystem_reclaimed_at == OLD
    assert after[ws_id].updated_at == OLD
    assert after[marked_id].updated_at == OLD


@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize("layout", ["absent", "deleted", "mixed"])
async def test_complete_paths_mark_and_single_workspace_skips_marked_rows(
    factory, tmp_path, single, layout
):
    [ws_id] = await seed(factory)
    kinds = {"absent": [], "deleted": ["auth", "compose", "git/worktrees"], "mixed": ["auth"]}
    for kind in kinds[layout]:
        path = tmp_path / kind / ws_id
        path.mkdir(parents=True)
        (path / "data").write_text("data")
    result = await run(factory, tmp_path, single=single, workspace_id=ws_id, execute=True)
    assert result.status == "succeeded"
    assert (await rows(factory))[ws_id].filesystem_reclaimed_at == NOW
    assert not (await run(factory, tmp_path, single=single, workspace_id=ws_id)).plan.candidates


@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize("failure", ["delete", "compose", "unmount", "worktree", "companion"])
async def test_partial_cleanup_does_not_mark_but_independent_success_does(
    factory, tmp_path, monkeypatch, single, failure
):
    [ws_id] = await seed(factory)
    [ok_id] = await seed(factory)
    if failure == "companion":
        async with factory() as session:
            ws = await session.get(Workspace, ws_id)
            ws.task_policy = {
                "companions": [{"name": "side", "repo_url": "https://github.com/example/side.git"}]
            }
            await session.flush()
            await session.execute(
                update(Workspace).where(Workspace.id == ws_id).values(updated_at=OLD)
            )
            await session.commit()
    original = gc._delete_gc_path

    def delete(target, *, work_dir):
        if target.path.name == ws_id and target.kind == "auth":
            return False, "permission denied", "PATH_DELETE_PERMISSION_DENIED"
        return original(target, work_dir=work_dir)

    async def compose(candidate):
        return gc.WorkspaceGCComposeTeardownResult(
            status="failed" if candidate.workspace_id == ws_id else "succeeded",
            reason_code="COMPOSE_FAILURE" if candidate.workspace_id == ws_id else "COMPOSE_OK",
        )

    def unmount(candidate, *, work_dir):
        return ("UNMOUNT_REFUSED", "busy") if candidate.workspace_id == ws_id else None

    async def worktree(candidate):
        if candidate.workspace_id != ws_id:
            return gc.WorkspaceGCWorktreeRemoveResult(
                status="succeeded", reason_code="WORKTREE_REMOVE_SUCCEEDED"
            )
        targets = ()
        if failure == "companion":
            assert candidate.companion_worktrees
            targets = (
                WorkspaceGCWorktreeRemoveTargetResult(
                    worktree_id=candidate.companion_worktrees[0].path.name,
                    status="failed",
                    reason_code="WORKTREE_REMOVE_FAILED",
                    error="busy",
                ),
            )
        return gc.WorkspaceGCWorktreeRemoveResult(
            status="failed",
            reason_code="WORKTREE_REMOVE_FAILED",
            error="busy",
            target_results=targets,
        )

    kwargs = {}
    if failure == "delete":
        monkeypatch.setattr(gc, "_delete_gc_path", delete)
    elif failure == "compose":
        kwargs["compose_teardown"] = compose
    elif failure == "unmount":
        monkeypatch.setattr(gc, "_unmount_candidate_auth_overlay", unmount)
    else:
        kwargs["worktree_remover"] = worktree
    result = await run(factory, tmp_path, single=single, workspace_id=ws_id, execute=True, **kwargs)
    assert result.status == "partial"
    assert result.delete_errors
    after = await rows(factory)
    assert after[ws_id].filesystem_reclaimed_at is None
    if not single:
        assert after[ok_id].filesystem_reclaimed_at == NOW


@pytest.mark.parametrize("change", ["status", "timestamp", "marker"])
async def test_obsolete_plan_cannot_mark_changed_workspace(factory, tmp_path, change):
    [ws_id] = await seed(factory, status="failed")

    async def compose(candidate):
        async with factory() as session:
            values = {"updated_at": OLD}
            if change == "status":
                repo = WorkspaceRepository(session)
                ws = await repo.get(ws_id)
                await repo.transition(
                    ws, to=WorkspaceStatus.monitoring_pr, reason_code="OPERATOR_REMONITOR"
                )
            elif change == "timestamp":
                values["updated_at"] = NOW
            else:
                values["filesystem_reclaimed_at"] = OLD
            await session.execute(update(Workspace).where(Workspace.id == ws_id).values(**values))
            await session.commit()
        return gc.WorkspaceGCComposeTeardownResult(status="succeeded", reason_code="COMPOSE_OK")

    await run(
        factory, tmp_path, execute=True, compose_teardown=compose, include_statuses=["failed"]
    )
    assert (await rows(factory))[ws_id].filesystem_reclaimed_at == (
        OLD if change == "marker" else None
    )


@pytest.mark.parametrize(
    "policy",
    [
        "default",
        "explicit",
        "recent",
        "recent_explicit",
        "disabled_default",
        "disabled_explicit",
        "failed_no_work",
        "age_cap",
    ],
)
async def test_all_predicates_exclude_marked_rows_before_limits(
    factory, tmp_path, monkeypatch, policy
):
    status = "failed" if policy in {"failed_no_work", "age_cap"} else "completed"
    age = NOW - timedelta(hours=1) if policy in {"recent", "recent_explicit"} else OLD
    marked = await seed(factory, count=2, status=status, marked=True, age=age)
    [live_id] = await seed(factory, status=status, age=age + timedelta(seconds=10))
    monkeypatch.setattr(
        gc, "_failed_terminal_workspace_has_no_work", lambda _: policy == "failed_no_work"
    )
    kwargs = {"limit": 1}
    if policy in {"explicit", "disabled_explicit", "recent_explicit"}:
        kwargs["include_statuses"] = [status]
    if policy.startswith("disabled"):
        kwargs["cleanup_enabled"] = False
    plan = (await run(factory, tmp_path, **kwargs)).plan
    selected = {c.workspace_id for c in [*plan.candidates, *plan.preserved]}
    assert selected == {live_id}
    assert selected.isdisjoint(marked)


@pytest.mark.parametrize("status", ["cancelled", "destroyed", "superseded"])
async def test_explicit_terminal_statuses_exclude_reclaimed_before_limit(factory, tmp_path, status):
    await seed(factory, count=2, status=status, marked=True)
    [ws_id] = await seed(factory, status=status, age=OLD + timedelta(hours=1))
    result = await run(factory, tmp_path, include_statuses=[status], limit=1, execute=True)
    assert [c.workspace_id for c in result.plan.candidates] == [ws_id]
    assert (await rows(factory))[ws_id].filesystem_reclaimed_at == NOW


async def test_shared_base_failure_does_not_prevent_workspace_completion(factory, tmp_path):
    [ws_id] = await seed(factory)

    async def reap(_):
        return {"status": "partial", "reason_code": "BASE_FAILED", "errors": ["busy"]}

    result = await run(factory, tmp_path, execute=True, claude_base_reap=reap)
    assert result.status == "partial"
    assert (await rows(factory))[ws_id].filesystem_reclaimed_at == NOW
