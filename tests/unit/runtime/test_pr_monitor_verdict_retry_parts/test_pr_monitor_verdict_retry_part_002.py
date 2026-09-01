"""Bounded correction-retry regressions (part 2)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.adapters.base import AgentRunResult
from awf.common.commands import AsyncioSubprocessRunner, CommandResult
from awf.common.compose_exec import ComposeExecCleanupError
from awf.runtime.ownership import AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdictExecutionError,
    AgentVerdictProtocolError,
)
from awf.runtime.pr_monitor_runner.constants import _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)
from awf.runtime.validation_worktree import (
    ValidationWorktreeCheck,
    ValidationWorktreeCleanup,
)
from tests.unit.runtime._verdict_retry_fixtures import (
    _agent_error,
    _invoke,
    _VerdictRunner,
)

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]


@pytest.mark.unit
async def test_provider_failure_cleans_dirty_worktree_when_head_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uncommitted agent edits must be discarded when provider failure leaves HEAD unchanged."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /tmp/fake.git\n", encoding="utf-8")
    item_start_head = "a" * 40
    cleanup_calls: list[dict[str, object]] = []

    async def _cleanup(**kwargs: object) -> ValidationWorktreeCleanup:
        cleanup_calls.append(kwargs)
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=False, paths=("dirty.py",)),
            restore_ref=item_start_head,
            cleaned_paths=("dirty.py",),
        )

    monkeypatch.setattr(
        "awf.runtime.validation_worktree.cleanup_validation_worktree_side_effects",
        _cleanup,
    )

    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_agent_error()],
        heads_after_attempt=[item_start_head],
    )

    with pytest.raises(AgentVerdictExecutionError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == "AGENT_CLI_FAILED"
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0]["restore_ref"] == item_start_head
    assert cleanup_calls[0]["worktree_path"] == worktree
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_provider_recovery_after_agent_run_rolls_back_unaccepted_commits(
    tmp_path: Path,
) -> None:
    """In-run provider recovery must roll back agent edits before retrying."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )

    async def _raise_provider_recovery_after_agent_run(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = runner.heads_after_attempt[runner.attempt - 1]
        raise ProviderRecoveryRetryError()

    runner._run_monitor_agent_with_service_recovery = _raise_provider_recovery_after_agent_run

    with pytest.raises(ProviderRecoveryRetryError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_provider_recovery_after_agent_run_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Failed in-run provider-recovery rollback must abort instead of retrying."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        reset_fails=True,
    )

    async def _raise_provider_recovery_after_agent_run(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = runner.heads_after_attempt[runner.attempt - 1]
        raise ProviderRecoveryRetryError()

    runner._run_monitor_agent_with_service_recovery = _raise_provider_recovery_after_agent_run

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: _MonitorAgentServiceRecoveryFailedError("agent service unhealthy"),
        lambda: _MonitorAgentServiceRecoverySupersededError("monitor claim lost"),
    ],
)
async def test_service_recovery_exit_after_agent_run_rolls_back_unaccepted_commits(
    tmp_path: Path,
    exc_factory: object,
) -> None:
    """Post-invocation service-recovery exits must roll back agent edits before propagating."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )
    service_recovery_exc = exc_factory()  # type: ignore[operator]

    async def _raise_service_recovery_after_agent_run(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = runner.heads_after_attempt[runner.attempt - 1]
        raise service_recovery_exc

    runner._run_monitor_agent_with_service_recovery = _raise_service_recovery_after_agent_run

    with pytest.raises(type(service_recovery_exc)):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_unexpected_failure_after_agent_run_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Failed rollback after an unexpected invocation error must fail closed."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        reset_fails=True,
    )

    async def _raise_unexpected_after_agent_run(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = runner.heads_after_attempt[runner.attempt - 1]
        raise RuntimeError("unexpected failure after agent edit")

    runner._run_monitor_agent_with_service_recovery = _raise_unexpected_after_agent_run

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "roll back" in str(caught.value).lower()
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
async def test_unexpected_failure_rolls_back_before_post_exception_hook_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rollback must precede post-exception hook repair so repair failure cannot strand edits."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    mirror_path = tmp_path / "mirror.git"
    mirror_path.mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )
    hook_repair_stages: list[str] = []

    monkeypatch.setattr(
        comment_verdict,
        "mirror_path_for_worktree",
        lambda _path: mirror_path,
    )

    async def _repair_mirror_hooks_path(_path: Path) -> bool:
        stage = (
            "before_comment_agent" if not hook_repair_stages else "after_comment_agent_exception"
        )
        hook_repair_stages.append(stage)
        if stage == "after_comment_agent_exception":
            raise OSError("hooks poisoned")
        return True

    monkeypatch.setattr(comment_verdict, "repair_mirror_hooks_path", _repair_mirror_hooks_path)

    async def _raise_unexpected_after_agent_run(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = runner.heads_after_attempt[runner.attempt - 1]
        raise RuntimeError("unexpected failure after agent edit")

    runner._run_monitor_agent_with_service_recovery = _raise_unexpected_after_agent_run

    with pytest.raises(_MonitorMirrorHooksPathRepairFailedError):
        await _invoke(runner)

    assert hook_repair_stages == [
        "before_comment_agent",
        "after_comment_agent_exception",
    ]
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_service_recovery_exit_after_agent_run_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Failed service-recovery rollback must abort instead of propagating the exit."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        reset_fails=True,
    )

    async def _raise_service_recovery_failed_after_agent_run(
        **kwargs: object,
    ) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = runner.heads_after_attempt[runner.attempt - 1]
        raise _MonitorAgentServiceRecoveryFailedError("agent service unhealthy")

    runner._run_monitor_agent_with_service_recovery = _raise_service_recovery_failed_after_agent_run

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: _MonitorAgentRuntimeOwnershipRepairFailedError(
            AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
        ),
        lambda: _MonitorHeadObjectMissingError(
            _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
            "missing head",
        ),
        lambda: _MonitorMirrorHooksPathRepairFailedError("hooks poisoned"),
    ],
)
async def test_infrastructure_service_recovery_exit_rollback_failure_preserves_reason(
    tmp_path: Path,
    exc_factory: object,
) -> None:
    """Failed rollback must not mask infrastructure service-recovery exit reason codes."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        reset_fails=True,
    )
    service_recovery_exc = exc_factory()  # type: ignore[operator]

    async def _raise_infrastructure_exit_after_agent_run(
        **kwargs: object,
    ) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = runner.heads_after_attempt[runner.attempt - 1]
        raise service_recovery_exc

    runner._run_monitor_agent_with_service_recovery = _raise_infrastructure_exit_after_agent_run

    with pytest.raises(type(service_recovery_exc)) as caught:
        await _invoke(runner)

    assert caught.value is service_recovery_exc
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
async def test_provider_recovery_before_protocol_correction_rolls_back_first_attempt_commit(
    tmp_path: Path,
) -> None:
    """Provider recovery on the correction attempt must not strand first-attempt commits."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        provider_recovery_suppress_attempts=frozenset({1}),
    )

    with pytest.raises(ProviderRecoveryRetryError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_non_fixed_verdict_rejected_when_rollback_cannot_resolve_head(
    tmp_path: Path,
) -> None:
    """Unreadable HEAD during rollback must fail closed before accepting a verdict."""
    (tmp_path / "ws_protocol").mkdir()
    fixed_head = "b" * 40
    # Sequence covers: attempt0 start + evidence + post-attempt0 tip capture,
    # attempt1 start + pre-sink HEAD + evidence + correction mutation-gate read,
    # then unreadable HEAD on accept-path rollback.
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing", "AWF-VERDICT: NEEDS_HUMAN: design choice"],
        heads_after_attempt=[fixed_head, fixed_head],
        dirty_after_attempt=[True, False],
        rev_parse_sequence=[
            fixed_head,
            fixed_head,
            fixed_head,
            fixed_head,
            fixed_head,
            fixed_head,
            fixed_head,
            None,
        ],
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "roll back" in str(caught.value).lower()
    assert len(runner.prompts) == 2


@pytest.mark.unit
async def test_rollback_fails_closed_when_head_unreadable(tmp_path: Path) -> None:
    """Direct rollback must reject unreadable HEAD instead of reporting success."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=["a" * 40],
        rev_parse_sequence=[None],
    )

    ok = await comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id="ws_protocol",
        worktree_path=worktree,
        item_start_head="a" * 40,
        state=None,
    )

    assert ok is False


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.unit
async def test_protocol_retry_rollback_uses_merge_safety_git_env(tmp_path: Path) -> None:
    """Reset and cleanup during verdict retry rollback must ignore replace refs."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    _git(worktree, "init", "-q")
    _git(worktree, "config", "user.email", "awf@example.com")
    _git(worktree, "config", "user.name", "AWF Test")
    (worktree / "file.txt").write_text("start\n", encoding="utf-8")
    _git(worktree, "add", "file.txt")
    _git(worktree, "commit", "-qm", "start")
    item_start_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    (worktree / "file.txt").write_text("changed\n", encoding="utf-8")
    _git(worktree, "add", "file.txt")
    _git(worktree, "commit", "-qm", "agent edit")
    fixed_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    captured_envs: list[dict[str, str] | None] = []

    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[fixed_head],
        rev_parse_sequence=[fixed_head],
    )
    subprocess_runner = AsyncioSubprocessRunner()

    async def _capturing_run(cmd: list[str], **kwargs: object) -> CommandResult:
        captured_envs.append(kwargs.get("env"))  # type: ignore[arg-type]
        return await subprocess_runner.run(cmd, **kwargs)

    runner._deps.runner.run = _capturing_run

    ok = await comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id="ws_protocol",
        worktree_path=worktree,
        item_start_head=item_start_head,
        state=None,
    )

    assert ok is True
    merge_safety_envs = [
        env for env in captured_envs if env is not None and env.get("GIT_NO_REPLACE_OBJECTS") == "1"
    ]
    assert len(merge_safety_envs) >= 2
    assert all(env.get("GIT_GRAFT_FILE") == os.devnull for env in merge_safety_envs)


@pytest.mark.unit
async def test_protocol_retry_rollback_restores_real_tree_with_replace_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """refs/replace on the start head must not survive protocol retry rollback.

      Regression for PRRT_kwDOSJAM6s6beVRL: without GIT_NO_REPLACE_OBJECTS,
    ``git reset --hard <start>`` leaves HEAD at the start ref while checking out
      the replacement commit's tree, so cleanup falsely reports a clean worktree.
    """
    monkeypatch.delenv("GIT_NO_REPLACE_OBJECTS", raising=False)
    monkeypatch.delenv("GIT_GRAFT_FILE", raising=False)
    monkeypatch.delenv("GIT_REPLACE_REF_BASE", raising=False)

    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    _git(worktree, "init", "-q")
    _git(worktree, "config", "user.email", "awf@example.com")
    _git(worktree, "config", "user.name", "AWF Test")
    (worktree / "file.txt").write_text("start\n", encoding="utf-8")
    _git(worktree, "add", "file.txt")
    _git(worktree, "commit", "-qm", "start")
    item_start_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()

    (worktree / "file.txt").write_text("changed\n", encoding="utf-8")
    _git(worktree, "add", "file.txt")
    _git(worktree, "commit", "-qm", "agent edit")
    agent_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()

    changed_tree = _git(worktree, "rev-parse", f"{agent_head}^{{tree}}").stdout.strip()
    forged = _git(
        worktree,
        "commit-tree",
        changed_tree,
        "-p",
        item_start_head,
        "-m",
        "forged replacement",
    ).stdout.strip()
    _git(worktree, "update-ref", f"refs/replace/{item_start_head}", forged)

    poisoned_reset = subprocess.run(
        ["git", "-C", str(worktree), "reset", "--hard", item_start_head],
        check=False,
        capture_output=True,
        text=True,
    )
    assert poisoned_reset.returncode == 0
    assert (worktree / "file.txt").read_text(encoding="utf-8") == "changed\n"
    _git(worktree, "reset", "--hard", agent_head)

    subprocess_runner = AsyncioSubprocessRunner()
    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=False),
            runner=subprocess_runner,
        ),
    )

    async def _rev_parse_head(worktree_path: Path) -> str | None:
        result = await subprocess_runner.run(
            git_worktree_command(worktree_path, "rev-parse", "HEAD")
        )
        if not result.ok:
            return None
        return result.stdout.strip()

    runner._rev_parse_head = _rev_parse_head

    ok = await comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id="ws_protocol",
        worktree_path=worktree,
        item_start_head=item_start_head,
        state=None,
    )

    assert ok is True
    assert (worktree / "file.txt").read_text(encoding="utf-8") == "start\n"
    current_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    assert current_head == item_start_head


@pytest.mark.unit
async def test_protocol_retry_rollback_aborts_when_live_head_advances_before_reset(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bfemF: refuse reset when HEAD moves after snapshot capture."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    _git(worktree, "init", "-q")
    _git(worktree, "config", "user.email", "awf@example.com")
    _git(worktree, "config", "user.name", "AWF Test")
    (worktree / "file.txt").write_text("start\n", encoding="utf-8")
    _git(worktree, "add", "file.txt")
    _git(worktree, "commit", "-qm", "start")
    item_start_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    (worktree / "file.txt").write_text("agent\n", encoding="utf-8")
    _git(worktree, "add", "file.txt")
    _git(worktree, "commit", "-qm", "agent edit")
    agent_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    (worktree / "file.txt").write_text("concurrent\n", encoding="utf-8")
    _git(worktree, "add", "file.txt")
    _git(worktree, "commit", "-qm", "concurrent writer")
    concurrent_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()

    subprocess_runner = AsyncioSubprocessRunner()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[concurrent_head],
        rev_parse_sequence=[agent_head],
    )
    runner._deps.runner.run = subprocess_runner.run

    ok = await comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id="ws_protocol",
        worktree_path=worktree,
        item_start_head=item_start_head,
        state=None,
    )

    assert ok is False
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == concurrent_head


@pytest.mark.unit
async def test_protocol_retry_rollback_holds_writer_lock_through_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6bmptC: rollback must be one writer-locked operation."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    item_start_head = "a" * 40
    agent_head = "b" * 40
    lock_events: list[str] = []
    lock_held = False
    live_head = agent_head
    cleanup_called = False

    @contextlib.asynccontextmanager
    async def _writer_lock(_worktree_path: Path):
        nonlocal lock_held
        assert lock_held is False
        lock_events.append("enter")
        lock_held = True
        try:
            yield
        finally:
            lock_held = False
            lock_events.append("exit")

    async def _run_git(command: list[str], **_kwargs: object) -> CommandResult:
        nonlocal live_head
        assert lock_held is True
        if "reset" in command and "--hard" in command:
            live_head = command[-1]
            return CommandResult(returncode=0, stdout="", stderr="")
        if "rev-parse" in command:
            ref = command[-1]
            return CommandResult(
                returncode=0,
                stdout=f"{live_head if ref == 'HEAD' else ref}\n",
                stderr="",
            )
        return CommandResult(returncode=0, stdout="", stderr="")

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return agent_head

    async def _cleanup(
        *,
        run_git: Callable[[list[str]], Awaitable[CommandResult]],
        **_kwargs: object,
    ) -> ValidationWorktreeCleanup:
        nonlocal cleanup_called
        cleanup_called = True
        assert (await run_git(["status", "--porcelain"])).ok
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref=item_start_head,
        )

    monkeypatch.setattr(
        comment_verdict,
        "hold_exclusive_worktree_writer_lock",
        _writer_lock,
    )
    monkeypatch.setattr(
        "awf.runtime.validation_worktree.cleanup_validation_worktree_side_effects",
        _cleanup,
    )
    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=False),
            runner=SimpleNamespace(run=_run_git),
        ),
        _rev_parse_head=_rev_parse_head,
    )

    assert await comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id="ws_protocol",
        worktree_path=worktree,
        item_start_head=item_start_head,
        state=None,
    )
    assert cleanup_called is True
    assert lock_events == ["enter", "exit"]


@pytest.mark.unit
async def test_protocol_retry_rollback_aborts_when_head_advances_while_waiting_for_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRRT_kwDOSJAM6s6boEEH: never clean a commit made before the rollback lock."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    item_start_head = "a" * 40
    concurrent_head = "b" * 40
    live_head = item_start_head
    cleanup_called = False

    @contextlib.asynccontextmanager
    async def _writer_lock(_worktree_path: Path):
        nonlocal live_head
        live_head = concurrent_head
        yield

    async def _run_git(command: list[str], **_kwargs: object) -> CommandResult:
        if "rev-parse" in command:
            return CommandResult(returncode=0, stdout=f"{live_head}\n", stderr="")
        raise AssertionError(f"rollback must abort before destructive command: {command!r}")

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return item_start_head

    async def _cleanup(**_kwargs: object) -> ValidationWorktreeCleanup:
        nonlocal cleanup_called
        cleanup_called = True
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref=item_start_head,
        )

    monkeypatch.setattr(
        comment_verdict,
        "hold_exclusive_worktree_writer_lock",
        _writer_lock,
    )
    monkeypatch.setattr(
        "awf.runtime.validation_worktree.cleanup_validation_worktree_side_effects",
        _cleanup,
    )
    runner = SimpleNamespace(
        _deps=SimpleNamespace(
            adapter=SimpleNamespace(is_hosted=False),
            runner=SimpleNamespace(run=_run_git),
        ),
        _rev_parse_head=_rev_parse_head,
    )

    assert (
        await comment_verdict._rollback_unaccepted_protocol_retry_changes(
            runner,
            workspace_id="ws_protocol",
            worktree_path=worktree,
            item_start_head=item_start_head,
            state=None,
        )
        is False
    )
    assert cleanup_called is False


@pytest.mark.unit
async def test_protocol_retry_rollback_resets_despite_agent_uncommitted_residue(
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6bfgXE: agent commits plus uncommitted residue must still roll back."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    _git(worktree, "init", "-q")
    _git(worktree, "config", "user.email", "awf@example.com")
    _git(worktree, "config", "user.name", "AWF Test")
    (worktree / "file.txt").write_text("start\n", encoding="utf-8")
    _git(worktree, "add", "file.txt")
    _git(worktree, "commit", "-qm", "start")
    item_start_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    (worktree / "file.txt").write_text("agent\n", encoding="utf-8")
    _git(worktree, "add", "file.txt")
    _git(worktree, "commit", "-qm", "agent edit")
    agent_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    (worktree / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    subprocess_runner = AsyncioSubprocessRunner()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[agent_head],
        rev_parse_sequence=[agent_head],
    )
    runner._deps.runner.run = subprocess_runner.run

    ok = await comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id="ws_protocol",
        worktree_path=worktree,
        item_start_head=item_start_head,
        state=None,
    )

    assert ok is True
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == item_start_head
    assert not (worktree / "dirty.txt").exists()
    assert (worktree / "file.txt").read_text(encoding="utf-8") == "start\n"


@pytest.mark.unit
async def test_provider_recovery_before_protocol_correction_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Failed rollback before provider recovery must abort instead of retrying."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        provider_recovery_suppress_attempts=frozenset({1}),
        reset_fails=True,
    )

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
async def test_provider_error_does_not_consume_protocol_retry(tmp_path: Path) -> None:
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_agent_error()],
        heads_after_attempt=["a" * 40],
        provider_error_action=ProviderRecoveryRetryError(),
    )

    with pytest.raises(ProviderRecoveryRetryError):
        await _invoke(runner)

    assert runner.prompts == ["ORIGINAL REVIEW PROMPT"]


@pytest.mark.unit
async def test_worker_cancellation_after_agent_edit_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Worker cancel must roll back agent edits before propagating CancelledError."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )

    async def _raise_cancel_after_agent_edit(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = runner.heads_after_attempt[runner.attempt - 1]
        raise asyncio.CancelledError()

    runner._run_monitor_agent_with_service_recovery = _raise_cancel_after_agent_edit

    with pytest.raises(asyncio.CancelledError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_worker_cancellation_during_provider_recovery_check_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Worker cancel during pre-launch provider recovery check must roll back first."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )

    async def _raise_cancel_on_correction_pre_launch(_workspace_id: str) -> bool:
        if runner.provider_recovery_check_count == 1:
            raise asyncio.CancelledError()
        return await _VerdictRunner._provider_recovery_suppresses_cli(runner, _workspace_id)

    runner._provider_recovery_suppresses_cli = _raise_cancel_on_correction_pre_launch

    with pytest.raises(asyncio.CancelledError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_worker_cancellation_during_correction_start_head_read_rolls_back(
    tmp_path: Path,
) -> None:
    """Cancel during correction-start rev-parse must roll back attempt-0 residue.

    Production regression for PRRT_kwDOSJAM6s6eJCpZ: after a malformed first
    attempt advances HEAD, the correction-start ``_rev_parse_head`` probe ran
    outside the rollback-guarded ``try``. Cancellation (or a raise while
    spawning Git) bypassed the CancelledError / Exception rollback handlers and
    left unaccepted local state for a later monitor cycle.
    """
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    attempt_one_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[attempt_one_head],
        dirty_after_attempt=[True],
    )
    runner.current_head = item_start_head
    # Sequence through attempt 0: start, evidence, post-attempt tip; 4th call is
    # correction-start and must be inside the guarded region.
    rev_parse_calls = 0

    async def _cancel_on_correction_start(_worktree_path: Path) -> str | None:
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        if rev_parse_calls == 1:
            return item_start_head
        if rev_parse_calls in (2, 3):
            runner.current_head = attempt_one_head
            return attempt_one_head
        if rev_parse_calls == 4:
            raise asyncio.CancelledError()
        return runner.current_head

    runner._rev_parse_head = _cancel_on_correction_start

    with pytest.raises(asyncio.CancelledError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert rev_parse_calls >= 4
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_correction_start_head_read_exception_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Exception during correction-start rev-parse must roll back attempt-0 residue."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    attempt_one_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[attempt_one_head],
        dirty_after_attempt=[True],
    )
    runner.current_head = item_start_head
    rev_parse_calls = 0

    async def _raise_on_correction_start(_worktree_path: Path) -> str | None:
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        if rev_parse_calls == 1:
            return item_start_head
        if rev_parse_calls in (2, 3):
            runner.current_head = attempt_one_head
            return attempt_one_head
        if rev_parse_calls == 4:
            raise RuntimeError("git spawn failed during correction-start rev-parse")
        return runner.current_head

    runner._rev_parse_head = _raise_on_correction_start

    with pytest.raises(RuntimeError, match="correction-start rev-parse"):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert rev_parse_calls >= 4
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_valid_fixed_verdict_does_not_probe_tip_before_parse(
    tmp_path: Path,
) -> None:
    """Valid attempt-0 FIXED must not depend on the post-attempt tip probe.

    Production regression for PRRT_kwDOSJAM6s6eJ2Tm: tip was probed before
    parsing stdout even though ``verified_attempt_tip`` is only consumed on the
    protocol-retry path. A transient Git spawn failure discarded a valid FIXED
    verdict and rolled back accepted evidence.
    """
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )
    runner.current_head = item_start_head
    # Sequence through attempt 0: start, evidence. Tip must not run.
    rev_parse_calls = 0

    async def _raise_on_post_attempt_tip(_worktree_path: Path) -> str | None:
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        if rev_parse_calls == 1:
            return item_start_head
        if rev_parse_calls == 2:
            runner.current_head = fixed_head
            return fixed_head
        raise OSError("git spawn failed during post-attempt tip rev-parse")

    runner._rev_parse_head = _raise_on_post_attempt_tip

    result = await _invoke(runner)

    assert result.verdict == "fix_committed"
    assert rev_parse_calls == 2
    assert runner.reset_targets == []
    assert runner.current_head == fixed_head


@pytest.mark.unit
async def test_post_attempt_tip_head_read_exception_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Exception during post-attempt tip rev-parse must roll back attempt-0 residue.

    Production regression for PRRT_kwDOSJAM6s6eJUbE: after attempt 0 edits or
    self-commits, the post-attempt ``_rev_parse_head`` probe ran outside the
    Exception rollback regions (only CancelledError was caught around it). An
    OSError/RuntimeError while spawning Git left unaccepted local state intact
    for a later monitor cycle.
    """
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    attempt_one_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[attempt_one_head],
        dirty_after_attempt=[True],
    )
    runner.current_head = item_start_head
    # Sequence through attempt 0: start, evidence, then post-attempt tip raises.
    rev_parse_calls = 0

    async def _raise_on_post_attempt_tip(_worktree_path: Path) -> str | None:
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        if rev_parse_calls == 1:
            return item_start_head
        if rev_parse_calls == 2:
            runner.current_head = attempt_one_head
            return attempt_one_head
        if rev_parse_calls == 3:
            raise OSError("git spawn failed during post-attempt tip rev-parse")
        return runner.current_head

    runner._rev_parse_head = _raise_on_post_attempt_tip

    with pytest.raises(OSError, match="post-attempt tip rev-parse"):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert rev_parse_calls >= 3
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_post_attempt_tip_head_read_exception_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Failed rollback after post-attempt tip probe failure must abort closed."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    attempt_one_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[attempt_one_head],
        dirty_after_attempt=[True],
        reset_fails=True,
    )
    runner.current_head = item_start_head
    rev_parse_calls = 0

    async def _raise_on_post_attempt_tip(_worktree_path: Path) -> str | None:
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        if rev_parse_calls == 1:
            return item_start_head
        if rev_parse_calls == 2:
            runner.current_head = attempt_one_head
            return attempt_one_head
        if rev_parse_calls == 3:
            raise OSError("git spawn failed during post-attempt tip rev-parse")
        return runner.current_head

    runner._rev_parse_head = _raise_on_post_attempt_tip

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "post-attempt tip" in str(caught.value).lower()
    assert len(runner.prompts) == 1
    assert rev_parse_calls >= 3
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == attempt_one_head


