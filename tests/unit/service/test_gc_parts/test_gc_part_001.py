from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import awf.service.gc as gc
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    ResourceReservationRepository,
    SecretLeaseIssue,
    SecretLeaseRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceEventRepository,
    WorkspaceLogStreamRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from awf.service.gc import (
    COMPLETED_PR_IMMEDIATE_RECLAIM,
    COMPLETED_PR_NOT_MERGED,
    COMPLETED_PR_RETENTION_EXPIRED,
    FAILED_WORKSPACE_NO_WORK,
    FAILED_WORKSPACE_TRIAGE_PRESERVED,
    WORKSPACE_WITHIN_RETENTION,
    WorkspaceGCComposeTeardownResult,
    WorkspaceGCWorktreeRemoveResult,
    plan_terminal_workspace_gc,
    run_terminal_workspace_gc,
    run_workspace_filesystem_gc,
)

"""Terminal workspace filesystem GC tests."""


@pytest.fixture(autouse=True)
def _mock_default_worktree_remover():
    with patch(
        "awf.service.gc._default_worktree_remover",
        new=AsyncMock(
            return_value=WorkspaceGCWorktreeRemoveResult(
                status="succeeded",
                reason_code="WORKTREE_REMOVE_SUCCEEDED",
            )
        ),
    ):
        yield


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


async def _task_attempt_for_workspace(
    session: AsyncSession,
    workspace_id: str,
) -> str:
    workspace = await session.get(Workspace, workspace_id)
    assert workspace is not None
    task = await TaskRepository(session).create_or_get(
        repo_url=workspace.repo_url,
        base_branch=workspace.branch_base,
        title=workspace.task_title,
        prompt=workspace.task_prompt,
        external_id=f"gc-{workspace_id}",
        idempotency_key=None,
        task_class=workspace.task_class,
        owned_paths=list(workspace.owned_paths),
    )
    attempt = await TaskAttemptRepository(session).create_for_workspace(
        task=task,
        workspace=workspace,
    )
    return attempt.id


async def _set_workspace_gc_state(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    **values: object,
) -> None:
    async with session_factory() as session:
        await session.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        await session.commit()


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
def test_gc_to_utc_accepts_naive_datetime() -> None:
    naive = datetime(2026, 5, 8, 12, 30)

    assert gc._to_utc(naive) == naive.replace(tzinfo=UTC)  # noqa: SLF001


