from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import awf.service.gc as gc
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import (
    SecretLeaseIssue,
    SecretLeaseRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from awf.service.gc import (
    COMPLETED_PR_RETENTION_EXPIRED,
    FAILED_WORKSPACE_NO_WORK,
    FAILED_WORKSPACE_TRIAGE_PRESERVED,
    PATH_ALREADY_REMOVED,
    PRESERVED_FAILED_AGE_CAP_RECLAIMED,
    TERMINAL_WORKSPACE_RETENTION_EXPIRED,
    WORKSPACE_WITHIN_RETENTION,
    WorkspaceGCCandidate,
    WorkspaceGCComposeTeardownResult,
    WorkspaceGCPath,
    WorkspaceGCPreserved,
    WorkspaceGCWorktreeRemoveResult,
    _classify_workspace_for_gc,
    _container_command_is_idle,
    _delete_gc_path,
    _delete_gc_path_outcome,
    _estimate_bytes,
    _failed_terminal_workspace_has_no_work,
    _snapshot_has_no_work,
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
        pr_merge_sha="a" * 40,
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
    assert {data["status"] for data in first_candidate["paths"].values()} == {"skipped"}
    assert {data["reason_code"] for data in first_candidate["paths"].values()} == {
        "DOCKER_COMPOSE_DOWN_FAILED"
    }
    assert {data["error"] for data in first_candidate["paths"].values()} == {"network still in use"}
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
    assert {data["status"] for data in third_candidate["paths"].values()} == {"already_removed"}
    assert {data["reason_code"] for data in third_candidate["paths"].values()} == {
        "PATH_ALREADY_REMOVED"
    }
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
        pr_merge_sha="a" * 40,
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
        pr_merge_sha="a" * 40,
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
async def test_single_workspace_gc_reports_failed_missing_workspace_compose_teardown(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = "ws_missing"
    calls: list[tuple[str, str, Path]] = []

    async def _compose_teardown(candidate: object) -> WorkspaceGCComposeTeardownResult:
        assert isinstance(candidate, gc.WorkspaceGCCandidate)
        calls.append((candidate.workspace_id, candidate.reason_code, candidate.compose.path))
        return WorkspaceGCComposeTeardownResult(
            status="failed",
            reason_code="DOCKER_COMPOSE_DOWN_FAILED",
            error="volume still in use",
        )

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        now=now,
        compose_teardown=_compose_teardown,
    )

    assert result.plan.candidates == []
    assert result.deleted_paths == []
    assert result.delete_errors == []
    assert result.status == "partial"
    assert result.reason_code == "CLEANUP_EXECUTION_PARTIAL"
    assert result.compose_teardowns[workspace_id].to_dict() == {
        "status": "failed",
        "reason_code": "DOCKER_COMPOSE_DOWN_FAILED",
        "error": "volume still in use",
    }
    assert result.to_dict()["compose_teardowns"] == {
        workspace_id: {
            "status": "failed",
            "reason_code": "DOCKER_COMPOSE_DOWN_FAILED",
            "error": "volume still in use",
        }
    }
    assert calls == [
        (
            workspace_id,
            "WORKSPACE_GC_EMPTY_PLAN_COMPOSE_TEARDOWN",
            work_dir / "compose" / workspace_id,
        )
    ]


@pytest.mark.unit
async def test_single_workspace_gc_cleanup_disabled_skips_missing_workspace_fallback_compose_teardown(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = "ws_missing"
    calls: list[str] = []

    async def _compose_teardown(candidate: object) -> WorkspaceGCComposeTeardownResult:
        assert isinstance(candidate, gc.WorkspaceGCCandidate)
        calls.append(candidate.workspace_id)
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
        now=now,
        compose_teardown=_compose_teardown,
    )

    assert result.plan.candidates == []
    assert result.plan.preserved == []
    assert result.plan.cleanup_enabled is False
    assert calls == []
    assert result.compose_teardowns == {}


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
        pr_merge_sha="a" * 40,
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
async def test_gc_execution_reports_permission_denied_loudly_not_already_removed(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A root-owned auth dir that the deleting process cannot remove must surface
    # as a loud, reason-coded ``partial`` failure -- never a silent success.
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="a" * 40,
    )
    auth = work_dir / "auth" / workspace_id
    _write(auth / "codex" / "auth.json", "auth")

    def _raise_permission(_path: object) -> None:
        raise PermissionError(13, "Operation not permitted")

    monkeypatch.setattr("shutil.rmtree", _raise_permission)

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        min_age_hours=24,
        now=now,
    )

    assert result.status == "partial"
    auth_errors = [error for error in result.delete_errors if error.kind == "auth"]
    assert auth_errors, result.delete_errors
    assert auth_errors[0].reason_code == "PATH_DELETE_PERMISSION_DENIED"
    payload = result.to_dict()
    assert payload["candidates"][0]["paths"]["auth"]["status"] == "failed"
    assert payload["candidates"][0]["paths"]["auth"]["reason_code"] == (
        "PATH_DELETE_PERMISSION_DENIED"
    )
    assert auth.exists()


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

    deleted, error, reason_code = _delete_gc_path(gc_path, work_dir=tmp_path / "service")

    assert deleted is False
    assert error == "path is outside the expected service GC roots"
    assert reason_code == "PATH_DELETE_FAILED"
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

    deleted, error, reason_code = _delete_gc_path(gc_path, work_dir=tmp_path / "service")

    assert deleted is False
    assert error is None
    assert reason_code is None


@pytest.mark.unit
def test_delete_gc_path_handles_rmtree_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "service" / "git" / "worktrees" / "ws_error"
    target.mkdir(parents=True)
    gc_path = WorkspaceGCPath(
        kind="worktree",
        path=target,
        exists=True,
        estimated_bytes=0,
    )

    monkeypatch.setattr("shutil.rmtree", lambda _p: (_ for _ in ()).throw(OSError("disk gone")))

    deleted, error, reason_code = _delete_gc_path(gc_path, work_dir=tmp_path / "service")

    assert deleted is False
    assert error == "disk gone"
    # A generic OSError (no permission errno) stays a plain delete failure.
    assert reason_code == "PATH_DELETE_FAILED"


@pytest.mark.unit
def test_delete_gc_path_treats_enoent_rmtree_race_as_already_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A concurrent GC run can remove the dir between the preflight ``exists``
    # probe and ``rmtree``, surfacing as ENOENT. That is an idempotent no-op,
    # not a ``PATH_DELETE_FAILED`` partial-run failure.
    import errno

    target = tmp_path / "service" / "git" / "worktrees" / "ws_race"
    target.mkdir(parents=True)
    gc_path = WorkspaceGCPath(
        kind="worktree",
        path=target,
        exists=True,
        estimated_bytes=0,
    )

    def _raise_enoent(_path: object) -> None:
        raise FileNotFoundError(errno.ENOENT, "No such file or directory")

    monkeypatch.setattr("shutil.rmtree", _raise_enoent)

    deleted, error, reason_code = _delete_gc_path(gc_path, work_dir=tmp_path / "service")

    assert deleted is False
    assert error is None
    assert reason_code == PATH_ALREADY_REMOVED


@pytest.mark.unit
def test_delete_gc_path_outcome_maps_enoent_race_to_already_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The outcome wrapper must not turn the ENOENT race into a ``failed`` path
    # outcome (which would flip the whole GC run to ``partial``).
    import errno

    target_path = tmp_path / "service" / "git" / "worktrees" / "ws_race"
    target_path.mkdir(parents=True)
    target = WorkspaceGCPath(
        kind="worktree",
        path=target_path,
        exists=True,
        estimated_bytes=0,
    )
    candidate = WorkspaceGCCandidate(
        workspace_id="ws_race",
        status=WorkspaceStatus.destroyed,
        updated_at=datetime.now(UTC),
        age_hours=72,
        reason_code=TERMINAL_WORKSPACE_RETENTION_EXPIRED,
        worktree=target,
        compose=WorkspaceGCPath(kind="compose", path=target_path, exists=False, estimated_bytes=0),
        auth=WorkspaceGCPath(kind="auth", path=target_path, exists=False, estimated_bytes=0),
    )

    def _raise_enoent(_path: object) -> None:
        raise FileNotFoundError(errno.ENOENT, "No such file or directory")

    monkeypatch.setattr("shutil.rmtree", _raise_enoent)

    outcome = _delete_gc_path_outcome(candidate, target, work_dir=tmp_path / "service")

    assert outcome.status == "already_removed"
    assert outcome.reason_code == PATH_ALREADY_REMOVED
    assert outcome.deleted is False
    assert outcome.error is None


@pytest.mark.unit
def test_delete_gc_path_outcome_records_permission_denied_from_preflight_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A root-owned 0700 parent makes even the not-exists preflight stat raise
    # PermissionError. The outcome wrapper must route that probe through the
    # permission-aware delete and record PATH_DELETE_PERMISSION_DENIED -- never
    # let the stat escape and abort the whole execute run.
    import errno

    target_path = tmp_path / "service" / "auth" / "ws_root_owned_parent"
    target_path.mkdir(parents=True)
    target = WorkspaceGCPath(
        kind="auth",
        path=target_path,
        exists=True,
        estimated_bytes=0,
    )
    candidate = WorkspaceGCCandidate(
        workspace_id="ws_root_owned_parent",
        status=WorkspaceStatus.destroyed,
        updated_at=datetime.now(UTC),
        age_hours=72,
        reason_code=TERMINAL_WORKSPACE_RETENTION_EXPIRED,
        worktree=WorkspaceGCPath(
            kind="worktree", path=target_path, exists=False, estimated_bytes=0
        ),
        compose=WorkspaceGCPath(kind="compose", path=target_path, exists=False, estimated_bytes=0),
        auth=target,
    )

    def _raise_eacces(_self: object, *_args: object, **_kwargs: object) -> bool:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(Path, "exists", _raise_eacces)

    outcome = _delete_gc_path_outcome(candidate, target, work_dir=tmp_path / "service")

    assert outcome.status == "failed"
    assert outcome.reason_code == "PATH_DELETE_PERMISSION_DENIED"
    assert outcome.deleted is False
    assert outcome.error is not None


@pytest.mark.unit
def test_delete_gc_path_outcome_maps_absent_path_to_already_removed(
    tmp_path: Path,
) -> None:
    # A genuinely absent path returns ``(False, None, None)`` from
    # ``_delete_gc_path``; the wrapper must still report it as an idempotent
    # ``already_removed`` no-op, not a failure.
    target_path = tmp_path / "service" / "git" / "worktrees" / "ws_gone"
    target = WorkspaceGCPath(
        kind="worktree",
        path=target_path,
        exists=False,
        estimated_bytes=0,
    )
    candidate = WorkspaceGCCandidate(
        workspace_id="ws_gone",
        status=WorkspaceStatus.destroyed,
        updated_at=datetime.now(UTC),
        age_hours=72,
        reason_code=TERMINAL_WORKSPACE_RETENTION_EXPIRED,
        worktree=target,
        compose=WorkspaceGCPath(kind="compose", path=target_path, exists=False, estimated_bytes=0),
        auth=WorkspaceGCPath(kind="auth", path=target_path, exists=False, estimated_bytes=0),
    )

    outcome = _delete_gc_path_outcome(candidate, target, work_dir=tmp_path / "service")

    assert outcome.status == "already_removed"
    assert outcome.reason_code == PATH_ALREADY_REMOVED
    assert outcome.deleted is False
    assert outcome.error is None


@pytest.mark.unit
def test_delete_gc_path_reports_permission_denied_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Root-owned per-workspace dirs cannot be removed by the host uid-1000
    # process. Such a refusal must surface as a distinct, reason-coded failure
    # -- never silently collapse to ``already_removed`` success.
    target = tmp_path / "service" / "auth" / "ws_root_owned"
    target.mkdir(parents=True)
    gc_path = WorkspaceGCPath(
        kind="auth",
        path=target,
        exists=True,
        estimated_bytes=0,
    )

    def _raise_permission(_path: object) -> None:
        raise PermissionError(13, "Operation not permitted")

    monkeypatch.setattr("shutil.rmtree", _raise_permission)

    deleted, error, reason_code = _delete_gc_path(gc_path, work_dir=tmp_path / "service")

    assert deleted is False
    assert reason_code == "PATH_DELETE_PERMISSION_DENIED"
    assert error is not None


@pytest.mark.unit
def test_delete_gc_path_classifies_eperm_oserror_as_permission_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno

    target = tmp_path / "service" / "auth" / "ws_eperm"
    target.mkdir(parents=True)
    gc_path = WorkspaceGCPath(
        kind="auth",
        path=target,
        exists=True,
        estimated_bytes=0,
    )

    def _raise_eperm(_path: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr("shutil.rmtree", _raise_eperm)

    deleted, error, reason_code = _delete_gc_path(gc_path, work_dir=tmp_path / "service")

    assert deleted is False
    assert reason_code == "PATH_DELETE_PERMISSION_DENIED"
    assert error is not None


@pytest.mark.unit
def test_delete_gc_path_records_permission_denied_from_preflight_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A root-owned 0700 parent dir makes the preflight stat probes
    # (exists/is_symlink/is_dir) raise PermissionError -- pathlib only swallows
    # ENOENT/ENOTDIR/EBADF/ELOOP, not EACCES. Such a refusal must be recorded as
    # PATH_DELETE_PERMISSION_DENIED, never escape the GC run.
    import errno

    target = tmp_path / "service" / "auth" / "ws_root_owned_parent"
    target.mkdir(parents=True)
    gc_path = WorkspaceGCPath(
        kind="auth",
        path=target,
        exists=True,
        estimated_bytes=0,
    )

    def _raise_eacces(_self: object, *_args: object, **_kwargs: object) -> bool:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(Path, "exists", _raise_eacces)

    deleted, error, reason_code = _delete_gc_path(gc_path, work_dir=tmp_path / "service")

    assert deleted is False
    assert reason_code == "PATH_DELETE_PERMISSION_DENIED"
    assert error is not None


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


def test_snapshot_has_no_work_unavailable():
    snap = RuntimeSnapshot(stack_state="unavailable", services=[])
    assert _snapshot_has_no_work(snap) is False


def test_snapshot_has_no_work_no_agent():
    snap = RuntimeSnapshot(
        stack_state="running",
        services=[
            RuntimeService(
                name="other", state="running", command="sleep", container_id="id", image="img"
            )
        ],
    )
    assert _snapshot_has_no_work(snap) is False


def test_container_command_is_idle_no_command():
    assert _container_command_is_idle(None) is False
    assert _container_command_is_idle("") is False


def test_classify_workspace_completed_retention_expired():
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.completed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=25),
        compose_project_name="proj",
        pr_url="http://github.com/pr/1",
    )
    res = _classify_workspace_for_gc(
        ws,
        work_dir=Path("/tmp"),
        now=datetime.now(UTC),
        cutoff_at=datetime.now(UTC) - timedelta(hours=24),
        default_policy=False,
        cleanup_enabled=True,
    )
    assert isinstance(res, WorkspaceGCCandidate)
    assert res.reason_code == COMPLETED_PR_RETENTION_EXPIRED

    ws2 = Workspace(
        id="ws_2",
        status=WorkspaceStatus.completed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=25),
        compose_project_name="proj",
        pr_url=None,
    )
    res2 = _classify_workspace_for_gc(
        ws2,
        work_dir=Path("/tmp"),
        now=datetime.now(UTC),
        cutoff_at=datetime.now(UTC) - timedelta(hours=24),
        default_policy=False,
        cleanup_enabled=True,
    )
    assert isinstance(res2, WorkspaceGCCandidate)
    assert res2.reason_code == TERMINAL_WORKSPACE_RETENTION_EXPIRED


