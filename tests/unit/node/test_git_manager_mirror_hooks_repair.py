"""Happy-path coverage for GitManager mirror hook repair."""

from __future__ import annotations

import asyncio
import shutil
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
    async def test_repair_waits_for_shared_mirror_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mirror = tmp_path / "mirror.git"
        mirror.mkdir()
        subprocess.run(
            ["git", "init", "--bare", str(mirror)],
            check=True,
            capture_output=True,
        )
        started = False

        async def _repair_hooks_path_config(**_kwargs: object) -> bool:
            nonlocal started
            started = True
            return False

        monkeypatch.setattr(git_module, "_repair_hooks_path_config", _repair_hooks_path_config)
        lock = git_module.GitManager._lock_for_mirror(mirror)  # noqa: SLF001
        await lock.acquire()
        task = asyncio.create_task(git_module.repair_mirror_hooks_path(mirror))
        try:
            await asyncio.sleep(0)
            assert started is False
            assert task.done() is False
        finally:
            lock.release()

        assert await task is False
        assert started is True

    @pytest.mark.unit
    async def test_prunes_and_retries_when_linked_worktree_metadata_disappears(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mirror = tmp_path / "mirror.git"
        mirror.mkdir()
        subprocess.run(
            ["git", "init", "--bare", str(mirror)],
            check=True,
            capture_output=True,
        )
        linked_git_dir = mirror / "worktrees" / "workspace"
        linked_git_dir.mkdir(parents=True)
        repair_calls = 0
        prune_calls = 0

        async def _repair_hooks_path_config(**_kwargs: object) -> bool:
            nonlocal repair_calls
            repair_calls += 1
            return False

        async def _run_git_worktree_prune(path: Path) -> None:
            nonlocal prune_calls
            prune_calls += 1
            assert path == mirror
            # Real ``git worktree prune`` removes the dead linked-worktree metadata,
            # so the retry pass no longer reports it as stale.
            shutil.rmtree(linked_git_dir)

        monkeypatch.setattr(git_module, "_repair_hooks_path_config", _repair_hooks_path_config)
        monkeypatch.setattr(git_module, "_run_git_worktree_prune", _run_git_worktree_prune)

        assert await git_module.repair_mirror_hooks_path(mirror) is True
        assert prune_calls == 1
        assert repair_calls == 2

    @pytest.mark.unit
    async def test_fails_closed_when_stale_metadata_survives_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mirror = tmp_path / "mirror.git"
        mirror.mkdir()
        subprocess.run(
            ["git", "init", "--bare", str(mirror)],
            check=True,
            capture_output=True,
        )
        linked_git_dir = mirror / "worktrees" / "workspace"
        linked_git_dir.mkdir(parents=True)
        prune_calls = 0

        async def _repair_hooks_path_config(**_kwargs: object) -> bool:
            return False

        async def _run_git_worktree_prune(path: Path) -> None:
            nonlocal prune_calls
            prune_calls += 1
            # Prune cannot clear metadata that stays unreadable (e.g. a
            # permission-denied gitdir back-reference of a live worktree).

        monkeypatch.setattr(git_module, "_repair_hooks_path_config", _repair_hooks_path_config)
        monkeypatch.setattr(git_module, "_run_git_worktree_prune", _run_git_worktree_prune)

        with pytest.raises(git_module.GitOperationError) as raised:
            await git_module.repair_mirror_hooks_path(mirror)

        assert prune_calls == 1
        assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
        assert raised.value.operation == "mirror.worktree_metadata_stale"

    @pytest.mark.unit
    async def test_removes_include_exposing_poisoned_hooks_path(self, tmp_path: Path) -> None:
        mirror = tmp_path / "mirror.git"
        mirror.mkdir()
        subprocess.run(
            ["git", "init", "--bare", str(mirror)],
            check=True,
            capture_output=True,
        )
        included_config = tmp_path / "included-hooks.conf"
        included_config.write_text("[core]\n\thooksPath = /dev/null\n", encoding="utf-8")
        subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "include.path", str(included_config)],
            check=True,
            capture_output=True,
        )

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        check = subprocess.run(
            [
                "git",
                "--git-dir",
                str(mirror),
                "config",
                "--local",
                "--includes",
                "--get-all",
                "core.hooksPath",
            ],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0
        assert check.stdout == ""
        include_check = subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "--local", "--get-all", "include.path"],
            capture_output=True,
            text=True,
        )
        assert include_check.returncode != 0

    @pytest.mark.unit
    async def test_tolerates_concurrent_include_repair(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mirror = tmp_path / "mirror.git"
        mirror.mkdir()
        subprocess.run(
            ["git", "init", "--bare", str(mirror)],
            check=True,
            capture_output=True,
        )
        included_config = tmp_path / "included-hooks.conf"
        included_config.write_text("[core]\n\thooksPath = /dev/null\n", encoding="utf-8")
        subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "include.path", str(included_config)],
            check=True,
            capture_output=True,
        )
        original_unset = git_module._unset_matching_include_path

        async def _concurrent_unset(
            *,
            git_args: tuple[str, ...],
            config_scope_args: tuple[str, ...],
            config_path: Path,
            included_origin: Path,
            operation_prefix: str,
        ) -> bool:
            removed = await original_unset(
                git_args=git_args,
                config_scope_args=config_scope_args,
                config_path=config_path,
                included_origin=included_origin,
                operation_prefix=operation_prefix,
            )
            assert removed is True
            return False

        monkeypatch.setattr(git_module, "_unset_matching_include_path", _concurrent_unset)

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        check = subprocess.run(
            [
                "git",
                "--git-dir",
                str(mirror),
                "config",
                "--local",
                "--includes",
                "--get-all",
                "core.hooksPath",
            ],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0
        assert check.stdout == ""
        include_check = subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "--local", "--get-all", "include.path"],
            capture_output=True,
            text=True,
        )
        assert include_check.returncode != 0

    @pytest.mark.unit
    async def test_removes_include_with_multiple_hooks_paths_from_same_included_origin(
        self, tmp_path: Path
    ) -> None:
        mirror = tmp_path / "mirror.git"
        mirror.mkdir()
        subprocess.run(
            ["git", "init", "--bare", str(mirror)],
            check=True,
            capture_output=True,
        )
        included_config = tmp_path / "included-hooks.conf"
        included_config.write_text(
            "[core]\n\thooksPath = /dev/null\n\thooksPath = /tmp/awf-poisoned-hooks\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "include.path", str(included_config)],
            check=True,
            capture_output=True,
        )

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        check = subprocess.run(
            [
                "git",
                "--git-dir",
                str(mirror),
                "config",
                "--local",
                "--includes",
                "--get-all",
                "core.hooksPath",
            ],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0
        assert check.stdout == ""
        include_check = subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "--local", "--get-all", "include.path"],
            capture_output=True,
            text=True,
        )
        assert include_check.returncode != 0

    @pytest.mark.unit
    async def test_removes_mirror_gitdir_include_exposed_from_worktree_context(
        self, tmp_path: Path
    ) -> None:
        mirror, worktree = self._mirror_with_attached_worktree(tmp_path, create_hooks_dir=False)
        included_config = tmp_path / "mirror-gitdir-included-hooks.conf"
        included_config.write_text("[core]\n\thooksPath = /dev/null\n", encoding="utf-8")
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(mirror),
                "config",
                "includeIf.gitdir:**/worktrees/**.path",
                str(included_config),
            ],
            check=True,
            capture_output=True,
        )

        mirror_probe = subprocess.run(
            [
                "git",
                "--git-dir",
                str(mirror),
                "config",
                "--local",
                "--includes",
                "--get-all",
                "core.hooksPath",
            ],
            capture_output=True,
            text=True,
        )
        assert mirror_probe.returncode != 0
        context_probe = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "config",
                "--local",
                "--includes",
                "--get-all",
                "core.hooksPath",
            ],
            capture_output=True,
            text=True,
        )
        assert context_probe.returncode == 0
        assert context_probe.stdout == "/dev/null\n"

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        check = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "config",
                "--local",
                "--includes",
                "--get-all",
                "core.hooksPath",
            ],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0
        assert check.stdout == ""
        include_check = subprocess.run(
            [
                "git",
                "--git-dir",
                str(mirror),
                "config",
                "--local",
                "--get-regexp",
                r"^includeIf\..*\.path$",
            ],
            capture_output=True,
            text=True,
        )
        assert include_check.returncode != 0

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
    async def test_ignores_config_worktree_when_worktree_config_extension_is_disabled(
        self, tmp_path: Path
    ) -> None:
        mirror, worktree = self._mirror_with_attached_worktree(tmp_path, create_hooks_dir=False)
        linked_git_dir = git_module.linked_worktree_git_dir(worktree)
        assert linked_git_dir is not None
        config_worktree = linked_git_dir / "config.worktree"
        config_worktree.write_text("[core]\n\thooksPath = /dev/null\n", encoding="utf-8")

        extension_check = subprocess.run(
            ["git", "-C", str(worktree), "config", "--local", "--get", "extensions.worktreeConfig"],
            capture_output=True,
            text=True,
        )
        assert extension_check.returncode != 0

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is False
        assert config_worktree.read_text(encoding="utf-8") == "[core]\n\thooksPath = /dev/null\n"

    @pytest.mark.unit
    async def test_linked_worktree_config_probes_include_safe_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mirror = tmp_path / "mirror.git"
        linked_git_dir = mirror / "worktrees" / "workspace"
        linked_git_dir.mkdir(parents=True)
        (mirror / "config").write_text("[extensions]\n\tworktreeConfig = true\n", encoding="utf-8")
        worktree = tmp_path / "workspace"
        worktree.mkdir()
        (linked_git_dir / "gitdir").write_text(str(worktree / ".git"), encoding="utf-8")
        (linked_git_dir / "config.worktree").write_text("", encoding="utf-8")
        calls: list[tuple[tuple[str, ...], tuple[str, ...], Path, str]] = []

        async def _repair_hooks_path_config(
            *,
            git_args: tuple[str, ...],
            config_scope_args: tuple[str, ...],
            config_path: Path,
            operation_prefix: str,
        ) -> bool:
            calls.append((git_args, config_scope_args, config_path, operation_prefix))
            return False

        monkeypatch.setattr(git_module, "_repair_hooks_path_config", _repair_hooks_path_config)

        result = await git_module.repair_mirror_hooks_path(mirror)

        safe_args = tuple(git_module.git_safe_directory_config_args(worktree))
        assert result is False
        assert calls == [
            (("--git-dir", str(mirror)), ("--local",), mirror / "config", "mirror"),
            (
                (*safe_args, "-C", str(worktree)),
                ("--local",),
                mirror / "config",
                "mirror",
            ),
            (
                (*safe_args, "-C", str(worktree)),
                ("--worktree",),
                linked_git_dir / "config.worktree",
                "worktree",
            ),
        ]

    @pytest.mark.unit
    async def test_removes_worktree_include_exposing_poisoned_hooks_path(
        self, tmp_path: Path
    ) -> None:
        mirror, worktree = self._mirror_with_attached_worktree(tmp_path, create_hooks_dir=False)
        subprocess.run(
            ["git", "-C", str(worktree), "config", "extensions.worktreeConfig", "true"],
            check=True,
            capture_output=True,
        )
        included_config = tmp_path / "worktree-included-hooks.conf"
        included_config.write_text("[core]\n\thooksPath = /dev/null\n", encoding="utf-8")
        subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "config",
                "--worktree",
                "include.path",
                str(included_config),
            ],
            check=True,
            capture_output=True,
        )

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        check = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "config",
                "--worktree",
                "--includes",
                "--get-all",
                "core.hooksPath",
            ],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0
        assert check.stdout == ""
        include_check = subprocess.run(
            ["git", "-C", str(worktree), "config", "--worktree", "--get-all", "include.path"],
            capture_output=True,
            text=True,
        )
        assert include_check.returncode != 0

    @pytest.mark.unit
    async def test_removes_worktree_gitdir_include_exposing_poisoned_hooks_path(
        self, tmp_path: Path
    ) -> None:
        mirror, worktree = self._mirror_with_attached_worktree(tmp_path, create_hooks_dir=False)
        subprocess.run(
            ["git", "-C", str(worktree), "config", "extensions.worktreeConfig", "true"],
            check=True,
            capture_output=True,
        )
        linked_git_dir = git_module.linked_worktree_git_dir(worktree)
        assert linked_git_dir is not None
        included_config = tmp_path / "worktree-gitdir-included-hooks.conf"
        included_config.write_text("[core]\n\thooksPath = /dev/null\n", encoding="utf-8")
        subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "config",
                "--worktree",
                f"includeIf.gitdir:{linked_git_dir}.path",
                str(included_config),
            ],
            check=True,
            capture_output=True,
        )

        file_probe = subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(linked_git_dir / "config.worktree"),
                "--includes",
                "--get-all",
                "core.hooksPath",
            ],
            capture_output=True,
            text=True,
        )
        assert file_probe.returncode != 0
        context_probe = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "config",
                "--worktree",
                "--includes",
                "--get-all",
                "core.hooksPath",
            ],
            capture_output=True,
            text=True,
        )
        assert context_probe.returncode == 0
        assert context_probe.stdout == "/dev/null\n"

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is True
        check = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "config",
                "--worktree",
                "--includes",
                "--get-all",
                "core.hooksPath",
            ],
            capture_output=True,
            text=True,
        )
        assert check.returncode != 0
        assert check.stdout == ""
        include_check = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "config",
                "--worktree",
                "--get-regexp",
                r"^includeIf\..*\.path$",
            ],
            capture_output=True,
            text=True,
        )
        assert include_check.returncode != 0

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
    async def test_skips_stale_linked_worktree_entry(self, tmp_path: Path) -> None:
        mirror, worktree = self._mirror_with_attached_worktree(tmp_path, create_hooks_dir=False)
        subprocess.run(
            ["git", "--git-dir", str(mirror), "config", "core.hooksPath", "/dev/null"],
            check=True,
            capture_output=True,
        )
        linked_git_dir = git_module.linked_worktree_git_dir(worktree)
        assert linked_git_dir is not None
        assert linked_git_dir.exists()
        shutil.rmtree(worktree)

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

    @pytest.mark.unit
    async def test_repair_fails_when_poisoned_hooks_origin_is_unmapped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / "mirror.git" / "config"
        config_path.parent.mkdir()
        probe_value = git_module._HooksPathConfigValue(  # noqa: SLF001
            hooks_path="/dev/null",
            origin_path=None,
        )

        async def _probe_hooks_path_config(**_kwargs: object) -> tuple[object, ...]:
            return (probe_value,)

        monkeypatch.setattr(git_module, "_probe_hooks_path_config", _probe_hooks_path_config)

        with pytest.raises(git_module.GitOperationError) as raised:
            await git_module._repair_hooks_path_config(  # noqa: SLF001
                git_args=("--git-dir", str(config_path.parent)),
                config_scope_args=("--local",),
                config_path=config_path,
                operation_prefix="mirror",
            )

        assert raised.value.operation == "mirror.hooks_path_include_repair"
        assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
        assert raised.value.stdout == "/dev/null"
        assert "origin is not directly included" in raised.value.stderr

    @pytest.mark.unit
    async def test_repair_fails_when_include_path_probe_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / "mirror.git" / "config"
        config_path.parent.mkdir()

        async def _run_git_config(**_kwargs: object) -> tuple[int, str, str]:
            return 2, "", "config read failed"

        monkeypatch.setattr(git_module, "_run_git_config", _run_git_config)

        with pytest.raises(git_module.GitOperationError) as raised:
            await git_module._unset_matching_include_path(  # noqa: SLF001
                git_args=("--git-dir", str(config_path.parent)),
                config_scope_args=("--local",),
                config_path=config_path,
                included_origin=tmp_path / "included-hooks.conf",
                operation_prefix="mirror",
            )

        assert raised.value.operation == "mirror.hooks_path_include_probe"
        assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
        assert raised.value.returncode == 2
        assert raised.value.stderr == "config read failed"

    @pytest.mark.unit
    async def test_repair_fails_when_includeif_probe_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / "mirror.git" / "config"
        config_path.parent.mkdir()
        calls: list[tuple[str, ...]] = []

        async def _run_git_config(
            *, args: tuple[str, ...], **_kwargs: object
        ) -> tuple[int, str, str]:
            calls.append(args)
            if args == ("--get-all", "include.path"):
                return 1, "", ""
            return 2, "", "includeIf probe failed"

        monkeypatch.setattr(git_module, "_run_git_config", _run_git_config)

        with pytest.raises(git_module.GitOperationError) as raised:
            await git_module._unset_matching_include_path(  # noqa: SLF001
                git_args=("--git-dir", str(config_path.parent)),
                config_scope_args=("--local",),
                config_path=config_path,
                included_origin=tmp_path / "included-hooks.conf",
                operation_prefix="mirror",
            )

        assert calls == [
            ("--get-all", "include.path"),
            ("--get-regexp", r"^includeIf\..*\.path$"),
        ]
        assert raised.value.operation == "mirror.hooks_path_include_probe"
        assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
        assert raised.value.returncode == 2
        assert raised.value.stderr == "includeIf probe failed"

    @pytest.mark.unit
    async def test_repair_ignores_malformed_includeif_probe_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / "mirror.git" / "config"
        config_path.parent.mkdir()

        async def _run_git_config(
            *, args: tuple[str, ...], **_kwargs: object
        ) -> tuple[int, str, str]:
            if args == ("--get-all", "include.path"):
                return 1, "", ""
            if args == ("--get-regexp", r"^includeIf\..*\.path$"):
                return 0, "includeIf.gitdir:bad.path\n", ""
            raise AssertionError(f"unexpected git config args: {args!r}")

        monkeypatch.setattr(git_module, "_run_git_config", _run_git_config)

        removed = await git_module._unset_matching_include_path(  # noqa: SLF001
            git_args=("--git-dir", str(config_path.parent)),
            config_scope_args=("--local",),
            config_path=config_path,
            included_origin=tmp_path / "included-hooks.conf",
            operation_prefix="mirror",
        )

        assert removed is False

    @pytest.mark.unit
    async def test_repair_fails_when_matching_include_unset_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / "mirror.git" / "config"
        config_path.parent.mkdir()
        included_config = tmp_path / "included-hooks.conf"
        included_config.write_text("[core]\n\thooksPath = /dev/null\n", encoding="utf-8")

        async def _run_git_config(
            *, args: tuple[str, ...], **_kwargs: object
        ) -> tuple[int, str, str]:
            if args == ("--get-all", "include.path"):
                return 0, f"not-it.conf\n{included_config}\n", ""
            if args == ("--get-regexp", r"^includeIf\..*\.path$"):
                return 1, "", ""
            assert args[0] == "--unset-all"
            return 2, "", "include unset failed"

        monkeypatch.setattr(git_module, "_run_git_config", _run_git_config)

        with pytest.raises(git_module.GitOperationError) as raised:
            await git_module._unset_matching_include_path(  # noqa: SLF001
                git_args=("--git-dir", str(config_path.parent)),
                config_scope_args=("--local",),
                config_path=config_path,
                included_origin=included_config,
                operation_prefix="mirror",
            )

        assert raised.value.operation == "mirror.hooks_path_include_repair"
        assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
        assert raised.value.returncode == 2
        assert raised.value.stderr == "include unset failed"

    @pytest.mark.unit
    async def test_repair_fails_when_hooks_path_unset_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / "mirror.git" / "config"
        config_path.parent.mkdir()
        probe_value = git_module._HooksPathConfigValue(  # noqa: SLF001
            hooks_path="/dev/null",
            origin_path=config_path,
        )

        async def _probe_hooks_path_config(**_kwargs: object) -> tuple[object, ...]:
            return (probe_value,)

        async def _run_git_config(**_kwargs: object) -> tuple[int, str, str]:
            return 2, "", "hooksPath unset failed"

        monkeypatch.setattr(git_module, "_probe_hooks_path_config", _probe_hooks_path_config)
        monkeypatch.setattr(git_module, "_run_git_config", _run_git_config)

        with pytest.raises(git_module.GitOperationError) as raised:
            await git_module._repair_hooks_path_config(  # noqa: SLF001
                git_args=("--git-dir", str(config_path.parent)),
                config_scope_args=("--local",),
                config_path=config_path,
                operation_prefix="mirror",
            )

        assert raised.value.operation == "mirror.hooks_path_repair"
        assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
        assert raised.value.returncode == 2
        assert raised.value.stderr == "hooksPath unset failed"
