"""Tests for shared protected-file diff loaders."""

from __future__ import annotations

import pytest

from awf.common.commands import FakeCommandRunner
from awf.control.protected_file_diffs import (
    changed_paths_from_name_status_z,
    committed_changed_paths_since,
    git_show_text,
    protected_file_diffs_for_committed_paths,
)


@pytest.mark.unit
def test_changed_paths_from_name_status_z_accepts_empty_output() -> None:
    assert changed_paths_from_name_status_z("") == ()


@pytest.mark.unit
def test_changed_paths_from_name_status_z_parses_renames_copies_and_deduplicates() -> None:
    paths = changed_paths_from_name_status_z(
        "M\0pyproject.toml\0"
        "R100\0.github/workflows/old.yml\0.github/workflows/ci.yml\0"
        "C75\0pyproject.toml\0pyproject-copy.toml\0"
    )

    assert paths == (
        "pyproject.toml",
        ".github/workflows/old.yml",
        ".github/workflows/ci.yml",
        "pyproject-copy.toml",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        ("M\tpyproject.toml\n", "expected NUL-delimited output"),
        ("M\0pyproject.toml", "missing terminating NUL"),
        ("\0pyproject.toml\0", "empty status field"),
        ("R100\0.github/workflows/old.yml\0", "truncated"),
        ("M\0\0", "malformed"),
    ],
)
def test_changed_paths_from_name_status_z_rejects_malformed_output(
    stdout: str,
    message: str,
) -> None:
    with pytest.raises(ValueError) as excinfo:
        changed_paths_from_name_status_z(stdout)

    assert message in str(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_committed_changed_paths_since_parses_git_name_status(tmp_path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="M\0pyproject.toml\0")

    paths = await committed_changed_paths_since(
        runner,
        worktree_path=tmp_path,
        base_ref="origin/development",
    )

    assert paths == ("pyproject.toml",)
    assert runner.calls[0].args[-5:] == [
        "diff",
        "--name-status",
        "-z",
        "origin/development..HEAD",
        "--",
    ]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_committed_changed_paths_since_raises_on_git_failure(tmp_path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=128, stderr="fatal: bad revision")

    with pytest.raises(RuntimeError) as excinfo:
        await committed_changed_paths_since(
            runner,
            worktree_path=tmp_path,
            base_ref="origin/development",
        )

    assert "git diff --name-status -z failed" in str(excinfo.value)
    assert "bad revision" in str(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_committed_changed_paths_since_wraps_malformed_git_output(tmp_path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout="M\0pyproject.toml")

    with pytest.raises(RuntimeError) as excinfo:
        await committed_changed_paths_since(
            runner,
            worktree_path=tmp_path,
            base_ref="origin/development",
        )

    assert "malformed committed-path output" in str(excinfo.value)


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
@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: path '.github/workflows/ci.yml' does not exist in 'HEAD'",
        "fatal: Path '.github/workflows/ci.yml' exists on disk, but not in 'HEAD'",
        (
            "fatal: path '.github/workflows/ci.yml' does not exist "
            "(neither on disk nor in the index)"
        ),
        "fatal: path missing",
    ],
)
async def test_git_show_text_returns_none_for_missing_path(
    tmp_path,
    stderr: str,
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=128, stderr=stderr)

    assert (
        await git_show_text(
            runner,
            worktree_path=tmp_path,
            refspec="HEAD:.github/workflows/ci.yml",
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_git_show_text_raises_for_unexpected_git_error(tmp_path) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=128, stderr="fatal: bad revision 'bad-ref:pyproject.toml'")

    with pytest.raises(RuntimeError) as excinfo:
        await git_show_text(
            runner,
            worktree_path=tmp_path,
            refspec="bad-ref:pyproject.toml",
        )

    message = str(excinfo.value)
    assert "git show failed" in message
    assert "bad-ref:pyproject.toml" in message
    assert str(tmp_path) in message
    assert "bad revision" in message