@pytest.mark.unit
async def test_worker_cancellation_during_post_attempt_tip_head_read_rolls_back(
    tmp_path: Path,
) -> None:
    """Cancel during post-attempt tip rev-parse must roll back attempt-0 residue."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    attempt_one_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[attempt_one_head],
        dirty_after_attempt=[True],
    )
    runner.current_head = item_start_head
    rev_parse_calls = 0

    async def _cancel_on_post_attempt_tip(_worktree_path: Path) -> str | None:
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        if rev_parse_calls == 1:
            return item_start_head
        if rev_parse_calls == 2:
            runner.current_head = attempt_one_head
            return attempt_one_head
        if rev_parse_calls == 3:
            raise asyncio.CancelledError()
        return runner.current_head

    runner._rev_parse_head = _cancel_on_post_attempt_tip

    with pytest.raises(asyncio.CancelledError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert rev_parse_calls >= 3
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_correction_end_head_read_exception_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Exception during correction-end rev-parse must roll back before re-raise.

    Production regression for PRRT_kwDOSJAM6s6eJ2Tg: after the correction
    attempt edits or self-commits, the mutation-gate ``_rev_parse_head`` probe
    handled only a None return. An OSError/RuntimeError while spawning Git
    escaped the attempt loop (outer handler catches only CancelledError) and
    left unaccepted local/hosted state for a later monitor cycle.
    """
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    attempt_one_head = "b" * 40
    correction_head = "c" * 40
    # Sequence: attempt0 start, attempt0 evidence, post-attempt0 tip,
    # correction start, pre-sink HEAD, correction evidence, mutation-gate end raises.
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "malformed after editing",
            "AWF-VERDICT: FALSE POSITIVE: contradiction after self-commit on retry",
        ],
        heads_after_attempt=[attempt_one_head, correction_head],
        dirty_after_attempt=[True, False],
    )
    runner.current_head = item_start_head
    rev_parse_calls = 0

    async def _raise_on_correction_end(_worktree_path: Path) -> str | None:
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        if rev_parse_calls == 1:
            return item_start_head
        if rev_parse_calls == 2:
            runner.current_head = attempt_one_head
            return attempt_one_head
        if rev_parse_calls == 3:
            return attempt_one_head
        if rev_parse_calls == 4:
            return attempt_one_head
        if rev_parse_calls == 5:
            return correction_head
        if rev_parse_calls == 6:
            return correction_head
        if rev_parse_calls == 7:
            raise OSError("git spawn failed during correction-end rev-parse")
        return runner.current_head

    runner._rev_parse_head = _raise_on_correction_end

    with pytest.raises(OSError, match="correction-end rev-parse"):
        await _invoke(runner)

    assert len(runner.prompts) == 2
    assert rev_parse_calls >= 7
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_correction_end_head_read_exception_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Failed rollback after correction-end probe failure must abort closed."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    attempt_one_head = "b" * 40
    correction_head = "c" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "malformed after editing",
            "AWF-VERDICT: FALSE POSITIVE: contradiction after self-commit on retry",
        ],
        heads_after_attempt=[attempt_one_head, correction_head],
        dirty_after_attempt=[True, False],
        reset_fails=True,
    )
    runner.current_head = item_start_head
    rev_parse_calls = 0

    async def _raise_on_correction_end(_worktree_path: Path) -> str | None:
        nonlocal rev_parse_calls
        rev_parse_calls += 1
        if rev_parse_calls == 1:
            return item_start_head
        if rev_parse_calls == 2:
            runner.current_head = attempt_one_head
            return attempt_one_head
        if rev_parse_calls == 3:
            return attempt_one_head
        if rev_parse_calls == 4:
            return attempt_one_head
        if rev_parse_calls == 5:
            return correction_head
        if rev_parse_calls == 6:
            return correction_head
        if rev_parse_calls == 7:
            raise OSError("git spawn failed during correction-end rev-parse")
        return runner.current_head

    runner._rev_parse_head = _raise_on_correction_end

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "correction" in str(caught.value).lower() or "end" in str(caught.value).lower()
    assert len(runner.prompts) == 2
    assert rev_parse_calls >= 7
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == correction_head


