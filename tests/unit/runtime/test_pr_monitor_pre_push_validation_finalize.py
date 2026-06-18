"""Pre-push validation dirty-finalize and tail regression tests (part 2).

Split from ``test_pr_monitor_pre_push_validation`` to keep first-party files
under the maintainability line limit; see
``test_core_decomposition_maintainability``.
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
async def test_validated_push_finalizes_monitor_dirty_state_before_validation(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Monitor-authored dirty residue should be committed before validation starts."""
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
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")
    # Operation-owned committed delta includes the dirty path, so the finalize
    # proceeds. The staged delta is NOT consulted (removed for
    # PRRT_kwDOSJAM6s6KdVXx); the live working-tree delta is NOT consulted
    # (removed for PRRT_kwDOSJAM6s6KbbE6).
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    # Post-commit re-validation: only the committed delta is re-checked, and
    # it is still confined to the operation-owned path, so the finalize
    # proceeds to the verify recheck.
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")
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
async def test_pre_push_validation_finalize_strands_operation_owned_staged_dirt_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Operation-owned staged dirt (empty committed delta) strands fail-closed (deferred).

    When the repair operation's ``_commit_dirty_worktree`` returns False
    *before* creating a commit (for example ``git commit`` fails after the
    agent already staged its edits via ``git add -A``), the operation's
    staged edits are still dirty in the worktree but
    ``git diff --name-status -z operation_start_head..HEAD`` is empty (HEAD never
    moved). ``PRRT_kwDOSJAM6s6KYd-r`` previously recovered this case by
    unioning the staged delta ``git diff --name-status -z --cached
    operation_start_head`` into ``owned_delta_paths``. But per ``git diff -h``
    the ``--cached [<commit>]`` form compares the *current index* against the
    commit, so a tracked file staged after the repair-start dirty guard by a
    failed cleanup or another local process is treated as operation-owned
    merely because it is staged. ``_commit_dirty_worktree`` then stages it via
    ``git add -A`` and the post-commit re-validation sees it as committed and
    confined to the owned set, silently sweeping the unrelated file into the PR
    instead of failing closed (review thread ``PRRT_kwDOSJAM6s6KdVXx``,
    mirroring the ``PRRT_kwDOSJAM6s6KbbE6`` working-tree-delta removal and the
    ``PRRT_kwDOSJAM6s6KcSj`` untracked fold-in removal).

    The staged-delta branch was therefore removed and this recovery now strands
    the operation's own staged edits as
    ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` — fail-closed and visible to a
    human, rather than a silent sweep of unrelated dirt. Restoring this
    recovery without the over-broadening requires capturing the operation's
    attempted paths (the ``stage_paths`` the commit sink computes) and
    threading them to the gate; tracked as a deferred follow-up (see
    ``plans/PRRT_kwDOSJAM6s6KdVXx_PLAN.md``).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    # Dirt on a path the operation staged but did not commit (failed commit).
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
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
    # Pre-commit ownership gate: the committed delta is empty (HEAD never
    # moved). The staged delta is NOT consulted (removed for
    # PRRT_kwDOSJAM6s6KdVXx), and the live working-tree delta is NOT consulted
    # (removed for PRRT_kwDOSJAM6s6KbbE6), so the operation-owned staged path
    # is treated as unrelated and the finalize skips — fail-closed, not a
    # silent sweep.
    cmd.queue_result(returncode=0, stdout="")  # committed delta
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
    # The operation-owned staged tracked edits strand fail-closed (deferred
    # recovery) — the commit sink must not run.
    commit_dirty.assert_not_awaited()
    # Validation must never run on a dirty worktree.
    assert validation.calls == []
    # The dirty check is not re-run after a skipped finalize (no verify pass).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_strands_operation_owned_unstaged_dirt_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Operation-owned unstaged dirt (failed ``git add -A``) strands fail-closed (deferred).

    When the repair operation's ``_commit_dirty_worktree`` returns False
    *before* creating a commit because ``git add -A`` failed
    (``remote_repair.py``), the operation's edits were never staged and remain
    as unstaged working-tree changes, even though the repair-start dirty guard
    proved the worktree was clean at ``operation_start_head``. Both
    ``git diff --name-status -z operation_start_head..HEAD`` (HEAD never moved)
    and ``git diff --name-status -z --cached operation_start_head`` (nothing was
    staged) are empty, so the committed + staged union leaves
    ``owned_delta_paths`` empty.

    ``PRRT_kwDOSJAM6s6KaUHP`` previously recovered this case by unioning the
    live working-tree delta ``git diff --name-status -z operation_start_head``
    (commit vs working tree). That diff reports *every* tracked file differing
    from the anchor, so an unrelated tracked modification introduced after the
    repair-start guard was treated as operation-owned and swept into the PR via
    ``_commit_dirty_worktree``'s ``git add -A``, with the post-commit
    re-validation unable to catch it (review thread
    ``PRRT_kwDOSJAM6s6KbbE6``). The working-tree-delta branch was therefore
    removed and this recovery now strands the operation's own unstaged tracked
    edits as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` — fail-closed and
    visible to a human, rather than a silent sweep of unrelated dirt. Restoring
    this recovery without the over-broadening requires capturing the
    operation's attempted paths (the ``stage_paths`` the commit sink computes)
    and threading them to the gate; tracked as a deferred follow-up (see
    ``plans/PRRT_kwDOSJAM6s6KbbE6_PLAN.md``).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    # Dirt on a path the operation edited but never staged (failed ``git add -A``).
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
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
    # Pre-commit ownership gate: committed delta empty (HEAD never moved). The
    # staged delta is NOT consulted (removed for PRRT_kwDOSJAM6s6KdVXx); the
    # live working-tree delta is NOT consulted (removed for
    # PRRT_kwDOSJAM6s6KbbE6), so the unstaged operation-owned path is treated
    # as unrelated and the finalize skips.
    cmd.queue_result(returncode=0, stdout="")  # committed delta
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
    # The operation-owned unstaged tracked edits strand fail-closed (deferred
    # recovery) — the commit sink must not run.
    commit_dirty.assert_not_awaited()
    # Validation must never run on a dirty worktree.
    assert validation.calls == []
    # The dirty check is not re-run after a skipped finalize (no verify pass).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_skips_unrelated_working_tree_only_dirt(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Unrelated working-tree-only tracked dirt must stay fail-closed, not be swept in.

    The ownership gate added for ``PRRT_kwDOSJAM6s6KaUHP`` unioned the live
    working-tree delta ``git diff --name-status -z operation_start_head``
    (commit vs working tree) into ``owned_delta_paths``. That diff reports *any*
    tracked file that differs from the anchor, so a tracked modification
    introduced after the repair-start dirty guard by a failed cleanup or another
    local process is treated as operation-owned merely because it differs from
    ``operation_start_head``. The gate then passes, ``_commit_dirty_worktree``
    runs a fresh ``git status`` and ``git add -A --`` on every non-ignored dirty
    path (so the unrelated edit is staged and committed), and the post-commit
    re-validation (``PRRT_kwDOSJAM6s6KZP8f``/``Ka0aO``) does not catch it because
    the pre-commit ``owned_delta_paths`` already contained that path. The
    previous fail-closed behavior for unrelated dirt (``PRRT_kwDOSJAM6s6KXLaI``)
    was lost for the working-tree-only case.

    The gate must use paths captured/attempted by the repair operation (the
    committed delta, the staged delta, and the operation's untracked output),
    not every live worktree difference since ``operation_start_head``. A tracked
    modification that is NOT committed, NOT staged, and NOT untracked must stay
    fail-closed as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` and never be
    committed into the PR (review thread ``PRRT_kwDOSJAM6s6KbbE6``).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    # A tracked file modified after the repair-start guard by an unrelated
    # process: present in the working tree (so the live working-tree diff
    # against operation_start_head carries it) but NOT committed, NOT staged,
    # and NOT untracked.
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("unrelated/lefover.log",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    # On the unfixed (KaUHP) code the gate treats the unrelated path as owned
    # and the commit sink commits it; the post-commit re-validation + verify
    # recheck then run. Queue a clean verify so the unfixed path can reach the
    # commit_dirty assertion. On the fixed code the finalize skips before any
    # of these run, so the extra side effect is simply unused.
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # The operation committed nothing (HEAD never moved), so the committed
    # delta carries nothing. The staged delta is NOT consulted (removed for
    # PRRT_kwDOSJAM6s6KdVXx).
    cmd.queue_result(returncode=0, stdout="")  # committed delta
    # The live working-tree diff against operation_start_head DOES carry the
    # unrelated path (it differs from the anchor). On the KaUHP code this makes
    # the gate treat it as operation-owned; the fix must not consult this diff.
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0unrelated/lefover.log\0"))
    # Post-commit re-validation (unfixed path only): the commit sink committed
    # the unrelated path, so the committed delta carries it — still "owned" on
    # the unfixed code because the pre-commit owned set also held it via the
    # working-tree delta, so the re-validation passes. Unused on the fixed code.
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0unrelated/lefover.log\0"))
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # post-finalize rev-parse HEAD
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
    # The unrelated working-tree-only tracked edit must not be committed.
    commit_dirty.assert_not_awaited()
    # Validation must never run on a dirty worktree.
    assert validation.calls == []
    # The dirty check is not re-run after a skipped finalize (no verify pass).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_skips_unrelated_staged_dirt(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Unrelated staged dirt must stay fail-closed, not be swept in.

    The ownership gate added for ``PRRT_kwDOSJAM6s6KYd-r`` unioned the *staged*
    delta ``git diff --name-status -z --cached operation_start_head`` into
    ``owned_delta_paths``. Per ``git diff -h``, the ``--cached [<commit>]`` form
    compares the *current index* against the commit, so a tracked file staged
    after the repair-start dirty guard by a failed cleanup or another local
    process is treated as operation-owned merely because it is staged. The gate
    then passes, ``_commit_dirty_worktree`` runs a fresh ``git status`` and
    ``git add -A --`` on every non-ignored dirty path (so the unrelated staged
    file is committed), and the post-commit re-validation
    (``PRRT_kwDOSJAM6s6KZP8f``/``Ka0aO``) does not catch it because the
    pre-commit ``owned_delta_paths`` already contained that path.

    The gate must use paths captured/attempted by the repair operation (the
    committed delta), not whatever is in the live index at finalization time.
    A tracked file that is staged by an unrelated process after the
    repair-start guard must stay fail-closed as
    ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` and never be committed into the
    PR (review thread ``PRRT_kwDOSJAM6s6KdVXx``, mirroring the
    ``PRRT_kwDOSJAM6s6KbbE6`` working-tree-delta removal and the
    ``PRRT_kwDOSJAM6s6KcSj`` untracked fold-in removal).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    # A tracked file staged after the repair-start guard by an unrelated
    # process: present in the index (so the staged diff against
    # operation_start_head carries it) but NOT committed (the committed delta
    # is empty). The dirty check sees it as a staged modification.
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("unrelated/staged.log",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    # On the unfixed (KYd-r) code the gate treats the unrelated staged path as
    # owned and the commit sink commits it; the post-commit re-validation +
    # verify recheck then run. Queue a clean verify so the unfixed path can
    # reach the commit_dirty assertion. On the fixed code the finalize skips
    # before any of these run, so the extra side effect is simply unused.
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # The operation committed nothing (HEAD never moved), so neither the
    # committed delta carries the unrelated path.
    cmd.queue_result(returncode=0, stdout="")  # committed delta
    # The staged diff against operation_start_head DOES carry the unrelated
    # path (it was staged by an unrelated process). On the KYd-r code this
    # makes the gate treat it as operation-owned; the fix must not consult this
    # diff.
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0unrelated/staged.log\0"))
    # Post-commit re-validation (unfixed path only): the commit sink committed
    # the unrelated staged path, so the committed delta carries it — still
    # "owned" on the unfixed code because the pre-commit owned set also held it
    # via the staged delta, so the re-validation passes. Unused on the fixed
    # code.
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0unrelated/staged.log\0"))
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # post-finalize rev-parse HEAD
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
    # The unrelated staged path must not be committed into the PR.
    commit_dirty.assert_not_awaited()
    # Validation must never run on a dirty worktree.
    assert validation.calls == []
    # The dirty check is not re-run after a skipped finalize (no verify pass).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_skips_unrelated_dirt_outside_operation_delta(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Unrelated dirt outside the operation's committed and staged delta must stay fail-closed.

    The pre-push dirty finalize must only commit dirt the current monitor
    operation owns — i.e. paths within its committed delta
    (``operation_start_head..HEAD``) or its staged delta
    (``git diff --name-status -z --cached operation_start_head``). Dirt on a path
    that the operation never touched (introduced after the repair-start dirty
    guard by a failed cleanup or another local process) must NOT be swept
    into the PR via ``_commit_dirty_worktree``; the finalize must skip and
    the push must fail-closed as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``
    (regression for review thread ``PRRT_kwDOSJAM6s6KXLaI``; the staged-delta
    union was added in review thread ``PRRT_kwDOSJAM6s6KYd-r``).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    # Dirt on an unrelated path the operation never committed.
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("unrelated/lefover.log",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
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
    # The finalize must not commit unrelated dirt.
    commit_dirty.assert_not_awaited()
    # Validation must never run on a dirty worktree.
    assert validation.calls == []
    # The dirty check is not re-run after a skipped finalize (no verify pass).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_skips_when_no_operation_anchor(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A dirty finalize with no operation-owned anchor must stay fail-closed.

    Callers without an operation-start anchor (e.g. ``_run_sync_base``) pass
    ``operation_start_head=None``. The finalize must not commit unowned dirt
    in that case — it must skip and the push must fail-closed as
    ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` so unrelated dirt is never swept
    into the PR (review thread ``PRRT_kwDOSJAM6s6KXLaI``).
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
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=None,
    )

    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id is None
    # No anchor -> the finalize must not commit.
    commit_dirty.assert_not_awaited()
    assert validation.calls == []
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_skips_when_operation_delta_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A dirty finalize whose operation delta cannot be resolved must stay fail-closed.

    ``_operation_owned_delta_paths`` runs ``git diff --name-status -z
    operation_start_head..HEAD``. When that git command fails (e.g. the start
    ref is unknown), the delta cannot be proven, so the finalize must skip and
    the push must fail-closed as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``
    rather than commit unowned dirt (review thread ``PRRT_kwDOSJAM6s6KXLaI``).
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
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # The operation-owned delta diff fails -> delta unavailable.
    cmd.queue_result(returncode=1, stdout="", stderr="unknown revision")
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
    operation_start_head = "deadbeef" * 5

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
    # Delta unavailable -> the finalize must not commit.
    commit_dirty.assert_not_awaited()
    assert validation.calls == []
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_skips_when_operation_delta_malformed(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A dirty finalize whose ``--name-status -z`` output is malformed must stay fail-closed.

    ``_operation_owned_delta_paths`` parses ``git diff --name-status -z`` with
    ``_changed_paths_from_name_status_z``, which raises
    ``ProtectedScopeDiffError`` on malformed NUL-delimited output (e.g. a
    truncated record missing its terminating NUL or a non-``-z`` line). The
    helper must treat that as delta-unavailable and return ``None`` so the
    finalize skips and the push fails-closed as
    ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` rather than committing unowned
    dirt (review thread ``PRRT_kwDOSJAM6s6KaAWk``).
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
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # The committed delta succeeds but its ``--name-status -z`` output is
    # malformed (a non-``-z`` line: no NUL delimiter), so the parser raises and
    # the helper returns None -> delta unavailable.
    cmd.queue_result(returncode=0, stdout="M\tsrc/fix.py\n")
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
    operation_start_head = "deadbeef" * 5

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
    # Malformed delta -> the finalize must not commit.
    commit_dirty.assert_not_awaited()
    assert validation.calls == []
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_rechecks_tree_after_no_op_finalize(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A no-op finalize (protected-scope repair restored the only dirty files) must recheck the tree.

    ``_commit_dirty_worktree`` can have side effects (e.g. protected-scope
    repair) but return False because there was nothing left to commit. Before
    the fix, the stale dirty ``pre_validation_check`` was reused and pre-push
    validation failed as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` even though
    the worktree was now clean. The no-commit path must re-run
    ``_pre_push_validation_worktree_check`` so a cleanup-only repair can proceed
    to validation instead of being stranded as pre-existing dirty.
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
    # First check is dirty; the no-op finalize recheck must observe a clean tree.
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
    # Operation-owned committed delta includes the dirty path, so the finalize
    # proceeds. The staged delta is NOT consulted (removed for
    # PRRT_kwDOSJAM6s6KdVXx); the live working-tree delta is NOT consulted
    # (removed for PRRT_kwDOSJAM6s6KbbE6).
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")  # re-captured HEAD after finalize
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    # Protected-scope repair restored the only dirty file -> nothing to commit.
    commit_dirty = AsyncMock(return_value=False)
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
    # The no-op finalize path rechecks the worktree once (dirty check + recheck).
    assert check_worktree_clean.await_count == 2
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
async def test_pre_push_validation_finalize_preserves_policy_blocked_reason_code(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A policy-blocked finalize must surface MONITOR_POLICY_BLOCKED, not generic dirty.

    ``_commit_dirty_worktree`` raises ``_MonitorPolicyBlockedError`` when
    monitor-authored changes violate blocking workspace policy. Before the
    fix, the broad ``except Exception`` in
    ``_try_finalize_pre_push_dirty_repair_state`` swallowed it and returned
    ``None``, so the stale dirty check was reused and the failure was
    reported as the generic ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``,
    losing the policy reason code end-to-end (regression for thread
    ``PRRT_kwDOSJAM6s6KUmpr``).
    """
    from awf.runtime.pr_monitor_runner.constants import _MONITOR_POLICY_BLOCKED_REASON
    from awf.runtime.pr_monitor_runner.types import _MonitorPolicyBlockedError

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    finalize_start_head = "a" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{finalize_start_head}\n")  # initial rev-parse HEAD
    # Operation-owned delta includes the dirty path, so the finalize proceeds.
    # The committed delta is parsed from ``--name-status -z``; the staged delta
    # is unqueued and resolves to the default empty result (no staged paths).
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    # ``_pre_push_validation_cleanup`` -> ``check_validation_worktree_clean``
    # (status): report the policy-blocked residue the finalize was trying to
    # commit (the policy check runs before the actual ``git commit``).
    cmd.queue_result(returncode=0, stdout=" M src/fix.py\n")
    # ``git restore --source <finalize_start_head> --staged --worktree -- src/fix.py``.
    cmd.queue_result(returncode=0)
    # Post-restore status recheck (no more residue after the restore).
    cmd.queue_result(returncode=0, stdout="")
    # HEAD verification: ``rev-parse <finalize_start_head>`` + ``rev-parse HEAD``.
    cmd.queue_result(returncode=0, stdout=f"{finalize_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{finalize_start_head}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    monkeypatch.setattr(
        runner,
        "_commit_dirty_worktree",
        AsyncMock(side_effect=_MonitorPolicyBlockedError("policy blocked finalize")),
    )
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
    assert result.reason_code == _MONITOR_POLICY_BLOCKED_REASON
    assert result.validation_run_id is None
    # The finalize failure must not re-check the tree (no verify/recheck pass).
    assert check_worktree_clean.await_count == 1
    # The finalize MUST roll back the policy-blocked residue to the
    # finalize-start HEAD before returning so the next monitor attempt does
    # not trip ``PRE_EXISTING_DIRTY_WORKTREE`` (the policy reason is
    # non-terminal, so the loop retries — review thread
    # ``PRRT_kwDOSJAM6s6KjRRL``, mirroring ``PRRT_kwDOSJAM6s6Kg7Dm``).
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(
        f"restore --source {finalize_start_head} --staged --worktree" in call
        for call in joined_calls
    ), joined_calls


@pytest.mark.unit
async def test_pre_push_validation_finalize_preserves_ownership_repair_reason_code(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """An ownership-repair-failed finalize must surface its reason code, not generic dirty.

    ``_commit_dirty_worktree`` raises
    ``_MonitorAgentRuntimeOwnershipRepairFailedError`` (carrying a
    ``reason_code`` property) when monitor cannot repair agent worktree
    ownership. Before the fix, the broad ``except Exception`` in
    ``_try_finalize_pre_push_dirty_repair_state`` swallowed it and returned
    ``None``, so the stale dirty check was reused and the failure was
    reported as the generic ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``,
    losing the ownership-repair reason code end-to-end (regression for
    thread ``PRRT_kwDOSJAM6s6KUmpr``).
    """
    from awf.runtime.ownership import AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
    from awf.runtime.pr_monitor_runner.types import (
        _MonitorAgentRuntimeOwnershipRepairFailedError,
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
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Operation-owned delta includes the dirty path, so the finalize proceeds.
    # The committed delta is parsed from ``--name-status -z``; the staged delta
    # is unqueued and resolves to the default empty result (no staged paths).
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    monkeypatch.setattr(
        runner,
        "_commit_dirty_worktree",
        AsyncMock(
            side_effect=_MonitorAgentRuntimeOwnershipRepairFailedError(
                AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
            )
        ),
    )
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
    assert result.reason_code == AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
    assert result.validation_run_id is None
    # The finalize failure must not re-check the tree (no verify/recheck pass).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_preserves_protected_scope_diff_unavailable_reason_code(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A protected-scope-diff-unavailable finalize must surface its reason code, not generic dirty.

    ``_commit_dirty_worktree`` -> ``_repair_protected_scope_changes_before_commit``
    raises ``ProtectedScopeDiffError`` when the committed diff against the remote
    PR branch cannot be verified. Before the fix, the broad ``except Exception`` in
    ``_try_finalize_pre_push_dirty_repair_state`` swallowed it and returned
    ``None``, so the stale dirty check was reused and the failure was reported
    as the generic ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``, losing the
    protected-scope diff-unavailable reason code end-to-end (regression for thread
    ``PRRT_kwDOSJAM6s6KWpSB``).
    """
    from awf.runtime.pr_monitor_runner.constants import (
        _PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
    )
    from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=("src/fix.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Operation-owned delta includes the dirty path, so the finalize proceeds.
    # The committed delta is parsed from ``--name-status -z``; the staged delta
    # is unqueued and resolves to the default empty result (no staged paths).
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    monkeypatch.setattr(
        runner,
        "_commit_dirty_worktree",
        AsyncMock(side_effect=ProtectedScopeDiffError("diff baseline unavailable")),
    )
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
    assert result.reason_code == _PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON
    assert result.validation_run_id is None
    # The finalize failure must not re-check the tree (no verify/recheck pass).
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_threads_remote_branch_and_url_to_commit_sink(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """The dirty finalize must pass the PR branch and remote URL into the commit sink.

    Operation-owned residue can contain a protected file that was already
    restored to the remote PR branch. ``_commit_dirty_worktree`` ->
    ``_repair_protected_scope_changes_before_commit`` only filters that safe
    rollback when ``protected_scope_revert_remote_branch`` (and the remote URL
    when needed) is supplied. The validation fix-pass commit path already
    forwards them; the dirty-finalize path previously omitted them, so the
    restored protected file was still counted as a violation and the monitor
    launched another provider repair or fell back to a no-commit dirty failure
    instead of committing the rollback and proceeding to validation
    (regression for review thread ``PRRT_kwDOSJAM6s6KZjtR``).
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
    # Operation-owned committed delta includes the dirty path. The staged
    # delta is NOT consulted (removed for PRRT_kwDOSJAM6s6KdVXx); the live
    # working-tree delta is NOT consulted (removed for PRRT_kwDOSJAM6s6KbbE6).
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    # Post-commit re-validation: only the committed delta is re-checked, and
    # it is still operation-owned.
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
    commit_dirty = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", commit_dirty)
    state = MonitorState()
    operation_start_head = "0" * 40
    remote_branch = f"awf/{workspace_id}"
    remote_url = "https://example.invalid/awf.git"

    result = await pre_push_validation_module._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=remote_branch,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        operation_start_head=operation_start_head,
        remote_url=remote_url,
    )

    assert result.passed is True
    commit_dirty.assert_awaited_once_with(
        workspace_id=workspace_id,
        message=f"awf: finalize PR monitor repair for {workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        state=state,
        protected_scope_revert_remote_branch=remote_branch,
        remote_push_url=remote_url,
    )


@pytest.mark.unit
async def test_pre_push_validation_finalize_skips_when_dirty_check_has_no_paths(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A dirty check that reports no paths must skip finalize and keep fail-closed dirty.

    ``_try_finalize_pre_push_dirty_repair_state`` derives ``dirty_paths`` from
    ``check.paths``; an empty set (a status that is not clean but reports no
    concrete paths) cannot be proven operation-owned, so the finalize returns
    ``None`` and the caller keeps the fail-closed dirty check instead of
    committing nothing.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        paths=(),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    # Operation-owned delta resolves fine; the empty ``dirty_paths`` short-
    # circuits before the commit sink is reached.
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
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

    # The empty-paths dirty check is kept (fail-closed), no commit attempted.
    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    commit_dirty.assert_not_awaited()
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_returns_none_on_generic_commit_sink_exception(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A generic (non-recovery, non-reason-coded) commit-sink exception keeps fail-closed dirty.

    ``_commit_dirty_worktree`` can raise an unexpected exception that is neither
    a provider-recovery control-flow exception nor a reason-coded commit failure.
    The broad ``except Exception`` must return ``None`` so the stale dirty check
    is reused and the failure is reported as fail-closed
    ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` rather than propagating an
    unstructured crash through the monitor loop.
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
    check_worktree_clean = AsyncMock(side_effect=[dirty_check])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    monkeypatch.setattr(
        runner,
        "_commit_dirty_worktree",
        AsyncMock(side_effect=RuntimeError("unexpected commit sink blowup")),
    )
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

    # The generic exception is swallowed; the stale dirty check is reused.
    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert check_worktree_clean.await_count == 1


@pytest.mark.unit
async def test_pre_push_validation_finalize_no_commit_recheck_still_dirty(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A no-op finalize whose recheck is still dirty must surface the dirty recheck.

    When ``_commit_dirty_worktree`` returns False and the post-no-op recheck
    still observes dirt (e.g. an unrelated process touched the tree), the
    finalize must return that dirty recheck so validation fails closed on the
    fresh state rather than the stale pre-finalize check.
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
    recheck_dirty = ValidationWorktreeCheck(
        clean=False,
        paths=("src/unrelated.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    )
    check_worktree_clean = AsyncMock(side_effect=[dirty_check, recheck_dirty])
    monkeypatch.setattr(
        pre_push_validation_module,
        "_pre_push_validation_worktree_check",
        check_worktree_clean,
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")  # initial rev-parse HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z("M\0src/fix.py\0"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=True))  # type: ignore[assignment]
    # No-op commit: protected-scope repair restored nothing commitable, but the
    # recheck still sees dirt from an unrelated source.
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

    # The fresh dirty recheck (unrelated path) is surfaced, fail-closed.
    assert result.passed is False
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.workspace_head_sha is None or result.workspace_head_sha == "a" * 40
    assert check_worktree_clean.await_count == 2
