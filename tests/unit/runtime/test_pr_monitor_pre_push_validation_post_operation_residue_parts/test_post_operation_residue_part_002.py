"""Post-operation residue cleanup regressions for PR monitor pre-push validation."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import pre_push_validation as pre_push_validation_module
from awf.runtime.pr_monitor_runner.pre_push_validation_dirty_finalize import (
    _cleanup_proven_post_operation_residue_paths,
    _content_provable_as_git_oneline_capture,
    _dirty_paths_provable_as_post_operation_residue,
    _empty_oneline_path_provable_as_cli_capture,
    _is_git_cli_flag_capture_path,
    _path_exists_at_head,
    _path_is_symlink_in_index,
    _path_provable_as_git_cli_flag_capture,
    _read_residue_path_content,
    _try_cleanup_pre_push_post_operation_residue,
    _worktree_unstaged_change_paths,
)
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_STATUS_FAILED,
    ValidationWorktreeCheck,
)
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._post_operation_residue_test_helpers import (
    queue_post_operation_residue_proof_commands,
    queue_residue_cleanup_anchor_and_delta,
    queue_snapshot_residue_reproof_commands,
    seed_oneline_capture_residue,
)
from tests.unit.runtime._pre_push_validation_helpers import (
    _FakeValidation,
    _mark_git_worktree,
    _name_status_z,
    _set_resolved_profile,
    _validation_result,
)


@pytest.mark.unit
async def test_pre_push_validation_post_operation_residue_cleanup_fails_closed_when_head_advances_after_pin(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Residue cleanup must fail closed when HEAD advances after the pinned anchor.

    Regression for review thread ``PRRT_kwDOSJAM6s6NhEAw``: capturing cleanup
    HEAD only after the owned-delta/residue proofs let a concurrent local commit
    become the cleanup anchor without an owned-delta check.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    pinned_head = "a" * 40
    advanced_head = "b" * 40
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("--oneline",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{pinned_head}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # finalize gate
    queue_residue_cleanup_anchor_and_delta(
        cmd,
        head_sha=pinned_head,
        owned_delta_z=_name_status_z("M\0src/fix.py\0"),
    )
    queue_post_operation_residue_proof_commands(cmd, worktree=worktree)
    cmd.queue_result(returncode=0, stdout=f"{advanced_head}\n")  # pre-cleanup HEAD verify: moved
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=MonitorState(),
        operation_start_head="0" * 40,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    commit_dirty.assert_not_awaited()
    assert validation.calls == []
    cleanup.assert_not_awaited()
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("", False),
        ("\n", False),
        ("\n\n", False),
        ("  \n  ", False),
        ("abcdef0 Fix something\n1234567 Another commit\n", True),
        ("abcdef0 Fix something\n\n", False),
        ("abcdef0 Fix something\n\n1234567 Another commit\n", False),
        ('{"fixture": true}\n', False),
        ("not a git log line\n", False),
        ("\x85", False),
    ],
)
def test_content_provable_as_git_oneline_capture(content: str, expected: bool) -> None:
    """Residue proof requires git-log-shaped content; blank-only lines must fail closed."""
    assert _content_provable_as_git_oneline_capture(content) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("--oneline", True),
        ("apps/console/--oneline", False),
        ("tests/fixtures/--oneline", False),
    ],
)
def test_empty_oneline_path_provable_as_cli_capture_only_at_repo_root(
    path: str,
    expected: bool,
) -> None:
    """Empty ``--oneline`` residue proof is limited to the repo-root artifact (PRRT_kwDOSJAM6s6NhRVJ)."""
    assert _empty_oneline_path_provable_as_cli_capture(path) is expected


@pytest.mark.unit
async def test_path_provable_as_git_cli_flag_capture_empty_requires_repo_root(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Empty subdirectory ``--oneline`` files must not pass CLI residue proof."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    fixture_path = "tests/fixtures/--oneline"
    seed_oneline_capture_residue(worktree, fixture_path, content="")
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert (
        await _path_provable_as_git_cli_flag_capture(
            runner,
            worktree_path=worktree,
            path=fixture_path,
        )
        is False
    )
    root_path = "--oneline"
    seed_oneline_capture_residue(worktree, root_path, content="")
    assert (
        await _path_provable_as_git_cli_flag_capture(
            runner,
            worktree_path=worktree,
            path=root_path,
        )
        is True
    )


@pytest.mark.unit
async def test_path_provable_as_git_cli_flag_capture_non_empty_requires_repo_root(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Subdirectory ``--oneline`` files with log-shaped content must not pass CLI residue proof."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    fixture_path = "tests/fixtures/--oneline"
    seed_oneline_capture_residue(
        worktree,
        fixture_path,
        content="abcdef0 Fix something\n1234567 Another commit\n",
    )
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert (
        await _path_provable_as_git_cli_flag_capture(
            runner,
            worktree_path=worktree,
            path=fixture_path,
        )
        is False
    )
    root_path = "--oneline"
    seed_oneline_capture_residue(
        worktree,
        root_path,
        content="abcdef0 Fix something\n1234567 Another commit\n",
    )
    assert (
        await _path_provable_as_git_cli_flag_capture(
            runner,
            worktree_path=worktree,
            path=root_path,
        )
        is True
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("--oneline", True),
        ("apps/console/--oneline", True),
        ("fixtures/--help", False),
        ("docs/--example", False),
        ("--old", False),
    ],
)
def test_is_git_cli_flag_capture_path_whitelists_known_artifacts_only(
    path: str,
    expected: bool,
) -> None:
    """Only observed CLI-capture basenames are provable residue (PRRT_kwDOSJAM6s6NgvbL)."""
    assert _is_git_cli_flag_capture_path(path) is expected


@pytest.mark.unit
async def test_pre_push_validation_post_operation_residue_skips_legitimate_flag_shaped_path(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Legitimate ``fixtures/--help`` repair output must not be deleted as CLI residue.

    A repair that committed ``src/fix.py`` but left ``fixtures/--help`` uncommitted
    is disjoint from the committed delta, absent at HEAD, and has no unstaged edits.
    Residue proof must fail closed instead of restoring/deleting the fixture
    (review thread ``PRRT_kwDOSJAM6s6NgvbL``).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    head_sha = "a" * 40
    fixture_path = "fixtures/--help"
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=(fixture_path,),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # finalize gate
    queue_residue_cleanup_anchor_and_delta(
        cmd,
        head_sha=head_sha,
        owned_delta_z=_name_status_z("M\0src/fix.py\0"),
    )
    cmd.queue_result(returncode=0, stdout="")  # unstaged delta: staged-only fixture
    cmd.queue_result(returncode=128, stdout="")  # cat-file: fixture absent at HEAD
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=MonitorState(),
        operation_start_head="0" * 40,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    commit_dirty.assert_not_awaited()
    assert validation.calls == []
    cleanup.assert_not_awaited()
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_post_operation_residue_skips_legitimate_oneline_fixture_path(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Legitimate ``tests/fixtures/--oneline`` repair output must not be deleted.

    A repair that committed ``src/fix.py`` but left ``tests/fixtures/--oneline``
    uncommitted is disjoint from the committed delta, absent at HEAD, and has no
    unstaged edits. Basename whitelisting alone would treat it as CLI residue;
    content proof must fail closed instead (review thread
    ``PRRT_kwDOSJAM6s6Ng6Bh``).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    head_sha = "a" * 40
    fixture_path = "tests/fixtures/--oneline"
    seed_oneline_capture_residue(
        worktree,
        fixture_path,
        content='{"fixture": true}\n',
    )
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=(fixture_path,),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # finalize gate
    queue_residue_cleanup_anchor_and_delta(
        cmd,
        head_sha=head_sha,
        owned_delta_z=_name_status_z("M\0src/fix.py\0"),
    )
    cmd.queue_result(returncode=0, stdout="")  # unstaged delta: staged-only fixture
    cmd.queue_result(returncode=128, stdout="")  # cat-file: fixture absent at HEAD
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=MonitorState(),
        operation_start_head="0" * 40,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    commit_dirty.assert_not_awaited()
    assert validation.calls == []
    cleanup.assert_not_awaited()
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_post_operation_residue_skips_oneline_shaped_legitimate_fixture_path(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Log-shaped ``tests/fixtures/--oneline`` repair output must not be deleted as CLI residue."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    head_sha = "a" * 40
    fixture_path = "tests/fixtures/--oneline"
    seed_oneline_capture_residue(
        worktree,
        fixture_path,
        content="abcdef0 Fix something\n1234567 Another commit\n",
    )
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=(fixture_path,),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # finalize gate
    queue_residue_cleanup_anchor_and_delta(
        cmd,
        head_sha=head_sha,
        owned_delta_z=_name_status_z("M\0src/fix.py\0"),
    )
    cmd.queue_result(returncode=0, stdout="")  # unstaged delta: staged-only fixture
    cmd.queue_result(returncode=128, stdout="")  # cat-file: fixture absent at HEAD
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=MonitorState(),
        operation_start_head="0" * 40,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    commit_dirty.assert_not_awaited()
    assert validation.calls == []
    cleanup.assert_not_awaited()
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_post_operation_residue_skips_empty_legitimate_oneline_fixture_path(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Empty ``tests/fixtures/--oneline`` repair output must not be deleted as CLI residue.

    A repair that committed ``src/fix.py`` but left an empty
    ``tests/fixtures/--oneline`` file uncommitted is disjoint from the committed
    delta. Emptiness alone must not prove CLI capture (review thread
    ``PRRT_kwDOSJAM6s6NhRVJ``).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    head_sha = "a" * 40
    fixture_path = "tests/fixtures/--oneline"
    seed_oneline_capture_residue(worktree, fixture_path, content="")
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=(fixture_path,),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # finalize gate
    queue_residue_cleanup_anchor_and_delta(
        cmd,
        head_sha=head_sha,
        owned_delta_z=_name_status_z("M\0src/fix.py\0"),
    )
    cmd.queue_result(returncode=0, stdout="")  # unstaged delta: staged-only fixture
    cmd.queue_result(returncode=128, stdout="")  # cat-file: fixture absent at HEAD
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=MonitorState(),
        operation_start_head="0" * 40,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    commit_dirty.assert_not_awaited()
    assert validation.calls == []
    cleanup.assert_not_awaited()
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_post_operation_residue_skips_whitespace_only_legitimate_oneline_fixture_path(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Whitespace-only ``tests/fixtures/--oneline`` repair output must not be deleted.

    Newline-only staged fixture bytes are non-empty but ``splitlines()`` yields
    no lines, so content proof must fail closed instead of treating them as
    git log capture (review thread ``PRRT_kwDOSJAM6s6NiUD7``).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    head_sha = "a" * 40
    fixture_path = "tests/fixtures/--oneline"
    seed_oneline_capture_residue(worktree, fixture_path, content="\n\n")
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=(fixture_path,),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # finalize gate
    queue_residue_cleanup_anchor_and_delta(
        cmd,
        head_sha=head_sha,
        owned_delta_z=_name_status_z("M\0src/fix.py\0"),
    )
    cmd.queue_result(returncode=0, stdout="")  # unstaged delta: staged-only fixture
    cmd.queue_result(returncode=128, stdout="")  # cat-file: fixture absent at HEAD
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=MonitorState(),
        operation_start_head="0" * 40,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    commit_dirty.assert_not_awaited()
    assert validation.calls == []
    cleanup.assert_not_awaited()
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_read_residue_path_content_returns_none_when_worktree_read_raises_oserror(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Unreadable on-disk residue must fail closed instead of treating bytes as proof."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    residue_path = worktree / "--oneline"
    residue_path.write_text("deadbeef residue\n", encoding="utf-8")
    original_read_text = Path.read_text

    def _read_text_raises_for_residue(self: Path, *args: object, **kwargs: object) -> str:
        if self == residue_path:
            raise OSError("permission denied")
        return original_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _read_text_raises_for_residue)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    content = await _read_residue_path_content(
        runner,
        worktree_path=worktree,
        path="--oneline",
    )

    assert content is None


@pytest.mark.unit
@pytest.mark.skipif(os.name != "posix", reason="symlink semantics are POSIX-specific")
async def test_read_residue_path_content_rejects_symlink_to_log_shaped_target(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Symlink residue must fail closed even when the target looks like CLI capture."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    target = worktree / "capture.txt"
    target.write_text("deadbeef accidental log line\n", encoding="utf-8")
    (worktree / "--oneline").symlink_to(target)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    content = await _read_residue_path_content(
        runner,
        worktree_path=worktree,
        path="--oneline",
    )

    assert content is None
    assert (
        await _path_provable_as_git_cli_flag_capture(
            runner,
            worktree_path=worktree,
            path="--oneline",
        )
        is False
    )


@pytest.mark.unit
async def test_read_residue_path_content_rejects_staged_index_symlink(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Staged index symlinks must fail closed before ``git show`` content proof."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    cmd = FakeCommandRunner()
    cmd.queue_result(
        returncode=0,
        stdout="120000 deadbeefdeadbeefdeadbeefdeadbeefdeadbeef 0\t--oneline\0",
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    assert (
        await _path_provable_as_git_cli_flag_capture(
            runner,
            worktree_path=worktree,
            path="--oneline",
        )
        is False
    )
    assert [call.args[call.args.index("-C") + 2 :] for call in cmd.calls] == [
        ["--literal-pathspecs", "ls-files", "--stage", "-z", "--", "--oneline"],
    ]


@pytest.mark.unit
async def test_path_is_symlink_in_index_fail_closed_when_ls_files_fails(
    tmp_path: Path,
) -> None:
    """``git ls-files --stage`` failures must fail closed before symlink proof."""
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stderr="fatal: not a git repository")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=cmd))

    assert (
        await _path_is_symlink_in_index(
            runner,
            worktree_path=tmp_path,
            path="--oneline",
        )
        is None
    )


@pytest.mark.unit
async def test_read_residue_path_content_fails_closed_when_index_mode_unavailable(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """When ``git ls-files --stage`` fails, residue proof must fail closed."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stderr="fatal: not a git repository")
    cmd.queue_result(returncode=128, stderr="fatal: not a git repository")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    assert (
        await _read_residue_path_content(
            runner,
            worktree_path=worktree,
            path="--oneline",
        )
        is None
    )
    assert (
        await _path_provable_as_git_cli_flag_capture(
            runner,
            worktree_path=worktree,
            path="--oneline",
        )
        is False
    )
    assert [call.args[call.args.index("-C") + 2 :] for call in cmd.calls] == [
        ["--literal-pathspecs", "ls-files", "--stage", "-z", "--", "--oneline"],
        ["--literal-pathspecs", "ls-files", "--stage", "-z", "--", "--oneline"],
    ]