@pytest.mark.unit
async def test_hosted_gate_failure_before_state_record_rolls_back_remote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Policy gate after hosted sync must rewind PR head when state was not recorded."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    synced_head = "b" * 40
    state = MonitorState(last_push_sha=item_start_head)
    remote_rollbacks: list[dict[str, object]] = []

    async def _record_remote_rollback(*args: object, **kwargs: object) -> bool:
        remote_rollbacks.append(dict(kwargs))
        return True

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.agent_service_recovery._rollback_hosted_terminal_head_on_remote",
        _record_remote_rollback,
    )

    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["unused"],
        heads_after_attempt=[synced_head],
    )
    runner._deps.adapter.is_hosted = True
    runner.current_head = synced_head

    async def _raise_policy_blocked_after_hosted_sync(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = synced_head
        raise _MonitorPolicyBlockedError("protected-scope policy blocked hosted repair")

    runner._run_monitor_agent_with_service_recovery = _raise_policy_blocked_after_hosted_sync

    with pytest.raises(_MonitorPolicyBlockedError):
        await comment_verdict._invoke_cli_for_verdict_result(
            runner,
            workspace_id="ws_protocol",
            prompt="ORIGINAL REVIEW PROMPT",
            commit_message="fix: review item",
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            operation_start_head=item_start_head,
            state=state,
        )

    assert state.last_push_sha == item_start_head
    assert not state.hosted_terminal_head_advanced
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head
    assert len(remote_rollbacks) == 1
    assert remote_rollbacks[0]["rollback_target_sha"] == item_start_head
    assert remote_rollbacks[0]["expected_remote_head_sha"] == synced_head


@pytest.mark.unit
async def test_policy_blocked_during_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Supply-chain policy block during commit sink must roll back before propagating."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )

    async def _raise_policy_blocked_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise _MonitorPolicyBlockedError("Supply-chain policy blocked review fix.")

    runner._commit_dirty_worktree = _raise_policy_blocked_during_commit

    with pytest.raises(_MonitorPolicyBlockedError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_protected_scope_diff_during_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Protected-scope diff failure during commit sink must roll back before propagating."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )
    diff_exc = ProtectedScopeDiffError("protected-scope diff unavailable")

    async def _raise_protected_scope_diff_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise diff_exc

    runner._commit_dirty_worktree = _raise_protected_scope_diff_during_commit

    with pytest.raises(ProtectedScopeDiffError) as caught:
        await _invoke(runner)

    assert caught.value is diff_exc
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_provider_recovery_during_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Provider recovery during commit sink must roll back before propagating."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )

    async def _raise_provider_recovery_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise ProviderRecoveryRetryError()

    runner._commit_dirty_worktree = _raise_provider_recovery_during_commit

    with pytest.raises(ProviderRecoveryRetryError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: _MonitorAgentServiceRecoveryFailedError("agent service unhealthy"),
        lambda: _MonitorAgentServiceRecoverySupersededError("monitor claim lost"),
    ],
)
async def test_service_recovery_exit_during_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
    exc_factory: object,
) -> None:
    """Post-invocation service-recovery exits during commit sink must roll back first."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )
    service_recovery_exc = exc_factory()  # type: ignore[operator]

    async def _raise_service_recovery_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise service_recovery_exc

    runner._commit_dirty_worktree = _raise_service_recovery_during_commit

    with pytest.raises(type(service_recovery_exc)):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: _MonitorAgentRuntimeOwnershipRepairFailedError(
            AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
        ),
        lambda: _MonitorHeadObjectMissingError(
            _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
            "missing head",
        ),
        lambda: _MonitorMirrorHooksPathRepairFailedError("hooks poisoned"),
    ],
)
async def test_infrastructure_exit_during_commit_sink_rollback_failure_preserves_reason(
    tmp_path: Path,
    exc_factory: object,
) -> None:
    """Failed commit-sink rollback must not mask terminal infrastructure reason codes."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        reset_fails=True,
    )
    infrastructure_exc = exc_factory()  # type: ignore[operator]

    async def _raise_infrastructure_exit_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise infrastructure_exc

    runner._commit_dirty_worktree = _raise_infrastructure_exit_during_commit

    with pytest.raises(type(infrastructure_exc)) as caught:
        await _invoke(runner)

    assert caught.value is infrastructure_exc
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
async def test_provider_recovery_during_commit_sink_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Failed commit-sink provider-recovery rollback must abort instead of retrying."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        reset_fails=True,
    )

    async def _raise_provider_recovery_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise ProviderRecoveryRetryError()

    runner._commit_dirty_worktree = _raise_provider_recovery_during_commit

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
async def test_worker_cancellation_during_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Worker cancel during commit sink must roll back before propagating."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )

    async def _raise_cancel_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise asyncio.CancelledError()

    runner._commit_dirty_worktree = _raise_cancel_during_commit

    with pytest.raises(asyncio.CancelledError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_worker_cancellation_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Worker cancel must fail closed when rollback cannot discard agent edits."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["malformed after editing"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        reset_fails=True,
    )

    async def _raise_cancel_after_agent_edit(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = runner.heads_after_attempt[runner.attempt - 1]
        raise asyncio.CancelledError()

    runner._run_monitor_agent_with_service_recovery = _raise_cancel_after_agent_edit

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
async def test_compose_cleanup_failure_rolls_back_before_post_exception_hook_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compose cleanup hook repair failure must roll back again before propagating."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    mirror_path = tmp_path / "mirror.git"
    mirror_path.mkdir()
    item_start_head = "a" * 40
    dirty_head = "b" * 40
    cleanup_error = ComposeExecCleanupError(
        invocation_id="cleanup-failed",
        source="recovery",
        label="agent",
        message="cleanup failed",
    )
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[dirty_head],
        dirty_after_attempt=[True],
    )
    runner.current_head = item_start_head
    hook_repair_stages: list[str] = []

    monkeypatch.setattr(
        comment_verdict,
        "mirror_path_for_worktree",
        lambda _path: mirror_path,
    )

    async def _repair_mirror_hooks_path(_path: Path) -> bool:
        stage = (
            "before_comment_agent" if not hook_repair_stages else "after_comment_agent_exception"
        )
        hook_repair_stages.append(stage)
        if stage == "after_comment_agent_exception":
            runner.current_head = dirty_head
            raise OSError("hooks poisoned")
        return True

    monkeypatch.setattr(comment_verdict, "repair_mirror_hooks_path", _repair_mirror_hooks_path)

    async def _raise_cleanup(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = dirty_head
        raise cleanup_error

    runner._run_monitor_agent_with_service_recovery = _raise_cleanup

    with pytest.raises(_MonitorMirrorHooksPathRepairFailedError):
        await _invoke(runner)

    assert hook_repair_stages == [
        "before_comment_agent",
        "after_comment_agent_exception",
    ]
    assert runner.reset_targets == [item_start_head, item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_compose_cleanup_hook_repair_rollback_failure_is_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed post-hook-repair rollback must abort instead of masking hook repair failure."""
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()
    mirror_path = tmp_path / "mirror.git"
    mirror_path.mkdir()
    item_start_head = "a" * 40
    dirty_head = "b" * 40
    cleanup_error = ComposeExecCleanupError(
        invocation_id="cleanup-failed",
        source="recovery",
        label="agent",
        message="cleanup failed",
    )
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[dirty_head],
        dirty_after_attempt=[True],
    )
    runner.current_head = item_start_head
    hook_repair_stages: list[str] = []
    reset_attempts = 0

    monkeypatch.setattr(
        comment_verdict,
        "mirror_path_for_worktree",
        lambda _path: mirror_path,
    )

    async def _repair_mirror_hooks_path(_path: Path) -> bool:
        stage = (
            "before_comment_agent" if not hook_repair_stages else "after_comment_agent_exception"
        )
        hook_repair_stages.append(stage)
        if stage == "after_comment_agent_exception":
            runner.current_head = dirty_head
            raise OSError("hooks poisoned")
        return True

    monkeypatch.setattr(comment_verdict, "repair_mirror_hooks_path", _repair_mirror_hooks_path)

    async def _run_git(cmd: list[str], **kwargs: object) -> CommandResult:
        del kwargs
        nonlocal reset_attempts
        if "reset" in cmd and "--hard" in cmd:
            reset_attempts += 1
            runner.reset_targets.append(cmd[-1])
            if reset_attempts >= 2:
                return CommandResult(returncode=1, stdout="", stderr="reset failed")
            runner.current_head = cmd[-1]
            return CommandResult(returncode=0, stdout="", stderr="")
        if "rev-parse" in cmd:
            ref = cmd[-1]
            if ref.upper() == "HEAD":
                return CommandResult(returncode=0, stdout=f"{runner.current_head}\n", stderr="")
            return CommandResult(returncode=0, stdout=f"{ref}\n", stderr="")
        if "status" in cmd and "--porcelain" in cmd:
            return CommandResult(returncode=0, stdout="", stderr="")
        return CommandResult(returncode=0, stdout="", stderr="")

    runner._run_git = _run_git
    runner._deps.runner.run = _run_git

    async def _raise_cleanup(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = dirty_head
        raise cleanup_error

    runner._run_monitor_agent_with_service_recovery = _raise_cleanup

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert "roll back" in str(caught.value).lower()
    assert hook_repair_stages == [
        "before_comment_agent",
        "after_comment_agent_exception",
    ]
    assert runner.reset_targets == [item_start_head, item_start_head]
    assert runner.current_head == dirty_head


@pytest.mark.unit
async def test_compose_cleanup_failure_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Compose cleanup failures must not leave unpushed sink commits without provenance."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    sink_commit_head = "b" * 40
    cleanup_error = ComposeExecCleanupError(
        invocation_id="cleanup-failed",
        source="recovery",
        label="agent",
        message="cleanup failed",
    )
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[sink_commit_head],
        dirty_after_attempt=[True],
    )
    runner.current_head = item_start_head

    async def _raise_cleanup(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        raise cleanup_error

    runner._run_monitor_agent_with_service_recovery = _raise_cleanup

    with pytest.raises(ComposeExecCleanupError) as caught:
        await _invoke(runner)

    assert caught.value is cleanup_error
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head
