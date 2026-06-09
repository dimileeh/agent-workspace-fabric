from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

import awf.service.gc as gc
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.gc import (
    WorkspaceGCComposeTeardownResult,
    WorkspaceGCWorktreeRemoveResult,
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


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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
        pr_merge_sha="a" * 40,
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
        "_unmount_candidate_auth_overlay",
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
    assert {data["status"] for data in first_payload["candidates"][0]["paths"].values()} == {
        "skipped"
    }
    assert {data["reason_code"] for data in first_payload["candidates"][0]["paths"].values()} == {
        "DOCKER_COMPOSE_DOWN_FAILED"
    }
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
    assert {data["status"] for data in third_payload["candidates"][0]["paths"].values()} == {
        "already_removed"
    }
    assert third.path_outcomes[0].to_dict()["reason_code"] == "PATH_ALREADY_REMOVED"


@pytest.mark.unit
async def test_service_gc_unmounts_auth_overlay_before_removing_auth_dir(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # The Claude auth overlay is mounted at ``auth/<id>/claude/merged``; the
    # terminal/service GC path must unmount it before ``rmtree`` of ``auth/<id>``,
    # otherwise a live mount makes removal fail with EBUSY and strands the mount.
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="b" * 40,
    )
    auth = work_dir / "auth" / workspace_id
    _write(auth / "claude" / "merged" / "settings.json", "{}")

    auth_present_at_unmount: list[bool] = []
    teardown = Mock(side_effect=lambda **_kw: auth_present_at_unmount.append(auth.exists()))

    with patch("awf.node.auth_mounts.teardown_workspace_auth_overlay", teardown):
        result = await run_terminal_workspace_gc(
            session_factory,
            work_dir=work_dir,
            min_age_hours=24,
            execute=True,
            now=now,
        )

    assert result.status == "succeeded"
    teardown.assert_called_once_with(work_dir=work_dir, workspace_id=workspace_id)
    # The overlay was unmounted while the auth dir still existed (before removal),
    # and the auth dir is then deleted rather than left stranded.
    assert auth_present_at_unmount == [True]
    assert not auth.exists()


@pytest.mark.unit
async def test_service_gc_unmounts_auth_overlay_after_compose_teardown(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # While the agent container is up it bind-mounts the overlay ``merged`` dir, so
    # a pre-teardown umount fails EBUSY and the auth dir is stranded. The overlay
    # unmount must therefore run *after* the compose stack is torn down (which stops
    # the container), so the umount succeeds before the auth-dir removal.
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    compose_slug = "stored"
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        compose_file_path=str(work_dir / "compose" / compose_slug / "compose.yml"),
        pr=True,
        pr_merge_sha="d" * 40,
    )
    _write(work_dir / "compose" / compose_slug / "compose.yml", "compose")
    _write(work_dir / "auth" / workspace_id / "claude" / "merged" / "settings.json", "{}")

    events: list[str] = []

    async def _compose_teardown(_candidate: object) -> WorkspaceGCComposeTeardownResult:
        events.append("teardown")
        return WorkspaceGCComposeTeardownResult(
            status="succeeded",
            reason_code="DOCKER_COMPOSE_DOWN_SUCCEEDED",
        )

    teardown = Mock(side_effect=lambda **_kw: events.append("unmount"))

    with patch("awf.node.auth_mounts.teardown_workspace_auth_overlay", teardown):
        result = await run_terminal_workspace_gc(
            session_factory,
            work_dir=work_dir,
            min_age_hours=24,
            execute=True,
            now=now,
            compose_teardown=_compose_teardown,
        )

    assert result.status == "succeeded"
    # The compose stack is torn down before the overlay is unmounted.
    assert events == ["teardown", "unmount"]
    assert not (work_dir / "auth" / workspace_id).exists()


@pytest.mark.unit
async def test_service_gc_skips_overlay_unmount_when_compose_teardown_fails(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # A failed compose teardown means the container is still up and no paths are
    # deleted, so there is nothing to strand: the overlay unmount (which would fail
    # EBUSY against the live container anyway) is skipped entirely.
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    compose_slug = "stored"
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        compose_file_path=str(work_dir / "compose" / compose_slug / "compose.yml"),
        pr=True,
        pr_merge_sha="e" * 40,
    )
    _write(work_dir / "compose" / compose_slug / "compose.yml", "compose")
    _write(work_dir / "auth" / workspace_id / "claude" / "merged" / "settings.json", "{}")

    async def _compose_teardown(_candidate: object) -> WorkspaceGCComposeTeardownResult:
        return WorkspaceGCComposeTeardownResult(
            status="failed",
            reason_code="DOCKER_COMPOSE_DOWN_FAILED",
            error="network still in use",
        )

    teardown = Mock()

    with patch("awf.node.auth_mounts.teardown_workspace_auth_overlay", teardown):
        result = await run_terminal_workspace_gc(
            session_factory,
            work_dir=work_dir,
            min_age_hours=24,
            execute=True,
            now=now,
            compose_teardown=_compose_teardown,
        )

    assert result.status == "partial"
    teardown.assert_not_called()
    # Paths are preserved for a later retry rather than stranded.
    assert (work_dir / "auth" / workspace_id).exists()


@pytest.mark.unit
async def test_service_gc_fails_loudly_and_skips_auth_dir_on_umount_failure(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # A genuine umount failure must be logged with its reason code AND surfaced as
    # a loud ``partial`` result: the auth dir is left in place (never rmtree'd over
    # a possibly-live mount), while worktree/compose paths still proceed.
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="c" * 40,
    )
    _write(work_dir / "git" / "worktrees" / workspace_id / "repo.txt", "repo")
    auth = work_dir / "auth" / workspace_id
    _write(auth / "claude" / "merged" / "settings.json", "{}")

    # ``umount(8)`` exits non-zero with its kernel reason on stderr; ``repr`` of
    # the CalledProcessError drops that text, so the handler must forward stderr
    # explicitly for the EBUSY root cause to be greppable.
    kernel_reason = "umount: /…/merged: target is busy."
    teardown = Mock(side_effect=subprocess.CalledProcessError(32, "umount", stderr=kernel_reason))

    with (
        patch("awf.node.auth_mounts.teardown_workspace_auth_overlay", teardown),
        capture_logs() as logs,
    ):
        result = await run_terminal_workspace_gc(
            session_factory,
            work_dir=work_dir,
            min_age_hours=24,
            execute=True,
            now=now,
        )

    # Loud, not silent: the run reports ``partial`` with the reason code, and the
    # auth dir is preserved rather than stranding a live mount.
    assert result.status == "partial"
    assert result.reason_code == "CLEANUP_EXECUTION_PARTIAL"
    assert any(
        error.kind == "auth_overlay_unmount"
        and error.reason_code == "CLAUDE_AUTH_OVERLAY_UNMOUNT_FAILED"
        for error in result.delete_errors
    )
    auth_outcomes = [outcome for outcome in result.path_outcomes if outcome.kind == "auth"]
    assert [outcome.status for outcome in auth_outcomes] == ["skipped"]
    assert auth.exists()
    # The worktree path is unaffected and still reclaimed.
    assert not (work_dir / "git" / "worktrees" / workspace_id).exists()
    # The failure is logged with its reason code and the kernel stderr rather than
    # reduced to a bare return code.
    assert any(
        entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_UNMOUNT_FAILED"
        and entry.get("workspace_id") == workspace_id
        and entry.get("stderr") == kernel_reason
        for entry in logs
    )


@pytest.mark.unit
async def test_service_gc_fails_loudly_when_overlay_unmount_unverifiable(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # A capability-less GC process (CLI/API container) that cannot verify the
    # worker released the overlay must fail loudly and preserve the auth dir,
    # never report a no-op as success.
    from awf.node.auth_mounts import OverlayUnmountUnverifiableError

    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="f" * 40,
    )
    auth = work_dir / "auth" / workspace_id
    _write(auth / "claude" / "upper" / "settings.json", "{}")

    teardown = Mock(
        side_effect=OverlayUnmountUnverifiableError(
            reason_code="CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE",
            message="no CAP_SYS_ADMIN",
        )
    )

    with patch("awf.node.auth_mounts.teardown_workspace_auth_overlay", teardown):
        result = await run_terminal_workspace_gc(
            session_factory,
            work_dir=work_dir,
            min_age_hours=24,
            execute=True,
            now=now,
        )

    assert result.status == "partial"
    assert any(
        error.kind == "auth_overlay_unmount"
        and error.reason_code == "CLAUDE_AUTH_OVERLAY_UNMOUNT_INCAPABLE"
        for error in result.delete_errors
    )
    auth_outcomes = [outcome for outcome in result.path_outcomes if outcome.kind == "auth"]
    assert [outcome.status for outcome in auth_outcomes] == ["skipped"]
    assert auth.exists()


@pytest.mark.unit
async def test_service_gc_skips_auth_dir_after_unmount_failure_in_worktree_fail_branch(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # When worktree removal also fails, the auth dir must still be skipped (not
    # rmtree'd) after an overlay unmount failure, covering the worktree-fail
    # delete branch's auth guard.
    work_dir = tmp_path / "service"
    now = datetime(2026, 4, 26, 12, tzinfo=UTC)
    workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=200),
        pr=True,
        pr_merge_sha="9" * 40,
    )
    auth = work_dir / "auth" / workspace_id
    _write(auth / "claude" / "merged" / "settings.json", "{}")
    _write(work_dir / "compose" / workspace_id / "compose.yml", "compose")

    teardown = Mock(side_effect=subprocess.CalledProcessError(32, "umount"))

    async def _worktree_remove(_candidate: object) -> WorkspaceGCWorktreeRemoveResult:
        return WorkspaceGCWorktreeRemoveResult(
            status="failed",
            reason_code="WORKTREE_REMOVE_FAILED",
            error="git worktree remove failed",
        )

    with patch("awf.node.auth_mounts.teardown_workspace_auth_overlay", teardown):
        result = await run_terminal_workspace_gc(
            session_factory,
            work_dir=work_dir,
            min_age_hours=24,
            execute=True,
            now=now,
            worktree_remover=_worktree_remove,
        )

    assert result.status == "partial"
    auth_outcomes = [outcome for outcome in result.path_outcomes if outcome.kind == "auth"]
    assert [outcome.status for outcome in auth_outcomes] == ["skipped"]
    assert auth.exists()
    # Compose is not auth and still gets removed in the worktree-fail branch.
    assert not (work_dir / "compose" / workspace_id).exists()