@pytest.mark.unit
def test_default_candidate_predicate_requires_pr_metadata():
    cutoff = datetime(2026, 4, 26, 12, tzinfo=UTC)
    predicate = gc._workspace_gc_candidate_predicate(
        eligible_statuses={WorkspaceStatus.completed.value},
        cutoff_at=cutoff,
        default_policy=True,
        cleanup_enabled=True,
    )
    assert predicate is not None
    compiled = str(predicate.compile(compile_kwargs={"literal_binds": True}))
    assert "pr_number" in compiled or "pr_url" in compiled


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
        pr_merge_sha="a" * 40,
    )
    await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=100),
        title="middle",
        pr=True,
        pr_merge_sha="a" * 40,
    )
    await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=2),
        title="fresh",
        pr=True,
        pr_merge_sha="a" * 40,
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
            pr_merge_sha="a" * 40,
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
        pr_merge_sha="a" * 40,
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
        pr_merge_sha="a" * 40,
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
        pr_merge_sha="a" * 40,
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
        pr_merge_sha="a" * 40,
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
    await _set_workspace_gc_state(
        session_factory,
        workspace_id,
        status="superseded",
        compose_project_name="awf_superseded_gc",
        updated_at=now - timedelta(hours=200),
    )

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
async def test_default_plan_preserves_recent_superseded_no_work_within_retention(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
    )
    await _set_workspace_gc_state(
        session_factory,
        workspace_id,
        status="superseded",
        compose_project_name="awf_recent_superseded_gc",
        updated_at=now - timedelta(hours=1),
    )

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

    assert plan.candidates == []
    assert plan.preserved_count == 1
    assert plan.preserved[0].workspace_id == workspace_id
    assert plan.preserved[0].reason_code == WORKSPACE_WITHIN_RETENTION


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
    await _set_workspace_gc_state(
        session_factory,
        workspace_id,
        status="superseded",
        compose_project_name="awf_single_superseded_gc",
        updated_at=now - timedelta(hours=200),
    )
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
async def test_default_plan_preserves_superseded_when_agent_container_not_running(
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
    await _set_workspace_gc_state(
        session_factory,
        workspace_id,
        status="superseded",
        compose_project_name="awf_superseded_gc_not_running",
        updated_at=now - timedelta(hours=200),
    )

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
                        state="exited",
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

    assert plan.candidates == []
    assert plan.preserved_count == 1
    assert plan.preserved[0].workspace_id == workspace_id
    assert plan.preserved[0].reason_code == FAILED_WORKSPACE_TRIAGE_PRESERVED


@pytest.mark.unit
async def test_default_plan_preserves_superseded_when_agent_service_missing(
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
    await _set_workspace_gc_state(
        session_factory,
        workspace_id,
        status="superseded",
        compose_project_name="awf_superseded_missing_agent_service",
        updated_at=now - timedelta(hours=200),
    )

    monkeypatch.setattr(
        gc,
        "_RUNTIME_INSPECTOR",
        _StaticRuntimeInspector(RuntimeSnapshot(stack_state="stopped", services=[])),
    )

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        min_age_hours=24,
        now=now,
    )

    assert plan.candidates == []
    assert plan.preserved_count == 1
    assert plan.preserved[0].workspace_id == workspace_id
    assert plan.preserved[0].reason_code == FAILED_WORKSPACE_TRIAGE_PRESERVED


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
        pr_merge_sha="a" * 40,
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
        "requires_pr_merge": False,
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
        pr_merge_sha="a" * 40,
    )
    other_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now,
        title="other completed workspace",
        pr=True,
        pr_merge_sha="a" * 40,
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
async def test_single_workspace_gc_ignore_retention_reclaims_recent_merged_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # A freshly-merged workspace whose ``updated_at`` is well within the
    # retention window is reclaimed immediately when ignore_retention=True; the
    # durable DB row + events survive (only pressure dirs go). The candidate is
    # tagged COMPLETED_PR_IMMEDIATE_RECLAIM so audit logs distinguish this
    # post-merge bypass from a workspace that naturally aged out of retention.
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now,
        pr=True,
        pr_merge_sha="a" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    auth = work_dir / "auth" / workspace_id
    _write(worktree / "repo.txt", "repo")
    _write(auth / "codex" / "auth.json", "auth")

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        min_age_hours=168,
        ignore_retention=True,
        now=now,
    )

    assert result.dry_run is False
    assert [candidate.workspace_id for candidate in result.plan.candidates] == [workspace_id]
    assert result.plan.candidates[0].reason_code == COMPLETED_PR_IMMEDIATE_RECLAIM
    assert not worktree.exists()
    assert not auth.exists()
    async with session_factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.completed.value
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            limit=50,
        )
    assert events  # the durable audit log is preserved


@pytest.mark.unit
async def test_single_workspace_gc_default_retention_defers_recent_merged_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # Regression guard: with ignore_retention left at its default, the same
    # recently-merged workspace is still deferred within the retention window.
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now,
        pr=True,
        pr_merge_sha="a" * 40,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        min_age_hours=168,
        now=now,
    )

    assert result.plan.candidates == []
    assert [preserved.reason_code for preserved in result.plan.preserved] == [
        WORKSPACE_WITHIN_RETENTION,
    ]
    assert worktree.exists()


@pytest.mark.unit
async def test_single_workspace_gc_ignore_retention_still_preserves_unmerged_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # ignore_retention only releases merged PRs early; a completed workspace
    # whose PR has not merged is still preserved for inspection.
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now,
        pr=True,
        pr_merge_sha=None,
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        min_age_hours=168,
        ignore_retention=True,
        now=now,
    )

    assert result.plan.candidates == []
    assert [preserved.reason_code for preserved in result.plan.preserved] == [
        COMPLETED_PR_NOT_MERGED,
    ]
    assert worktree.exists()


@pytest.mark.unit
async def test_single_workspace_gc_tears_down_compose_for_preserved_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    compose_file = work_dir / "compose" / "stored-preserved" / "compose.yml"
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now,
        compose_file_path=str(compose_file),
        pr=True,
        pr_merge_sha=None,
    )
    await _set_workspace_gc_state(
        session_factory,
        workspace_id,
        compose_project_name="awf_preserved_compose",
    )
    worktree = work_dir / "git" / "worktrees" / workspace_id
    _write(worktree / "repo.txt", "repo")
    _write(compose_file, "compose")
    calls: list[tuple[str, str, str | None, str | None, Path]] = []

    async def _compose_teardown(
        candidate: object,
    ) -> WorkspaceGCComposeTeardownResult:
        assert isinstance(candidate, gc.WorkspaceGCCandidate)
        calls.append(
            (
                candidate.workspace_id,
                candidate.reason_code,
                candidate.compose_project_name,
                candidate.compose_file_path,
                candidate.compose.path,
            )
        )
        return WorkspaceGCComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        min_age_hours=168,
        ignore_retention=True,
        compose_teardown=_compose_teardown,
        now=now,
    )

    assert result.plan.candidates == []
    assert [preserved.reason_code for preserved in result.plan.preserved] == [
        COMPLETED_PR_NOT_MERGED,
    ]
    assert result.plan.preserved[0].compose_project_name == "awf_preserved_compose"
    assert result.plan.preserved[0].compose_file_path == str(compose_file)
    assert result.compose_teardowns[workspace_id].reason_code == "DOCKER_COMPOSE_DOWN_SUCCEEDED"
    assert calls == [
        (
            workspace_id,
            COMPLETED_PR_NOT_MERGED,
            "awf_preserved_compose",
            str(compose_file),
            compose_file.parent,
        )
    ]
    assert worktree.exists()
    assert compose_file.exists()


