"""Cancellation and reason-sanitization regression coverage for re-asks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import FakeCommandRunner
from awf.runtime.pr_monitor_runner import comments
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.helpers import _sanitize_verdict_reason


@pytest.mark.unit
async def test_needs_human_reason_reask_reraises_cancellation_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup failure must not replace the monitor's cancellation signal."""
    cleanup_calls: list[dict[str, object]] = []

    async def _invoke_cli_for_verdict_result(**_kwargs: object) -> VerdictResult:
        raise asyncio.CancelledError

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return "e" * 40

    async def _check_reask_primary_worktree_clean(_runner: object, **kwargs: object) -> str:
        cleanup_calls.append(kwargs)
        return "could not inspect primary worktree"

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

    with pytest.raises(asyncio.CancelledError):
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
            operation_start_head=None,
            base_branch="main",
            remote_branch="awf/ws_1",
            operation_id=None,
            operation_type=None,
            monitor_log=None,
        )

    assert cleanup_calls == [
        {
            "worktree_path": tmp_path / "ws_1",
            "restore_ref": "e" * 40,
        }
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "credential_only_reason",
    (
        "ghp_abcdefghijklmnopqrstuvwxyz1234567890",
        "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz1234567890",
        "ghp_abcdefghijklmnopqrstuvwxyz1234567890.",
        '"ghp_abcdefghijklmnopqrstuvwxyz1234567890"',
    ),
)
def test_sanitize_verdict_reason_treats_credential_only_reason_as_missing(
    credential_only_reason: str,
) -> None:
    """A redacted credential alone is not an actionable operator decision."""
    assert _sanitize_verdict_reason(credential_only_reason) is None


@pytest.mark.unit
def test_sanitize_verdict_reason_preserves_meaningful_text_with_redacted_details() -> None:
    reason = "A maintainer must decide whether to rotate GITHUB_TOKEN=secretValue123456."

    assert _sanitize_verdict_reason(reason) == (
        "A maintainer must decide whether to rotate GITHUB_TOKEN=<redacted>"
    )


@pytest.mark.unit
async def test_isolated_reask_git_lifecycle_ignores_object_lookup_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-ask checkout Git commands must not inherit an agent object store."""
    worktree = tmp_path / "ws_reask_object_env"
    (worktree / ".git").mkdir(parents=True)
    command_runner = FakeCommandRunner()
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")
    monkeypatch.setenv("REASK_TEST_PRESERVED", "preserved")

    async def _prepare_primary_worktree(_runner: object, **_kwargs: object) -> None:
        """Keep this regression focused on the isolated checkout lifecycle."""
        return

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        """Avoid unrelated ownership work while inspecting Git environments."""
        return True

    monkeypatch.setattr(comments, "_prepare_reask_primary_worktree", _prepare_primary_worktree)
    monkeypatch.setattr(comments, "repair_agent_runtime_ownership", _repair_agent_runtime_ownership)

    reask_worktree = await comments._create_isolated_reask_worktree(
        runner,
        worktree_path=worktree,
        restore_ref="a" * 40,
    )

    assert reask_worktree is not None
    assert await comments._remove_isolated_reask_worktree(runner, reask_worktree) is None
    assert len(command_runner.calls) == 5
    assert "worktree" in command_runner.calls[0].args
    assert "add" in command_runner.calls[0].args
    assert "ls-tree" in command_runner.calls[1].args
    assert "checkout" in command_runner.calls[2].args
    assert "read-tree" in command_runner.calls[3].args
    assert "--reset" in command_runner.calls[3].args
    assert "--no-sparse-checkout" in command_runner.calls[3].args
    assert "worktree" in command_runner.calls[4].args
    assert "remove" in command_runner.calls[4].args
    assert all(call.env is not None for call in command_runner.calls)
    for call in command_runner.calls:
        assert "GIT_OBJECT_DIRECTORY" not in call.env
        assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in call.env
        assert call.env["REASK_TEST_PRESERVED"] == "preserved"
