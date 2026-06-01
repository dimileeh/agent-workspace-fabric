"""Pre-push validation fix-pass and repair flow tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor_runner import pre_push_validation as pre_push_validation_module
from awf.runtime.validation import ValidationResult
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
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
from tests.unit.runtime.test_pr_monitor_pre_push_validation import (
    _FakeValidation,
    _mark_git_worktree,
    _provider_coverage_failure_without_command,
    _set_resolved_profile,
    _validation_result,
    _validation_runs,
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
async def test_pre_push_validation_fix_pass_uses_initial_ignored_snapshot_across_retries(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry attempts should reuse the first ignored snapshot instead of recapturing all ignored files."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = "workspace_fix_retry"
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True, exist_ok=True)
    fix_pass_calls: list[dict[str, object]] = []
    validation_calls: list[dict[str, object]] = []
    validation_results = [
        pre_push_validation._PrePushValidationResult(
            passed=False,
            validation_run_id="vr1",
            workspace_head_sha="a" * 40,
            reason_code="PRE_PUSH_VALIDATION_FAILED",
            message="attempt 1 failed",
            validation_reason_code="PYTEST_TEST_FAILURE",
            result=_validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
            ignore_ignored_paths=(),
            ignore_ignored_paths_snapshot=(),
        ),
        pre_push_validation._PrePushValidationResult(
            passed=False,
            validation_run_id="vr2",
            workspace_head_sha="a" * 40,
            reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
            message="attempt 2 failed",
            result=None,
        ),
    ]

    async def _run_pre_push_validation(
        _self: Any,
        *,
        ignore_ignored_paths: tuple[str, ...] | None,
        ignore_all_ignored: bool,
        capture_ignored_paths_snapshot: bool,
        **_kwargs: object,
    ) -> pre_push_validation._PrePushValidationResult:
        validation_calls.append(
            {
                "ignore_ignored_paths": ignore_ignored_paths,
                "ignore_all_ignored": ignore_all_ignored,
                "capture_ignored_paths_snapshot": capture_ignored_paths_snapshot,
            }
        )
        return validation_results.pop(0)

    async def _run_fix_pass(_runner: object, **_kwargs: object) -> tuple[bool, str | None]:
        fix_pass_calls.append(cast(dict[str, object], _kwargs))
        return True, None

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

    assert len(validation_calls) == 2
    assert validation_calls[0]["ignore_all_ignored"] is True
    assert validation_calls[0]["ignore_ignored_paths"] is None
    assert validation_calls[0]["capture_ignored_paths_snapshot"] is True
    assert validation_calls[1]["ignore_all_ignored"] is True
    assert validation_calls[1]["ignore_ignored_paths"] == ()
    assert validation_calls[1]["capture_ignored_paths_snapshot"] is False
    assert len(fix_pass_calls) == 1
    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_rejects_new_ignored_paths_on_retry(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Newly introduced ignored entries should block retry logic before a fix pass."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = "workspace_fix_retry_new_ignored"
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True, exist_ok=True)
    validation_calls: list[dict[str, object]] = []
    fix_pass_calls: list[dict[str, object]] = []
    validation_results = [
        pre_push_validation._PrePushValidationResult(
            passed=False,
            validation_run_id="vr1",
            workspace_head_sha="a" * 40,
            reason_code="PRE_PUSH_VALIDATION_FAILED",
            message="attempt 1 failed",
            validation_reason_code="PYTEST_TEST_FAILURE",
            result=_validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
            ignore_ignored_paths=(".venv/",),
            ignore_ignored_paths_snapshot=(".venv/existing-artifact.log",),
            ignore_ignored_paths_snapshot_signatures=(
                (".venv/existing-artifact.log", "sig-existing"),
            ),
        ),
        pre_push_validation._PrePushValidationResult(
            passed=False,
            validation_run_id="vr2",
            workspace_head_sha="a" * 40,
            reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
            message="attempt 2 failed",
            result=None,
            ignore_ignored_paths=(".venv/",),
            ignore_ignored_paths_snapshot=(
                ".venv/existing-artifact.log",
                ".venv/new-artifact.log",
            ),
            ignore_ignored_paths_snapshot_signatures=(
                (".venv/existing-artifact.log", "sig-existing"),
                (".venv/new-artifact.log", "sig-new"),
            ),
        ),
    ]

    async def _run_pre_push_validation(
        _self: Any,
        **_kwargs: object,
    ) -> pre_push_validation._PrePushValidationResult:
        validation_calls.append(cast(dict[str, object], _kwargs))
        return validation_results.pop(0)

    async def _run_fix_pass(_runner: object, **kwargs: object) -> tuple[bool, str | None]:
        fix_pass_calls.append(cast(dict[str, object], kwargs))
        return True, None

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

    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert len(validation_calls) == 2
    assert len(fix_pass_calls) == 1


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_cleans_ignored_artifacts_after_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed fix pass should clean new ignored artifacts before retrying validation."""
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
        ignore_ignored_paths=(".venv/",),
        ignore_ignored_paths_snapshot=(".venv/existing-artifact.log",),
        ignore_ignored_paths_snapshot_signatures=((".venv/existing-artifact.log", "sig-existing"),),
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
            "ignore_ignored_paths": (".venv/",),
            "ignore_ignored_paths_snapshot": (".venv/existing-artifact.log",),
            "ignore_ignored_paths_snapshot_signatures": (
                (".venv/existing-artifact.log", "sig-existing"),
            ),
        }
    ]
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
        ignore_ignored_paths=(".venv/",),
        ignore_ignored_paths_snapshot=(".venv/existing-artifact.log",),
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
async def test_run_pre_push_validation_rejects_new_ignored_entries_before_validation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gained ignored snapshot should fail before any validation command executes."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    local_head = "a" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(_validation_result(tmp_path, ok=False))  # type: ignore[assignment]

    async def _run_pre_push_validation_worktree_check(
        _self: object,
        **_kwargs: object,
    ) -> ValidationWorktreeCheck:
        return ValidationWorktreeCheck(
            clean=True,
            ignored_paths=(".venv/",),
            ignored_paths_snapshot=(
                ".venv/existing-artifact.log",
                ".venv/new-artifact.log",
            ),
            ignored_paths_snapshot_signatures=(
                (".venv/existing-artifact.log", "sig-existing"),
                (".venv/new-artifact.log", "sig-new"),
            ),
        )

    started_runs: list[str] = []

    async def _start_pre_push_validation_run(
        _self: object,
        **_kwargs: object,
    ) -> str:
        started_runs.append("started")
        return "vr-gained-ignored"

    finish_calls: list[dict[str, object]] = []

    async def _finish_pre_push_validation_run(
        _self: object,
        validation_run_id: str,
        *,
        status: str,
        reason_code: str | None,
        retry_count: int = 0,
        coverage: dict[str, object] | None = None,
        command_retries: list[int] | None = None,
    ) -> None:
        finish_calls.append(
            {
                "validation_run_id": validation_run_id,
                "status": status,
                "reason_code": reason_code,
                "retry_count": retry_count,
                "coverage": coverage,
                "command_retries": command_retries,
            }
        )

    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_worktree_check",
        _run_pre_push_validation_worktree_check,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_start_pre_push_validation_run",
        _start_pre_push_validation_run,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_finish_pre_push_validation_run",
        _finish_pre_push_validation_run,
    )

    result = await pre_push_validation._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
        ignore_ignored_paths=(".venv/",),
        ignore_all_ignored=True,
        capture_ignored_paths_snapshot=False,
        baseline_ignored_roots=(".venv/",),
        baseline_ignored_paths_snapshot=(".venv/existing-artifact.log",),
        baseline_ignored_paths_snapshot_signatures=(
            (".venv/existing-artifact.log", "sig-existing"),
        ),
    )

    assert result.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert result.validation_run_id == "vr-gained-ignored"
    assert "Validation worktree ignored entries changed after setup baseline" in result.message
    assert started_runs == ["started"]
    assert len(finish_calls) == 1
    assert finish_calls[0]["status"] == "failed"
    assert finish_calls[0]["reason_code"] == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert runner._deps.validation.calls == []