def test_failed_terminal_workspace_has_no_work_exception(monkeypatch: pytest.MonkeyPatch):
    class _RaisingRuntimeInspector:
        async def inspect(self, compose_project_name: str) -> RuntimeSnapshot:
            assert compose_project_name == "proj"
            raise RuntimeError("mocked err")

    ws = Workspace(id="ws_1", compose_project_name="proj")
    monkeypatch.setattr(gc, "_RUNTIME_INSPECTOR", _RaisingRuntimeInspector())

    assert _failed_terminal_workspace_has_no_work(ws) is False


def test_classify_workspace_failed_no_work_but_within_retention():
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.failed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=1),
        compose_project_name="proj",
    )
    with patch("awf.service.gc._failed_terminal_workspace_has_no_work", return_value=True):
        res = _classify_workspace_for_gc(
            ws,
            work_dir=Path("/tmp"),
            now=datetime.now(UTC),
            cutoff_at=datetime.now(UTC) - timedelta(hours=24),
            default_policy=True,
            cleanup_enabled=True,
        )
        assert isinstance(res, WorkspaceGCPreserved)
        assert res.reason_code == WORKSPACE_WITHIN_RETENTION


def test_classify_workspace_failed_no_work_expired():
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.failed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=25),
        compose_project_name="proj",
    )
    with patch("awf.service.gc._failed_terminal_workspace_has_no_work", return_value=True):
        res = _classify_workspace_for_gc(
            ws,
            work_dir=Path("/tmp"),
            now=datetime.now(UTC),
            cutoff_at=datetime.now(UTC) - timedelta(hours=24),
            default_policy=True,
            cleanup_enabled=True,
        )
        assert isinstance(res, WorkspaceGCCandidate)
        assert res.reason_code == FAILED_WORKSPACE_NO_WORK


