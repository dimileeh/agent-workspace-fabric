"""Pre-push validation fix-pass recovered-delta environment tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor_runner.constants import (
    _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
    _MIRROR_HOOKS_PATH_POISONED_REASON,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor_pre_push_validation import (
    _mark_git_worktree,
    _validation_result,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a scoped async SQLAlchemy session factory for tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _failed_validation_result(
    pre_push_validation: Any,
    tmp_path: Path,
    *,
    workspace_head_sha: str,
) -> object:
    return pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=workspace_head_sha,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )


async def _make_fix_pass_runner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> tuple[str, object, FakeCommandRunner, FakeAdapter]:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    return workspace_id, runner, cmd, adapter


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_recovered_delta_strips_git_object_lookup_env(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovered commit-range protected-scope diff must ignore inherited object dirs."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass

    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    fix_start_head = "1" * 40
    recovered_head = "2" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
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

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        *_args: object,
        **_kwargs: object,
    ) -> str:
        return recovered_head

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        return False

    async def _rollback_failed_fix_pass(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(fix_pass, "verify_head_object_exists", _verify_head_object_exists)
    monkeypatch.setattr(
        fix_pass,
        "_recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )
    monkeypatch.setattr(fix_pass, "mirror_path_for_worktree", lambda _worktree_path: None)
    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback_failed_fix_pass,
    )
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
    assert failure_reason is None
    recovered_delta_call = next(
        call
        for call in cmd.calls
        if call.args[-4:] == ["diff", "--name-status", "-z", f"{fix_start_head}..{recovered_head}"]
    )
    assert recovered_delta_call.env is not None
    assert "GIT_OBJECT_DIRECTORY" not in recovered_delta_call.env
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in recovered_delta_call.env


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_returns_post_agent_mirror_repair_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass

    fix_start_head = "1" * 40
    workspace_id, runner, cmd, adapter = await _make_fix_pass_runner(factory, tmp_path)
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter.queue(stdout="attempted fix\n")
    repair_calls = 0

    async def _repair_mirror_hooks(**_kwargs: object) -> str | None:
        nonlocal repair_calls
        repair_calls += 1
        return _MIRROR_HOOKS_PATH_POISONED_REASON if repair_calls == 2 else None

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        raise AssertionError("commit should not run after post-agent mirror repair failure")

    monkeypatch.setattr(fix_pass, "mirror_path_for_worktree", lambda _worktree_path: tmp_path)
    monkeypatch.setattr(
        fix_pass, "_repair_pre_push_validation_fix_mirror_hooks", _repair_mirror_hooks
    )
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)

    committed, failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=_failed_validation_result(
            pre_push_validation,
            tmp_path,
            workspace_head_sha=fix_start_head,
        ),
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert (committed, failure_reason) == (False, _MIRROR_HOOKS_PATH_POISONED_REASON)
    assert repair_calls == 2


@pytest.mark.unit
@pytest.mark.parametrize(
    ("timeout_reason_code", "dirty_changes_committed"),
    (
        ("AGENT_IDLE_TIMEOUT", True),
        ("AGENT_TIMEOUT", True),
        ("AGENT_TIMEOUT", False),
    ),
)
async def test_pre_push_validation_fix_pass_timeout_cleanup_failure_preserves_then_propagates(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeout_reason_code: str,
    dirty_changes_committed: bool,
) -> None:
    """A cleanup error masking a timeout preserves work but remains authoritative."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass

    fix_start_head = "2" * 40
    workspace_id, runner, _cmd, _adapter = await _make_fix_pass_runner(factory, tmp_path)
    committed_head = "3" * 40
    rev_parse_heads = [
        fix_start_head,
        committed_head if dirty_changes_committed else fix_start_head,
    ]
    cleanup_error = ComposeExecCleanupError(
        invocation_id="awf_timeout_cleanup",
        source="agent",
        label="monitor-pre-push-validation-fix",
        message="tagged process still running",
    )
    cleanup_error.agent_reason_code = timeout_reason_code
    rollback_calls: list[str] = []
    commit_calls: list[dict[str, object]] = []
    cleanup_calls: list[str] = []

    async def _run_agent_with_recovery(**kwargs: object) -> None:
        assert kwargs["timeout_rerun_requires_preservation"] is True
        raise cleanup_error

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return rev_parse_heads.pop(0)

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return True

    async def _commit_dirty_worktree(**kwargs: object) -> bool:
        commit_calls.append(dict(kwargs))
        return dirty_changes_committed

    async def _head_descends_from(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _cleanup_committed_fix_pass(*_args: object, **kwargs: object) -> None:
        cleanup_calls.append(str(kwargs["committed_head"]))

    async def _rollback_failed_fix_pass(*_args: object, **kwargs: object) -> None:
        rollback_calls.append(str(kwargs["reason"]))

    monkeypatch.setattr(
        runner,
        "_run_monitor_agent_with_service_recovery",
        _run_agent_with_recovery,
    )
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    monkeypatch.setattr(fix_pass, "mirror_path_for_worktree", lambda _worktree_path: None)
    monkeypatch.setattr(fix_pass, "verify_head_object_exists", _verify_head_object_exists)
    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback_failed_fix_pass,
    )
    monkeypatch.setattr(pre_push_validation, "_head_descends_from", _head_descends_from)
    monkeypatch.setattr(
        pre_push_validation,
        "_cleanup_committed_pre_push_validation_fix_pass",
        _cleanup_committed_fix_pass,
    )

    with pytest.raises(ComposeExecCleanupError) as raised:
        await pre_push_validation._run_pre_push_validation_fix_pass(
            runner,
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            remote_branch="codex/pr",
            remote_url=None,
            state=None,
            validation_result=_failed_validation_result(
                pre_push_validation,
                tmp_path,
                workspace_head_sha=fix_start_head,
            ),
            pass_number=1,
            total_passes=1,
            validation_commands=("pytest -q",),
        )

    assert raised.value is cleanup_error
    assert rollback_calls == []
    assert len(commit_calls) == 1
    assert commit_calls[0]["operation_start_head"] == fix_start_head
    assert cleanup_calls == ([committed_head] if dirty_changes_committed else [])


