"""Pre-push validation fix-pass and repair flow tests (part 1).

Split from the original module to keep first-party files under the
maintainability line limit; see ``test_core_decomposition_maintainability``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.session import make_session_factory
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
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
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a scoped async SQLAlchemy session factory for tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_failed_pre_push_validation_cleans_before_fix_pass(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed validation pass must not hand dirty validation side effects to the fix agent."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    local_head = "d" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=" M apps/console/next-env.d.ts\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=False))  # type: ignore[assignment]
    fix_called = False

    async def _assert_clean_before_fix(
        _runner: object, **_kwargs: object
    ) -> tuple[bool, str | None]:
        """Assert validation did cleanup worktree state before starting a fix pass."""
        nonlocal fix_called
        fix_called = True
        assert any(
            call.args[-4:]
            == [
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored=matching",
            ]
            for call in cmd.calls
        )
        return False, None

    monkeypatch.setattr(
        pre_push_validation,
        "_run_pre_push_validation_fix_pass",
        _assert_clean_before_fix,
    )

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_FIX_FAILED"
    assert fix_called is True


@pytest.mark.unit
async def test_pre_push_validation_untracked_cleanup_allows_fix_pass(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A failed validation with removable untracked artifacts should still run fix passes."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    local_head = "d" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="?? validation-artifact.log\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {local_head[:8]}\n")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=False))  # type: ignore[assignment]

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_FIX_FAILED"
    assert "fix pass failed" in str(result.stderr)
    assert adapter.calls


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_runs_cleanup_after_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed fix pass should clean side effects against the committed head."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed tests\n")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    fix_start_head = "1" * 40
    committed_head = "2" * 40
    rev_parse_results = [fix_start_head, committed_head]

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
    assert cleanup_calls == [
        {
            "worktree_path": worktree,
            "restore_ref": committed_head,
        }
    ]
    assert rev_parse_results == []


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_detects_agent_self_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fix-pass agent that self-commits (HEAD advances, clean tree) is not rolled back."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    adapter = FakeAdapter()
    adapter.queue(stdout="self-committed fix\n")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    fix_start_head = "1" * 40
    advanced_head = "2" * 40
    rev_parse_results: list[str | None] = [fix_start_head, advanced_head]

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return rev_parse_results.pop(0)

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        """Clean worktree after the agent self-committed: nothing left to commit."""
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
        raise AssertionError("self-committed repair must not be rolled back")

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
    assert cleanup_calls == [
        {
            "worktree_path": worktree,
            "restore_ref": advanced_head,
        }
    ]
    assert rev_parse_results == []
    joined_calls = [
        " ".join(call.args) for call in cast(FakeCommandRunner, runner._deps.runner).calls
    ]
    assert not any(f"reset --hard {fix_start_head}" in call for call in joined_calls)


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_prompt_carries_task_tag(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tagged workspace's pre-push fix prompt must instruct the agent to tag self-commits.

    Regression for PRRT_kwDOSJAM6s6I_fvp: the monitor pre-push validation-fix path built
    ``ValidationFixContext`` without ``task_tag``, so a fix agent that self-committed its repair
    left a commit pushed without the Jira key.
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    from awf.db.repositories import WorkspaceRepository

    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        ws.task_tag = "PROJ-123"
        await s.commit()
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    adapter = FakeAdapter()
    adapter.queue(stdout="self-committed fix\n")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    fix_start_head = "1" * 40
    advanced_head = "2" * 40
    rev_parse_results: list[str | None] = [fix_start_head, advanced_head]

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return rev_parse_results.pop(0)

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        """Clean worktree after the agent self-committed: nothing left to commit."""
        return False

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
    assert adapter.calls
    assert "PROJ-123" in adapter.calls[0]


@pytest.mark.unit
@pytest.mark.parametrize("post_commit_head", ["fix_start", None])
async def test_pre_push_validation_fix_pass_genuine_no_commit_still_rolls_back(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    post_commit_head: str | None,
) -> None:
    """A clean worktree with HEAD unchanged (or unreadable) still rolls back to fix_start_head."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    adapter = FakeAdapter()
    adapter.queue(stdout="no-op fix\n")
    cmd = FakeCommandRunner()
    fix_start_head = "1" * 40
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {fix_start_head[:8]}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    reread_head = fix_start_head if post_commit_head == "fix_start" else None
    rev_parse_results: list[str | None] = [fix_start_head, reread_head]

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
    assert any(f"reset --hard {fix_start_head}" in call for call in joined_calls)
    assert cleanup_calls == [{"worktree_path": worktree, "restore_ref": fix_start_head}]
    assert rev_parse_results == []


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_non_descendant_head_is_reparented(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean worktree whose HEAD was rewritten off ``fix_start_head`` in a way that
    DROPS the validation-fix work (the ``git reset --hard HEAD~1`` + recommit hole) is
    no longer rolled back. Per issue #411 we STOP DISCRIMINATING and ALWAYS re-parent
    the agent's resulting tree onto ``fix_start_head`` (no ``merge-tree``), so the fix
    pass converges; any dropped work stays auditable and is caught by re-validation."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    adapter = FakeAdapter()
    adapter.queue(stdout="agent reset HEAD backward\n")
    cmd = FakeCommandRunner()
    fix_start_head = "1" * 40
    divergent_head = "3" * 40
    current_tree = "b" * 40
    start_tree = "c" * 40
    reparented_head = "4" * 40
    # merge-base --is-ancestor reports non-descendant (exit 1); we always re-parent:
    # rev-parse the two trees (they differ), derive a message, commit-tree onto
    # fix_start_head, then reset --hard to the reconstructed commit.
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0, stdout=f"{current_tree}\n")
    cmd.queue_result(returncode=0, stdout=f"{start_tree}\n")
    cmd.queue_result(returncode=0, stdout="agent fix message\n")
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
        """Clean worktree after the agent moved HEAD: nothing left to commit."""
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
        raise AssertionError("a non-descendant rewrite must be re-parented, not rolled back")

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
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(
        f"merge-base --is-ancestor {fix_start_head} {divergent_head}" in call
        for call in joined_calls
    )
    # No tree-content discrimination: the reconstructed commit is parented on
    # fix_start_head using the agent's resulting (current) tree.
    assert not any("merge-tree" in call for call in joined_calls)
    assert any(
        f"commit-tree {current_tree} -p {fix_start_head} -m" in call for call in joined_calls
    )
    assert any(f"reset --hard {reparented_head}" in call for call in joined_calls)
    # The pre-fix-pass head is never restored: it is preserved as the parent.
    assert not any(f"reset --hard {fix_start_head}" in call for call in joined_calls)
    assert cleanup_calls == [{"worktree_path": worktree, "restore_ref": reparented_head}]
    assert rev_parse_results == []


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_amend_self_commit_is_reparented(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-descendant rewrite (``git commit --amend``) is accepted as a committed
    repair by re-parenting onto ``fix_start_head`` (issue #408 behavior is now
    subsumed by the always-reparent rule of issue #411 — no ``merge-tree``)."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    adapter = FakeAdapter()
    adapter.queue(stdout="amended self-commit\n")
    cmd = FakeCommandRunner()
    fix_start_head = "1" * 40
    amended_head = "2" * 40
    amended_tree = "a" * 40
    start_tree = "b" * 40
    reparented_head = "5" * 40
    # merge-base --is-ancestor reports non-descendant (exit 1); we re-parent the
    # amended tree onto fix_start_head (trees differ), then reset to the new commit.
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0, stdout=f"{amended_tree}\n")
    cmd.queue_result(returncode=0, stdout=f"{start_tree}\n")
    cmd.queue_result(returncode=0, stdout="amended commit message\n")
    cmd.queue_result(returncode=0, stdout=f"{reparented_head}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {reparented_head[:8]}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    rev_parse_results: list[str | None] = [fix_start_head, amended_head]

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return rev_parse_results.pop(0)

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        """Clean worktree after the agent amended its self-commit."""
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
        raise AssertionError("amended rewrite must be re-parented, not rolled back")

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
    assert cleanup_calls == [{"worktree_path": worktree, "restore_ref": reparented_head}]
    assert rev_parse_results == []
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert not any("merge-tree" in call for call in joined_calls)
    assert any(
        f"commit-tree {amended_tree} -p {fix_start_head} -m" in call for call in joined_calls
    )
    assert any(f"reset --hard {reparented_head}" in call for call in joined_calls)
    assert not any(f"reset --hard {fix_start_head}" in call for call in joined_calls)


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_self_commit_cleanup_failure_surfaces_reason(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup failure against a self-committed head surfaces the cleanup reason code."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    adapter = FakeAdapter()
    adapter.queue(stdout="self-committed fix\n")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    fix_start_head = "1" * 40
    advanced_head = "2" * 40
    rev_parse_results: list[str | None] = [fix_start_head, advanced_head]

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return rev_parse_results.pop(0)

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        return False

    async def _pre_push_validation_cleanup(
        _runner: object,
        **kwargs: object,
    ) -> ValidationWorktreeCleanup:
        return ValidationWorktreeCleanup(
            cleaned=False,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref=cast(str, kwargs["restore_ref"]),
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        )

    async def _rollback_should_not_run(*_args: object, **_kwargs: object) -> str | None:
        raise AssertionError("self-committed repair must not be rolled back")

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
    assert cleanup_failure_reason == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert rev_parse_results == []


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_commit_head_capture_failure_is_infrastructure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-commit HEAD capture failure is not a validation-worktree cleanup failure."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed tests\n")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    fix_start_head = "1" * 40
    rev_parse_results: list[str | None] = [fix_start_head, None]

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return rev_parse_results.pop(0)

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        return True

    async def _pre_push_validation_cleanup(
        _runner: object,
        **_kwargs: object,
    ) -> ValidationWorktreeCleanup:
        raise AssertionError("cleanup should not run without a committed HEAD")

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
    assert failure_reason == pre_push_validation.PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON
    assert rev_parse_results == []


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_cleanup_failure_stops_retry(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup failure after a committed fix must surface terminally instead of retrying."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = "workspace_fix_commit_cleanup_failed"
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
            raise AssertionError("cleanup failure should stop before retry validation")
        return validation_result

    async def _run_fix_pass(_runner: object, **_kwargs: object) -> tuple[bool, str | None]:
        return True, VALIDATION_WORKTREE_CLEANUP_FAILED

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
    assert result.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert "fix pass cleanup failed" in result.message


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_infrastructure_failure_avoids_cleanup_label(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Committed fix-pass infrastructure failures should not be reported as cleanup failures."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = "workspace_fix_commit_head_unavailable"
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
            raise AssertionError("infrastructure failure should stop before retry validation")
        return validation_result

    async def _run_fix_pass(_runner: object, **_kwargs: object) -> tuple[bool, str | None]:
        return True, pre_push_validation.PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON

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
    assert (
        result.reason_code == pre_push_validation.PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON
    )
    assert "fix pass infrastructure failed" in result.message
    assert "cleanup failed" not in result.message