@pytest.mark.unit
def test_pre_push_validation_ignored_entries_drifted_rejects_removed_snapshot_paths() -> None:
    """Deleted baseline ignored artifacts should be treated as ignored drift."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    assert pre_push_validation._pre_push_validation_ignored_entries_drifted(
        baseline_ignored_roots=(".venv/",),
        baseline_ignored_snapshot=(".venv/existing-artifact.log",),
        baseline_ignored_snapshot_signatures=((".venv/existing-artifact.log", "sig-existing"),),
        current_ignored_roots=(".venv/",),
        current_ignored_snapshot=(),
        current_ignored_snapshot_signatures=(),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("baseline_signatures", "current_signatures"),
    [
        (((".venv/existing-artifact.log", "sig-existing"),), ()),
        ((), ((".venv/existing-artifact.log", "sig-existing"),)),
    ],
)
def test_pre_push_validation_ignored_entries_drifted_rejects_one_sided_signature_drift(
    baseline_signatures: tuple[tuple[str, str], ...],
    current_signatures: tuple[tuple[str, str], ...],
) -> None:
    """One-sided ignored artifact signatures should be treated as ignored drift."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    assert pre_push_validation._pre_push_validation_ignored_entries_drifted(
        baseline_ignored_roots=(".venv/",),
        baseline_ignored_snapshot=(".venv/existing-artifact.log",),
        baseline_ignored_snapshot_signatures=baseline_signatures,
        current_ignored_roots=(".venv/",),
        current_ignored_snapshot=(".venv/existing-artifact.log",),
        current_ignored_snapshot_signatures=current_signatures,
    )


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_rolls_back_when_commit_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed validation-fix commit must not leave staged changes for the next repair."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    fix_start_head = "e" * 40
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {fix_start_head[:8]}\n")
    cmd.queue_result(returncode=0, stdout="?? generated.tmp\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted fix\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        """Simulate a validation-fix commit failure."""
        return False

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=fix_start_head,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )

    committed, rollback_failed = await pre_push_validation._run_pre_push_validation_fix_pass(
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
    assert rollback_failed is None
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(f"reset --hard {fix_start_head}" in call for call in joined_calls)
    assert any("clean -ffdx" in call for call in joined_calls)


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_without_failure_returns_false() -> None:
    """A validation result with no command failure should not invoke a fix agent."""
    validation_result = pre_push_validation_module._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_provider",
        workspace_head_sha="a" * 40,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="coverage provider failed",
        validation_reason_code="COVERAGE_PROVIDER_FAILED",
        result=ValidationResult(coverage=_provider_coverage_failure_without_command()),
    )

    committed, rollback_failed = await pre_push_validation_module._run_pre_push_validation_fix_pass(
        object(),
        workspace_id="ws_provider",
        compose_project="proj",
        compose_file=Path("compose.yml"),
        remote_branch="awf/ws_provider",
        remote_url=None,
        state=None,
        validation_result=validation_result,
        pass_number=1,
        total_passes=1,
        validation_commands=(),
    )

    assert committed is False
    assert rollback_failed is None


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_rolls_back_when_commit_raises(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit-path exception should not strand the fix-pass worktree delta."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    fix_start_head = "9" * 40
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {fix_start_head[:8]}\n")
    cmd.queue_result(returncode=0, stdout="?? generated.tmp\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted fix\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        """Simulate a validation-fix commit failure."""
        raise RuntimeError("commit path failed")

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=fix_start_head,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )

    committed, rollback_failed = await pre_push_validation._run_pre_push_validation_fix_pass(
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
    assert rollback_failed is None
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(f"reset --hard {fix_start_head}" in call for call in joined_calls)
    assert any("clean -ffdx" in call for call in joined_calls)


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_rollback_preserves_ignored_paths(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Rollback should keep ignored artifacts like .venv while removing generated files."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    cmd = cast(FakeCommandRunner, runner._deps.runner)
    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    restore_ref = "d" * 40

    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {restore_ref[:8]}\n")
    cmd.queue_result(returncode=0, stdout="?? generated.tmp\n!! .venv/\n")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")

    rollback_failure_reason = (
        await pre_push_validation._rollback_failed_pre_push_validation_fix_pass(
            runner,
            workspace_id="workspace",
            worktree_path=worktree,
            restore_ref=restore_ref,
            ignore_ignored_paths=(".venv",),
            pass_number=1,
            reason="test",
        )
    )

    assert rollback_failure_reason is None
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(f"reset --hard {restore_ref}" in call for call in joined_calls)
    assert any("clean -ffdx -- generated.tmp" in call for call in joined_calls)
    assert all(not ("clean -ffdx" in call and ".venv" in call) for call in joined_calls)


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_rollback_failure_is_bubbled_as_pre_push_validation_rollback_failed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed rollback after a fix pass should surface a distinct rollback failure code."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'f' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"{'f' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {'f' * 8}\n")
    cmd.queue_result(returncode=0, stdout="")
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted fix\n")

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(  # type: ignore[assignment]
        _validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
    )

    async def _rollback_failed(*_args: object, **_kwargs: object) -> str:
        """Simulate a rollback failure in fix-pass cleanup."""
        return "PRE_PUSH_VALIDATION_ROLLBACK_FAILED"

    async def _commit_failed(**_kwargs: object) -> bool:
        """Simulate a repair commit failure exception path."""
        return False

    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback_failed,
    )
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_failed)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_ROLLBACK_FAILED"


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_post_reset_cleanup_failure_surfaces_cleanup_reason(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful reset plus failed cleanup should not be labeled rollback failed."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    restore_ref = "6" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {restore_ref[:8]}\n")
    cmd.queue_result(returncode=0, stdout="?? validation-artifact.log\n")
    cmd.queue_result(returncode=1, stderr="clean failed")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    cmd.queue_result(returncode=0, stdout=f"{restore_ref}\n")
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted fix\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(  # type: ignore[assignment]
        _validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
    )

    async def _commit_failed(**_kwargs: object) -> bool:
        """Simulate a repair commit failure after the agent attempted a fix."""
        return False

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_failed)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert "rollback failed" not in result.stderr
    assert "cleanup failed" in result.stderr
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_rollback_does_not_clean_when_reset_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A failed rollback reset should preserve untracked files for manual recovery."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    cmd = cast(FakeCommandRunner, runner._deps.runner)
    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    restore_ref = "b" * 40
    cmd.queue_result(returncode=1, stdout="")

    rollback_failure_reason = (
        await pre_push_validation._rollback_failed_pre_push_validation_fix_pass(
            runner,
            workspace_id="workspace",
            worktree_path=worktree,
            restore_ref=restore_ref,
            pass_number=1,
            reason="reset_failed",
        )
    )

    assert rollback_failure_reason == "PRE_PUSH_VALIDATION_ROLLBACK_FAILED"
    joined_calls = [" ".join(call.args) for call in cmd.calls]
    assert any(f"reset --hard {restore_ref}" in call for call in joined_calls)
    assert not any("clean -ffdx" in call for call in joined_calls)


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_revalidates_before_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair passes should re-run validation before allowing push."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    first_head = "b" * 40
    fixed_head = "c" * 40
    cmd.queue_result(returncode=0, stdout=f"{first_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{first_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{fixed_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{fixed_head}\n")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed validation\n")
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
    committed: list[str] = []

    async def _commit_dirty(**kwargs: object) -> bool:
        """Record a synthetic commit and return a successful outcome."""
        committed.append(str(kwargs["message"]))
        return True

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert committed == [f"awf: pre-push validation fix for {workspace_id}"]
    assert len(adapter.calls) == 1
    runs = await _validation_runs(factory, workspace_id)
    assert runs[-1].target_head_sha == fixed_head


@pytest.mark.unit
async def test_pre_push_validation_fix_prompt_includes_underlying_reason_code(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix prompts should include the first failing validation reason code."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    first_head = "d" * 40
    fixed_head = "e" * 40
    cmd.queue_result(returncode=0, stdout=f"{first_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{first_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{fixed_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{fixed_head}\n")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed validation\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(  # type: ignore[assignment]
        _validation_result(
            tmp_path,
            ok=False,
            reason_code="PYTEST_TEST_FAILURE",
        ),
        _validation_result(tmp_path, ok=True),
    )
    committed: list[str] = []

    async def _commit_dirty(**kwargs: object) -> bool:
        """Record a synthetic commit and return a successful outcome."""
        committed.append(str(kwargs["message"]))
        return True

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert committed == [f"awf: pre-push validation fix for {workspace_id}"]
    assert len(adapter.calls) == 1
    assert "Reason code: PYTEST_TEST_FAILURE" in adapter.calls[0]


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_commits_agent_failure_evidence(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-zero fix agents should preserve evidence and still commit attempted fixes."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    first_head = "f" * 40
    fixed_head = "1" * 40
    cmd.queue_result(returncode=0, stdout=f"{first_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{first_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{fixed_head}\n")
    cmd.queue_result(returncode=0, stdout=f"{fixed_head}\n")
    cmd.queue_result(returncode=0, stdout="", stderr="")
    adapter = FakeAdapter()
    adapter.queue(stdout="agent stdout", stderr="agent stderr", returncode=2)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(  # type: ignore[assignment]
        _validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
        _validation_result(tmp_path, ok=True),
    )
    committed: list[dict[str, object]] = []

    async def _commit_dirty(**kwargs: object) -> bool:
        """Record the attempted fix commit and report success."""
        committed.append(kwargs)
        return True

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is False
    assert result.pushed is True
    assert len(adapter.calls) == 1
    assert committed[0]["message"] == f"awf: pre-push validation fix for {workspace_id}"
    assert "agent stdout" in "\n".join(committed[0]["command_evidence"])  # type: ignore[index]
    assert "agent stderr" in "\n".join(committed[0]["command_evidence"])  # type: ignore[index]


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_cleanup_failure_blocks_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Fix-pass cleanup failures should surface as fix failures and avoid push."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'2' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"{'2' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {'2' * 8}\n")
    cmd.queue_result(returncode=0, stdout="")
    adapter = FakeAdapter()
    adapter.queue(
        exc=ComposeExecCleanupError(
            invocation_id="awf_pre_push_fix_cleanup",
            source="agent",
            label="monitor-pre-push-validation-fix",
            message="tagged process still running",
        )
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(  # type: ignore[assignment]
        _validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
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
    assert "fix pass failed" in result.stderr
    assert "git push" not in [" ".join(call.args) for call in cmd.calls]


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_commit_fail_returns_fix_failed_reason_code(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed fix commit attempts should surface ``PRE_PUSH_VALIDATION_FIX_FAILED``."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'f' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"{'f' * 40}\n")
    cmd.queue_result(returncode=0, stdout=f"HEAD is now at {'f' * 8}\n")
    cmd.queue_result(returncode=0, stdout="")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = _FakeValidation(  # type: ignore[assignment]
        _validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
    )

    async def _no_commit(**_kwargs: object) -> bool:
        """Return a failed commit result for the fix-pass test."""
        return False

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _no_commit)

    result = await runner._validated_git_push_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PRE_PUSH_VALIDATION_FIX_FAILED"
    assert result.details is not None
    assert result.details["validation_reason_code"] == "PYTEST_TEST_FAILURE"
    assert result.details["failing_command"] == "pytest -q"
    assert result.details["failing_returncode"] == 1
    assert "fix pass failed" in result.stderr
