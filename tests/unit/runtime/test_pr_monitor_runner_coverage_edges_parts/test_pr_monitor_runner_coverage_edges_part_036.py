"""Protected-scope and repair start-head edge tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor_runner import remote_repair as pr_remote_repair
from awf.runtime.pr_monitor_runner import remote_repair_protected as pr_remote_repair_protected
from awf.runtime.pr_monitor_runner.types import (
    BaseFetchError,
    ProtectedScopeDiffError,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_protected_scope_status_check_wraps_diff_read_failures(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.owned_paths = ["src/**"]
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _raise_diff_read_failure(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("could not read protected file")

    monkeypatch.setattr(
        runner,
        "_protected_file_diffs_for_status_paths",
        _raise_diff_read_failure,
    )

    with pytest.raises(ProtectedScopeDiffError, match="Could not read dirty protected-scope"):
        await runner._protected_scope_violations_for_status(
            workspace_id=workspace_id,
            status_stdout=" M .github/workflows/ci.yml\n",
        )


@pytest.mark.unit
async def test_sync_base_protected_scope_covers_missing_and_empty_diff_edges(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    worktree = tmp_path / "worktree"

    with pytest.raises(ProtectedScopeDiffError, match="Workspace row ws_missing"):
        await runner._protected_scope_violations_for_sync_base_push(
            workspace_id="ws_missing",
            worktree_path=worktree,
            remote_branch="awf/ws_missing",
            base_branch="development",
        )

    async def _no_remote_changes(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return ("remote-base", ())

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _no_remote_changes)
    assert (
        await runner._protected_scope_violations_for_sync_base_push(
            workspace_id=workspace_id,
            worktree_path=worktree,
            remote_branch=f"awf/{workspace_id}",
            base_branch="development",
        )
        == []
    )

    async def _remote_changes(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return ("remote-base", ("src/remote.py",))

    async def _base_fetch_fails(**_kwargs: object) -> None:
        raise BaseFetchError("network reset")

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _remote_changes)
    monkeypatch.setattr(runner, "_fetch_base", _base_fetch_fails)
    with pytest.raises(ProtectedScopeDiffError, match="Could not refresh the base branch"):
        await runner._protected_scope_violations_for_sync_base_push(
            workspace_id=workspace_id,
            worktree_path=worktree,
            remote_branch=f"awf/{workspace_id}",
            base_branch="development",
        )

    async def _fetch_base_ok(**_kwargs: object) -> None:
        return None

    async def _merged_base(**_kwargs: object) -> str:
        return "merged-base"

    async def _no_base_changes(**_kwargs: object) -> tuple[str, ...]:
        return ()

    monkeypatch.setattr(runner, "_fetch_base", _fetch_base_ok)
    monkeypatch.setattr(runner, "_merge_base_with_head", _merged_base)
    monkeypatch.setattr(runner, "_changed_paths_between_ref_and_head", _no_base_changes)
    assert (
        await runner._protected_scope_violations_for_sync_base_push(
            workspace_id=workspace_id,
            worktree_path=worktree,
            remote_branch=f"awf/{workspace_id}",
            base_branch="development",
        )
        == []
    )

    async def _different_base_changes(**_kwargs: object) -> tuple[str, ...]:
        return ("src/base.py",)

    monkeypatch.setattr(runner, "_changed_paths_between_ref_and_head", _different_base_changes)
    assert (
        await runner._protected_scope_violations_for_sync_base_push(
            workspace_id=workspace_id,
            worktree_path=worktree,
            remote_branch=f"awf/{workspace_id}",
            base_branch="development",
        )
        == []
    )


@pytest.mark.unit
async def test_sync_base_protected_scope_wraps_committed_diff_read_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _remote_changes(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return ("remote-base", (".github/workflows/ci.yml",))

    async def _fetch_base_ok(**_kwargs: object) -> None:
        return None

    async def _merged_base(**_kwargs: object) -> str:
        return "merged-base"

    async def _base_changes(**_kwargs: object) -> tuple[str, ...]:
        return (".github/workflows/ci.yml",)

    async def _raise_committed_diff_read(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("show failed")

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _remote_changes)
    monkeypatch.setattr(runner, "_fetch_base", _fetch_base_ok)
    monkeypatch.setattr(runner, "_merge_base_with_head", _merged_base)
    monkeypatch.setattr(runner, "_changed_paths_between_ref_and_head", _base_changes)
    monkeypatch.setattr(
        pr_remote_repair_protected,
        "protected_file_diffs_for_committed_paths",
        _raise_committed_diff_read,
    )

    with pytest.raises(ProtectedScopeDiffError, match="sync-base protected-scope"):
        await runner._protected_scope_violations_for_sync_base_push(
            workspace_id=workspace_id,
            worktree_path=tmp_path / "worktree",
            remote_branch=f"awf/{workspace_id}",
            base_branch="development",
        )


@pytest.mark.unit
async def test_repair_operation_start_head_uses_fallback_when_worktree_missing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mirror_path = tmp_path / "mirror.git"
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    monkeypatch.setattr(
        pr_remote_repair,
        "mirror_path_for_worktree",
        lambda _worktree_path: mirror_path,
    )

    head, result = await runner._repair_operation_start_head_result(
        workspace_id="ws_missing_worktree",
        worktree_path=tmp_path / "does-not-exist",
        operation_type="review_fix",
        fallback_head_sha="f" * 40,
    )

    assert head == "f" * 40
    assert result is None
    assert len(cmd.calls) == 1
    assert cmd.calls[0].args == [
        "git",
        "--git-dir",
        str(mirror_path),
        "cat-file",
        "-e",
        f"{'f' * 40}^{{commit}}",
    ]


@pytest.mark.unit
async def test_repair_operation_start_head_uses_fallback_when_rev_parse_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    mirror_path = tmp_path / "mirror.git"
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stderr="fatal: not a git repository\n")
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    monkeypatch.setattr(
        pr_remote_repair,
        "mirror_path_for_worktree",
        lambda _worktree_path: mirror_path,
    )

    head, result = await runner._repair_operation_start_head_result(
        workspace_id="ws_bad_worktree_head",
        worktree_path=worktree,
        operation_type="sync_base",
        fallback_head_sha="b" * 40,
    )

    assert head == "b" * 40
    assert result is None
    assert len(cmd.calls) == 2
    assert cmd.calls[1].args == [
        "git",
        "--git-dir",
        str(mirror_path),
        "cat-file",
        "-e",
        f"{'b' * 40}^{{commit}}",
    ]


@pytest.mark.unit
async def test_repair_operation_start_head_uses_candidate_when_rev_parse_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    mirror_path = tmp_path / "mirror.git"
    candidate_head = "c" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stderr="fatal: not a git repository\n")
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _open_merge_candidate_head_sha(_workspace_id: str) -> str:
        return candidate_head

    monkeypatch.setattr(
        runner,
        "_open_merge_candidate_head_sha",
        _open_merge_candidate_head_sha,
    )
    monkeypatch.setattr(
        pr_remote_repair,
        "mirror_path_for_worktree",
        lambda _worktree_path: mirror_path,
    )

    head, result = await runner._repair_operation_start_head_result(
        workspace_id="ws_bad_worktree_candidate_head",
        worktree_path=worktree,
        operation_type="sync_base",
    )

    assert head == candidate_head
    assert result is None
    assert len(cmd.calls) == 2
    assert cmd.calls[1].args == [
        "git",
        "--git-dir",
        str(mirror_path),
        "cat-file",
        "-e",
        f"{candidate_head}^{{commit}}",
    ]


@pytest.mark.unit
async def test_repair_operation_start_head_accepts_mocked_no_mirror_fallback(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback_head = "e" * 40
    checked_paths: list[Path] = []
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _fallback_exists(worktree_path: Path) -> bool:
        checked_paths.append(worktree_path)
        return True

    monkeypatch.setattr(
        pr_remote_repair,
        "mirror_path_for_worktree",
        lambda _worktree_path: None,
    )
    monkeypatch.setattr(
        pr_remote_repair,
        "verify_head_object_exists",
        _fallback_exists,
    )

    worktree = tmp_path / "does-not-exist"
    head, result = await runner._repair_operation_start_head_result(
        workspace_id="ws_no_mirror_fallback",
        worktree_path=worktree,
        operation_type="comment_repair",
        fallback_head_sha=fallback_head,
    )

    assert head == fallback_head
    assert result is None
    assert checked_paths == [worktree]
    assert cmd.calls == []


@pytest.mark.unit
async def test_repair_operation_start_head_rejects_no_mirror_fallback_when_guard_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback_head = "e" * 40
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _fallback_missing(_worktree_path: Path) -> bool:
        return False

    monkeypatch.setattr(
        pr_remote_repair,
        "mirror_path_for_worktree",
        lambda _worktree_path: None,
    )
    monkeypatch.setattr(
        pr_remote_repair,
        "verify_head_object_exists",
        _fallback_missing,
    )

    head, result = await runner._repair_operation_start_head_result(
        workspace_id="ws_no_mirror_missing_fallback",
        worktree_path=tmp_path / "does-not-exist",
        operation_type="comment_repair",
        fallback_head_sha=fallback_head,
    )

    assert head == ""
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "REPAIR_START_HEAD_UNAVAILABLE"
    assert result.details["fallback_head_sha"] == fallback_head
    assert result.details["fallback_source"] == "status"
    assert cmd.calls == []


@pytest.mark.unit
async def test_repair_operation_start_head_rejects_dangling_candidate_fallback(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mirror_path = tmp_path / "mirror.git"
    candidate_head = "c" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stderr="missing commit\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _open_merge_candidate_head_sha(_workspace_id: str) -> str:
        return candidate_head

    monkeypatch.setattr(
        runner,
        "_open_merge_candidate_head_sha",
        _open_merge_candidate_head_sha,
    )
    monkeypatch.setattr(
        pr_remote_repair,
        "mirror_path_for_worktree",
        lambda _worktree_path: mirror_path,
    )

    head, result = await runner._repair_operation_start_head_result(
        workspace_id="ws_dangling_candidate",
        worktree_path=tmp_path / "does-not-exist",
        operation_type="sync_base",
    )

    assert head == ""
    assert result is not None
    assert result.failed is True
    assert result.reason_code == "REPAIR_START_HEAD_UNAVAILABLE"
    assert result.details["fallback_head_sha"] == candidate_head
    assert result.details["fallback_source"] == "candidate"
    assert cmd.calls[0].args[-3:] == [
        "cat-file",
        "-e",
        f"{candidate_head}^{{commit}}",
    ]
