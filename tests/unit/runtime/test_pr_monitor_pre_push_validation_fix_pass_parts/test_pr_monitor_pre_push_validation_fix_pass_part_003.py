"""Pre-push validation fix-pass re-parent tests (part 3).

Covers the always-reparent rule for non-descendant fix-pass HEADs (issue #411):
``_reparent_fix_pass_commit`` reconstructs whatever the agent produced as a single
commit parented on ``fix_start_head`` instead of discriminating amend vs. work-dropping
reset+recommit. Split into a new ``_parts`` file to keep first-party files under the
maintainability line limit; see ``test_core_decomposition_maintainability``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.git_identity import git_identity_config_args
from awf.db.session import make_session_factory
from awf.runtime.validation_worktree import (
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
from tests.unit.runtime.test_pr_monitor_pre_push_validation import (
    _FakeValidation,
    _mark_git_worktree,
    _set_resolved_profile,
    _validation_result,
    _validation_runs,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a scoped async SQLAlchemy session factory for tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _commit_tree_call(cmd: FakeCommandRunner) -> list[str] | None:
    """Return the recorded ``commit-tree`` argv, if any."""
    for call in cmd.calls:
        if "commit-tree" in call.args:
            return call.args
    return None


def _commit_tree_message(args: list[str]) -> str:
    """Extract the ``-m`` message from a ``commit-tree`` argv."""
    return args[args.index("-m") + 1]


@pytest.mark.unit
async def test_reparent_fix_pass_commit_success_uses_identity_and_parent(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A successful re-parent commits the current tree onto ``fix_start_head`` using the
    AWF git identity, derives the message from the agent commit, and resets to it."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    fix_start_head = "1" * 40
    current_head = "2" * 40
    current_tree = "a" * 40
    start_tree = "b" * 40
    new_sha = "9" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{current_tree}\n")
    cmd.queue_result(returncode=0, stdout=f"{start_tree}\n")
    cmd.queue_result(returncode=0, stdout="agent body line\n")
    cmd.queue_result(returncode=0, stdout=f"{new_sha}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {new_sha[:8]}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    head, no_net_change, failure_reason = await pre_push_validation._reparent_fix_pass_commit(
        runner,
        workspace_id="workspace",
        worktree_path=worktree,
        fix_start_head=fix_start_head,
        current_head=current_head,
        pass_number=1,
    )

    assert head == new_sha
    assert no_net_change is False
    assert failure_reason is None
    commit_args = _commit_tree_call(cmd)
    assert commit_args is not None
    for identity_arg in git_identity_config_args():
        assert identity_arg in commit_args
    assert commit_args[commit_args.index("commit-tree") + 1] == current_tree
    assert commit_args[commit_args.index("-p") + 1] == fix_start_head
    assert _commit_tree_message(commit_args) == "agent body line"
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(f"reset --hard {new_sha}" in call for call in joined_calls)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("current_returncode", "current_stdout", "start_returncode", "start_stdout"),
    [
        pytest.param(1, "", 0, "b\n", id="current_tree_exit_nonzero"),
        pytest.param(0, "  \n", 0, "b\n", id="current_tree_blank"),
        pytest.param(0, "a\n", 1, "", id="start_tree_exit_nonzero"),
        pytest.param(0, "a\n", 0, "  \n", id="start_tree_blank"),
    ],
)
async def test_reparent_fix_pass_commit_rev_parse_failures_are_reparent_failed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    current_returncode: int,
    current_stdout: str,
    start_returncode: int,
    start_stdout: str,
) -> None:
    """A failed or blank tree rev-parse aborts re-parenting with the reparent reason."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=current_returncode, stdout=current_stdout)
    # Only the start-tree rev-parse runs when the current-tree rev-parse succeeded.
    if current_returncode == 0 and current_stdout.strip():
        cmd.queue_result(returncode=start_returncode, stdout=start_stdout)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    head, no_net_change, failure_reason = await pre_push_validation._reparent_fix_pass_commit(
        runner,
        workspace_id="workspace",
        worktree_path=worktree,
        fix_start_head="1" * 40,
        current_head="2" * 40,
        pass_number=1,
    )

    assert head is None
    assert no_net_change is False
    assert failure_reason == pre_push_validation.PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON
    assert _commit_tree_call(cmd) is None