@pytest.mark.unit
async def test_read_residue_path_content_reads_staged_index_when_file_missing_on_disk(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Staged-only residue must be proven via ``git show :path``, not a missing worktree file."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    cmd = FakeCommandRunner()
    cmd.queue_result(
        returncode=0,
        stdout="100644 deadbeefdeadbeefdeadbeefdeadbeefdeadbeef 0\t--oneline\0",
    )
    cmd.queue_result(returncode=0, stdout="deadbeef staged residue\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    content = await _read_residue_path_content(
        runner,
        worktree_path=worktree,
        path="--oneline",
    )

    assert content == "deadbeef staged residue\n"
    assert (
        await _path_provable_as_git_cli_flag_capture(
            runner,
            worktree_path=worktree,
            path="--oneline",
        )
        is True
    )


@pytest.mark.unit
async def test_read_residue_path_content_returns_none_when_git_show_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Failed index reads must not be treated as provable CLI residue."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=1, stdout="", stderr="fatal: path not in index\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    content = await _read_residue_path_content(
        runner,
        worktree_path=worktree,
        path="--oneline",
    )

    assert content is None


@pytest.mark.unit
async def test_path_provable_as_git_cli_flag_capture_returns_false_when_content_unreadable(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Unreadable staged residue must not pass CLI flag-capture proof."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=1, stdout="", stderr="fatal: path not in index\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    assert (
        await _path_provable_as_git_cli_flag_capture(
            runner,
            worktree_path=worktree,
            path="--oneline",
        )
        is False
    )


@pytest.mark.unit
async def test_worktree_unstaged_change_paths_returns_none_when_diff_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Residue proof must fail closed when unstaged delta cannot be resolved."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stdout="", stderr="diff failed\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    assert await _worktree_unstaged_change_paths(runner, worktree_path=worktree) is None


@pytest.mark.unit
async def test_worktree_unstaged_change_paths_returns_none_on_malformed_output(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Malformed unstaged name-status output must not unlock residue cleanup."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="M src/fix.py\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    assert await _worktree_unstaged_change_paths(runner, worktree_path=worktree) is None


@pytest.mark.unit
async def test_path_exists_at_head_returns_true_when_tracked(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Tracked modifications are never provable post-operation residue."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    assert await _path_exists_at_head(runner, worktree_path=worktree, path="--oneline") is True


@pytest.mark.unit
async def test_path_exists_at_head_returns_none_on_unexpected_returncode(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Unexpected ``cat-file`` failures must not be treated as absent paths."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=2, stdout="", stderr="fatal: ambiguous argument\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    assert await _path_exists_at_head(runner, worktree_path=worktree, path="--oneline") is None


@pytest.mark.unit
async def test_dirty_paths_provable_fails_closed_when_unstaged_delta_unavailable(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Missing unstaged delta evidence must block residue cleanup."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stdout="", stderr="diff failed\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    assert (
        await _dirty_paths_provable_as_post_operation_residue(
            runner,
            worktree_path=worktree,
            dirty_paths={"--oneline"},
        )
        is False
    )


@pytest.mark.unit
async def test_dirty_paths_provable_fails_closed_when_path_tracked_at_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A tracked ``--oneline`` edit must not be deleted as CLI residue."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    assert (
        await _dirty_paths_provable_as_post_operation_residue(
            runner,
            worktree_path=worktree,
            dirty_paths={"--oneline"},
        )
        is False
    )


@pytest.mark.unit
async def test_dirty_paths_provable_fails_closed_when_head_check_unavailable(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Unknown HEAD membership must block residue cleanup."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=2, stdout="", stderr="fatal: ambiguous argument\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    assert (
        await _dirty_paths_provable_as_post_operation_residue(
            runner,
            worktree_path=worktree,
            dirty_paths={"--oneline"},
        )
        is False
    )


@pytest.mark.unit
async def test_try_cleanup_post_operation_residue_returns_none_when_dirty_paths_empty(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A dirty check without paths must not attempt residue cleanup."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    head_sha = "b" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # pinned HEAD before owned delta
    cmd.queue_result(
        returncode=0, stdout=_name_status_z("M\0src/fix.py\0")
    )  # non-empty owned delta
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )

    result = await _try_cleanup_pre_push_post_operation_residue(
        runner,
        workspace_id="ws-empty-dirty",
        worktree_path=worktree,
        check=dirty_check,
        operation_start_head="a" * 40,
    )

    assert result is None


@pytest.mark.unit
async def test_cleanup_proven_post_operation_residue_paths_fails_on_status_failed_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Residue cleanup must fail closed when the pre-cleanup snapshot cannot be read."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    status_failed = ValidationWorktreeCheck(
        clean=False,
        reason_code=VALIDATION_WORKTREE_STATUS_FAILED,
        message="status failed",
    )
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        AsyncMock(return_value=status_failed),
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    cleanup = await _cleanup_proven_post_operation_residue_paths(
        runner,
        worktree_path=worktree,
        restore_ref="a" * 40,
        proven_paths={"--oneline"},
    )

    assert cleanup.ok is False
    assert cleanup.reason_code == VALIDATION_WORKTREE_STATUS_FAILED


@pytest.mark.unit
async def test_cleanup_proven_post_operation_residue_paths_fails_when_snapshot_content_unprovable(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Residue cleanup must re-validate snapshot content, not only path names.

    Regression for review thread ``PRRT_kwDOSJAM6s6Nlpit``: another process can
    rewrite the same dirty path after residue proof but before cleanup; matching
    path names alone must not authorize restore/clean of the new unproven edit.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    seed_oneline_capture_residue(worktree, "--oneline", content="unrelated repair edit\n")
    snapshot = ValidationWorktreeCheck(
        clean=False,
        paths=("--oneline",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        AsyncMock(return_value=snapshot),
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # unstaged delta: staged-only residue
    cmd.queue_result(returncode=128, stdout="")  # cat-file: path absent at HEAD
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    cleanup = await _cleanup_proven_post_operation_residue_paths(
        runner,
        worktree_path=worktree,
        restore_ref="a" * 40,
        proven_paths={"--oneline"},
    )

    assert cleanup.ok is False
    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert "content or staging state" in (cleanup.message or "")


@pytest.mark.unit
async def test_cleanup_proven_post_operation_residue_paths_fails_when_snapshot_has_unstaged_edits(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Residue cleanup must fail when same-path snapshot gains unstaged edits."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    seed_oneline_capture_residue(worktree, "--oneline", content="")
    snapshot = ValidationWorktreeCheck(
        clean=False,
        paths=("--oneline",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        AsyncMock(return_value=snapshot),
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="M\0--oneline\0")  # unstaged delta on proven path
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path,
    )

    cleanup = await _cleanup_proven_post_operation_residue_paths(
        runner,
        worktree_path=worktree,
        restore_ref="a" * 40,
        proven_paths={"--oneline"},
    )

    assert cleanup.ok is False
    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert "content or staging state" in (cleanup.message or "")


@pytest.mark.unit
async def test_pre_push_validation_post_operation_residue_cleanup_fails_when_owned_delta_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Residue cleanup must fail closed when the pinned committed delta cannot be resolved."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    head_sha = "a" * 40
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("--oneline",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # finalize gate
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # pinned cleanup anchor HEAD
    cmd.queue_result(returncode=1, stdout="", stderr="bad object\n")  # owned delta unavailable
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=MonitorState(),
        operation_start_head="0" * 40,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    cleanup.assert_not_awaited()
    assert validation.calls == []


@pytest.mark.unit
async def test_pre_push_validation_post_operation_residue_cleanup_fails_when_untracked_clean_fails(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Untracked residue cleanup must fail closed when scoped ``git clean`` fails."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    head_sha = "a" * 40
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        untracked_paths=("--oneline",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # finalize gate
    queue_residue_cleanup_anchor_and_delta(
        cmd,
        head_sha=head_sha,
        owned_delta_z=_name_status_z("M\0src/fix.py\0"),
    )
    queue_post_operation_residue_proof_commands(cmd, worktree=worktree)
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # pre-cleanup HEAD verify
    queue_snapshot_residue_reproof_commands(cmd)
    cmd.queue_result(returncode=1, stdout="", stderr="clean failed\n")  # git clean failure
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # head_after
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=MonitorState(),
        operation_start_head="0" * 40,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    cleanup.assert_not_awaited()
    assert validation.calls == []
