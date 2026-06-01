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
def test_monitor_precommit_autofix_skips_deterministic_hook_without_marker() -> None:
    output = (
        "trim trailing whitespace...........................................Failed\n"
        "- hook id: trailing-whitespace\n"
        "- exit code: 1\n\n"
        "ruff check........................................................Failed\n"
        "- hook id: awf-ruff-check\n"
        "- exit code: 1\n"
        "- files were modified by this hook\n\n"
        "Found 1 error.\n"
    )
    commit_result = CommandResult(returncode=1, stdout=output, stderr="")

    assert _monitor_precommit_autofix_repair_paths(commit_result) == ()


@pytest.mark.unit
def test_monitor_precommit_autofix_skips_formatter_check_repair_paths() -> None:
    output = (
        "ruff format........................................................Failed\n"
        "- hook id: awf-ruff-format-check\n"
        "- exit code: 1\n"
        "- files were modified by this hook\n\n"
        "Would reformat: src/awf/runtime/pr_monitor_runner/precommit_autofix.py\n"
        "Would reformat: src/awf/runtime/pr_monitor_runner/precommit_autofix.py\n"
    )
    commit_result = CommandResult(returncode=1, stdout=output, stderr="")

    assert _monitor_precommit_autofix_repair_paths(commit_result) == ()


@pytest.mark.unit
def test_monitor_precommit_autofix_keeps_normalizer_paths_when_format_check_cofails() -> None:
    output = (
        "fix end of files................................................Failed\n"
        "- hook id: end-of-file-fixer\n"
        "- exit code: 1\n"
        "- files were modified by this hook\n\n"
        "Fixing docs/awf-plans/ws_761.conformance.json\n"
        "ruff format........................................................Failed\n"
        "- hook id: awf-ruff-format-check\n"
        "- exit code: 1\n\n"
        "Would reformat: src/awf/runtime/pr_monitor_runner/precommit_autofix.py\n"
    )
    commit_result = CommandResult(returncode=1, stdout=output, stderr="")

    assert _monitor_precommit_autofix_repair_paths(commit_result) == (
        "docs/awf-plans/ws_761.conformance.json",
    )


def _deterministic_autofix_stderr(*paths: str) -> str:
    return (
        "fix end of files................................................Failed\n"
        "- hook id: end-of-file-fixer\n"
        "- exit code: 1\n"
        "- files were modified by this hook\n\n" + "".join(f"Fixing {path}\n" for path in paths)
    )


def _deterministic_autofix_with_cofailed_mypy_stderr(*paths: str) -> str:
    return (
        _deterministic_autofix_stderr(*paths)
        + "mypy...................................................................Failed\n"
        "- hook id: awf-mypy\n"
        "- exit code: 1\n\n"
        "src/awf/runtime/pr_monitor_runner/precommit_autofix.py:1: error: example\n"
    )


@pytest.mark.unit
def test_monitor_precommit_autofix_keeps_normalizer_paths_when_semantic_hook_cofails() -> None:
    fixed_path = "docs/awf-plans/ws_761.conformance.json"
    commit_result = CommandResult(
        returncode=1,
        stdout="",
        stderr=_deterministic_autofix_with_cofailed_mypy_stderr(fixed_path),
    )

    assert _monitor_precommit_autofix_repair_paths(commit_result) == (fixed_path,)


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
async def test_monitor_precommit_autofix_retry_returns_none_when_only_staged_repair_paths_remain(
    tmp_path: Path,
) -> None:
    fixed_path = "src/awf/fixed.py"
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout=f"M  {fixed_path}\n")

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
async def test_monitor_precommit_autofix_retry_allows_untracked_operation_paths(
    tmp_path: Path,
) -> None:
    fixed_path = "src/awf/fixed.py"
    untracked_path = "docs/new-notes.md"
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout=f"?? {untracked_path}\nMM {fixed_path}\n")
    runner.queue_result(returncode=0)
    runner.queue_result(returncode=0)

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
        operation_dirty_paths=(untracked_path, fixed_path),
    )

    assert retry is not None
    retry_result, restaged_paths = retry
    assert retry_result.ok
    assert restaged_paths == (fixed_path,)
    assert runner.calls[1].args[-3:] == ["add", "--", fixed_path]


@pytest.mark.unit
async def test_monitor_precommit_autofix_retry_allows_hook_modified_files_inside_untracked_operation_directory(
    tmp_path: Path,
) -> None:
    fixed_path = "docs/newdir/file.py"
    operation_dir = "docs/newdir/"
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout=f"AM {fixed_path}\n")
    runner.queue_result(returncode=0)
    runner.queue_result(returncode=0)

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
        operation_dirty_paths=(operation_dir,),
    )

    assert retry is not None
    retry_result, restaged_paths = retry
    assert retry_result.ok
    assert restaged_paths == (fixed_path,)
    assert runner.calls[1].args[-3:] == ["add", "--", fixed_path]


@pytest.mark.unit
async def test_monitor_precommit_autofix_retry_rejects_paths_outside_untracked_operation_directory(
    tmp_path: Path,
) -> None:
    fixed_path = "docs/newdir-sibling/file.py"
    operation_dir = "docs/newdir/"
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout=f"AM {fixed_path}\n")

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
        operation_dirty_paths=(operation_dir,),
    )

    assert retry is None
    assert len(runner.calls) == 1


