"""Pre-push validation dirty-finalize post-commit committed-delta regression tests.

Split from ``test_pr_monitor_pre_push_validation_finalize_post_commit`` to keep
first-party files under the maintainability line limit enforced by
``tests/unit/test_core_decomposition_maintainability``. Covers the post-commit
committed-delta ownership re-validation: working-tree-only unowned dirt,
operation-owned rename/non-ASCII dirt, untracked-dirt fail-closed behavior, and
the no-commit-clean self-commit re-validation paths.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import pre_push_validation as pre_push_validation_module
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    ValidationWorktreeCheck,
    ValidationWorktreeCleanup,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._pre_push_validation_helpers import (
    _FakeValidation,
    _mark_git_worktree,
    _name_status_z,
    _set_resolved_profile,
    _validation_result,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a scoped async SQLAlchemy session factory for tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_pre_push_validation_finalize_ignores_working_tree_only_unowned_dirt(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Post-commit re-validation must only inspect the committed delta.

    After a successful finalize commit, the post-commit safety check compares
    the committed delta against ``owned_delta_paths``. An unrelated tracked
    edit that remains *only* in the working tree (the finalize commit did not
    add it) must not be flagged as ``PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA``,
    because the finalize commit did not sweep it into the PR. Re-using the full
    ``_operation_owned_delta_paths`` union (which includes the
    commit-vs-working-tree diff) would flag that working-tree-only dirt and
    fail-closed on a valid finalize (regression for review thread
    ``PRRT_kwDOSJAM6s6Ka0aO``).

    Note: the live working-tree delta was removed from the pre-commit ownership
    gate by ``PRRT_kwDOSJAM6s6KbbE6`` (it over-broadened ownership to every
    tracked working-tree difference). The dirty path here is therefore owned
    via the committed delta, and the unrelated working-tree-only path is simply
    never consulted by the gate — the post-commit committed-delta-only
    re-validation remains the load-bearing guard against unowned commits.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    clean_check = ValidationWorktreeCheck(clean=True)
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, clean_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock(
        return_value=ValidationWorktreeCleanup(
            cleaned=False,
            check=clean_check,
            restore_ref="b" * 40,
        )
    )
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Pre-commit ownership gate (committed delta only; the staged delta is NOT
    # consulted — removed for PRRT_kwDOSJAM6s6KdVXx; the live working-tree
    # delta is NOT consulted — removed for PRRT_kwDOSJAM6s6KbbE6): the dirty
    # path ``src/fix.py`` is operation-owned via the committed delta.
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # committed delta
    # Post-commit re-validation inspects ONLY the committed delta. The finalize
    # commit added the operation-owned ``src/fix.py`` and did NOT commit the
    # unrelated working-tree-only ``unrelated/extra.py``, so the committed delta
    # carries only the owned path and the finalize must proceed to the verify
    # recheck (which observes a clean tree). The unrelated working-tree-only
    # path must not be flagged as ``PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA``.
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # committed delta
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # re-captured HEAD after finalize
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is True
    assert result.workspace_head_sha == "b" * 40
    commit_dirty.assert_awaited_once_with(
        workspace_id=workspace_id,
        message=f"awf: finalize PR monitor repair for {workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        protected_scope_revert_remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
    )
    cleanup.assert_awaited_once()
    assert check_worktree_clean.await_count == 2


@pytest.mark.unit
async def test_pre_push_validation_finalize_commits_operation_owned_rename_source_dirt(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Operation-owned rename source dirt must be finalized, not stranded as pre-existing dirty.

    When a repair leaves a committed rename (e.g. ``git add -A`` and
    ``git commit`` both succeeded, moving ``oldname.txt`` to ``newname.txt``
    since ``operation_start_head``), ``check_validation_worktree_clean`` parses
    ``git status --porcelain`` and ``changed_paths_from_porcelain`` yields
    *both* the rename source (``oldname.txt``) and destination
    (``newname.txt``). ``_operation_owned_delta_paths`` must build the
    operation-owned set from ``git diff --name-status -z`` (which emits both
    names for ``R``/``C`` records and never C-quotes paths under ``-z``), not
    from raw ``git diff --name-only`` (which only yields the destination). If
    the owned set omits the rename source, ``unrelated_dirty = dirty_paths -
    owned_delta_paths`` treats the operation's own rename source as unrelated
    dirt and the finalize fails as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``
    instead of finalizing the operation's own rename (review thread
    ``PRRT_kwDOSJAM6s6KaAWk``).

    Note: the rename was previously exercised via the staged delta, but the
    staged delta was removed for ``PRRT_kwDOSJAM6s6KdVXx`` (it over-broadened
    ownership to whatever is in the live index at finalization time). The
    committed delta carries the same ``--name-status -z`` rename record, so
    the KaAWk path-representation concern is still covered here.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    # Porcelain reports the rename as ``R  oldname.txt -> newname.txt``, and
    # ``changed_paths_from_porcelain`` yields both names.
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("oldname.txt", "newname.txt"),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    clean_check = ValidationWorktreeCheck(clean=True)
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, clean_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock(
        return_value=ValidationWorktreeCleanup(
            cleaned=False,
            check=clean_check,
            restore_ref="b" * 40,
        )
    )
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Pre-commit ownership gate (committed delta only): the operation
    # committed the rename since ``operation_start_head``, so the committed
    # delta carries the rename record, which ``--name-status -z`` emits as both
    # the source and destination. The staged delta is NOT consulted (removed
    # for PRRT_kwDOSJAM6s6KdVXx); the live working-tree delta is NOT consulted
    # (removed for PRRT_kwDOSJAM6s6KbbE6).
    cmd.queue_result(
        returncode=0,
        stdout=_name_status_z("R100\0oldname.txt\0newname.txt\0"),
    )
    # Post-commit re-validation inspects ONLY the committed delta: the rename
    # is still confined to the operation-owned set.
    cmd.queue_result(
        returncode=0,
        stdout=_name_status_z("R100\0oldname.txt\0newname.txt\0"),
    )
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # re-captured HEAD after finalize
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is True
    assert result.workspace_head_sha == "b" * 40
    commit_dirty.assert_awaited_once_with(
        workspace_id=workspace_id,
        message=f"awf: finalize PR monitor repair for {workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        protected_scope_revert_remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
    )
    cleanup.assert_awaited_once()


@pytest.mark.unit
async def test_pre_push_validation_finalize_commits_operation_owned_non_ascii_dirt(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Operation-owned non-ASCII dirt must be finalized, not stranded as pre-existing dirty.

    With ``core.quotepath=true`` (the git default), ``git status --porcelain``
    C-quotes non-ASCII paths, but ``changed_paths_from_porcelain`` already
    unquotes them via ``unquote_porcelain_path``, so the dirty set carries
    the decoded path (``caf\\u00e9.txt``). ``git diff --name-only`` also
    C-quotes non-ASCII paths, and the raw line parsing in
    ``_operation_owned_delta_paths`` did not unquote them, so the owned set
    held the C-quoted form and never matched the decoded dirty path — the
    operation's own non-ASCII dirt was stranded as
    ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``. ``git diff --name-status -z``
    never C-quotes paths (the ``-z`` form always emits raw bytes), so parsing
    that output yields the same decoded path representation the dirty check
    uses (review thread ``PRRT_kwDOSJAM6s6KaAWk``).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    # The porcelain parser already decoded the C-quoted ``"caf\\303\\251.txt"``
    # form, so the dirty set carries the decoded UTF-8 path.
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("caf\u00e9.txt",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    clean_check = ValidationWorktreeCheck(clean=True)
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, clean_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock(
        return_value=ValidationWorktreeCleanup(
            cleaned=False,
            check=clean_check,
            restore_ref="b" * 40,
        )
    )
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Pre-commit ownership gate (committed delta only): the operation committed
    # the non-ASCII path since ``operation_start_head``, so the committed
    # delta carries it as raw UTF-8 bytes (``--name-status -z`` never
    # C-quotes paths). The staged delta is NOT consulted (removed for
    # PRRT_kwDOSJAM6s6KdVXx); the live working-tree delta is NOT consulted
    # (removed for PRRT_kwDOSJAM6s6KbbE6).
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0caf\u00e9.txt\0"))
    # Post-commit re-validation inspects ONLY the committed delta: the
    # non-ASCII path is still confined to the operation-owned set.
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0caf\u00e9.txt\0"))
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # re-captured HEAD after finalize
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is True
    assert result.workspace_head_sha == "b" * 40
    commit_dirty.assert_awaited_once_with(
        workspace_id=workspace_id,
        message=f"awf: finalize PR monitor repair for {workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        protected_scope_revert_remote_branch=f"awf/{workspace_id}",
        remote_push_url=None,
    )
    cleanup.assert_awaited_once()


@pytest.mark.unit
async def test_pre_push_validation_finalize_strands_operation_owned_untracked_dirt_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Operation-owned purely untracked dirt strands fail-closed (deferred).

    ``git diff --name-status -z`` (committed, staged, and working-tree) cannot
    see a purely untracked path: the agent created the file but ``git add -A``
    never reached it, so it is not staged, not committed, and absent from the
    commit-vs-working-tree diff. The pre-push cleanliness check uses
    ``git status --porcelain``, which DOES list untracked files, so the dirty
    set carries the path while the operation-owned delta computed from diffs is
    empty.

    ``PRRT_kwDOSJAM6s6Ka0aK`` previously recovered this case by folding
    ``check.untracked_paths`` into ``owned_delta_paths``. But the repair-start
    dirty guard only proves the worktree was clean at ``operation_start_head``
    at repair *start*; ``check.untracked_paths`` is computed at pre-push
    validation time, which is later. A failed cleanup or another local process
    can create an untracked file in that window, and the fold-in treated it as
    operation-owned solely because it was untracked — ``_commit_dirty_worktree``
    then staged it via ``git add -A`` and the post-commit re-validation saw it as
    committed and confined, silently sweeping the unrelated untracked file into
    the PR instead of failing closed (review thread
    ``PRRT_kwDOSJAM6s6KcSj``). The untracked fold-in was therefore removed and
    this recovery now strands the operation's own purely-untracked repair
    output as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` — fail-closed and
    visible to a human, rather than a silent sweep of unrelated dirt. Restoring
    this recovery without the over-broadening requires capturing only the
    operation's attempted untracked paths and threading them to the gate;
    tracked as a deferred follow-up (see
    ``plans/PRRT_kwDOSJAM6s6KcSj_PLAN.md``).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    # Porcelain reports the purely untracked file as ``?? src/fix.py``; it is
    # not agent-runtime-ignored so it stays in both ``paths`` and
    # ``untracked_paths``.
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        untracked_paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Pre-commit ownership gate: the committed and staged deltas are empty
    # because the path is purely untracked — not committed, not staged. The
    # live working-tree delta is NOT consulted (removed for
    # PRRT_kwDOSJAM6s6KbbE6). The untracked fold-in is NOT applied (removed for
    # PRRT_kwDOSJAM6s6KcSj), so the purely-untracked path is treated as
    # unrelated and the finalize skips — fail-closed, not a silent sweep.
    cmd.queue_result(returncode=0, stdout="")  # committed delta
    cmd.queue_result(returncode=0, stdout="")  # staged delta
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    # The operation-owned purely-untracked repair output strands fail-closed
    # (deferred recovery) — the commit sink must not run.
    commit_dirty.assert_not_awaited()
    # Validation must never run on a dirty worktree.
    assert validation.calls == []
    # The dirty check is not re-run after a skipped finalize (no verify pass).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_excludes_agent_runtime_untracked_dirt(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Agent-runtime untracked dirt must stay fail-closed, not be swept into the PR.

    ``check_validation_worktree_clean`` suppresses AWF-agent-runtime artifacts
    (``.claude/agent-memory/``) unconditionally, so an untracked memory file
    never appears in ``check.paths`` nor ``check.untracked_paths``. Folding
    ``check.untracked_paths`` into the owned set must not re-introduce those
    artifacts (they are already absent), and a purely untracked memory file
    must stay fail-closed as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` rather
    than be committed into the PR (review thread ``PRRT_kwDOSJAM6s6Ka0aK``).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    # The dirty check suppresses the agent-runtime untracked file, so neither
    # ``paths`` nor ``untracked_paths`` carry it; the gate has nothing to own
    # and must stay fail-closed.
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=(".claude/agent-memory/reviewer.json",),
        untracked_paths=(),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # The committed and staged deltas are empty (the path is purely untracked)
    # AND ``check.untracked_paths`` is empty (the dirty check suppressed it),
    # so the owned set is empty and the finalize must skip and fail closed —
    # the agent-runtime artifact must never be committed into the PR. The live
    # working-tree delta is NOT consulted (removed for PRRT_kwDOSJAM6s6KbbE6).
    cmd.queue_result(returncode=0, stdout="")  # committed delta
    cmd.queue_result(returncode=0, stdout="")  # staged delta
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    # The agent-runtime artifact must not be committed into the PR.
    commit_dirty.assert_not_awaited()
    assert validation.calls == []
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_skips_unrelated_untracked_dirt(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Unrelated purely-untracked dirt must not be swept into the PR.

    The repair-start dirty guard
    (``_pre_existing_dirty_repair_worktree_result``) proves the worktree was
    clean at ``operation_start_head`` at repair *start*, but the pre-push
    cleanliness check (``check_validation_worktree_clean``) computes
    ``check.untracked_paths`` at pre-push validation time, which is later. A
    failed cleanup or another local process can create an untracked file in
    that window; it is NOT a path the operation captured or attempted (it is
    neither committed nor staged).

    ``PRRT_kwDOSJAM6s6Ka0aK`` folded ``check.untracked_paths`` into
    ``owned_delta_paths`` solely because the worktree was clean at
    ``operation_start_head``, treating every current untracked path as
    operation-owned. That let ``_commit_dirty_worktree`` stage the unrelated
    untracked file via ``git add -A`` and the post-commit re-validation see it
    as committed and confined to the owned set, silently sweeping the
    unrelated file into the PR instead of failing closed (review thread
    ``PRRT_kwDOSJAM6s6KcSj``). The untracked fold-in is removed for the same
    reason the live working-tree delta was removed
    (``PRRT_kwDOSJAM6s6KbbE6``): silent over-broadening is worse than visible
    fail-closed.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    # A failed cleanup or another local process created this purely-untracked
    # file AFTER the repair-start guard, between the operation's own committed
    # edits. Porcelain reports it as ``?? unrelated/cleanup.log``; it is not
    # agent-runtime-ignored so it stays in both ``paths`` and
    # ``untracked_paths``. The operation's own edits were committed, so the
    # worktree would otherwise be clean — this untracked file is the only dirt.
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("unrelated/cleanup.log",),
        untracked_paths=("unrelated/cleanup.log",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Pre-commit ownership gate: the committed and staged deltas are empty
    # relative to ``operation_start_head`` because the unrelated untracked file
    # is purely untracked (not committed, not staged). The live working-tree
    # delta is NOT consulted (removed for PRRT_kwDOSJAM6s6KbbE6), and the
    # untracked fold-in is NOT applied (removed for PRRT_kwDOSJAM6s6KcSj), so
    # the unrelated untracked path is treated as unrelated dirt and the
    # finalize skips — fail-closed, not a silent sweep.
    cmd.queue_result(returncode=0, stdout="")  # committed delta
    cmd.queue_result(returncode=0, stdout="")  # staged delta
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    # The unrelated untracked file must NOT be committed into the PR.
    commit_dirty.assert_not_awaited()
    # Validation must never run on a dirty worktree.
    assert validation.calls == []
    # The dirty check is not re-run after a skipped finalize (no verify pass).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_no_commit_clean_blocks_self_commit_unowned_delta(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A no-commit clean finalize must re-validate a self-commit's owned delta.

    The protected-scope repair agent invoked inside ``_commit_dirty_worktree``
    can self-commit, advancing HEAD past ``finalize_start_head``. If that
    self-commit cleans the worktree, the sink returns False (no remaining
    ``stage_paths``) and the ``if not committed`` branch returns the clean
    recheck WITHOUT comparing the new HEAD/committed delta against
    ``owned_delta_paths``. An agent-created commit containing paths outside the
    operation-owned delta then bypasses the post-commit
    ``PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA`` gate and proceeds to
    validation/push. The no-commit-clean path must first detect HEAD movement
    and run the same committed-delta ownership check (regression for review
    thread ``PRRT_kwDOSJAM6s6KpCpP``).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_constants import (
        _PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON,
    )

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    clean_check = ValidationWorktreeCheck(clean=True)
    # Only the initial pre-validation check is dirty; the no-commit recheck is
    # clean because the protected-scope repair agent's self-commit cleaned the
    # tree. The post-commit fail-closed branch must NOT re-run the worktree
    # cleanliness check — the committed-delta gate is the load-bearing guard.
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, clean_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Pre-commit ownership gate (committed delta only): the dirty path is owned
    # via the committed delta, so the gate lets the finalize proceed. The staged
    # delta is NOT consulted (removed for PRRT_kwDOSJAM6s6KdVXx); the live
    # working-tree delta is NOT consulted (removed for PRRT_kwDOSJAM6s6KbbE6).
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # committed delta
    # Post-no-commit-clean HEAD: the protected-scope repair agent self-committed
    # after ``finalize_start_head`` (``'a' * 40``), advancing HEAD to a new SHA.
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # no-commit-clean rev-parse HEAD
    # No-commit-clean committed-delta re-validation: the agent's self-commit
    # introduced an extra unowned path outside ``owned_delta_paths``, so the
    # committed delta now carries both the operation-owned path and the unowned
    # extra path.
    cmd.queue_result(
        returncode=0,
        stdout=_name_status_z("M\0src/fix.py\0", "M\0unrelated/extra.py\0"),
    )  # no-commit-clean committed delta re-validation
    # Final rev-parse HEAD captured by ``_run_pre_push_validation`` after the
    # finalize returns the fail-closed unowned-delta check.
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # post-finalize rev-parse HEAD
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    # The protected-scope repair agent self-committed and cleaned the tree, so
    # there is nothing left for the finalize commit to stage; the sink returns
    # False even though HEAD advanced.
    monkeypatch.setattr(runner, "_commit_dirty_worktree", AsyncMock(return_value=False))
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is False
    assert result.reason_code == _PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON
    assert result.validation_run_id is None
    assert result.workspace_head_sha == "b" * 40
    # Validation must never run when the no-commit-clean path fails closed on
    # an unowned self-commit delta.
    assert validation.calls == []
    # The no-commit-clean path re-runs the worktree check exactly once (the
    # initial dirty check + the clean recheck); the committed-delta gate, not
    # a third worktree check, fails the finalize closed.
    assert check_worktree_clean.await_count == 2


@pytest.mark.unit
async def test_pre_push_validation_finalize_no_commit_clean_delta_unavailable_when_self_commit_delta_missing(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A no-commit-clean finalize whose self-commit delta can't be inspected must fail closed.

    When the protected-scope repair agent self-commits (advancing HEAD past
    ``finalize_start_head``) and leaves the tree clean, the no-commit-clean
    path re-validates the committed delta. If that re-validation cannot
    inspect the committed delta (``git diff`` failed or its ``--name-status
    -z`` output was malformed), the finalize must fail closed with the
    dedicated delta-unavailable reason — NOT proceed to validation on the
    strength of a clean tree that may hide an uninspectable agent commit
    (regression for review thread ``PRRT_kwDOSJAM6s6KpCpP``, mirroring the
    ``committed=True`` delta-unavailable gate ``PRRT_kwDOSJAM6s6KhtZJ``).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_constants import (
        _PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE_REASON,
    )

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    clean_check = ValidationWorktreeCheck(clean=True)
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, clean_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # committed delta
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # no-commit-clean rev-parse HEAD
    # No-commit-clean committed-delta re-validation: ``git diff`` fails, so
    # the agent's self-commit delta cannot be inspected.
    cmd.queue_result(returncode=1, stdout="", stderr="unknown revision")
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # post-finalize rev-parse HEAD
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner._deps.validation = validation  # type: ignore[assignment]
    monkeypatch.setattr(runner, "_commit_dirty_worktree", AsyncMock(return_value=False))
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is False
    assert result.reason_code == _PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE_REASON
    assert result.validation_run_id is None
    assert result.workspace_head_sha == "b" * 40
    assert validation.calls == []
    assert check_worktree_clean.await_count == 2


@pytest.mark.unit
async def test_pre_push_validation_finalize_no_commit_clean_proceeds_when_self_commit_owned(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A no-commit-clean finalize with an operation-owned self-commit must proceed to validation.

    When the protected-scope repair agent self-commits (advancing HEAD past
    ``finalize_start_head``) but the self-commit's committed delta is confined
    to the operation-owned delta, the no-commit-clean path must accept the
    clean recheck and proceed to validation — the HEAD-movement gate must not
    strand a legitimate operation-owned self-commit (regression for review
    thread ``PRRT_kwDOSJAM6s6KpCpP``).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    clean_check = ValidationWorktreeCheck(clean=True)
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, clean_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cleanup = AsyncMock(
        return_value=ValidationWorktreeCleanup(
            cleaned=False,
            check=clean_check,
            restore_ref="b" * 40,
        )
    )
    monkeypatch.setattr(pre_push_validation_module, "_pre_push_validation_cleanup", cleanup)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))  # committed delta
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # no-commit-clean rev-parse HEAD
    # No-commit-clean committed-delta re-validation: the agent's self-commit is
    # confined to the operation-owned path, so the gate passes.
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # post-finalize rev-parse HEAD
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    monkeypatch.setattr(runner, "_commit_dirty_worktree", AsyncMock(return_value=False))
    state = MonitorState()
    operation_start_head = "0" * 40

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
    )

    assert result.passed is True
    assert result.workspace_head_sha == "b" * 40
    assert check_worktree_clean.await_count == 2
    cleanup.assert_awaited_once()
