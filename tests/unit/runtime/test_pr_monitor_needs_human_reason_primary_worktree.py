"""Primary-worktree cleanup coverage for NEEDS_HUMAN clarification re-asks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.runtime.pr_monitor_runner import comments
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.types import _MonitorPolicyBlockedError
from tests.unit.runtime.test_pr_monitor_needs_human_reason import (
    _git,
    _init_awf_linked_worktree,
    _LocalCommandRunner,
)


@pytest.mark.unit
@pytest.mark.parametrize("reask_raises", (False, True))
async def test_needs_human_reason_reask_blocks_when_primary_worktree_check_fails(
    reask_raises: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed primary-worktree check must stop the fix cycle."""
    cleanup_calls: list[dict[str, object]] = []
    audit_events: list[dict[str, object]] = []

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        if reask_raises:
            raise RuntimeError("re-ask failed")
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _check_reask_primary_worktree_clean(_runner: object, **kwargs: object) -> str:
        cleanup_calls.append(kwargs)
        return "could not inspect primary worktree"

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return "c" * 40

    async def _record_pr_monitor_audit_event(**kwargs: object) -> None:
        audit_events.append(kwargs)

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
        _rev_parse_head=_rev_parse_head,
    )
    monkeypatch.setattr(
        comments,
        "_check_reask_primary_worktree_clean",
        _check_reask_primary_worktree_clean,
    )

    with pytest.raises(_MonitorPolicyBlockedError) as raised:
        await comments._enforce_needs_human_reason(
            runner,
            result=VerdictResult(verdict="needs_human"),
            original_prompt="original review task",
            workspace_id="ws_1",
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
            operation_start_head="a" * 40,
            base_branch="main",
            remote_branch="awf/ws_1",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )

    assert raised.value.reason_code == "VALIDATION_WORKTREE_CLEANUP_FAILED"
    assert audit_events == []
    assert cleanup_calls == [
        {
            "worktree_path": tmp_path / "ws_1",
            "restore_ref": "c" * 40,
        }
    ]


@pytest.mark.unit
async def test_needs_human_reason_reask_preserves_primary_commit_made_during_reask(
    tmp_path: Path,
) -> None:
    """A clean primary worktree with a new HEAD still fails closed without reset."""
    workspace_id = "ws_reask_primary_commit"
    worktree = _init_awf_linked_worktree(tmp_path, workspace_id)

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        reask = kwargs["isolated_worktree_host_path"]
        assert isinstance(reask, Path)
        (worktree / "tracked.py").write_text("x = 2\n", encoding="utf-8")
        _git(worktree, "add", "tracked.py")
        _git(worktree, "commit", "-qm", "independent primary change")
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        return None

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_LocalCommandRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
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
    assert _git(worktree, "log", "-1", "--format=%s").stdout.strip() == "independent primary change"
    assert (worktree / "tracked.py").read_text(encoding="utf-8") == "x = 2\n"
    assert not list(worktree.parent.glob("*__companion__isolated_reask_*"))
