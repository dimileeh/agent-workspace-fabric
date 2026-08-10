"""Cancellation coverage for isolated NEEDS_HUMAN reason re-asks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewComment,
    ReviewThread,
    _review_thread_body_hash,
    _review_thread_body_state_key,
)
from awf.runtime.pr_monitor_runner import comments
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.helpers import (
    _drop_stale_review_comment_addressed_state,
    _drop_stale_review_thread_addressed_state,
    _review_comment_body_hash,
    _review_comment_body_state_key,
)
from awf.runtime.pr_monitor_runner.types import _MonitorPolicyBlockedError
from tests.unit.runtime.test_pr_monitor_needs_human_reason import (
    _git,
    _init_real_worktree,
    _LocalCommandRunner,
)


@pytest.mark.unit
async def test_needs_human_reason_reask_cleans_worktree_when_cancelled(
    tmp_path: Path,
) -> None:
    """Cancellation must not leave clarification edits for the next fix-cycle item."""
    workspace_id = "ws_cancelled_reask"
    worktree = _init_real_worktree(tmp_path, workspace_id)
    config = worktree / ".env"
    config.write_text("MODE=original\n", encoding="utf-8")

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        reask = kwargs["isolated_worktree_host_path"]
        assert isinstance(reask, Path)
        (reask / "tracked.py").write_text("x = 2\n", encoding="utf-8")
        (reask / ".env").write_text("MODE=clarification-edit\n", encoding="utf-8")
        (reask / "generated.env").write_text("GENERATED=during-reask\n", encoding="utf-8")
        raise asyncio.CancelledError

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_LocalCommandRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )

    with pytest.raises(asyncio.CancelledError):
        await comments._enforce_needs_human_reason(
            runner,
            result=VerdictResult(verdict="needs_human"),
            original_prompt="original review task",
            workspace_id=workspace_id,
            pr_number=1,
            item_id="thread_1",
            item_kind="thread",
            item_author=None,
            item_path=None,
            item_line=None,
            commit_message="fix: address thread_1",
            compose_project="project",
            compose_file=Path("compose.yml"),
            state=None,
            task_tag=None,
            operation_start_head=None,
            base_branch="main",
            remote_branch=f"awf/{workspace_id}",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )

    assert (worktree / "tracked.py").read_text(encoding="utf-8") == "x = 1\n"
    assert config.read_text(encoding="utf-8") == "MODE=original\n"
    assert not list(worktree.glob(".awf-needs-human-reask-*"))


@pytest.mark.unit
async def test_needs_human_reason_reask_cleanup_survives_second_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second shutdown cancel cannot strand the isolated clarification checkout."""
    workspace_id = "ws_reask_second_cancel"
    worktree = _init_real_worktree(tmp_path, workspace_id)
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class _BlockingWorktreeRemoveRunner(_LocalCommandRunner):
        async def run(self, args: list[str]) -> CommandResult:
            if "worktree" in args and "remove" in args:
                cleanup_started.set()
                await release_cleanup.wait()
                result = await super().run(args)
                cleanup_finished.set()
                return result
            return await super().run(args)

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        raise asyncio.CancelledError

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_BlockingWorktreeRemoveRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )

    task = asyncio.create_task(
        comments._enforce_needs_human_reason(
            runner,
            result=VerdictResult(verdict="needs_human"),
            original_prompt="original review task",
            workspace_id=workspace_id,
            pr_number=1,
            item_id="thread_1",
            item_kind="thread",
            item_author=None,
            item_path=None,
            item_line=None,
            commit_message="fix: address thread_1",
            compose_project="project",
            compose_file=Path("compose.yml"),
            state=None,
            task_tag=None,
            operation_start_head=None,
            base_branch="main",
            remote_branch=f"awf/{workspace_id}",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )
    )
    await asyncio.wait_for(cleanup_started.wait(), timeout=5.0)
    task.cancel()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)

    assert cleanup_finished.is_set()
    assert not list(worktree.glob(".awf-needs-human-reask-*"))


