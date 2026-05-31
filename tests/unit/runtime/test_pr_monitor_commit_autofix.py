"""Unit tests for PR monitor pre-commit autofix commit retries."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.runtime.pr_monitor_runner.commit_autofix import (
    _monitor_precommit_autofix_repair_paths,
    _retry_monitor_precommit_autofix_commit_once,
)


@pytest.mark.unit
def test_monitor_commit_autofix_does_not_import_executor_quality_gates() -> None:
    source_path = (
        Path(__file__).resolve().parents[3] / "src/awf/runtime/pr_monitor_runner/commit_autofix.py"
    )
    tree = ast.parse(source_path.read_text())

    imported_modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}

    assert "awf.control.executor.quality_gates" not in imported_modules


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
def test_monitor_precommit_autofix_deduplicates_formatter_repair_paths() -> None:
    output = (
        "ruff format........................................................Failed\n"
        "- hook id: awf-ruff-format-check\n"
        "- exit code: 1\n"
        "- files were modified by this hook\n\n"
        "Would reformat: src/awf/runtime/pr_monitor_runner/precommit_autofix.py\n"
        "Would reformat: src/awf/runtime/pr_monitor_runner/precommit_autofix.py\n"
    )
    commit_result = CommandResult(returncode=1, stdout=output, stderr="")

    assert _monitor_precommit_autofix_repair_paths(commit_result) == (
        "src/awf/runtime/pr_monitor_runner/precommit_autofix.py",
    )


def _deterministic_autofix_stderr(*paths: str) -> str:
    return (
        "fix end of files................................................Failed\n"
        "- hook id: end-of-file-fixer\n"
        "- exit code: 1\n"
        "- files were modified by this hook\n\n" + "".join(f"Fixing {path}\n" for path in paths)
    )


@pytest.mark.unit
async def test_monitor_precommit_autofix_retry_returns_none_when_status_fails(
    tmp_path: Path,
) -> None:
    fixed_path = "src/awf/fixed.py"
    runner = FakeCommandRunner()
    runner.queue_result(returncode=128, stderr="fatal: status failed")

    retry = await _retry_monitor_precommit_autofix_commit_once(
        runner=runner,
        workspace_id="ws_123",
        worktree_path=tmp_path,
        message="fix: monitor repair",
        commit_result=CommandResult(
            returncode=1,
            stdout="",
            stderr=_deterministic_autofix_stderr(fixed_path),
        ),
        operation_dirty_paths=(fixed_path,),
    )

    assert retry is None
    assert len(runner.calls) == 1
    assert runner.calls[0].args[-2:] == ["status", "--porcelain"]


@pytest.mark.unit
async def test_monitor_precommit_autofix_retry_returns_none_when_worktree_is_clean(
    tmp_path: Path,
) -> None:
    fixed_path = "src/awf/fixed.py"
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="")

    retry = await _retry_monitor_precommit_autofix_commit_once(
        runner=runner,
        workspace_id="ws_123",
        worktree_path=tmp_path,
        message="fix: monitor repair",
        commit_result=CommandResult(
            returncode=1,
            stdout="",
            stderr=_deterministic_autofix_stderr(fixed_path),
        ),
        operation_dirty_paths=(fixed_path,),
    )

    assert retry is None
    assert len(runner.calls) == 1


@pytest.mark.unit
async def test_monitor_precommit_autofix_retry_does_not_restage_staged_only_repair_paths(
    tmp_path: Path,
) -> None:
    fixed_path = "src/awf/fixed.py"
    staged_path = "src/awf/unaffected.py"
    hook_stderr = (
        "fix end of files................................................Failed\n"
        "- hook id: end-of-file-fixer\n"
        "- exit code: 1\n"
        "- files were modified by this hook\n\n"
        f"Fixing {staged_path}\n"
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
async def test_monitor_precommit_autofix_retry_returns_none_when_restage_fails(
    tmp_path: Path,
) -> None:
    fixed_path = "src/awf/fixed.py"
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout=f" M {fixed_path}\n")
    runner.queue_result(returncode=128, stderr="fatal: add failed")

    retry = await _retry_monitor_precommit_autofix_commit_once(
        runner=runner,
        workspace_id="ws_123",
        worktree_path=tmp_path,
        message="fix: monitor repair",
        commit_result=CommandResult(
            returncode=1,
            stdout="",
            stderr=_deterministic_autofix_stderr(fixed_path),
        ),
        operation_dirty_paths=(fixed_path,),
    )

    assert retry is None
    assert len(runner.calls) == 2
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
