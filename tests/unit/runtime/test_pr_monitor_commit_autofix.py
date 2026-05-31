"""Unit tests for PR monitor pre-commit autofix commit retries."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.runtime.pr_monitor_runner.commit_autofix import (
    _monitor_precommit_autofix_repair_paths,
    _retry_monitor_precommit_autofix_commit_once,
)


@pytest.mark.unit
def test_monitor_precommit_autofix_skips_semantic_ruff_check_autofix_paths() -> None:
    output = (
        "ruff check..............................................................Failed\n"
        "- hook id: awf-ruff-check\n"
        "- exit code: 1\n"
        "- files were modified by this hook\n\n"
        "I001 [*] Import block is un-sorted or un-formatted\n"
        "   --> src/awf/mcp/server.py:13:1\n"
        "Found 1 error.\n"
        "[*] 1 fixable with the `--fix` option.\n"
    )
    commit_result = CommandResult(returncode=1, stdout=output, stderr="")

    assert _monitor_precommit_autofix_repair_paths(commit_result) == ()


@pytest.mark.unit
def test_monitor_precommit_autofix_keeps_deterministic_hook_repair_paths() -> None:
    output = (
        "fix end of files................................................Failed\n"
        "- hook id: end-of-file-fixer\n"
        "- exit code: 1\n"
        "- files were modified by this hook\n\n"
        "Fixing docs/awf-plans/ws_761.conformance.json\n"
    )
    commit_result = CommandResult(returncode=1, stdout="", stderr=output)

    assert _monitor_precommit_autofix_repair_paths(commit_result) == (
        "docs/awf-plans/ws_761.conformance.json",
    )


@pytest.mark.unit
async def test_monitor_precommit_autofix_retry_allows_unaffected_staged_paths(
    tmp_path: Path,
) -> None:
    fixed_path = "src/awf/fixed.py"
    staged_path = "src/awf/unaffected.py"
    hook_stderr = (
        "fix end of files................................................Failed\n"
        "- hook id: end-of-file-fixer\n"
        "- exit code: 1\n"
        "- files were modified by this hook\n\n"
        f"Fixing {fixed_path}\n"
    )
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout=f"M  {staged_path}\nMM {fixed_path}\n")
    runner.queue_result(returncode=0)
    runner.queue_result(returncode=0)

    retry = await _retry_monitor_precommit_autofix_commit_once(
        runner=runner,
        workspace_id="ws_123",
        worktree_path=tmp_path,
        message="fix: monitor repair",
        commit_result=CommandResult(returncode=1, stdout="", stderr=hook_stderr),
        operation_dirty_paths=(staged_path, fixed_path),
    )

    assert retry is not None
    retry_result, restaged_paths = retry
    assert retry_result.ok
    assert restaged_paths == (fixed_path,)
    assert runner.calls[1].args[-3:] == ["add", "--", fixed_path]


@pytest.mark.unit
async def test_monitor_precommit_autofix_retry_rejects_non_repair_worktree_paths(
    tmp_path: Path,
) -> None:
    fixed_path = "src/awf/fixed.py"
    unrelated_path = "src/awf/unrelated.py"
    hook_stderr = (
        "fix end of files................................................Failed\n"
        "- hook id: end-of-file-fixer\n"
        "- exit code: 1\n"
        "- files were modified by this hook\n\n"
        f"Fixing {fixed_path}\n"
    )
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout=f" M {unrelated_path}\nMM {fixed_path}\n")

    retry = await _retry_monitor_precommit_autofix_commit_once(
        runner=runner,
        workspace_id="ws_123",
        worktree_path=tmp_path,
        message="fix: monitor repair",
        commit_result=CommandResult(returncode=1, stdout="", stderr=hook_stderr),
        operation_dirty_paths=(unrelated_path, fixed_path),
    )

    assert retry is None
    assert len(runner.calls) == 1