def test_classify_workspace_failed_has_work_but_expired_no_default_policy():
    ws = Workspace(
        id="ws_1",
        status=WorkspaceStatus.failed.value,
        updated_at=datetime.now(UTC) - timedelta(hours=25),
        compose_project_name="proj",
    )
    with patch("awf.service.gc._failed_terminal_workspace_has_no_work", return_value=False):
        res = _classify_workspace_for_gc(
            ws,
            work_dir=Path("/tmp"),
            now=datetime.now(UTC),
            cutoff_at=datetime.now(UTC) - timedelta(hours=24),
            default_policy=False,
            cleanup_enabled=True,
        )
        assert isinstance(res, WorkspaceGCPreserved)


def test_classify_preserved_failed_past_cap_reclaims_pressure_dirs():
    now = datetime.now(UTC)
    ws = Workspace(
        id="ws_capped",
        status=WorkspaceStatus.failed.value,
        updated_at=now - timedelta(hours=800),
        compose_project_name="proj",
        compose_file_path="/work/compose/ws_capped/compose.yml",
    )
    with patch("awf.service.gc._failed_terminal_workspace_has_no_work", return_value=False):
        res = _classify_workspace_for_gc(
            ws,
            work_dir=Path("/work"),
            now=now,
            cutoff_at=now - timedelta(hours=168),
            default_policy=True,
            cleanup_enabled=True,
            preserved_failed_cutoff_at=now - timedelta(hours=720),
        )
    assert isinstance(res, WorkspaceGCCandidate)
    assert res.reason_code == PRESERVED_FAILED_AGE_CAP_RECLAIMED
    # Pressure dirs are reaped; the durable DB row is never touched by classify.
    assert res.worktree.path == Path("/work/git/worktrees/ws_capped")
    assert res.auth.path == Path("/work/auth/ws_capped")
    assert res.compose.path == Path("/work/compose/ws_capped")


