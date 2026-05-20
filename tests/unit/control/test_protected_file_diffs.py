"""Tests for shared protected-file diff loaders."""

from __future__ import annotations

import pytest

from awf.common.commands import FakeCommandRunner
from awf.control.protected_file_diffs import (
    git_show_text,
    protected_file_diffs_for_committed_paths,
)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_protected_file_diffs_for_committed_paths_loads_only_classified_paths(
    tmp_path,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout='[project]\nname = "demo"\n')
    runner.queue_result(returncode=0, stdout='[project]\nname = "demo2"\n')

    diffs = await protected_file_diffs_for_committed_paths(
        runner,
        worktree_path=tmp_path,
        base_ref="origin/main",
        changed_paths=["src/awf/control/executor.py", "pyproject.toml"],
    )

    assert set(diffs) == {"pyproject.toml"}
    assert diffs["pyproject.toml"].old_text == '[project]\nname = "demo"\n'
    assert diffs["pyproject.toml"].new_text == '[project]\nname = "demo2"\n'
    assert [call.args[-2:] for call in runner.calls] == [
        ["show", "origin/main:pyproject.toml"],
        ["show", "HEAD:pyproject.toml"],
    ]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_git_show_text_returns_none_for_missing_path(tmp_path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(
        returncode=128,
        stderr="fatal: path 'pyproject.toml' does not exist in 'HEAD'",
    )

    assert (
        await git_show_text(
            runner,
            worktree_path=tmp_path,
            refspec="HEAD:pyproject.toml",
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_git_show_text_raises_for_unexpected_git_error(tmp_path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=128, stderr="fatal: bad object HEAD")

    with pytest.raises(RuntimeError, match="git show failed"):
        await git_show_text(
            runner,
            worktree_path=tmp_path,
            refspec="HEAD:pyproject.toml",
        )