@pytest.mark.unit
async def test_needs_human_reason_reask_promotes_cleanup_failure_after_terminal_error(
    tmp_path: Path,
) -> None:
    """A terminal re-ask error cannot hide an unremoved isolated checkout."""
    workspace_id = "ws_reask_terminal_cleanup_failure"
    worktree = _init_real_worktree(tmp_path, workspace_id)
    terminal_error = _MonitorPolicyBlockedError(
        "terminal re-ask failure",
        reason_code="TERMINAL_REASK_FAILURE",
    )

    class _FailedWorktreeRemoveRunner(_LocalCommandRunner):
        async def run(self, args: list[str]) -> CommandResult:
            if "worktree" in args and "remove" in args:
                return CommandResult(returncode=1, stdout="", stderr="worktree remove failed")
            return await super().run(args)

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        reask = kwargs["isolated_worktree_host_path"]
        assert isinstance(reask, Path)
        (reask / "tracked.py").write_text("x = 2\n", encoding="utf-8")
        raise terminal_error

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_FailedWorktreeRemoveRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )

    with pytest.raises(_MonitorPolicyBlockedError) as raised:
        await comments._enforce_needs_human_reason(
            runner,
            result=VerdictResult(verdict="needs_human"),
            original_prompt="original review task",
            workspace_id=workspace_id,
            pr_number=1,
            item_id="thread_1",
            item_kind="thread",
            item_author=None,
            item_path=None,
            item_line=None,
            commit_message="fix: address thread_1",
            compose_project="project",
            compose_file=Path("compose.yml"),
            state=None,
            task_tag=None,
            operation_start_head=None,
            base_branch="main",
            remote_branch=f"awf/{workspace_id}",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )

    assert raised.value.reason_code == "VALIDATION_WORKTREE_CLEANUP_FAILED"
    assert "git worktree remove" in str(raised.value)
    assert raised.value.__cause__ is terminal_error
    assert list(worktree.glob(".awf-needs-human-reask-*"))


@pytest.mark.unit
@pytest.mark.parametrize("outcome", ("success", "terminal_error", "error"))
async def test_needs_human_reason_reask_post_invocation_cleanup_survives_cancellation(
    outcome: str,
    tmp_path: Path,
) -> None:
    """Every post-invocation cleanup must finish before cancellation escapes."""
    workspace_id = f"ws_reask_post_invocation_cancel_{outcome}"
    worktree = _init_real_worktree(tmp_path, workspace_id)
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class _BlockingWorktreeRemoveRunner(_LocalCommandRunner):
        async def run(self, args: list[str]) -> CommandResult:
            if "worktree" in args and "remove" in args:
                cleanup_started.set()
                await release_cleanup.wait()
                result = await super().run(args)
                cleanup_finished.set()
                return result
            return await super().run(args)

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        reask = kwargs["isolated_worktree_host_path"]
        assert isinstance(reask, Path)
        (reask / "tracked.py").write_text("x = 2\n", encoding="utf-8")
        if outcome == "success":
            return VerdictResult(verdict="needs_human", reason="select a deployment region")
        if outcome == "terminal_error":
            raise _MonitorPolicyBlockedError("terminal re-ask failure")
        raise RuntimeError("ordinary re-ask failure")

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        return None

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_BlockingWorktreeRemoveRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
        _rev_parse_head=_rev_parse_head,
    )

    task = asyncio.create_task(
        comments._enforce_needs_human_reason(
            runner,
            result=VerdictResult(verdict="needs_human"),
            original_prompt="original review task",
            workspace_id=workspace_id,
            pr_number=1,
            item_id="thread_1",
            item_kind="thread",
            item_author=None,
            item_path=None,
            item_line=None,
            commit_message="fix: address thread_1",
            compose_project="project",
            compose_file=Path("compose.yml"),
            state=None,
            task_tag=None,
            operation_start_head=None,
            base_branch="main",
            remote_branch=f"awf/{workspace_id}",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )
    )
    await asyncio.wait_for(cleanup_started.wait(), timeout=5.0)
    task.cancel()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)

    assert cleanup_finished.is_set()
    assert not list(worktree.glob(".awf-needs-human-reask-*"))