def test_classify_preserved_failed_under_cap_is_left_alone():
    now = datetime.now(UTC)
    ws = Workspace(
        id="ws_young",
        status=WorkspaceStatus.failed.value,
        updated_at=now - timedelta(hours=100),
        compose_project_name="proj",
    )
    with patch("awf.service.gc._failed_terminal_workspace_has_no_work", return_value=False):
        res = _classify_workspace_for_gc(
            ws,
            work_dir=Path("/work"),
            now=now,
            cutoff_at=now - timedelta(hours=168),
            default_policy=True,
            cleanup_enabled=True,
            preserved_failed_cutoff_at=now - timedelta(hours=720),
        )
    assert isinstance(res, WorkspaceGCPreserved)
    assert res.reason_code == FAILED_WORKSPACE_TRIAGE_PRESERVED


def test_classify_preserved_failed_no_cutoff_preserves():
    now = datetime.now(UTC)
    ws = Workspace(
        id="ws_nocap",
        status=WorkspaceStatus.failed.value,
        updated_at=now - timedelta(hours=10_000),
        compose_project_name="proj",
    )
    with patch("awf.service.gc._failed_terminal_workspace_has_no_work", return_value=False):
        res = _classify_workspace_for_gc(
            ws,
            work_dir=Path("/work"),
            now=now,
            cutoff_at=now - timedelta(hours=168),
            default_policy=True,
            cleanup_enabled=True,
            preserved_failed_cutoff_at=None,
        )
    assert isinstance(res, WorkspaceGCPreserved)
    assert res.reason_code == FAILED_WORKSPACE_TRIAGE_PRESERVED