@pytest.mark.unit
async def test_monitor_precommit_autofix_retry_restages_normalizer_when_other_hook_cofails(
    tmp_path: Path,
) -> None:
    fixed_path = "docs/awf-plans/ws_761.conformance.json"
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout=f" M {fixed_path}\n")
    runner.queue_result(returncode=0)
    runner.queue_result(returncode=1, stderr="mypy still failed")

    retry = await _retry_monitor_precommit_autofix_commit_once(
        runner=runner,
        workspace_id="ws_123",
        worktree_path=tmp_path,
        message="fix: monitor repair",
        commit_result=CommandResult(
            returncode=1,
            stdout="",
            stderr=_deterministic_autofix_with_cofailed_mypy_stderr(fixed_path),
        ),
        operation_dirty_paths=(fixed_path,),
    )

    assert retry is not None
    retry_result, restaged_paths = retry
    assert not retry_result.ok
    assert restaged_paths == (fixed_path,)
    assert runner.calls[1].args[-3:] == ["add", "--", fixed_path]


@pytest.mark.unit
async def test_monitor_precommit_autofix_retry_restages_quoted_porcelain_paths(
    tmp_path: Path,
) -> None:
    fixed_path = "dir a/file b.txt"
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout=f' M "{fixed_path}"\n')
    runner.queue_result(returncode=0)
    runner.queue_result(returncode=0)

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

    assert retry is not None
    retry_result, restaged_paths = retry
    assert retry_result.ok
    assert restaged_paths == (fixed_path,)
    assert runner.calls[1].args[-3:] == ["add", "--", fixed_path]


@pytest.mark.unit
async def test_monitor_precommit_autofix_retry_rejects_untracked_paths_outside_operation(
    tmp_path: Path,
) -> None:
    fixed_path = "src/awf/fixed.py"
    untracked_path = "docs/new-notes.md"
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout=f"?? {untracked_path}\nMM {fixed_path}\n")

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
async def test_monitor_precommit_autofix_retry_restages_modified_rename_destination(
    tmp_path: Path,
) -> None:
    old_path = "src/awf/old_name.py"
    new_path = "src/awf/new_name.py"
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout=f"RM {old_path} -> {new_path}\n")
    runner.queue_result(returncode=0)
    runner.queue_result(returncode=0)

    retry = await _retry_monitor_precommit_autofix_commit_once(
        runner=runner,
        workspace_id="ws_123",
        worktree_path=tmp_path,
        message="fix: monitor repair",
        commit_result=CommandResult(
            returncode=1,
            stdout="",
            stderr=_deterministic_autofix_stderr(new_path),
        ),
        operation_dirty_paths=(old_path, new_path),
    )

    assert retry is not None
    retry_result, restaged_paths = retry
    assert retry_result.ok
    assert restaged_paths == (new_path,)
    assert runner.calls[1].args[-3:] == ["add", "--", new_path]


@pytest.mark.unit
async def test_monitor_precommit_autofix_retry_restages_quoted_rename_destination_with_arrow_in_old_path(
    tmp_path: Path,
) -> None:
    old_path = "src/old -> backup.py"
    new_path = "src/new_name.py"
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout=f'RM "{old_path}" -> {new_path}\n')
    runner.queue_result(returncode=0)
    runner.queue_result(returncode=0)

    retry = await _retry_monitor_precommit_autofix_commit_once(
        runner=runner,
        workspace_id="ws_123",
        worktree_path=tmp_path,
        message="fix: monitor repair",
        commit_result=CommandResult(
            returncode=1,
            stdout="",
            stderr=_deterministic_autofix_stderr(new_path),
        ),
        operation_dirty_paths=(old_path, new_path),
    )

    assert retry is not None
    retry_result, restaged_paths = retry
    assert retry_result.ok
    assert restaged_paths == (new_path,)
    assert runner.calls[1].args[-3:] == ["add", "--", new_path]


@pytest.mark.unit
@pytest.mark.parametrize("porcelain_status", (" R", " C"), ids=("worktree-rename", "worktree-copy"))
async def test_monitor_precommit_autofix_retry_restages_worktree_column_rename_and_copy_destination(
    tmp_path: Path,
    porcelain_status: str,
) -> None:
    old_path = "src/awf/old_name.py"
    new_path = "src/awf/new_name.py"
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout=f"{porcelain_status} {old_path} -> {new_path}\n")
    runner.queue_result(returncode=0)
    runner.queue_result(returncode=0)

    retry = await _retry_monitor_precommit_autofix_commit_once(
        runner=runner,
        workspace_id="ws_123",
        worktree_path=tmp_path,
        message="fix: monitor repair",
        commit_result=CommandResult(
            returncode=1,
            stdout="",
            stderr=_deterministic_autofix_stderr(new_path),
        ),
        operation_dirty_paths=(old_path, new_path),
    )

    assert retry is not None
    retry_result, restaged_paths = retry
    assert retry_result.ok
    assert restaged_paths == (new_path,)
    assert runner.calls[1].args[-3:] == ["add", "--", new_path]


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