@pytest.mark.unit
async def test_single_workspace_gc_cleanup_disabled_skips_fallback_compose_teardown(
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
        pr_merge_sha="a" * 40,
    )
    await _issue_gc_secret_lease(session_factory, workspace_id, now=now)
    calls: list[str] = []

    async def _compose_teardown(
        candidate: object,
    ) -> WorkspaceGCComposeTeardownResult:
        assert isinstance(candidate, gc.WorkspaceGCCandidate)
        calls.append(candidate.reason_code)
        return WorkspaceGCComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        cleanup_enabled=False,
        compose_teardown=_compose_teardown,
        now=now,
    )

    assert result.plan.candidates == []
    assert [preserved.reason_code for preserved in result.plan.preserved] == [
        "WORKSPACE_CLEANUP_DISABLED",
    ]
    assert calls == []
    assert result.compose_teardowns == {}
    assert result.secret_lease_revocations == {}
    async with session_factory() as session:
        leases = await SecretLeaseRepository(session).list_for_workspace(workspace_id)
    assert leases[0].status == "issued"


@pytest.mark.unit
async def test_single_workspace_gc_triage_preserved_skips_fallback_compose_teardown(
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
    await _issue_gc_secret_lease(session_factory, workspace_id, now=now)
    calls: list[str] = []

    async def _compose_teardown(
        candidate: object,
    ) -> WorkspaceGCComposeTeardownResult:
        assert isinstance(candidate, gc.WorkspaceGCCandidate)
        calls.append(candidate.reason_code)
        return WorkspaceGCComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        compose_teardown=_compose_teardown,
        now=now,
    )

    assert result.plan.candidates == []
    assert [preserved.reason_code for preserved in result.plan.preserved] == [
        FAILED_WORKSPACE_TRIAGE_PRESERVED,
    ]
    assert calls == []
    assert result.compose_teardowns == {}
    assert result.secret_lease_revocations == {}
    async with session_factory() as session:
        leases = await SecretLeaseRepository(session).list_for_workspace(workspace_id)
    assert leases[0].status == "issued"


@pytest.mark.unit
async def test_single_workspace_fallback_compose_teardown_releases_runtime_side_effects(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now,
        pr=True,
        pr_merge_sha=None,
    )
    await _issue_gc_secret_lease(session_factory, workspace_id, now=now)
    async with session_factory() as session:
        repo = ResourceReservationRepository(session)
        attempt_id = await _task_attempt_for_workspace(session, workspace_id)
        await repo.create(
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            node_id="node_1",
            steady_cpu=1.0,
            steady_memory_gb=2.0,
            peak_cpu=2.0,
            peak_memory_gb=4.0,
            disk_mb=1024,
            phase="steady",
            reserved_at=now - timedelta(hours=1),
        )
        await session.commit()

    async def _compose_teardown(
        _candidate: object,
    ) -> WorkspaceGCComposeTeardownResult:
        return WorkspaceGCComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        min_age_hours=168,
        ignore_retention=True,
        compose_teardown=_compose_teardown,
        now=now,
    )

    assert result.plan.candidates == []
    assert [preserved.reason_code for preserved in result.plan.preserved] == [
        COMPLETED_PR_NOT_MERGED,
    ]
    assert result.to_dict()["secret_leases"] == {
        workspace_id: {"revoked_count": 1, "reason_code": "TERMINAL_GC"}
    }
    assert result.reservation_releases[workspace_id]["released_count"] == 1
    async with session_factory() as session:
        leases = await SecretLeaseRepository(session).list_for_workspace(workspace_id)
        reservation = await ResourceReservationRepository(session).active_for_workspace(
            workspace_id
        )

    assert leases[0].status == "revoked"
    assert leases[0].revoke_reason_code == "TERMINAL_GC"
    assert reservation is None


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
        pr_merge_sha="a" * 40,
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
        pr_merge_sha="a" * 40,
    )
    second_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=210),
        pr=True,
        pr_merge_sha="a" * 40,
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
