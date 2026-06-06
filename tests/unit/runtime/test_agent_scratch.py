"""Tests for git-native agent-runtime scratch exclusion."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf.adapters.claude_code import ClaudeCodeAdapter
from awf.adapters.codex import CodexAdapter
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.runtime.agent_scratch import (
    AWF_SCRATCH_BLOCK_END,
    AWF_SCRATCH_BLOCK_START,
    apply_agent_scratch_excludes,
)
from awf.runtime.validation_worktree import check_validation_worktree_clean

_SCRATCH = (".claude/worktrees/",)


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a real git command in a temporary worktree."""
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_real_worktree(tmp_path: Path) -> Path:
    """Create a real git repo with a single tracked commit."""
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    _git(worktree, "init", "-q")
    _git(worktree, "config", "user.email", "awf@example.com")
    _git(worktree, "config", "user.name", "AWF Test")
    (worktree / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    _git(worktree, "add", "tracked.py")
    _git(worktree, "commit", "-qm", "initial")
    return worktree


def _real_run_git(worktree: Path):
    """Return an async ``run_git`` bound to a real worktree."""

    async def run_git(args: list[str]) -> CommandResult:
        proc = subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            text=True,
        )
        return CommandResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    return run_git


def _exclude_file(worktree: Path) -> Path:
    """Resolve the real ``info/exclude`` path for a worktree."""
    out = _git(worktree, "rev-parse", "--git-path", "info/exclude").stdout.strip()
    path = Path(out)
    return path if path.is_absolute() else worktree / path


@pytest.mark.unit
async def test_guard_passes_once_scratch_exclude_applied(tmp_path: Path) -> None:
    """An applied scratch exclude makes the guard treat the scratch as ignored."""
    worktree = _init_real_worktree(tmp_path)
    scratch = worktree / ".claude" / "worktrees" / "sub"
    scratch.mkdir(parents=True)
    (scratch / "file.txt").write_text("scratch\n", encoding="utf-8")
    run_git = _real_run_git(worktree)

    before = await check_validation_worktree_clean(
        run_git=run_git, worktree_path=worktree, ignore_all_ignored=True
    )
    assert before.clean is False
    assert any(".claude/worktrees" in path for path in before.untracked_paths)

    applied = await apply_agent_scratch_excludes(
        run_git=run_git, worktree_path=worktree, scratch_paths=_SCRATCH
    )
    assert applied is True

    after = await check_validation_worktree_clean(
        run_git=run_git, worktree_path=worktree, ignore_all_ignored=True
    )
    assert after.clean is True
    assert any(".claude/worktrees" in path for path in after.ignored_paths)


@pytest.mark.unit
async def test_genuine_untracked_file_still_trips_guard(tmp_path: Path) -> None:
    """A real stray project file is not hidden by the scratch exclude."""
    worktree = _init_real_worktree(tmp_path)
    scratch = worktree / ".claude" / "worktrees" / "sub"
    scratch.mkdir(parents=True)
    (scratch / "file.txt").write_text("scratch\n", encoding="utf-8")
    run_git = _real_run_git(worktree)
    await apply_agent_scratch_excludes(
        run_git=run_git, worktree_path=worktree, scratch_paths=_SCRATCH
    )

    (worktree / "stray_module.py").write_text("print('x')\n", encoding="utf-8")

    check = await check_validation_worktree_clean(
        run_git=run_git, worktree_path=worktree, ignore_all_ignored=True
    )
    assert check.clean is False
    assert "stray_module.py" in check.untracked_paths


@pytest.mark.unit
async def test_exclude_write_is_idempotent(tmp_path: Path) -> None:
    """Repeated applies leave exactly one AWF block and preserve other lines."""
    worktree = _init_real_worktree(tmp_path)
    exclude = _exclude_file(worktree)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("# pre-existing user exclude\n*.tmp\n", encoding="utf-8")
    run_git = _real_run_git(worktree)

    assert await apply_agent_scratch_excludes(
        run_git=run_git, worktree_path=worktree, scratch_paths=_SCRATCH
    )
    assert await apply_agent_scratch_excludes(
        run_git=run_git, worktree_path=worktree, scratch_paths=_SCRATCH
    )

    content = exclude.read_text(encoding="utf-8")
    assert content.count(AWF_SCRATCH_BLOCK_START) == 1
    assert content.count(AWF_SCRATCH_BLOCK_END) == 1
    assert content.count(".claude/worktrees/") == 1
    # Pre-existing non-AWF lines survive the rewrite.
    assert "# pre-existing user exclude" in content
    assert "*.tmp" in content


@pytest.mark.unit
async def test_apply_unions_with_another_adapters_scratch_paths(tmp_path: Path) -> None:
    """A second adapter's apply preserves a prior adapter's scratch patterns.

    ``info/exclude`` is shared across linked worktrees of a mirror, so a second
    agent type with different scratch paths must not clobber the first agent's
    exclusions (last-writer-wins). The two blocks are unioned instead.
    """
    worktree = _init_real_worktree(tmp_path)
    run_git = _real_run_git(worktree)
    exclude = _exclude_file(worktree)

    assert await apply_agent_scratch_excludes(
        run_git=run_git, worktree_path=worktree, scratch_paths=_SCRATCH
    )
    # A different adapter declaring a different scratch path applies next.
    assert await apply_agent_scratch_excludes(
        run_git=run_git, worktree_path=worktree, scratch_paths=(".other/scratch/",)
    )

    content = exclude.read_text(encoding="utf-8")
    # Exactly one managed block, but it now carries both adapters' patterns.
    assert content.count(AWF_SCRATCH_BLOCK_START) == 1
    assert content.count(AWF_SCRATCH_BLOCK_END) == 1
    assert ".claude/worktrees/" in content
    assert ".other/scratch/" in content
    # Re-applying the first adapter keeps the union and stays idempotent.
    assert await apply_agent_scratch_excludes(
        run_git=run_git, worktree_path=worktree, scratch_paths=_SCRATCH
    )
    content = exclude.read_text(encoding="utf-8")
    assert content.count(".claude/worktrees/") == 1
    assert content.count(".other/scratch/") == 1