@pytest.mark.unit
async def test_needs_human_reason_reask_persists_failed_post_invocation_cleanup_on_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation keeps the clarified human reason when cleanup also fails."""
    workspace_id = "ws_reask_post_invocation_cancel_cleanup_failure"
    worktree = _init_real_worktree(tmp_path, workspace_id)
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    persistence_started = asyncio.Event()
    release_persistence = asyncio.Event()
    persisted_states: list[dict[str, str]] = []
    state = MonitorState()

    class _BlockingFailedWorktreeRemoveRunner(_LocalCommandRunner):
        async def run(self, args: list[str]) -> CommandResult:
            if "worktree" in args and "remove" in args:
                cleanup_started.set()
                await release_cleanup.wait()
                return CommandResult(returncode=1, stdout="", stderr="worktree remove failed")
            return await super().run(args)

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        reask = kwargs["isolated_worktree_host_path"]
        assert isinstance(reask, Path)
        (reask / "tracked.py").write_text("x = 2\n", encoding="utf-8")
        return VerdictResult(verdict="needs_human", reason="select a deployment region")

    async def _persist_reask_cleanup_failure_after_cancellation(
        _runner: object,
        **kwargs: object,
    ) -> None:
        assert kwargs["workspace_id"] == "ws_reask_post_invocation_cancel_cleanup_failure"
        assert kwargs["item_id"] == "thread_1"
        assert kwargs["needs_human_reason"] == "select a deployment region"
        assert kwargs["item_body_hash"] == "thread-body-hash"
        assert kwargs["cleanup_error"] == (
            "`git worktree remove` could not remove the NEEDS_HUMAN reason re-ask checkout"
        )
        persistence_started.set()
        await release_persistence.wait()
        persisted_states.append(dict(state.threads_addressed_ids))

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_BlockingFailedWorktreeRemoveRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )
    monkeypatch.setattr(
        comments,
        "_persist_reask_cleanup_failure_after_cancellation",
        _persist_reask_cleanup_failure_after_cancellation,
    )
    task = asyncio.create_task(
        comments._enforce_needs_human_reason(
            runner,
            result=VerdictResult(verdict="needs_human"),
            original_prompt="original review task",
            workspace_id=workspace_id,
            pr_number=1,
            item_id="thread_1",
            item_kind="thread",
            item_author=None,
            item_path=None,
            item_line=None,
            item_body_hash="thread-body-hash",
            commit_message="fix: address thread_1",
            compose_project="project",
            compose_file=Path("compose.yml"),
            state=state,
            task_tag=None,
            operation_start_head=None,
            base_branch="main",
            remote_branch=f"awf/{workspace_id}",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )
    )
    await asyncio.wait_for(cleanup_started.wait(), timeout=5.0)
    task.cancel()
    release_cleanup.set()
    await asyncio.wait_for(persistence_started.wait(), timeout=5.0)
    task.cancel()
    release_persistence.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)

    assert persisted_states == [
        {
            "thread_1": "needs_human",
            "__review_thread_body_hash__:thread_1": "thread-body-hash",
            "__needs_human_reason__:thread_1": "select a deployment region",
        }
    ]
    assert list(worktree.glob(".awf-needs-human-reask-*"))


@pytest.mark.unit
async def test_needs_human_reason_reask_does_not_persist_synthetic_reason_after_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup failure cannot make up an agent NEEDS_HUMAN reason."""
    workspace_id = "ws_reask_cancel_cleanup_failure_without_reason"
    worktree = _init_real_worktree(tmp_path, workspace_id)
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    persisted_reasons: list[str | None] = []
    state = MonitorState()

    class _BlockingFailedWorktreeRemoveRunner(_LocalCommandRunner):
        async def run(self, args: list[str]) -> CommandResult:
            if "worktree" in args and "remove" in args:
                cleanup_started.set()
                await release_cleanup.wait()
                return CommandResult(returncode=1, stdout="", stderr="worktree remove failed")
            return await super().run(args)

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        raise asyncio.CancelledError

    async def _persist_reask_cleanup_failure_after_cancellation(
        _runner: object,
        **kwargs: object,
    ) -> None:
        assert kwargs["workspace_id"] == "ws_reask_cancel_cleanup_failure_without_reason"
        assert kwargs["item_id"] == "thread_1"
        persisted_reasons.append(kwargs["needs_human_reason"])
        assert kwargs["cleanup_error"] == (
            "`git worktree remove` could not remove the NEEDS_HUMAN reason re-ask checkout"
        )

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_BlockingFailedWorktreeRemoveRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )
    monkeypatch.setattr(
        comments,
        "_persist_reask_cleanup_failure_after_cancellation",
        _persist_reask_cleanup_failure_after_cancellation,
    )
    task = asyncio.create_task(
        comments._enforce_needs_human_reason(
            runner,
            result=VerdictResult(verdict="needs_human"),
            original_prompt="original review task",
            workspace_id=workspace_id,
            pr_number=1,
            item_id="thread_1",
            item_kind="thread",
            item_author=None,
            item_path=None,
            item_line=None,
            commit_message="fix: address thread_1",
            compose_project="project",
            compose_file=Path("compose.yml"),
            state=state,
            task_tag=None,
            operation_start_head=None,
            base_branch="main",
            remote_branch=f"awf/{workspace_id}",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )
    )
    await asyncio.wait_for(cleanup_started.wait(), timeout=5.0)
    task.cancel()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)

    assert persisted_reasons == [None]
    assert "__needs_human_reason__:thread_1" not in state.threads_addressed_ids


