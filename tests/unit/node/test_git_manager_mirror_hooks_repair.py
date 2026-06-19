"""Happy-path coverage for GitManager mirror hook repair."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from awf.node import git_manager as git_module


def _write_executable_hook(hooks_dir: Path, name: str = "pre-commit") -> None:
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / name
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)


def _git(args: list[str], cwd: Path) -> None:
    """Run a synchronous git command for fixture setup; fail loudly on error."""
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


class TestRepairMirrorHooksPath:
    @staticmethod
    def _mirror_with_attached_worktree(
        tmp_path: Path, *, create_hooks_dir: bool
    ) -> tuple[Path, Path]:
        repo = tmp_path / "origin"
        repo.mkdir()
        _git(["init", "-q", "-b", "main"], repo)
        _git(["config", "user.name", "AWF Test"], repo)
        _git(["config", "user.email", "awf@test.local"], repo)
        (repo / "README.md").write_text("initial\n", encoding="utf-8")
        _git(["add", "."], repo)
        _git(["commit", "-q", "-m", "init"], repo)

        mirror = tmp_path / "mirror.git"
        subprocess.run(
            ["git", "clone", "--bare", str(repo), str(mirror)],
            check=True,
            capture_output=True,
        )
        worktree = tmp_path / "workspace"
        subprocess.run(
            ["git", "--git-dir", str(mirror), "worktree", "add", str(worktree), "main"],
            check=True,
            capture_output=True,
        )
        if create_hooks_dir:
            _write_executable_hook(worktree / ".githooks" / "Lefthook")
        return mirror, worktree

    @pytest.mark.unit
    async def test_clears_poisoned_hooks_path(self, tmp_path: Path) -> None:
        mirror = tmp_path / "mirror.git"
        mirror.mkdir()
        subprocess.run(
            ["git", "init", "--bare", str(mirror)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "core.hooksPath", "/dev/null"],
            check=True,
            capture_output=True,
        )

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        check = subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "--local", "core.hooksPath"],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0

    @pytest.mark.unit
    async def test_clears_poisoned_worktree_local_hooks_path(self, tmp_path: Path) -> None:
        mirror, worktree = self._mirror_with_attached_worktree(tmp_path, create_hooks_dir=False)
        subprocess.run(
            ["git", "-C", str(worktree), "config", "extensions.worktreeConfig", "true"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(worktree), "config", "--worktree", "core.hooksPath", "/dev/null"],
            check=True,
            capture_output=True,
        )

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        check = subprocess.run(
            ["git", "-C", str(worktree), "config", "--worktree", "--get-all", "core.hooksPath"],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0
        assert check.stdout == ""

    @pytest.mark.unit
    async def test_clears_duplicate_poisoned_hooks_paths(self, tmp_path: Path) -> None:
        mirror = tmp_path / "mirror.git"
        mirror.mkdir()
        subprocess.run(
            ["git", "init", "--bare", str(mirror)],
            check=True,
            capture_output=True,
        )
        for hooks_path in ("/dev/null", "/tmp/awf-poisoned-hooks"):
            subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(mirror),
                    "config",
                    "--add",
                    "core.hooksPath",
                    hooks_path,
                ],
                check=True,
                capture_output=True,
            )

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        check = subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "--get-all", "core.hooksPath"],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0
        assert check.stdout == ""

    @pytest.mark.unit
    async def test_clears_agent_writable_allowed_hooks_path(self, tmp_path: Path) -> None:
        mirror, _worktree = self._mirror_with_attached_worktree(tmp_path, create_hooks_dir=True)
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(mirror),
                "config",
                "core.hooksPath",
                ".githooks/Lefthook",
            ],
            check=True,
            capture_output=True,
        )

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        check = subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "--get-all", "core.hooksPath"],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0
        assert check.stdout == ""

    @pytest.mark.unit
    async def test_clears_allowed_hooks_path_when_attached_worktree_lacks_directory(
        self, tmp_path: Path
    ) -> None:
        mirror, _worktree = self._mirror_with_attached_worktree(tmp_path, create_hooks_dir=False)
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(mirror),
                "config",
                "core.hooksPath",
                ".githooks/Lefthook",
            ],
            check=True,
            capture_output=True,
        )

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        check = subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "--get-all", "core.hooksPath"],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0
        assert check.stdout == ""

    @pytest.mark.unit
    async def test_clears_allowed_hooks_path_when_attached_worktree_hooks_directory_is_empty(
        self, tmp_path: Path
    ) -> None:
        mirror, worktree = self._mirror_with_attached_worktree(tmp_path, create_hooks_dir=False)
        (worktree / ".githooks" / "Lefthook").mkdir(parents=True)
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(mirror),
                "config",
                "core.hooksPath",
                ".githooks/Lefthook",
            ],
            check=True,
            capture_output=True,
        )

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        check = subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "--get-all", "core.hooksPath"],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0
        assert check.stdout == ""

    @pytest.mark.unit
    async def test_clears_allowed_hooks_path_when_attached_worktree_hook_is_not_executable(
        self, tmp_path: Path
    ) -> None:
        mirror, worktree = self._mirror_with_attached_worktree(tmp_path, create_hooks_dir=False)
        hooks_dir = worktree / ".githooks" / "Lefthook"
        hooks_dir.mkdir(parents=True)
        hook = hooks_dir / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook.chmod(0o644)
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(mirror),
                "config",
                "core.hooksPath",
                ".githooks/Lefthook",
            ],
            check=True,
            capture_output=True,
        )

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        check = subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "--get-all", "core.hooksPath"],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0
        assert check.stdout == ""

    @pytest.mark.unit
    async def test_clears_allowed_hooks_path_when_any_registered_worktree_lacks_directory(
        self, tmp_path: Path
    ) -> None:
        mirror, _worktree = self._mirror_with_attached_worktree(tmp_path, create_hooks_dir=True)
        missing_hooks_worktree = tmp_path / "workspace-missing-hooks"
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(mirror),
                "worktree",
                "add",
                "-b",
                "missing-hooks",
                str(missing_hooks_worktree),
                "main",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(mirror),
                "config",
                "core.hooksPath",
                ".githooks/Lefthook",
            ],
            check=True,
            capture_output=True,
        )

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        check = subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "--get-all", "core.hooksPath"],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0
        assert check.stdout == ""

    @pytest.mark.unit
    async def test_clears_allowed_hooks_path_without_registered_worktree(
        self, tmp_path: Path
    ) -> None:
        mirror = tmp_path / "mirror.git"
        mirror.mkdir()
        subprocess.run(
            ["git", "init", "--bare", str(mirror)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(mirror),
                "config",
                "core.hooksPath",
                ".githooks/Lefthook",
            ],
            check=True,
            capture_output=True,
        )

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        check = subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "--get-all", "core.hooksPath"],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0
        assert check.stdout == ""

    @pytest.mark.unit
    async def test_clears_unrecognized_absolute_hooks_path(self, tmp_path: Path) -> None:
        mirror = tmp_path / "mirror.git"
        mirror.mkdir()
        subprocess.run(
            ["git", "init", "--bare", str(mirror)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(mirror),
                "config",
                "core.hooksPath",
                "/tmp/empty-hooks",
            ],
            check=True,
            capture_output=True,
        )

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        check = subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "--get-all", "core.hooksPath"],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0
        assert check.stdout == ""

    @pytest.mark.unit
    async def test_clears_unrecognized_relative_hooks_path(self, tmp_path: Path) -> None:
        mirror = tmp_path / "mirror.git"
        mirror.mkdir()
        subprocess.run(
            ["git", "init", "--bare", str(mirror)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(mirror),
                "config",
                "core.hooksPath",
                "no-such-hooks",
            ],
            check=True,
            capture_output=True,
        )

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        check = subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "--get-all", "core.hooksPath"],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0
        assert check.stdout == ""

    @pytest.mark.unit
    async def test_removes_poisoned_hooks_path_and_agent_writable_hooks_path(
        self, tmp_path: Path
    ) -> None:
        mirror, _worktree = self._mirror_with_attached_worktree(tmp_path, create_hooks_dir=True)
        for hooks_path in (".githooks/Lefthook", "/dev/null"):
            subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(mirror),
                    "config",
                    "--add",
                    "core.hooksPath",
                    hooks_path,
                ],
                check=True,
                capture_output=True,
            )

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        check = subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "--get-all", "core.hooksPath"],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0
        assert check.stdout == ""

    @pytest.mark.unit
    async def test_treats_concurrent_hooks_path_cleanup_as_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mirror = tmp_path / "mirror.git"
        mirror.mkdir()
        subprocess.run(
            ["git", "init", "--bare", str(mirror)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "core.hooksPath", "/dev/null"],
            check=True,
            capture_output=True,
        )

        original_exec = asyncio.create_subprocess_exec
        unset_calls = 0

        async def _fake_exec(*args: object, **kwargs: object) -> object:
            nonlocal unset_calls
            if "--unset-all" in args and "core.hooksPath" in args:
                unset_calls += 1
                concurrent_unset = await original_exec(*args, **kwargs)
                await concurrent_unset.communicate()
            return await original_exec(*args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        assert unset_calls == 1
        check = subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "--local", "core.hooksPath"],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0

    @pytest.mark.unit
    async def test_ignores_git_object_lookup_envs_for_config_repair(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mirror = tmp_path / "mirror.git"
        mirror.mkdir()
        subprocess.run(
            ["git", "init", "--bare", str(mirror)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "core.hooksPath", "/dev/null"],
            check=True,
            capture_output=True,
        )
        monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "")
        monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "")

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        check = subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "--local", "core.hooksPath"],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0

    @pytest.mark.unit
    async def test_noop_when_hooks_path_not_set(self, tmp_path: Path) -> None:
        mirror = tmp_path / "mirror.git"
        mirror.mkdir()
        subprocess.run(
            ["git", "init", "--bare", str(mirror)],
            check=True,
            capture_output=True,
        )

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is False
