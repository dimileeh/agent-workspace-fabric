"""Terminal workspace filesystem GC tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import awf.service.gc as gc
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    SecretLeaseIssue,
    SecretLeaseRepository,
    WorkspaceEventRepository,
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from awf.service.gc import (
    COMPLETED_PR_RETENTION_EXPIRED,
    FAILED_WORKSPACE_NO_WORK,
    FAILED_WORKSPACE_TRIAGE_PRESERVED,
    WORKSPACE_WITHIN_RETENTION,
    WorkspaceGCComposeTeardownResult,
    WorkspaceGCPath,
    _delete_gc_path,
    _estimate_bytes,
    plan_terminal_workspace_gc,
    run_terminal_workspace_gc,
    run_workspace_filesystem_gc,
)


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


async def _workspace(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: WorkspaceStatus,
    updated_at: datetime,
    title: str = "gc candidate",
    compose_file_path: str | None = None,
    pr: bool = False,
    pr_merge_sha: str | None = None,
) -> str:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/repo.git",
            branch_base="development",
            task_title=title,
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace.status = status.value
        workspace.updated_at = updated_at
        workspace.compose_file_path = compose_file_path
        if pr:
            workspace.pr_url = "https://github.com/example/repo/pull/123"
            workspace.pr_number = 123
            workspace.pr_merge_sha = pr_merge_sha
        await session.commit()
        return workspace.id


async def _issue_gc_secret_lease(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    now: datetime,
) -> None:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        await SecretLeaseRepository(session).issue_declared_leases(
            workspace,
            leases=[
                SecretLeaseIssue(
                    secret_name="api-token",
                    kind="env",
                    target="API_TOKEN",
                    mode="ro",
                    required=True,
                    provider="env",
                    ref_digest="sha256:" + "e" * 64,
                    expires_at=now + timedelta(hours=1),
                    issue_metadata={"profile": "gc", "declaration_index": 0},
                )
            ],
            now=now,
        )
        await session.commit()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class _StaticRuntimeInspector:
    def __init__(self, snapshot: RuntimeSnapshot) -> None:
        self.snapshot = snapshot

    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
        assert compose_project_name is not None
        return self.snapshot


@pytest.mark.unit
def test_default_gc_predicates_handle_status_subsets() -> None:
    cutoff = datetime(2026, 4, 26, 12, tzinfo=UTC)

    assert (
        gc._workspace_gc_candidate_predicate(
            eligible_statuses={WorkspaceStatus.failed.value},
            cutoff_at=cutoff,
            default_policy=True,
            cleanup_enabled=True,
        )
        is None
    )
    assert (
        gc._workspace_gc_preserved_predicate(
            eligible_statuses=set(),
            cutoff_at=cutoff,
            default_policy=True,
            cleanup_enabled=True,
        )
        is None
    )
    assert (
        gc._workspace_gc_preserved_predicate(
            eligible_statuses={WorkspaceStatus.completed.value},
            cutoff_at=cutoff,
            default_policy=True,
            cleanup_enabled=True,
        )
        is not None
    )
    assert (
        gc._workspace_gc_preserved_predicate(
            eligible_statuses={WorkspaceStatus.failed.value},
            cutoff_at=cutoff,
            default_policy=True,
            cleanup_enabled=True,
        )
        is not None
    )


@pytest.mark.unit
async def test_plan_selects_completed_pr_workspace_after_retention(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        compose_file_path=str(work_dir / "compose" / "stored-compose-id" / "compose.yml"),
        pr=True,
        pr_merge_sha="b" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    compose = work_dir / "compose" / "stored-compose-id"
    auth = work_dir / "auth" / workspace_id
    log_file = work_dir / "logs" / workspace_id / "agent.log"
    artifact_file = work_dir / "artifacts" / workspace_id / "summary.json"
    _write(worktree / "repo.txt", "1234567")
    _write(compose / "compose.yml", "12345")
    _write(auth / "codex" / "auth.json", "123456789")
    _write(log_file, "keep logs")
    _write(artifact_file, "{}")

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        now=now,
    )

    assert plan.total_estimated_bytes == 21
    assert [candidate.workspace_id for candidate in plan.candidates] == [workspace_id]
    assert plan.preserved == []
    candidate = plan.candidates[0]
    assert candidate.status == WorkspaceStatus.completed.value
    assert candidate.reason_code == COMPLETED_PR_RETENTION_EXPIRED
    assert candidate.age_hours == 200
    assert candidate.worktree.path == worktree
    assert candidate.worktree.exists is True
    assert candidate.worktree.estimated_bytes == 7
    assert candidate.compose.path == compose
    assert candidate.compose.exists is True
    assert candidate.compose.estimated_bytes == 5
    assert candidate.auth.path == auth
    assert candidate.auth.exists is True
    assert candidate.auth.estimated_bytes == 9
    assert candidate.total_estimated_bytes == 21
    payload = plan.to_dict()
    assert payload["policy"]["retention_hours"] == 24
    rendered_targets = json.dumps(payload["candidates"])
    assert str(log_file) not in rendered_targets
    assert str(artifact_file) not in rendered_targets


@pytest.mark.unit
async def test_plan_excludes_active_and_destroying_workspaces(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    ineligible_statuses = [
        WorkspaceStatus.requested,
        WorkspaceStatus.provisioning,
        WorkspaceStatus.ready,
        WorkspaceStatus.running,
        WorkspaceStatus.validating,
        WorkspaceStatus.pushing,
        WorkspaceStatus.monitoring_pr,
        WorkspaceStatus.destroying,
    ]
    for status in ineligible_statuses:
        await _workspace(
            session_factory,
            status=status,
            updated_at=now - timedelta(days=30),
            title=f"{status.value} workspace",
        )

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        min_age_hours=1,
        now=now,
    )

    assert plan.candidates == []


@pytest.mark.unit
async def test_plan_reports_requested_statuses_when_no_statuses_are_eligible(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        include_statuses=[WorkspaceStatus.running],
        exclude_statuses=[WorkspaceStatus.completed],
        now=now,
    )

    assert plan.candidates == []
    assert plan.to_dict()["include_statuses"] == [WorkspaceStatus.running.value]
    assert plan.to_dict()["exclude_statuses"] == [WorkspaceStatus.completed.value]


@pytest.mark.unit
async def test_gc_secret_lease_revocation_skips_missing_workspace(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 4, 29, 12, tzinfo=UTC)

    summaries = await gc._revoke_gc_secret_leases(
        session_factory,
        workspace_ids=["ws_missing"],
        now=now,
    )

    assert summaries == {}


@pytest.mark.unit
async def test_plan_applies_min_age_filter_and_limit_oldest_first(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    oldest = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=300),
        title="oldest",
        pr=True,
    )
    await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=100),
        title="middle",
        pr=True,
    )
    await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=2),
        title="fresh",
        pr=True,
    )

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        min_age_hours=24,
        limit=1,
        now=now,
    )

    assert [candidate.workspace_id for candidate in plan.candidates] == [oldest]
    assert plan.preserved_count == 1

    older_than_120h = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        min_age_hours=120,
        now=now,
    )
    assert [candidate.workspace_id for candidate in older_than_120h.candidates] == [oldest]


@pytest.mark.unit
async def test_plan_applies_limit_to_workspace_queries(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    for index in range(3):
        await _workspace(
            session_factory,
            status=WorkspaceStatus.completed,
            updated_at=now - timedelta(hours=300 - index),
            title=f"candidate {index}",
            pr=True,
        )

    workspace_selects: list[str] = []

    def _capture_workspace_select(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = statement.upper()
        if normalized.lstrip().startswith("SELECT") and "FROM WORKSPACES" in normalized:
            workspace_selects.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture_workspace_select)
    try:
        plan = await plan_terminal_workspace_gc(
            session_factory,
            work_dir=tmp_path / "service",
            min_age_hours=24,
            limit=1,
            now=now,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture_workspace_select)

    assert len(plan.candidates) == 1
    assert workspace_selects
    assert all("LIMIT" in statement.upper() for statement in workspace_selects)


@pytest.mark.unit
async def test_plan_classifies_workspace_paths_in_threads(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    candidate_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=48),
        pr=True,
    )
    preserved_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=48),
    )
    _write(work_dir / "git" / "worktrees" / candidate_id / "repo.txt", "repo")

    to_thread_calls: list[str] = []

    async def _record_to_thread(
        func: Callable[..., object],
        /,
        *args: object,
        **kwargs: object,
    ) -> object:
        to_thread_calls.append(func.__name__)
        return func(*args, **kwargs)

    monkeypatch.setattr(gc.asyncio, "to_thread", _record_to_thread)

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        now=now,
    )

    assert [candidate.workspace_id for candidate in plan.candidates] == [candidate_id]
    assert [item.workspace_id for item in plan.preserved] == [preserved_id]
    assert to_thread_calls == [
        "_classify_workspace_for_gc",
        "_classify_workspace_for_gc",
    ]


@pytest.mark.unit
async def test_plan_tolerates_missing_workspace_paths(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=48),
        pr=True,
    )

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        now=now,
    )

    candidate = plan.candidates[0]
    assert candidate.workspace_id == workspace_id
    assert candidate.total_estimated_bytes == 0
    assert candidate.worktree.path == work_dir / "git" / "worktrees" / workspace_id
    assert candidate.compose.path == work_dir / "compose" / workspace_id
    assert candidate.auth.path == work_dir / "auth" / workspace_id
    assert candidate.worktree.exists is False
    assert candidate.compose.exists is False
    assert candidate.auth.exists is False


@pytest.mark.unit
async def test_run_defaults_to_dry_run_and_keeps_candidate_directories(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=48),
        pr=True,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    compose = work_dir / "compose" / workspace_id
    auth = work_dir / "auth" / workspace_id
    _write(worktree / "repo.txt", "repo")
    _write(compose / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        now=now,
    )

    assert result.dry_run is True
    assert result.deleted_paths == []
    assert worktree.exists()
    assert compose.exists()
    assert auth.exists()


@pytest.mark.unit
async def test_execute_deletes_only_workspace_pressure_dirs_and_preserves_db_and_logs(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=48),
        pr=True,
        pr_merge_sha="c" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    compose = work_dir / "compose" / workspace_id
    auth = work_dir / "auth" / workspace_id
    log_file = work_dir / "logs" / workspace_id / "agent.log"
    artifact_file = work_dir / "artifacts" / workspace_id / "report.json"
    _write(worktree / "repo.txt", "repo")
    _write(compose / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")
    _write(log_file, "agent log")
    _write(artifact_file, '{"ok": true}')
    async with session_factory() as session:
        await WorkspaceLogStreamRepository(session).create_or_get(
            workspace_id=workspace_id,
            stream_id="agent/stdout",
            source="agent",
            name="stdout",
            kind="stdout",
            path=str(log_file),
        )
        await session.commit()

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
    )

    assert result.dry_run is False
    assert result.status == "succeeded"
    assert result.reason_code == "CLEANUP_EXECUTION_SUCCEEDED"
    assert set(result.deleted_paths) == {worktree, compose, auth}
    assert not worktree.exists()
    assert not compose.exists()
    assert not auth.exists()
    assert log_file.exists()
    assert artifact_file.exists()
    payload = result.to_dict()
    path_statuses = {
        kind: data["status"] for kind, data in payload["candidates"][0]["paths"].items()
    }
    assert path_statuses == {
        "worktree": "deleted",
        "compose": "deleted",
        "auth": "deleted",
    }

    async with session_factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.completed.value
        assert len(workspace.events) == 1
        streams = await WorkspaceLogStreamRepository(session).list_for_workspace(workspace_id)
        assert [stream.path for stream in streams] == [str(log_file)]
        events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id)
        assert [event.reason_code for event in events] == ["CREATED"]
        assert (await session.execute(select(Workspace.id))).scalars().all() == [workspace_id]


@pytest.mark.unit
async def test_recent_completed_pr_workspace_is_preserved(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=2),
        pr=True,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    compose = work_dir / "compose" / workspace_id
    auth = work_dir / "auth" / workspace_id
    log_file = work_dir / "logs" / workspace_id / "agent.log"
    artifact_file = work_dir / "artifacts" / workspace_id / "report.json"
    _write(worktree / "repo.txt", "repo")
    _write(compose / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")
    _write(log_file, "keep logs")
    _write(artifact_file, "{}")

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
    )

    assert result.plan.candidates == []
    assert result.plan.preserved_count == 1
    assert result.plan.preserved[0].workspace_id == workspace_id
    assert result.plan.preserved[0].reason_code == WORKSPACE_WITHIN_RETENTION
    assert result.deleted_paths == []
    assert worktree.exists()
    assert compose.exists()
    assert auth.exists()
    assert log_file.exists()
    assert artifact_file.exists()


@pytest.mark.unit
async def test_failed_workspace_preserves_triage_assets_by_default(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=200),
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    compose = work_dir / "compose" / workspace_id
    auth = work_dir / "auth" / workspace_id
    log_file = work_dir / "logs" / workspace_id / "agent.log"
    artifact_file = work_dir / "artifacts" / workspace_id / "failure.json"
    _write(worktree / "repo.txt", "repo")
    _write(compose / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")
    _write(log_file, "failure log")
    _write(artifact_file, "{}")

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
    )

    assert result.plan.candidates == []
    assert result.plan.preserved_count == 1
    assert result.plan.preserved[0].workspace_id == workspace_id
    assert result.plan.preserved[0].reason_code == FAILED_WORKSPACE_TRIAGE_PRESERVED
    assert result.deleted_paths == []
    assert worktree.exists()
    assert compose.exists()
    assert auth.exists()
    assert log_file.exists()
    assert artifact_file.exists()


@pytest.mark.unit
async def test_default_plan_includes_superseded_no_work_candidate(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=200),
    )
    async with session_factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        workspace.status = "superseded"
        workspace.compose_project_name = "awf_superseded_gc"
        await session.commit()

    monkeypatch.setattr(
        gc,
        "_RUNTIME_INSPECTOR",
        _StaticRuntimeInspector(
            RuntimeSnapshot(
                stack_state="stopped",
                services=[
                    RuntimeService(
                        name="agent",
                        container_id="agent",
                        image="awf-agent",
                        state="running",
                        command="sleep infinity",
                    )
                ],
            )
        ),
    )

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        min_age_hours=24,
        now=now,
    )

    assert [candidate.workspace_id for candidate in plan.candidates] == [workspace_id]
    assert plan.candidates[0].status == "superseded"
    assert plan.candidates[0].reason_code == FAILED_WORKSPACE_NO_WORK


@pytest.mark.unit
async def test_single_workspace_filesystem_gc_keeps_superseded_no_work_on_dry_run(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=200),
    )
    async with session_factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        workspace.status = "superseded"
        workspace.compose_project_name = "awf_single_superseded_gc"
        await session.commit()
    _write(tmp_path / "service" / "git" / "worktrees" / workspace_id / "repo.txt", "repo")

    monkeypatch.setattr(
        gc,
        "_RUNTIME_INSPECTOR",
        _StaticRuntimeInspector(
            RuntimeSnapshot(
                stack_state="stopped",
                services=[
                    RuntimeService(
                        name="agent",
                        container_id="agent",
                        image="awf-agent",
                        state="running",
                        command="sleep infinity",
                    )
                ],
            )
        ),
    )

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=tmp_path / "service",
        workspace_id=workspace_id,
        now=now,
    )

    assert [candidate.workspace_id for candidate in result.plan.candidates] == [
        workspace_id,
    ]
    assert result.plan.candidates[0].reason_code == FAILED_WORKSPACE_NO_WORK


@pytest.mark.unit
async def test_cleanup_disabled_preserves_completed_pr_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
    )

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        min_age_hours=24,
        cleanup_enabled=False,
        now=now,
    )

    assert plan.candidates == []
    assert plan.preserved[0].workspace_id == workspace_id
    assert plan.preserved[0].reason_code == "WORKSPACE_CLEANUP_DISABLED"
    assert plan.to_dict()["policy"]["cleanup_enabled"] is False


@pytest.mark.unit
async def test_completed_workspace_without_pr_metadata_is_preserved(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
    )

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        min_age_hours=24,
        now=now,
    )

    assert plan.candidates == []
    assert plan.preserved[0].workspace_id == workspace_id
    assert plan.preserved[0].reason_code == "COMPLETED_WORKSPACE_WITHOUT_PR"


@pytest.mark.unit
async def test_explicit_status_filter_can_select_old_terminal_non_pr_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    old_failed = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=200),
    )
    recent_failed = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=2),
        title="recent failed",
    )

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        min_age_hours=24,
        include_statuses=[WorkspaceStatus.failed],
        now=now,
    )

    assert [candidate.workspace_id for candidate in plan.candidates] == [old_failed]
    assert plan.candidates[0].reason_code == FAILED_WORKSPACE_NO_WORK
    assert plan.preserved[0].workspace_id == recent_failed
    assert plan.preserved[0].reason_code == WORKSPACE_WITHIN_RETENTION
    assert plan.to_dict()["policy"] == {
        "cleanup_enabled": True,
        "retention_hours": 24,
        "eligible_statuses": [WorkspaceStatus.failed.value],
        "requires_pr_metadata": False,
        "preserves_failed_workspaces": False,
    }


@pytest.mark.unit
async def test_single_workspace_gc_deletes_only_requested_completed_workspace_after_retention(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    target_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
    )
    other_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now,
        title="other completed workspace",
        pr=True,
    )
    target_worktree = work_dir / "git" / "worktrees" / target_id
    target_auth = work_dir / "auth" / target_id
    other_worktree = work_dir / "git" / "worktrees" / other_id
    _write(target_worktree / "repo.txt", "repo")
    _write(target_auth / "codex" / "auth.json", "auth")
    _write(other_worktree / "repo.txt", "other")

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=target_id,
        execute=True,
        min_age_hours=24,
        now=now,
    )

    assert result.dry_run is False
    assert result.plan.candidates[0].workspace_id == target_id
    assert not target_worktree.exists()
    assert not target_auth.exists()
    assert other_worktree.exists()

    async with session_factory() as session:
        workspace = await session.get(Workspace, target_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.completed.value


@pytest.mark.unit
async def test_single_workspace_gc_revokes_active_secret_leases_before_auth_cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
    )
    await _issue_gc_secret_lease(session_factory, workspace_id, now=now)
    auth = work_dir / "auth" / workspace_id
    _write(auth / "codex" / "auth.json", "auth")

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        min_age_hours=24,
        now=now,
    )

    assert not auth.exists()
    assert result.to_dict()["secret_leases"] == {
        workspace_id: {"revoked_count": 1, "reason_code": "TERMINAL_GC"}
    }
    async with session_factory() as session:
        leases = await SecretLeaseRepository(session).list_for_workspace(workspace_id)
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.secret_lease",
            limit=10,
        )

    assert leases[0].status == "revoked"
    assert leases[0].revoke_reason_code == "TERMINAL_GC"
    assert {event.reason_code for event in events} == {
        "SECRET_LEASE_ISSUED",
        "SECRET_LEASE_REVOKED",
    }


@pytest.mark.unit
async def test_batch_terminal_gc_revokes_each_candidate_and_is_retry_safe(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    first_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
    )
    second_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=210),
        pr=True,
    )
    await _issue_gc_secret_lease(session_factory, first_id, now=now)
    await _issue_gc_secret_lease(session_factory, second_id, now=now)
    first_auth = work_dir / "auth" / first_id
    second_auth = work_dir / "auth" / second_id
    _write(first_auth / "codex" / "auth.json", "auth")
    _write(second_auth / "codex" / "auth.json", "auth")
    first_worktree = work_dir / "git" / "worktrees" / first_id
    first_worktree.parent.mkdir(parents=True, exist_ok=True)
    first_worktree.write_text("not a directory", encoding="utf-8")

    first = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        execute=True,
        min_age_hours=24,
        now=now,
    )
    second = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        execute=True,
        min_age_hours=24,
        now=now,
    )

    assert first.status == "partial"
    assert first.to_dict()["secret_leases"] == {
        first_id: {"revoked_count": 1, "reason_code": "TERMINAL_GC"},
        second_id: {"revoked_count": 1, "reason_code": "TERMINAL_GC"},
    }
    assert second.to_dict()["secret_leases"] == {
        first_id: {"revoked_count": 0, "reason_code": "TERMINAL_GC"},
        second_id: {"revoked_count": 0, "reason_code": "TERMINAL_GC"},
    }
    async with session_factory() as session:
        first_leases = await SecretLeaseRepository(session).list_for_workspace(first_id)
        second_leases = await SecretLeaseRepository(session).list_for_workspace(second_id)

    assert first_leases[0].status == "revoked"
    assert second_leases[0].status == "revoked"


@pytest.mark.unit
async def test_single_workspace_gc_preserves_logs_and_artifacts_after_retention(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="d" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    compose = work_dir / "compose" / workspace_id
    auth = work_dir / "auth" / workspace_id
    log_file = work_dir / "logs" / workspace_id / "agent.log"
    artifact_file = work_dir / "artifacts" / workspace_id / "summary.json"
    _write(worktree / "repo.txt", "repo")
    _write(compose / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")
    _write(log_file, "durable log")
    _write(artifact_file, '{"status": "kept"}')

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        min_age_hours=24,
        now=now,
    )

    assert result.status == "succeeded"
    assert set(result.deleted_paths) == {worktree, compose, auth}
    assert not worktree.exists()
    assert not compose.exists()
    assert not auth.exists()
    assert log_file.exists()
    assert artifact_file.exists()


@pytest.mark.unit
async def test_execute_gc_deletes_workspace_paths_in_threads(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
    )
    _write(work_dir / "git" / "worktrees" / workspace_id / "repo.txt", "repo")
    _write(work_dir / "compose" / workspace_id / "compose.yml", "compose")
    _write(work_dir / "auth" / workspace_id / "codex" / "auth.json", "auth")
    to_thread_calls: list[str] = []

    async def _record_to_thread(
        func: Callable[..., object],
        /,
        *args: object,
        **kwargs: object,
    ) -> object:
        to_thread_calls.append(func.__name__)
        return func(*args, **kwargs)

    monkeypatch.setattr(gc.asyncio, "to_thread", _record_to_thread)

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
    )

    assert result.status == "succeeded"
    assert [outcome.kind for outcome in result.path_outcomes] == [
        "worktree",
        "compose",
        "auth",
    ]
    assert to_thread_calls == [
        "_classify_workspace_for_gc",
        "_delete_gc_path_outcome",
        "_delete_gc_path_outcome",
        "_delete_gc_path_outcome",
    ]


@pytest.mark.unit
async def test_cleanup_is_idempotent_after_partial_compose_failure(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    compose_slug = "stored"
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        compose_file_path=str(work_dir / "compose" / compose_slug / "compose.yml"),
        pr=True,
    )
    compose = work_dir / "compose" / compose_slug
    worktree = work_dir / "git" / "worktrees" / workspace_id
    auth = work_dir / "auth" / workspace_id
    _write(worktree / "repo.txt", "repo")
    _write(compose / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")
    calls = 0

    async def _compose_teardown(_candidate: object) -> WorkspaceGCComposeTeardownResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return WorkspaceGCComposeTeardownResult(
                status="failed",
                reason_code="DOCKER_COMPOSE_DOWN_FAILED",
                error="network still in use",
            )
        return WorkspaceGCComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )

    first = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
        compose_teardown=_compose_teardown,
    )
    first_payload = first.to_dict()

    assert first.status == "partial"
    assert first.reason_code == "CLEANUP_EXECUTION_PARTIAL"
    assert first_payload["candidates"][0]["compose_teardown"]["reason_code"] == (
        "DOCKER_COMPOSE_DOWN_FAILED"
    )
    assert {
        data["status"] for data in first_payload["candidates"][0]["paths"].values()
    } == {"skipped"}
    assert {
        data["reason_code"] for data in first_payload["candidates"][0]["paths"].values()
    } == {"DOCKER_COMPOSE_DOWN_FAILED"}
    assert worktree.exists()
    assert compose.exists()
    assert auth.exists()

    second = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
        compose_teardown=_compose_teardown,
    )

    assert second.status == "succeeded"
    assert set(second.deleted_paths) == {worktree, compose, auth}
    assert not worktree.exists()
    assert not compose.exists()
    assert not auth.exists()

    third = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
        compose_teardown=_compose_teardown,
    )
    third_payload = third.to_dict()

    assert third.status == "succeeded"
    assert third.deleted_paths == []
    assert third.delete_errors == []
    assert {
        data["status"] for data in third_payload["candidates"][0]["paths"].values()
    } == {"already_removed"}
    assert third.path_outcomes[0].to_dict()["reason_code"] == "PATH_ALREADY_REMOVED"


@pytest.mark.unit
async def test_single_workspace_cleanup_is_idempotent_after_partial_compose_failure(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    compose_slug = "stored-single"
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        compose_file_path=str(work_dir / "compose" / compose_slug / "compose.yml"),
        pr=True,
    )
    compose = work_dir / "compose" / compose_slug
    worktree = work_dir / "git" / "worktrees" / workspace_id
    auth = work_dir / "auth" / workspace_id
    _write(worktree / "repo.txt", "repo")
    _write(compose / "compose.yml", "compose")
    _write(auth / "codex" / "auth.json", "auth")
    calls = 0

    async def _compose_teardown(_candidate: object) -> WorkspaceGCComposeTeardownResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return WorkspaceGCComposeTeardownResult(
                status="failed",
                reason_code="DOCKER_COMPOSE_DOWN_FAILED",
                error="network still in use",
            )
        return WorkspaceGCComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )

    first = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        min_age_hours=24,
        execute=True,
        now=now,
        compose_teardown=_compose_teardown,
    )
    first_payload = first.to_dict()
    first_candidate = first_payload["candidates"][0]

    assert first.status == "partial"
    assert first.reason_code == "CLEANUP_EXECUTION_PARTIAL"
    assert first_candidate["compose_teardown"] == {
        "status": "failed",
        "reason_code": "DOCKER_COMPOSE_DOWN_FAILED",
        "error": "network still in use",
    }
    assert {
        data["status"] for data in first_candidate["paths"].values()
    } == {"skipped"}
    assert {
        data["reason_code"] for data in first_candidate["paths"].values()
    } == {"DOCKER_COMPOSE_DOWN_FAILED"}
    assert {
        data["error"] for data in first_candidate["paths"].values()
    } == {"network still in use"}
    assert worktree.exists()
    assert compose.exists()
    assert auth.exists()

    second = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        min_age_hours=24,
        execute=True,
        now=now,
        compose_teardown=_compose_teardown,
    )

    assert second.status == "succeeded"
    assert set(second.deleted_paths) == {worktree, compose, auth}
    assert not worktree.exists()
    assert not compose.exists()
    assert not auth.exists()

    third = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        min_age_hours=24,
        execute=True,
        now=now,
        compose_teardown=_compose_teardown,
    )
    third_payload = third.to_dict()
    third_candidate = third_payload["candidates"][0]

    assert third.status == "succeeded"
    assert third.deleted_paths == []
    assert third.delete_errors == []
    assert {
        data["status"] for data in third_candidate["paths"].values()
    } == {"already_removed"}
    assert {
        data["reason_code"] for data in third_candidate["paths"].values()
    } == {"PATH_ALREADY_REMOVED"}
    assert all("error" not in data for data in third_candidate["paths"].values())


@pytest.mark.unit
async def test_gc_accepts_sync_compose_teardown_result(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")

    def _compose_teardown(_candidate: object) -> WorkspaceGCComposeTeardownResult:
        return WorkspaceGCComposeTeardownResult(
            status="skipped",
            reason_code="NO_COMPOSE_STACK",
        )

    result = await run_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        execute=True,
        now=now,
        compose_teardown=_compose_teardown,
    )

    assert result.status == "succeeded"
    assert result.to_dict()["candidates"][0]["compose_teardown"] == {
        "status": "skipped",
        "reason_code": "NO_COMPOSE_STACK",
    }
    assert not worktree.exists()


@pytest.mark.unit
async def test_default_gc_policy_ignores_non_pr_terminal_and_unknown_statuses(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    cancelled_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.cancelled,
        updated_at=now - timedelta(hours=200),
    )
    unknown_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        title="future status",
        pr=True,
    )
    async with session_factory() as session:
        unknown = await session.get(Workspace, unknown_id)
        assert unknown is not None
        unknown.status = "future_status"
        await session.commit()
    _write(work_dir / "git" / "worktrees" / cancelled_id / "repo.txt", "repo")
    _write(work_dir / "git" / "worktrees" / unknown_id / "repo.txt", "repo")

    batch = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=work_dir,
        min_age_hours=24,
        now=now,
    )
    single_unknown = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=unknown_id,
        execute=True,
        min_age_hours=24,
        now=now,
    )
    single_cancelled = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=cancelled_id,
        execute=True,
        min_age_hours=24,
        now=now,
    )

    assert batch.candidates == []
    assert batch.preserved == []
    assert single_unknown.plan.candidates == []
    assert single_unknown.deleted_paths == []
    assert single_cancelled.plan.candidates == []
    assert single_cancelled.deleted_paths == []
    assert (work_dir / "git" / "worktrees" / cancelled_id).exists()
    assert (work_dir / "git" / "worktrees" / unknown_id).exists()


@pytest.mark.unit
async def test_single_workspace_gc_ignores_active_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        now=now,
    )

    assert result.plan.candidates == []
    assert result.deleted_paths == []
    assert worktree.exists()


@pytest.mark.unit
async def test_single_workspace_gc_dry_run_for_missing_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=tmp_path / "service",
        workspace_id="ws_missing",
        execute=False,
        now=now,
    )

    assert result.dry_run is True
    assert result.plan.include_statuses == ()
    assert result.plan.candidates == []
    assert result.to_dict()["candidate_count"] == 0


@pytest.mark.unit
async def test_gc_execution_reports_refused_file_symlink_and_out_of_root_paths(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    outside_dir = tmp_path / "outside-compose"
    outside_dir.mkdir()
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        compose_file_path=str(outside_dir / "compose.yml"),
        pr=True,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    auth = work_dir / "auth" / workspace_id
    symlink_target = work_dir / "auth" / "target"
    symlink_target.mkdir(parents=True)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    worktree.write_text("not a directory", encoding="utf-8")
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.symlink_to(symlink_target, target_is_directory=True)

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        min_age_hours=24,
        now=now,
    )
    payload = result.to_dict()

    assert result.deleted_paths == []
    assert {(error.kind, error.error) for error in result.delete_errors} == {
        ("worktree", "refusing to delete non-directory path"),
        ("compose", "path is outside the expected service GC roots"),
        ("auth", "refusing to delete symlink"),
    }
    assert worktree.exists()
    assert auth.is_symlink()
    assert payload["delete_errors"] == [
        {
            "workspace_id": workspace_id,
            "kind": "worktree",
            "path": str(worktree),
            "reason_code": "PATH_DELETE_FAILED",
            "error": "refusing to delete non-directory path",
        },
        {
            "workspace_id": workspace_id,
            "kind": "compose",
            "path": str(outside_dir),
            "reason_code": "PATH_DELETE_FAILED",
            "error": "path is outside the expected service GC roots",
        },
        {
            "workspace_id": workspace_id,
            "kind": "auth",
            "path": str(auth),
            "reason_code": "PATH_DELETE_FAILED",
            "error": "refusing to delete symlink",
        },
    ]
    candidate = payload["candidates"][0]
    assert candidate["paths"]["worktree"]["error"] == "refusing to delete non-directory path"
    assert candidate["paths"]["compose"]["error"] == "path is outside the expected service GC roots"
    assert candidate["paths"]["auth"]["error"] == "refusing to delete symlink"
    assert result.path_outcomes[0].to_dict()["error"] == "refusing to delete non-directory path"


@pytest.mark.unit
def test_delete_gc_path_rejects_unknown_gc_kind(tmp_path: Path) -> None:
    target = tmp_path / "service" / "unknown" / "ws_unknown"
    target.mkdir(parents=True)
    gc_path = WorkspaceGCPath(
        kind="unknown",
        path=target,
        exists=True,
        estimated_bytes=0,
    )

    deleted, error = _delete_gc_path(gc_path, work_dir=tmp_path / "service")

    assert deleted is False
    assert error == "path is outside the expected service GC roots"
    assert gc_path.to_dict(error=error)["error"] == error
    assert target.exists()


@pytest.mark.unit
def test_delete_gc_path_treats_missing_path_as_already_removed(tmp_path: Path) -> None:
    gc_path = WorkspaceGCPath(
        kind="worktree",
        path=tmp_path / "service" / "git" / "worktrees" / "ws_missing",
        exists=False,
        estimated_bytes=0,
    )

    deleted, error = _delete_gc_path(gc_path, work_dir=tmp_path / "service")

    assert deleted is False
    assert error is None


@pytest.mark.unit
def test_delete_gc_path_handles_rmtree_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "service" / "git" / "worktrees" / "ws_error"
    target.mkdir(parents=True)
    gc_path = WorkspaceGCPath(
        kind="worktree",
        path=target,
        exists=True,
        estimated_bytes=0,
    )

    monkeypatch.setattr("shutil.rmtree", lambda _p: (_ for _ in ()).throw(OSError("permission denied")))

    deleted, error = _delete_gc_path(gc_path, work_dir=tmp_path / "service")

    assert deleted is False
    assert error == "permission denied"


@pytest.mark.unit
def test_estimate_bytes_treats_stat_races_as_zero_or_skipped() -> None:
    class _RacyFile:
        def exists(self) -> bool:
            return True

        def is_file(self) -> bool:
            return True

        def stat(self) -> object:
            raise OSError("file disappeared")

    class _StableChild:
        def is_file(self) -> bool:
            return True

        def stat(self) -> object:
            return type("Stat", (), {"st_size": 7})()

    class _RacyChild:
        def is_file(self) -> bool:
            return True

        def stat(self) -> object:
            raise OSError("child disappeared")

    class _RacyDirectory:
        def exists(self) -> bool:
            return True

        def is_file(self) -> bool:
            return False

        def rglob(self, _pattern: str) -> list[object]:
            return [_StableChild(), _RacyChild()]

    assert _estimate_bytes(_RacyFile()) == 0  # type: ignore[arg-type]
    assert _estimate_bytes(_RacyDirectory()) == 7  # type: ignore[arg-type]