@pytest.mark.unit
@pytest.mark.parametrize("workspace_exists", (True, False))
@pytest.mark.parametrize("item_kind", ("thread", "review"))
@pytest.mark.parametrize(
    ("needs_human_reason", "expected_reason"),
    (
        (
            "a maintainer must choose the deployment region",
            "a maintainer must choose the deployment region",
        ),
        (None, None),
    ),
)
async def test_persist_reask_cleanup_failure_after_cancellation_preserves_feedback_identity(
    monkeypatch: pytest.MonkeyPatch,
    workspace_exists: bool,
    item_kind: str,
    needs_human_reason: str | None,
    expected_reason: str | None,
) -> None:
    """A durable cleanup blocker retains its exact unresolved-feedback identity."""
    workspace = SimpleNamespace(
        monitor_threads_addressed={
            "other_thread": "fix_committed",
            "__needs_human_reason__:thread_1": "stale agent reason",
        }
    )
    session = SimpleNamespace(committed=False)
    audit_events: list[dict[str, object]] = []

    class _SessionContext:
        async def __aenter__(self) -> SimpleNamespace:
            return session

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def _commit() -> None:
        session.committed = True

    session.commit = _commit

    class _WorkspaceRepository:
        def __init__(self, _session: object) -> None:
            pass

        async def get_for_update(self, workspace_id: str) -> SimpleNamespace | None:
            assert workspace_id == "ws_1"
            return workspace if workspace_exists else None

    monkeypatch.setattr(comments, "WorkspaceRepository", _WorkspaceRepository)

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        audit_events.append(dict(kwargs))

    runner = SimpleNamespace(
        _deps=SimpleNamespace(session_factory=lambda: _SessionContext()),
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
    )

    if item_kind == "thread":
        feedback = ReviewThread(
            thread_id="thread_1",
            path="src/monitor.py",
            line=17,
            body_excerpt="worktree cleanup must be fixed",
            author="reviewer",
        )
        body_state_key = _review_thread_body_state_key(feedback.thread_id)
        item_body_hash = _review_thread_body_hash(feedback)
    else:
        feedback = ReviewComment(
            comment_id="thread_1",
            body_excerpt="worktree cleanup must be fixed",
            body="worktree cleanup must be fixed",
            author="reviewer",
        )
        body_state_key = _review_comment_body_state_key(feedback.comment_id)
        item_body_hash = _review_comment_body_hash(feedback)

    await comments._persist_reask_cleanup_failure_after_cancellation(
        runner,
        workspace_id="ws_1",
        pr_number=42,
        item_id="thread_1",
        item_kind=item_kind,
        item_author="reviewer",
        item_path="src/monitor.py",
        item_line=17,
        needs_human_reason=needs_human_reason,
        item_body_hash=item_body_hash,
        cleanup_error="worktree cleanup failed",
        base_branch="main",
        remote_branch="awf/ws_1",
        operation_id="operation_1",
        operation_type="monitor",
        monitor_log=None,
    )

    if workspace_exists:
        expected_state = {"other_thread": "fix_committed", "thread_1": "needs_human"}
        expected_state[body_state_key] = item_body_hash
        if expected_reason is not None:
            expected_state["__needs_human_reason__:thread_1"] = expected_reason
        assert workspace.monitor_threads_addressed == expected_state
        assert session.committed is True
        resumed_state = MonitorState(threads_addressed_ids=dict(expected_state))
        status = PRStatus(
            number=42,
            head_sha="abc123",
            mergeable=MergeableState.MERGEABLE,
            check_state=CheckState.SUCCESS,
            unresolved_inline_threads=(feedback,) if item_kind == "thread" else (),
            unresolved_review_comments=(feedback,) if item_kind == "review" else (),
            base_behind_count=0,
            merge_state_status=MergeStateStatus.CLEAN,
        )
        if item_kind == "thread":
            assert _drop_stale_review_thread_addressed_state(status, resumed_state) is False
        else:
            assert _drop_stale_review_comment_addressed_state(status, resumed_state) is False
        assert resumed_state.threads_addressed_ids == expected_state
        assert audit_events == [
            {
                "workspace_id": "ws_1",
                "event_type": "workspace.audit.comment_resolution",
                "action": f"address_{item_kind}",
                "outcome": "failed",
                "reason_code": "VALIDATION_WORKTREE_CLEANUP_FAILED",
                "pr_number": 42,
                "status": None,
                "base_branch": "main",
                "remote_branch": "awf/ws_1",
                "operation_id": "operation_1",
                "operation_type": "monitor",
                "monitor_log": None,
                "evidence": {
                    "item_id": "thread_1",
                    "item_kind": item_kind,
                    "item_author": "reviewer",
                    "item_path": "src/monitor.py",
                    "item_line": 17,
                    "reask_cleanup_error": "worktree cleanup failed",
                },
            }
        ]
    else:
        assert workspace.monitor_threads_addressed == {
            "other_thread": "fix_committed",
            "__needs_human_reason__:thread_1": "stale agent reason",
        }
        assert session.committed is False
        assert audit_events == []
