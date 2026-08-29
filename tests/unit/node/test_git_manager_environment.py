"""GitManager environment and writable-worktree helper tests."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from awf.node import git_manager as git_module
from awf.node.git_manager import GitManager


class TestGitEnvironment:
    """Tests for GitEnvironment."""

    @pytest.mark.unit
    async def test_run_uses_configured_environment(self, tmp_path: Path) -> None:
        """Verify run uses configured environment."""
        home = tmp_path / "home"
        home.mkdir()
        manager = GitManager(tmp_path / "work", env={"HOME": str(home), "AWF_TEST_ENV": "ok"})

        result = await manager._run(  # noqa: SLF001 - narrow regression for subprocess env.
            ["sh", "-c", 'printf \'%s:%s\' "$HOME" "$AWF_TEST_ENV"'],
            operation="env",
        )
        replacement_home = tmp_path / "replacement-home"
        replacement_home.mkdir()
        manager.replace_env({"HOME": str(replacement_home), "AWF_TEST_ENV": "refreshed"})
        refreshed = await manager._run(  # noqa: SLF001 - same environment seam.
            ["sh", "-c", 'printf \'%s:%s\' "$HOME" "$AWF_TEST_ENV"'],
            operation="refreshed-env",
        )

        assert result.stdout == f"{home}:ok"
        assert refreshed.stdout == f"{replacement_home}:refreshed"

    @pytest.mark.unit
    async def test_task_environment_survives_shared_replacement(self, tmp_path: Path) -> None:
        """An in-flight task keeps its pinned environment after a shared refresh."""
        manager = GitManager(tmp_path / "work", env={"AWF_TEST_ENV": "initial"})
        task_bound = asyncio.Event()
        shared_replaced = asyncio.Event()

        async def run_with_pinned_environment() -> str:
            token = manager.set_task_env({"AWF_TEST_ENV": "pinned"})
            try:
                task_bound.set()
                await shared_replaced.wait()
                result = await manager._run(  # noqa: SLF001 - environment regression seam.
                    ["sh", "-c", "printf '%s' \"$AWF_TEST_ENV\""],
                    operation="pinned-env",
                )
                return result.stdout
            finally:
                manager.reset_task_env(token)

        pinned_task = asyncio.create_task(run_with_pinned_environment())
        await asyncio.wait_for(task_bound.wait(), timeout=1)
        manager.replace_env({"AWF_TEST_ENV": "replacement"})
        shared_replaced.set()

        assert await pinned_task == "pinned"
        replacement = await manager._run(  # noqa: SLF001 - environment regression seam.
            ["sh", "-c", "printf '%s' \"$AWF_TEST_ENV\""],
            operation="replacement-env",
        )
        assert replacement.stdout == "replacement"


class TestAgentWritableWorktreeHelpers:
    """Tests for AgentWritableWorktreeHelpers."""

    @pytest.mark.unit
    async def test_prepare_agent_writable_worktree_skips_chown_when_not_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify prepare agent writable worktree skips chown when not root."""
        monkeypatch.setattr(os, "geteuid", lambda: 1000)

        async def _unexpected_to_thread(*_args: object, **_kwargs: object) -> None:
            """Test helper for unexpected to thread."""
            raise AssertionError("non-root process must not chown worktrees")

        monkeypatch.setattr(git_module.asyncio, "to_thread", _unexpected_to_thread)
        manager = GitManager(
            tmp_path / "awf-work",
            worktree_owner_uid=1000,
            worktree_owner_gid=1000,
        )

        await manager._prepare_agent_writable_worktree(  # noqa: SLF001
            layout_mirror=tmp_path / "mirror.git",
            worktree_path=tmp_path / "worktree",
        )

    @pytest.mark.unit
    def test_linked_worktree_git_dir_handles_absent_unreadable_and_relative_gitfiles(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify linked worktree git dir handles absent unreadable and relative gitfiles."""
        missing_gitfile = tmp_path / "missing"
        missing_gitfile.mkdir()
        assert git_module.linked_worktree_git_dir(missing_gitfile) is None

        unreadable = tmp_path / "unreadable"
        unreadable.mkdir()
        unreadable_gitfile = unreadable / ".git"
        unreadable_gitfile.write_text("gitdir: ../real.git\n", encoding="utf-8")
        original_read_text = Path.read_text

        def _read_text(path: Path, *args: object, **kwargs: object) -> str:
            """Test helper for read text."""
            if path == unreadable_gitfile:
                raise OSError("cannot read gitfile")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _read_text)
        with pytest.raises(git_module.GitOperationError) as raised:
            git_module.linked_worktree_git_dir(unreadable)
        assert raised.value.operation == "worktree.gitfile_probe"
        assert "cannot access worktree .git metadata" in raised.value.stderr

        malformed = tmp_path / "malformed"
        malformed.mkdir()
        (malformed / ".git").write_text("not a gitdir pointer\n", encoding="utf-8")
        assert git_module.linked_worktree_git_dir(malformed) is None

        relative = tmp_path / "relative"
        relative.mkdir()
        (relative / ".git").write_text(
            "gitdir: ../mirror.git/worktrees/ws_relative\n",
            encoding="utf-8",
        )

        assert (
            git_module.linked_worktree_git_dir(relative)
            == (relative / "../mirror.git/worktrees/ws_relative").resolve()
        )

    @pytest.mark.unit
    def test_chown_targets_skip_missing_and_duplicate_paths(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify chown targets skip missing and duplicate paths."""
        chowned: list[tuple[Path, int, int]] = []
        file_path = tmp_path / "owned-file"
        file_path.write_text("content\n", encoding="utf-8")
        missing_path = tmp_path / "missing"

        monkeypatch.setattr(
            os,
            "chown",
            lambda path, uid, gid: chowned.append((Path(path), uid, gid)),
        )

        git_module._chown_targets(  # noqa: SLF001
            (
                git_module._ChownTarget(file_path, recursive=True),  # noqa: SLF001
                git_module._ChownTarget(file_path, recursive=True),  # noqa: SLF001
                git_module._ChownTarget(missing_path, recursive=False),  # noqa: SLF001
            ),
            1000,
            1001,
        )

        assert chowned == [(file_path, 1000, 1001)]