@pytest.mark.unit
async def test_reparent_fix_pass_commit_no_net_change_signals_no_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Equal current and fix-start trees signal no-commit (no log/commit-tree/reset)."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    same_tree = "a" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{same_tree}\n")
    cmd.queue_result(returncode=0, stdout=f"{same_tree}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with structlog.testing.capture_logs() as captured:
        head, no_net_change, failure_reason = await pre_push_validation._reparent_fix_pass_commit(
            runner,
            workspace_id="workspace",
            worktree_path=worktree,
            fix_start_head="1" * 40,
            current_head="2" * 40,
            pass_number=1,
        )

    assert head is None
    assert no_net_change is True
    assert failure_reason is None
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert not any("commit-tree" in call for call in joined_calls)
    assert not any("reset --hard" in call for call in joined_calls)
    assert not any("log -1" in call for call in joined_calls)
    # A dedicated audit event records the no-net-change re-parent so it is
    # distinguishable in logs from a genuine no-commit rollback.
    no_net_change_events = [
        event
        for event in captured
        if event["event"] == "monitor.pre_push_validation_fix_reparent_no_net_change"
    ]
    assert len(no_net_change_events) == 1
    assert no_net_change_events[0]["workspace_id"] == "workspace"
    assert no_net_change_events[0]["fix_start_head"] == "1" * 40
    assert no_net_change_events[0]["current_head"] == "2" * 40
    assert no_net_change_events[0]["pass_number"] == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("commit_returncode", "commit_stdout"),
    [
        pytest.param(1, "", id="commit_tree_exit_nonzero"),
        pytest.param(0, "  \n", id="commit_tree_blank_stdout"),
    ],
)
async def test_reparent_fix_pass_commit_commit_tree_failure_is_reparent_failed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    commit_returncode: int,
    commit_stdout: str,
) -> None:
    """A failed (or empty) ``commit-tree`` aborts with the reparent reason; no reset."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")
    cmd.queue_result(returncode=0, stdout="message\n")
    cmd.queue_result(returncode=commit_returncode, stdout=commit_stdout, stderr="boom")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    head, no_net_change, failure_reason = await pre_push_validation._reparent_fix_pass_commit(
        runner,
        workspace_id="workspace",
        worktree_path=worktree,
        fix_start_head="1" * 40,
        current_head="2" * 40,
        pass_number=1,
    )

    assert head is None
    assert no_net_change is False
    assert failure_reason == pre_push_validation.PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert not any("reset --hard" in call for call in joined_calls)


@pytest.mark.unit
async def test_reparent_fix_pass_commit_reset_failure_is_reparent_failed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A failed ``reset --hard`` to the reconstructed commit aborts with the reparent reason."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    new_sha = "9" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")
    cmd.queue_result(returncode=0, stdout="message\n")
    cmd.queue_result(returncode=0, stdout=f"{new_sha}\n")
    cmd.queue_result(returncode=1, stderr="reset failed")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    head, no_net_change, failure_reason = await pre_push_validation._reparent_fix_pass_commit(
        runner,
        workspace_id="workspace",
        worktree_path=worktree,
        fix_start_head="1" * 40,
        current_head="2" * 40,
        pass_number=1,
    )

    assert head is None
    assert no_net_change is False
    assert failure_reason == pre_push_validation.PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(f"reset --hard {new_sha}" in call for call in joined_calls)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("log_returncode", "log_stdout"),
    [
        pytest.param(1, "", id="log_exit_nonzero"),
        pytest.param(0, "   \n", id="log_blank"),
    ],
)
async def test_reparent_fix_pass_commit_message_fallback(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    log_returncode: int,
    log_stdout: str,
) -> None:
    """A failed or blank ``git log`` message falls back to the standard fix message."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")
    cmd.queue_result(returncode=log_returncode, stdout=log_stdout)
    cmd.queue_result(returncode=0, stdout=f"{'9' * 40}\n")
    cmd.queue_result(returncode=0, stdout="HEAD is now at 99999999\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    _, _, failure_reason = await pre_push_validation._reparent_fix_pass_commit(
        runner,
        workspace_id="workspace",
        worktree_path=worktree,
        fix_start_head="1" * 40,
        current_head="2" * 40,
        pass_number=1,
    )

    assert failure_reason is None
    commit_args = _commit_tree_call(cmd)
    assert commit_args is not None
    assert _commit_tree_message(commit_args) == "awf: pre-push validation fix for workspace"


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_reset_recommit_drop_is_reparented(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A work-dropping reset+recommit is reconstructed as a child of ``fix_start_head``
    whose tree equals the agent's current tree (the drop stays visible/auditable) and is
    accepted instead of rolled back."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    adapter = FakeAdapter()
    adapter.queue(stdout="agent dropped work via reset+recommit\n")
    cmd = FakeCommandRunner()
    fix_start_head = "1" * 40
    divergent_head = "3" * 40
    current_tree = "a" * 40
    start_tree = "b" * 40
    reparented_head = "7" * 40
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0, stdout=f"{current_tree}\n")
    cmd.queue_result(returncode=0, stdout=f"{start_tree}\n")
    cmd.queue_result(returncode=0, stdout="recommit message\n")
    cmd.queue_result(returncode=0, stdout=f"{reparented_head}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {reparented_head[:8]}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    rev_parse_results: list[str | None] = [fix_start_head, divergent_head]

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return rev_parse_results.pop(0)

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        return False

    cleanup_calls: list[dict[str, object]] = []

    async def _pre_push_validation_cleanup(
        _runner: object,
        **kwargs: object,
    ) -> ValidationWorktreeCleanup:
        cleanup_calls.append(cast(dict[str, object], kwargs))
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref=cast(str, kwargs["restore_ref"]),
        )

    async def _rollback_should_not_run(*_args: object, **_kwargs: object) -> str | None:
        raise AssertionError("a reset+recommit drop must be re-parented, not rolled back")

    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _pre_push_validation_cleanup,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback_should_not_run,
    )
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=fix_start_head,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )

    committed, cleanup_failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=validation_result,
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert committed is True
    assert cleanup_failure_reason is None
    commit_args = _commit_tree_call(cmd)
    assert commit_args is not None
    # The reconstructed commit's tree is the agent's current tree, parented on
    # fix_start_head, so ``git diff fix_start_head..HEAD`` still exposes the drop.
    assert commit_args[commit_args.index("commit-tree") + 1] == current_tree
    assert commit_args[commit_args.index("-p") + 1] == fix_start_head
    assert cleanup_calls == [{"worktree_path": worktree, "restore_ref": reparented_head}]


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_reparent_failure_returns_reparent_reason(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-parent infra failure surfaces ``PRE_PUSH_VALIDATION_REPARENT_FAILED`` and does
    not roll back to ``fix_start_head``."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    adapter = FakeAdapter()
    adapter.queue(stdout="amended self-commit\n")
    cmd = FakeCommandRunner()
    fix_start_head = "1" * 40
    divergent_head = "3" * 40
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")
    cmd.queue_result(returncode=0, stdout="message\n")
    cmd.queue_result(returncode=1, stderr="commit-tree failed")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    rev_parse_results: list[str | None] = [fix_start_head, divergent_head]

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return rev_parse_results.pop(0)

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        return False

    async def _rollback_should_not_run(*_args: object, **_kwargs: object) -> str | None:
        raise AssertionError("a reparent infra failure must not trigger rollback")

    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback_should_not_run,
    )
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=fix_start_head,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )

    committed, failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=validation_result,
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert committed is True
    assert failure_reason == pre_push_validation.PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert not any(f"reset --hard {fix_start_head}" in call for call in joined_calls)


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_no_net_change_rolls_back(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-descendant HEAD whose tree equals ``fix_start_head``'s tree has nothing to
    preserve, so it falls through to the existing no-commit rollback."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    adapter = FakeAdapter()
    adapter.queue(stdout="no-op amend\n")
    cmd = FakeCommandRunner()
    fix_start_head = "1" * 40
    divergent_head = "3" * 40
    same_tree = "a" * 40
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0, stdout=f"{same_tree}\n")
    cmd.queue_result(returncode=0, stdout=f"{same_tree}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {fix_start_head[:8]}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    rev_parse_results: list[str | None] = [fix_start_head, divergent_head]

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return rev_parse_results.pop(0)

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        return False

    cleanup_calls: list[dict[str, object]] = []

    async def _pre_push_validation_cleanup(
        _runner: object,
        **kwargs: object,
    ) -> ValidationWorktreeCleanup:
        cleanup_calls.append(cast(dict[str, object], kwargs))
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref=cast(str, kwargs["restore_ref"]),
        )

    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _pre_push_validation_cleanup,
    )
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=fix_start_head,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )

    (
        committed,
        rollback_failure_reason,
    ) = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=validation_result,
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert committed is False
    assert rollback_failure_reason is None
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert not any("commit-tree" in call for call in joined_calls)
    assert any(f"reset --hard {fix_start_head}" in call for call in joined_calls)
    assert cleanup_calls == [{"worktree_path": worktree, "restore_ref": fix_start_head}]
    assert rev_parse_results == []


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_revalidates_after_reparent(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a non-descendant fix pass is re-parented, the loop re-runs validation on the
    reconstructed HEAD and, when it passes, allows the push (the dropped-work safety net).

    The re-parent plumbing is unit-tested directly elsewhere, so it is stubbed here to
    isolate the loop's re-validation-on-the-new-head behavior."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    first_head = "b" * 40
    amended_head = "c" * 40
    reparented_head = "e" * 40
    adapter = FakeAdapter()
    adapter.queue(stdout="self-amended fix\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(  # type: ignore[assignment]
        _validation_result(tmp_path, ok=False),
        _validation_result(tmp_path, ok=True),
    )
    # HEAD reads, in order: pass-1 validation HEAD, fix-pass fix_start_head, fix-pass
    # post-commit HEAD (non-descendant amend), pass-2 validation HEAD (reparented).
    head_reads: list[str | None] = [first_head, first_head, amended_head, reparented_head]

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return head_reads.pop(0)

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        """The agent already self-committed, so there is nothing dirty to commit."""
        return False

    async def _head_descends_from(*_args: object, **_kwargs: object) -> bool:
        """Force the non-descendant rewrite branch."""
        return False

    reparent_calls: list[str] = []

    async def _reparent_fix_pass_commit(
        _runner: object,
        **kwargs: object,
    ) -> tuple[str | None, bool, str | None]:
        reparent_calls.append(cast(str, kwargs["current_head"]))
        return reparented_head, False, None

    async def _pre_push_validation_cleanup(
        _runner: object,
        **kwargs: object,
    ) -> ValidationWorktreeCleanup:
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref=cast(str, kwargs["restore_ref"]),
        )

    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    monkeypatch.setattr(pre_push_validation, "_head_descends_from", _head_descends_from)
    monkeypatch.setattr(
        pre_push_validation,
        "_reparent_fix_pass_commit",
        _reparent_fix_pass_commit,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _pre_push_validation_cleanup,
    )

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert len(adapter.calls) == 1
    assert reparent_calls == [amended_head]
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-1].target_head_sha == reparented_head


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_reparent_failure_uses_reparent_label(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed fix pass carrying the reparent reason reads 'reparent failed', not
    the mislabeled 'cleanup failed'."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = "workspace_fix_reparent_failed"
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True, exist_ok=True)
    validation_calls = 0
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr1",
        workspace_head_sha="a" * 40,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="attempt 1 failed",
        validation_reason_code="PYTEST_TEST_FAILURE",
        result=_validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
    )

    async def _run_pre_push_validation(
        _self: Any,
        **_kwargs: object,
    ) -> pre_push_validation._PrePushValidationResult:
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls > 1:
            raise AssertionError("reparent failure should stop before retry validation")
        return validation_result

    async def _run_fix_pass(_runner: object, **_kwargs: object) -> tuple[bool, str | None]:
        return True, pre_push_validation.PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_run_pre_push_validation",
        _run_pre_push_validation,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_run_pre_push_validation_fix_pass",
        _run_fix_pass,
    )

    result = await pre_push_validation._run_pre_push_validation_with_fix_passes(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
        remote_url=None,
        state=None,
    )

    assert validation_calls == 1
    assert result.reason_code == pre_push_validation.PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON
    assert "fix pass reparent failed" in result.message
    assert "cleanup failed" not in result.message


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_committed_nondescendant_is_reparented(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fix-pass agent that rewrites HEAD (amend/reset to a non-descendant) AND leaves
    dirty work has that work committed by ``_commit_dirty_worktree`` as a child of the
    rewritten tip. The committed head must be re-parented onto ``fix_start_head`` so the
    original tip is preserved and the later non-force push stays fast-forward (issue #411
    gap on the committed-dirty branch)."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    adapter = FakeAdapter()
    adapter.queue(stdout="amended HEAD then left dirty work\n")
    cmd = FakeCommandRunner()
    fix_start_head = "1" * 40
    committed_head = "3" * 40
    current_tree = "a" * 40
    start_tree = "b" * 40
    reparented_head = "7" * 40
    # merge-base --is-ancestor reports non-descendant (exit 1); then the re-parent steps.
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0, stdout=f"{current_tree}\n")
    cmd.queue_result(returncode=0, stdout=f"{start_tree}\n")
    cmd.queue_result(returncode=0, stdout="amend message\n")
    cmd.queue_result(returncode=0, stdout=f"{reparented_head}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {reparented_head[:8]}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    rev_parse_results: list[str | None] = [fix_start_head, committed_head]

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return rev_parse_results.pop(0)

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        """The agent left dirty work that is committed onto the rewritten tip."""
        return True

    cleanup_calls: list[dict[str, object]] = []

    async def _pre_push_validation_cleanup(
        _runner: object,
        **kwargs: object,
    ) -> ValidationWorktreeCleanup:
        cleanup_calls.append(cast(dict[str, object], kwargs))
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref=cast(str, kwargs["restore_ref"]),
        )

    async def _rollback_should_not_run(*_args: object, **_kwargs: object) -> str | None:
        raise AssertionError("a committed reparent must not roll back to fix_start_head")

    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _pre_push_validation_cleanup,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback_should_not_run,
    )
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=fix_start_head,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )

    committed, cleanup_failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=validation_result,
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert committed is True
    assert cleanup_failure_reason is None
    commit_args = _commit_tree_call(cmd)
    assert commit_args is not None
    # The committed tree is reconstructed as a child of fix_start_head, so the pre-fix tip
    # is preserved and the push remains fast-forward.
    assert commit_args[commit_args.index("commit-tree") + 1] == current_tree
    assert commit_args[commit_args.index("-p") + 1] == fix_start_head
    # Side-effect cleanup runs against the re-parented head, not the orphaning commit.
    assert cleanup_calls == [{"worktree_path": worktree, "restore_ref": reparented_head}]
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(
        f"merge-base --is-ancestor {fix_start_head} {committed_head}" in call
        for call in joined_calls
    )


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_committed_descendant_skips_reparent(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common case — ``_commit_dirty_worktree`` commits onto a HEAD that still descends
    from ``fix_start_head`` — is accepted as-is with no re-parent (no ``commit-tree``)."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed and left dirty work on top\n")
    cmd = FakeCommandRunner()
    fix_start_head = "1" * 40
    committed_head = "2" * 40
    # merge-base --is-ancestor reports a descendant (exit 0): accept without re-parenting.
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    rev_parse_results: list[str | None] = [fix_start_head, committed_head]

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return rev_parse_results.pop(0)

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        return True

    cleanup_calls: list[dict[str, object]] = []

    async def _pre_push_validation_cleanup(
        _runner: object,
        **kwargs: object,
    ) -> ValidationWorktreeCleanup:
        cleanup_calls.append(cast(dict[str, object], kwargs))
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref=cast(str, kwargs["restore_ref"]),
        )

    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _pre_push_validation_cleanup,
    )
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=fix_start_head,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )

    committed, cleanup_failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=validation_result,
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert committed is True
    assert cleanup_failure_reason is None
    assert cleanup_calls == [{"worktree_path": worktree, "restore_ref": committed_head}]
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert not any("commit-tree" in call for call in joined_calls)


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_committed_reparent_failure_returns_reason(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-parent infra failure on the committed-dirty branch surfaces the reparent reason
    (committed stays True) and must not roll back to ``fix_start_head``."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    adapter = FakeAdapter()
    adapter.queue(stdout="amended HEAD then left dirty work\n")
    cmd = FakeCommandRunner()
    fix_start_head = "1" * 40
    committed_head = "3" * 40
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0, stdout=f"{'a' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"{'b' * 40}\n")
    cmd.queue_result(returncode=0, stdout="message\n")
    cmd.queue_result(returncode=1, stderr="commit-tree failed")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    rev_parse_results: list[str | None] = [fix_start_head, committed_head]

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return rev_parse_results.pop(0)

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        return True

    async def _rollback_should_not_run(*_args: object, **_kwargs: object) -> str | None:
        raise AssertionError("a reparent infra failure must not trigger rollback")

    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback_should_not_run,
    )
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=fix_start_head,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )

    committed, failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=validation_result,
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert committed is True
    assert failure_reason == pre_push_validation.PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert not any(f"reset --hard {fix_start_head}" in call for call in joined_calls)