def test_classify_preserved_superseded_past_cap_reclaims():
    now = datetime.now(UTC)
    ws = Workspace(
        id="ws_super",
        status="superseded",
        updated_at=now - timedelta(hours=800),
        compose_project_name="proj",
    )
    with patch("awf.service.gc._failed_terminal_workspace_has_no_work", return_value=False):
        res = _classify_workspace_for_gc(
            ws,
            work_dir=Path("/work"),
            now=now,
            cutoff_at=now - timedelta(hours=168),
            default_policy=True,
            cleanup_enabled=True,
            preserved_failed_cutoff_at=now - timedelta(hours=720),
        )
    assert isinstance(res, WorkspaceGCCandidate)
    assert res.reason_code == PRESERVED_FAILED_AGE_CAP_RECLAIMED


def test_preserved_failed_age_cap_reason_code_is_distinct():
    # Auditable separately from the 168 h idle / no-work cleanup paths.
    assert PRESERVED_FAILED_AGE_CAP_RECLAIMED != FAILED_WORKSPACE_NO_WORK
    assert PRESERVED_FAILED_AGE_CAP_RECLAIMED != FAILED_WORKSPACE_TRIAGE_PRESERVED


@pytest.mark.unit
async def test_plan_terminal_gc_reaps_preserved_failed_past_cap(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    old_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=800),
    )
    young_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=100),
    )
    with patch("awf.service.gc._failed_terminal_workspace_has_no_work", return_value=False):
        plan = await plan_terminal_workspace_gc(
            session_factory,
            work_dir=tmp_path,
            now=now,
            max_preserved_failed_hours=720,
        )
    capped = {c.workspace_id: c for c in plan.candidates}
    preserved = {p.workspace_id: p for p in plan.preserved}
    assert old_id in capped
    assert capped[old_id].reason_code == PRESERVED_FAILED_AGE_CAP_RECLAIMED
    assert young_id in preserved
    assert preserved[young_id].reason_code == FAILED_WORKSPACE_TRIAGE_PRESERVED