@pytest.mark.unit
@pytest.mark.parametrize("probe_failure", ("raises", "stalls", "cancels"))
async def test_pre_push_validation_fix_pass_timeout_head_probe_is_bounded_best_effort(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe_failure: str,
) -> None:
    """A preservation-only HEAD probe cannot mask an unproven cleanup failure."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass

    fix_start_head = "8" * 40
    workspace_id, runner, _cmd, _adapter = await _make_fix_pass_runner(factory, tmp_path)
    cleanup_error = ComposeExecCleanupError(
        invocation_id="awf_timeout_cleanup_head_probe",
        source="agent",
        label="monitor-pre-push-validation-fix",
        message="tagged process still running",
    )
    cleanup_error.agent_reason_code = "AGENT_TIMEOUT"
    probe_started = asyncio.Event()

    async def _run_agent_with_recovery(**_kwargs: object) -> None:
        raise cleanup_error

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return fix_start_head

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        probe_started.set()
        if probe_failure == "raises":
            raise OSError("cannot spawn git")
        if probe_failure == "cancels":
            raise asyncio.CancelledError
        await asyncio.Future()
        raise AssertionError("unreachable")

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        raise AssertionError("commit should not run when the HEAD probe cannot answer")

    monkeypatch.setattr(
        runner,
        "_run_monitor_agent_with_service_recovery",
        _run_agent_with_recovery,
    )
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    monkeypatch.setattr(fix_pass, "mirror_path_for_worktree", lambda _path: None)
    monkeypatch.setattr(fix_pass, "verify_head_object_exists", _verify_head_object_exists)
    monkeypatch.setattr(
        fix_pass,
        "_TIMEOUT_CLEANUP_HEAD_PROBE_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )

    expected_exception = (
        asyncio.CancelledError if probe_failure == "cancels" else ComposeExecCleanupError
    )
    fix_pass_task = asyncio.create_task(
        pre_push_validation._run_pre_push_validation_fix_pass(
            runner,
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            remote_branch="codex/pr",
            remote_url=None,
            state=None,
            validation_result=_failed_validation_result(
                pre_push_validation,
                tmp_path,
                workspace_head_sha=fix_start_head,
            ),
            pass_number=1,
            total_passes=1,
            validation_commands=("pytest -q",),
        )
    )
    probe_started_task: asyncio.Task[bool] | None = None
    fix_pass_result_consumed = False
    # Give unrelated setup and database scheduling their own generous guard.
    # The tight bound below begins only when the preservation probe actually
    # starts, which keeps this assertion meaningful under loaded CI shards.
    try:
        probe_started_task = asyncio.create_task(probe_started.wait())
        started, _pending = await asyncio.wait(
            {probe_started_task, fix_pass_task},
            timeout=5.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if probe_started_task not in started:
            if fix_pass_task in started:
                await fix_pass_task
                fix_pass_result_consumed = True
            pytest.fail("preservation probe did not start within 5 seconds")

        with pytest.raises(expected_exception) as raised:
            await asyncio.wait_for(fix_pass_task, timeout=0.5)
        fix_pass_result_consumed = True
    finally:
        for task in (fix_pass_task, probe_started_task):
            if task is None:
                continue
            if not task.done():
                task.cancel()
        try:
            if not fix_pass_result_consumed:
                with suppress(asyncio.CancelledError):
                    await fix_pass_task
        finally:
            if probe_started_task is not None:
                with suppress(asyncio.CancelledError):
                    await probe_started_task

    if probe_failure != "cancels":
        assert raised.value is cleanup_error


@pytest.mark.unit
@pytest.mark.parametrize(
    "failure_path",
    (
        "post_agent_mirror_repair",
        "recovery_anchor_missing",
        "filesystem_recovery_failed",
        "recovered_delta_failed",
        "recovered_protected_scope_blocked",
    ),
)
async def test_pre_push_validation_fix_pass_early_failure_preserves_timeout_cleanup_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_path: str,
) -> None:
    """Post-agent preservation failures cannot mask unproven timeout cleanup."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass

    fix_start_head = "6" * 40
    recovered_head = "7" * 40
    workspace_id, runner, cmd, _adapter = await _make_fix_pass_runner(factory, tmp_path)
    cleanup_error = ComposeExecCleanupError(
        invocation_id="awf_timeout_cleanup_early_failure",
        source="agent",
        label="monitor-pre-push-validation-fix",
        message="tagged process still running",
    )
    cleanup_error.agent_reason_code = "AGENT_TIMEOUT"
    repair_calls = 0

    if failure_path == "recovered_delta_failed":
        cmd.queue_result(returncode=1, stderr="could not inspect recovered delta")
    elif failure_path == "recovered_protected_scope_blocked":
        cmd.queue_result(returncode=0, stdout="M\0pyproject.toml\0")

    async def _run_agent_with_recovery(**_kwargs: object) -> None:
        raise cleanup_error

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return fix_start_head

    async def _repair_mirror_hooks(**_kwargs: object) -> str | None:
        nonlocal repair_calls
        repair_calls += 1
        if failure_path == "post_agent_mirror_repair" and repair_calls == 2:
            return _MIRROR_HOOKS_PATH_POISONED_REASON
        return None

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _mirror_commit_object_exists(*_args: object, **_kwargs: object) -> bool:
        return failure_path != "recovery_anchor_missing"

    async def _open_merge_candidate_head_sha(*_args: object, **_kwargs: object) -> None:
        return None

    async def _recover_missing_head_object_from_filesystem(
        *_args: object,
        **_kwargs: object,
    ) -> str | None:
        if failure_path == "filesystem_recovery_failed":
            return None
        return recovered_head

    async def _protected_scope_violations(*_args: object, **_kwargs: object) -> list[object]:
        if failure_path == "recovered_protected_scope_blocked":
            return [SimpleNamespace(path="pyproject.toml")]
        raise AssertionError("protected-scope check should not run for this failure path")

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        raise AssertionError("commit should not run after the selected preservation failure")

    async def _rollback_failed_fix_pass(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("timeout preservation must not roll back timed-out work")

    monkeypatch.setattr(
        runner,
        "_run_monitor_agent_with_service_recovery",
        _run_agent_with_recovery,
    )
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    monkeypatch.setattr(fix_pass, "mirror_path_for_worktree", lambda _path: tmp_path)
    monkeypatch.setattr(
        fix_pass,
        "_repair_pre_push_validation_fix_mirror_hooks",
        _repair_mirror_hooks,
    )
    monkeypatch.setattr(fix_pass, "verify_head_object_exists", _verify_head_object_exists)
    monkeypatch.setattr(fix_pass, "_mirror_commit_object_exists", _mirror_commit_object_exists)
    monkeypatch.setattr(fix_pass, "_open_merge_candidate_head_sha", _open_merge_candidate_head_sha)
    monkeypatch.setattr(
        fix_pass,
        "_recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )
    monkeypatch.setattr(
        fix_pass,
        "_protected_scope_violations_for_recovered_commit",
        _protected_scope_violations,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback_failed_fix_pass,
    )

    with pytest.raises(ComposeExecCleanupError) as raised:
        await pre_push_validation._run_pre_push_validation_fix_pass(
            runner,
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            remote_branch="codex/pr",
            remote_url=None,
            state=None,
            validation_result=_failed_validation_result(
                pre_push_validation,
                tmp_path,
                workspace_head_sha=fix_start_head,
            ),
            pass_number=1,
            total_passes=1,
            validation_commands=("pytest -q",),
        )

    assert raised.value is cleanup_error


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_timeout_after_successful_cleanup_commits_work(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary watchdog timeout still preserves work and permits later validation."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass

    fix_start_head = "4" * 40
    committed_head = "5" * 40
    workspace_id, runner, _cmd, _adapter = await _make_fix_pass_runner(factory, tmp_path)
    rev_parse_heads = [fix_start_head, committed_head]
    rollback_calls: list[str] = []
    commit_calls: list[dict[str, object]] = []

    async def _run_agent_with_recovery(**kwargs: object) -> None:
        assert kwargs["timeout_rerun_requires_preservation"] is True
        raise AgentRunError(
            agent="codex",
            result=CommandResult(returncode=124, stdout="timed-out work", stderr=""),
            reason_code="AGENT_TIMEOUT",
        )

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return rev_parse_heads.pop(0)

    async def _commit_dirty_worktree(**kwargs: object) -> bool:
        commit_calls.append(dict(kwargs))
        return True

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return True

    async def _head_descends_from(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _cleanup_committed_fix_pass(*_args: object, **_kwargs: object) -> None:
        return None

    async def _rollback_failed_fix_pass(*_args: object, **kwargs: object) -> None:
        rollback_calls.append(str(kwargs["reason"]))

    monkeypatch.setattr(
        runner, "_run_monitor_agent_with_service_recovery", _run_agent_with_recovery
    )
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    monkeypatch.setattr(fix_pass, "mirror_path_for_worktree", lambda _worktree_path: None)
    monkeypatch.setattr(fix_pass, "verify_head_object_exists", _verify_head_object_exists)
    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback_failed_fix_pass,
    )
    monkeypatch.setattr(pre_push_validation, "_head_descends_from", _head_descends_from)
    monkeypatch.setattr(
        pre_push_validation,
        "_cleanup_committed_pre_push_validation_fix_pass",
        _cleanup_committed_fix_pass,
    )

    committed, failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=_failed_validation_result(
            pre_push_validation,
            tmp_path,
            workspace_head_sha=fix_start_head,
        ),
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert (committed, failure_reason) == (True, None)
    assert rollback_calls == []
    assert len(commit_calls) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mirror_failure_reason", "rollback_failure_reason", "expected_failure_reason"),
    (
        (
            _MIRROR_HOOKS_PATH_POISONED_REASON,
            "PRE_PUSH_VALIDATION_ROLLBACK_FAILED",
            _MIRROR_HOOKS_PATH_POISONED_REASON,
        ),
        (None, "PRE_PUSH_VALIDATION_ROLLBACK_FAILED", "PRE_PUSH_VALIDATION_ROLLBACK_FAILED"),
    ),
)
async def test_pre_push_validation_fix_pass_cleanup_failure_preserves_specific_failure_reason(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mirror_failure_reason: str | None,
    rollback_failure_reason: str,
    expected_failure_reason: str,
) -> None:
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass

    fix_start_head = "2" * 40
    workspace_id, runner, cmd, adapter = await _make_fix_pass_runner(factory, tmp_path)
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter.queue(
        exc=ComposeExecCleanupError(
            invocation_id="awf_cleanup",
            source="agent",
            label="monitor-pre-push-validation-fix",
            message="tagged process still running",
        )
    )
    repair_calls = 0
    rollback_calls: list[str] = []

    async def _repair_mirror_hooks(**_kwargs: object) -> str | None:
        nonlocal repair_calls
        repair_calls += 1
        return mirror_failure_reason if repair_calls == 2 else None

    async def _rollback_failed_fix_pass(*_args: object, **kwargs: object) -> str:
        rollback_calls.append(str(kwargs["reason"]))
        return rollback_failure_reason

    monkeypatch.setattr(fix_pass, "mirror_path_for_worktree", lambda _worktree_path: tmp_path)
    monkeypatch.setattr(
        fix_pass, "_repair_pre_push_validation_fix_mirror_hooks", _repair_mirror_hooks
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback_failed_fix_pass,
    )

    committed, failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=_failed_validation_result(
            pre_push_validation,
            tmp_path,
            workspace_head_sha=fix_start_head,
        ),
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert (committed, failure_reason) == (False, expected_failure_reason)
    assert rollback_calls == ["compose_cleanup_failed"]


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_agent_exception_repairs_mirror_before_rollback_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass

    fix_start_head = "7" * 40
    workspace_id, runner, cmd, adapter = await _make_fix_pass_runner(factory, tmp_path)
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter.queue(exc=RuntimeError("unexpected fix-agent failure"))
    repair_calls = 0
    rollback_calls: list[str] = []

    async def _repair_mirror_hooks(**_kwargs: object) -> None:
        nonlocal repair_calls
        repair_calls += 1

    async def _rollback_failed_fix_pass(*_args: object, **kwargs: object) -> str:
        rollback_calls.append(str(kwargs["reason"]))
        return "PRE_PUSH_VALIDATION_ROLLBACK_FAILED"

    monkeypatch.setattr(fix_pass, "mirror_path_for_worktree", lambda _worktree_path: tmp_path)
    monkeypatch.setattr(
        fix_pass, "_repair_pre_push_validation_fix_mirror_hooks", _repair_mirror_hooks
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback_failed_fix_pass,
    )

    committed, failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=_failed_validation_result(
            pre_push_validation,
            tmp_path,
            workspace_head_sha=fix_start_head,
        ),
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert (committed, failure_reason) == (False, "PRE_PUSH_VALIDATION_ROLLBACK_FAILED")
    assert rollback_calls == ["agent_exception"]
    assert repair_calls == 2


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_returns_missing_head_when_recovery_anchor_missing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass

    fix_start_head = "3" * 40
    workspace_id, runner, cmd, adapter = await _make_fix_pass_runner(factory, tmp_path)
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter.queue(stdout="attempted fix\n")

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _mirror_commit_object_exists(*_args: object, **_kwargs: object) -> bool:
        return False

    async def _open_merge_candidate_head_sha(*_args: object, **_kwargs: object) -> None:
        return None

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        raise AssertionError("commit should not run when no recovery anchor is available")

    monkeypatch.setattr(fix_pass, "mirror_path_for_worktree", lambda _worktree_path: tmp_path)
    monkeypatch.setattr(fix_pass, "verify_head_object_exists", _verify_head_object_exists)
    monkeypatch.setattr(fix_pass, "_mirror_commit_object_exists", _mirror_commit_object_exists)
    monkeypatch.setattr(fix_pass, "_open_merge_candidate_head_sha", _open_merge_candidate_head_sha)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)

    committed, failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=_failed_validation_result(
            pre_push_validation,
            tmp_path,
            workspace_head_sha=fix_start_head,
        ),
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert (committed, failure_reason) == (False, _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON)


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_returns_commit_exception_rollback_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass

    fix_start_head = "4" * 40
    post_agent_head = "5" * 40
    workspace_id, runner, cmd, adapter = await _make_fix_pass_runner(factory, tmp_path)
    adapter.queue(stdout="attempted fix\n")
    rev_parse_heads = [fix_start_head, post_agent_head]
    rollback_calls: list[dict[str, object]] = []

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return rev_parse_heads.pop(0)

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        raise RuntimeError("commit sink failed")

    async def _rollback_failed_fix_pass(*_args: object, **kwargs: object) -> str:
        rollback_calls.append(dict(kwargs))
        return "PRE_PUSH_VALIDATION_ROLLBACK_FAILED"

    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    monkeypatch.setattr(fix_pass, "mirror_path_for_worktree", lambda _worktree_path: None)
    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback_failed_fix_pass,
    )

    committed, failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=_failed_validation_result(
            pre_push_validation,
            tmp_path,
            workspace_head_sha=fix_start_head,
        ),
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert (committed, failure_reason) == (False, "PRE_PUSH_VALIDATION_ROLLBACK_FAILED")
    assert rollback_calls[0]["reason"] == "commit_exception"
    assert rollback_calls[0]["restore_ref"] == post_agent_head