@pytest.mark.unit
@pytest.mark.parametrize("rollback_reason", [None, "VALIDATION_WORKTREE_CLEANUP_FAILED"])
async def test_pre_push_validation_fix_pass_committed_no_net_change_rolls_back(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rollback_reason: str | None,
) -> None:
    """When the committed-dirty rewrite nets out to ``fix_start_head``'s tree there is
    nothing to preserve, so the pass rolls back to the pre-fix tip and reports
    ``committed=False`` (propagating any rollback failure reason)."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    adapter = FakeAdapter()
    adapter.queue(stdout="no-op amend over dirty work\n")
    cmd = FakeCommandRunner()
    fix_start_head = "1" * 40
    committed_head = "3" * 40
    same_tree = "a" * 40
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0, stdout=f"{same_tree}\n")
    cmd.queue_result(returncode=0, stdout=f"{same_tree}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    rev_parse_results: list[str | None] = [fix_start_head, committed_head]

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return rev_parse_results.pop(0)

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        return True

    rollback_calls: list[dict[str, object]] = []

    async def _rollback(_runner: object, **kwargs: object) -> str | None:
        rollback_calls.append(cast(dict[str, object], kwargs))
        return rollback_reason

    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback,
    )
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=fix_start_head,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )

    committed, failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=validation_result,
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert committed is False
    assert failure_reason == rollback_reason
    assert rollback_calls == [
        {
            "workspace_id": workspace_id,
            "worktree_path": worktree,
            "restore_ref": fix_start_head,
            "pass_number": 1,
            "reason": "commit_reparent_no_net_change",
        }
    ]
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert not any("commit-tree" in call for call in joined_calls)