@pytest.mark.unit
async def test_plan_limit_bounds_candidates_across_candidate_and_preserved_queries(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # ``limit`` caps the candidate and preserved SQL queries independently, and
    # the preserved loop promotes age-capped failed rows into candidates. The
    # combined candidate count must still honor the batch limit rather than
    # reclaiming up to ~2x ``limit`` rows in one run.
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    completed_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=300),
        pr=True,
        pr_merge_sha="a" * 40,
    )
    capped_failed_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=800),
    )
    with patch("awf.service.gc._failed_terminal_workspace_has_no_work", return_value=False):
        plan = await plan_terminal_workspace_gc(
            session_factory,
            work_dir=tmp_path,
            now=now,
            limit=1,
            max_preserved_failed_hours=720,
        )
    # Without the combined-budget guard both queries would each yield one
    # candidate (the merged-completed row and the age-capped failed row).
    assert len(plan.candidates) == 1
    # Oldest-first wins the single slot; the failed row (800h) predates the
    # completed row (300h).
    assert [c.workspace_id for c in plan.candidates] == [capped_failed_id]
    assert completed_id not in {c.workspace_id for c in plan.candidates}


@pytest.mark.unit
async def test_age_capped_failed_not_starved_by_older_preserved_rows(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # The preserved query applies ``limit`` (oldest-first) before the age-cap
    # classification, and it also matches rows preserved indefinitely with no
    # age cap (completed-without-PR). An older such row must not fill the
    # preserved limit and starve an aged failed row out of being reclaimed.
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    older_completed_no_pr_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=2000),
    )
    aged_failed_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=800),
    )
    with patch("awf.service.gc._failed_terminal_workspace_has_no_work", return_value=False):
        plan = await plan_terminal_workspace_gc(
            session_factory,
            work_dir=tmp_path,
            now=now,
            limit=1,
            max_preserved_failed_hours=720,
        )
    candidates = {c.workspace_id: c for c in plan.candidates}
    # The age-capped failed row is fetched independently of the preserved query,
    # so it is reclaimed despite the older indefinitely-preserved row.
    assert aged_failed_id in candidates
    assert candidates[aged_failed_id].reason_code == PRESERVED_FAILED_AGE_CAP_RECLAIMED
    # The completed-without-PR row is preserved at any age (no age cap).
    assert older_completed_no_pr_id not in candidates