@pytest.mark.unit
def test_claude_code_declares_scratch_codex_does_not() -> None:
    """Scratch paths are sourced per-agent from the adapter."""
    runner = FakeCommandRunner()
    assert ClaudeCodeAdapter(runner=runner).runtime_scratch_paths == _SCRATCH
    assert CodexAdapter(runner=runner).runtime_scratch_paths == ()


@pytest.mark.unit
async def test_empty_scratch_paths_is_noop(tmp_path: Path) -> None:
    """No scratch paths means no git call and no exclude file written."""
    worktree = _init_real_worktree(tmp_path)
    exclude = _exclude_file(worktree)
    calls: list[list[str]] = []

    async def run_git(args: list[str]) -> CommandResult:
        calls.append(args)
        return CommandResult(returncode=0, stdout="", stderr="")

    applied = await apply_agent_scratch_excludes(
        run_git=run_git, worktree_path=worktree, scratch_paths=()
    )

    assert applied is False
    assert calls == []
    assert not exclude.exists() or AWF_SCRATCH_BLOCK_START not in exclude.read_text(
        encoding="utf-8"
    )


@pytest.mark.unit
async def test_rev_parse_failure_is_handled_gracefully(tmp_path: Path) -> None:
    """A failed rev-parse returns False and writes nothing."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    wrote: list[list[str]] = []

    async def run_git(args: list[str]) -> CommandResult:
        if args == ["rev-parse", "--git-path", "info/exclude"]:
            return CommandResult(returncode=128, stdout="", stderr="not a git repo")
        wrote.append(args)
        return CommandResult(returncode=0, stdout="", stderr="")

    applied = await apply_agent_scratch_excludes(
        run_git=run_git, worktree_path=worktree, scratch_paths=_SCRATCH
    )

    assert applied is False
    assert wrote == []


@pytest.mark.unit
async def test_empty_rev_parse_output_is_handled_gracefully(tmp_path: Path) -> None:
    """A blank rev-parse path returns False without raising."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    async def run_git(args: list[str]) -> CommandResult:
        return CommandResult(returncode=0, stdout="\n", stderr="")

    applied = await apply_agent_scratch_excludes(
        run_git=run_git, worktree_path=worktree, scratch_paths=_SCRATCH
    )

    assert applied is False


@pytest.mark.unit
async def test_absolute_rev_parse_path_is_written_in_place(tmp_path: Path) -> None:
    """An absolute info/exclude path (linked worktree) is used verbatim."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    exclude = tmp_path / "common" / "info" / "exclude"
    exclude.parent.mkdir(parents=True)

    async def run_git(args: list[str]) -> CommandResult:
        assert args == ["rev-parse", "--git-path", "info/exclude"]
        return CommandResult(returncode=0, stdout=f"{exclude}\n", stderr="")

    applied = await apply_agent_scratch_excludes(
        run_git=run_git, worktree_path=worktree, scratch_paths=_SCRATCH
    )

    assert applied is True
    content = exclude.read_text(encoding="utf-8")
    assert AWF_SCRATCH_BLOCK_START in content
    assert ".claude/worktrees/" in content
    # The path was absolute, so nothing was written under the worktree itself.
    assert not (worktree / "info").exists()


@pytest.mark.unit
async def test_missing_info_dir_is_created_before_write(tmp_path: Path) -> None:
    """A missing info/ parent dir is created so the exclude is still applied."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    # Parent ``info/`` deliberately does not exist (minimal/custom git env).
    exclude = tmp_path / "common" / "info" / "exclude"
    assert not exclude.parent.exists()

    async def run_git(args: list[str]) -> CommandResult:
        assert args == ["rev-parse", "--git-path", "info/exclude"]
        return CommandResult(returncode=0, stdout=f"{exclude}\n", stderr="")

    applied = await apply_agent_scratch_excludes(
        run_git=run_git, worktree_path=worktree, scratch_paths=_SCRATCH
    )

    assert applied is True
    content = exclude.read_text(encoding="utf-8")
    assert AWF_SCRATCH_BLOCK_START in content
    assert ".claude/worktrees/" in content


@pytest.mark.unit
async def test_exclude_write_oserror_is_handled_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unwritable exclude file returns False instead of raising."""
    worktree = _init_real_worktree(tmp_path)
    run_git = _real_run_git(worktree)

    def _boom(self: Path, *args: object, **kwargs: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", _boom)

    applied = await apply_agent_scratch_excludes(
        run_git=run_git, worktree_path=worktree, scratch_paths=_SCRATCH
    )

    assert applied is False


@pytest.mark.unit
async def test_non_utf8_exclude_is_handled_gracefully(tmp_path: Path) -> None:
    """A non-UTF-8 exclude file degrades to False instead of raising.

    ``read_text(encoding="utf-8")`` raises ``UnicodeDecodeError`` (a
    ``ValueError`` subclass, not ``OSError``) on binary content; the function's
    graceful-degradation contract must still hold.
    """
    worktree = _init_real_worktree(tmp_path)
    run_git = _real_run_git(worktree)
    exclude = _exclude_file(worktree)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")

    applied = await apply_agent_scratch_excludes(
        run_git=run_git, worktree_path=worktree, scratch_paths=_SCRATCH
    )

    assert applied is False
