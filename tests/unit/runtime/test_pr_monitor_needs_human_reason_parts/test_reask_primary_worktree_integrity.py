"""Primary-worktree integrity tests split from needs-human re-ask coverage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.runtime.pr_monitor_runner import comments
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.types import _MonitorPolicyBlockedError
from tests.unit.runtime.test_pr_monitor_needs_human_reason import (
    _git,
    _init_real_worktree,
    _LocalCommandRunner,
)


@pytest.mark.unit
async def test_needs_human_reason_reask_preserves_post_repair_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clarification cleanup must not reset the repair commit that preceded it."""
    cleanup_calls: list[dict[str, object]] = []

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        """Return this test scenario’s synthetic monitor-agent verdict."""
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _rev_parse_head(_worktree_path: Path) -> str:
        """Return the synthetic primary-worktree revision."""
        return "b" * 40

    async def _check_reask_primary_worktree_clean(_runner: object, **kwargs: object) -> None:
        """Assert the primary worktree stays unchanged in this test."""
        cleanup_calls.append(kwargs)

    runner = SimpleNamespace(
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )
    monkeypatch.setattr(
        comments,
        "_check_reask_primary_worktree_clean",
        _check_reask_primary_worktree_clean,
    )

    result = await comments._enforce_needs_human_reason(
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

    assert result == VerdictResult(verdict="needs_human", reason="select the deployment region")
    assert cleanup_calls == [
        {
            "worktree_path": tmp_path / "ws_1",
            "restore_ref": "b" * 40,
        }
    ]


@pytest.mark.unit
async def test_needs_human_reason_reask_isolates_ignored_files_before_continuing(
    tmp_path: Path,
) -> None:
    """A clarification re-ask must not see or alter ignored primary-worktree files."""
    workspace_id = "ws_ignored_reask"
    worktree = _init_real_worktree(tmp_path, workspace_id)
    config = worktree / ".env"
    config.write_text("MODE=original\n", encoding="utf-8")
    dependency = worktree / ".venv" / "dependency.py"
    dependency.parent.mkdir()
    dependency.write_text("dependency\n", encoding="utf-8")
    (worktree / ".gitignore").write_text("*.env\n.venv/\n", encoding="utf-8")
    _git(worktree, "add", ".gitignore")
    _git(worktree, "commit", "-qm", "ignore dependencies")
    reask_ref = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    reask_worktree_paths: list[Path] = []

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        """Return this test scenario’s synthetic monitor-agent verdict."""
        reask = kwargs["isolated_worktree_host_path"]
        assert isinstance(reask, Path)
        assert kwargs["isolated_worktree_ref"] == reask_ref
        reask_worktree_paths.append(reask)
        assert not (reask / ".venv").exists()
        (reask / ".env").write_text("MODE=clarification-edit\n", encoding="utf-8")
        (reask / "generated.env").write_text("GENERATED=during-reask\n", encoding="utf-8")
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _rev_parse_head(_worktree_path: Path) -> str:
        """Return the synthetic primary-worktree revision."""
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_LocalCommandRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _rev_parse_head=_rev_parse_head,
    )

    result = await comments._enforce_needs_human_reason(
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

    assert result == VerdictResult(verdict="needs_human", reason="select the deployment region")
    assert reask_worktree_paths[0].parent == worktree.parent
    assert config.read_text(encoding="utf-8") == "MODE=original\n"
    assert dependency.exists()
    assert not list(worktree.parent.glob("*__companion__isolated_reask_*"))


@pytest.mark.unit
async def test_needs_human_reason_reask_preserves_primary_changes_made_during_reask(
    tmp_path: Path,
) -> None:
    """A clarification cleanup cannot reset unrelated primary-worktree changes."""
    workspace_id = "ws_reask_primary_changes"
    worktree = _init_real_worktree(tmp_path, workspace_id)
    primary_output = worktree / "operator-output.txt"

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        """Return this test scenario’s synthetic monitor-agent verdict."""
        reask = kwargs["isolated_worktree_host_path"]
        assert isinstance(reask, Path)
        (worktree / "tracked.py").write_text("x = 2\n", encoding="utf-8")
        primary_output.write_text("created independently\n", encoding="utf-8")
        return VerdictResult(
            verdict="needs_human",
            reason="select the deployment region",
        )

    async def _record_pr_monitor_audit_event(**_kwargs: object) -> None:
        """Record pr monitor audit event for this test."""
        return

    async def _rev_parse_head(_worktree_path: Path) -> str:
        """Return the synthetic primary-worktree revision."""
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_LocalCommandRunner()),
        _worktrees_root=tmp_path,
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
        _record_pr_monitor_audit_event=_record_pr_monitor_audit_event,
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
    assert (worktree / "tracked.py").read_text(encoding="utf-8") == "x = 2\n"
    assert primary_output.read_text(encoding="utf-8") == "created independently\n"
    assert not list(worktree.parent.glob("*__companion__isolated_reask_*"))