@pytest.mark.unit
async def test_no_work_superseded_workspace_is_gc_candidate_without_recovery_metadata(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "service"
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
        compose_project_name="awf_superseded_meta_test",
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

    result = await run_workspace_filesystem_gc(
        session_factory,
        work_dir=work_dir,
        workspace_id=workspace_id,
        execute=True,
        min_age_hours=24,
        now=now,
    )

    assert result.status == "succeeded"
    assert result.plan.candidates[0].workspace_id == workspace_id
    assert result.plan.candidates[0].reason_code == FAILED_WORKSPACE_NO_WORK

    async with session_factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        assert workspace.status == "superseded"


@pytest.mark.unit
async def test_no_work_superseded_gc_candidate_includes_recovery_metadata_reference(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_dir = tmp_path / "service"
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
        compose_project_name="awf_superseded_recovery_meta",
        updated_at=now - timedelta(hours=200),
        failure_reason=FailureReason.agent_failure.value,
        failure_message="RESOURCE_EXHAUSTED RetryableQuotaError",
    )

    async with session_factory() as session:
        from awf.common.ids import new_event_id

        session.add(
            WorkspaceEvent(
                id=new_event_id(),
                workspace_id=workspace_id,
                event_type="workspace.provider_recovery_requested",
                reason_code="PROVIDER_FAILURE_DETECTED",
                old_state="superseded",
                new_state="superseded",
                payload={"reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED"},
            )
        )
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
        work_dir=work_dir,
        min_age_hours=24,
        now=now,
    )

    assert len(plan.candidates) == 1
    candidate = plan.candidates[0]
    assert candidate.workspace_id == workspace_id
    assert candidate.reason_code == FAILED_WORKSPACE_NO_WORK

    async with session_factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        assert workspace.failure_reason is not None

        events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id)
        assert any(e.event_type == "workspace.provider_recovery_requested" for e in events)


@pytest.mark.unit
async def test_completed_workspace_with_merge_sha_but_no_pr_metadata_is_preserved(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
    )
    async with session_factory() as session:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
        workspace.pr_merge_sha = "d" * 40
        await session.commit()

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        min_age_hours=24,
        now=now,
    )

    assert plan.candidates == []
    assert len(plan.preserved) == 1
    assert plan.preserved[0].workspace_id == workspace_id
    assert plan.preserved[0].reason_code == "COMPLETED_WORKSPACE_WITHOUT_PR"


@pytest.mark.unit
async def test_completed_workspace_without_pr_metadata_does_not_consume_candidate_slot(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    without_pr_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=300),
        title="no pr metadata",
    )
    async with session_factory() as session:
        workspace = await session.get(Workspace, without_pr_id)
        assert workspace is not None
        workspace.pr_merge_sha = "a" * 40
        await session.commit()

    with_pr_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        title="has pr metadata",
        pr=True,
        pr_merge_sha="b" * 40,
    )

    plan = await plan_terminal_workspace_gc(
        session_factory,
        work_dir=tmp_path / "service",
        min_age_hours=24,
        limit=1,
        now=now,
    )

    assert [c.workspace_id for c in plan.candidates] == [with_pr_id]
    preserved_ids = {p.workspace_id for p in plan.preserved}
    assert without_pr_id in preserved_ids
