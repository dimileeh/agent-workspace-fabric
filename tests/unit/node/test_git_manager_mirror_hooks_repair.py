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
    async def test_repo_url_derived_mirror_uses_same_lock_as_actual_mirror(
        self, tmp_path: Path
    ) -> None:
        """Verify repo url derived mirror uses same lock as actual mirror."""
        repo = tmp_path / "origin"
        repo.mkdir()
        _git(["init", "-q", "-b", "main"], repo)
        _git(["config", "user.name", "AWF Test"], repo)
        _git(["config", "user.email", "awf@test.local"], repo)
        (repo / "README.md").write_text("initial\n", encoding="utf-8")
        _git(["add", "."], repo)
        _git(["commit", "-q", "-m", "init"], repo)

        manager = git_module.GitManager(tmp_path / "git")
        repo_url = str(repo)
        actual_mirror = await manager.ensure_mirror(repo_url)

        origin_url = await git_module.read_mirror_origin_url(actual_mirror)
        assert origin_url == repo_url
        url_derived_mirror = manager._mirror_path(origin_url)  # noqa: SLF001

        assert url_derived_mirror.resolve() == actual_mirror.resolve()
        assert git_module.GitManager._lock_for_mirror(  # noqa: SLF001
            url_derived_mirror
        ) is git_module.GitManager._lock_for_mirror(actual_mirror)  # noqa: SLF001

    @pytest.mark.unit
    async def test_remove_worktree_waits_for_same_mirror_lock_as_repair(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify remove worktree waits for same mirror lock as repair."""
        repo_url = "git@github.com:example/repo.git"
        manager = git_module.GitManager(tmp_path / "git")
        mirror = manager._mirror_path(repo_url)  # noqa: SLF001
        worktree = manager.get_worktree_path("ws_dead")
        mirror.mkdir(parents=True)
        worktree.mkdir(parents=True)
        entered: list[str] = []

        async def _run(args: list[str], *, operation: str) -> git_module.GitResult:
            """Test helper for run."""
            del args
            entered.append(operation)
            return git_module.GitResult(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(manager, "_run", _run)
        lock = git_module.GitManager._lock_for_mirror(mirror)  # noqa: SLF001
        await lock.acquire()
        task = asyncio.create_task(
            manager.remove_worktree(workspace_id="ws_dead", repo_url=repo_url)
        )
        try:
            await asyncio.sleep(0)
            assert entered == []
            assert task.done() is False
        finally:
            lock.release()

        await task
        assert entered == ["worktree.remove", "worktree.prune"]

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

        # Both scan passes report nothing to repair, so the corrected contract
        # returns ``False`` even though stale metadata was pruned between them.
        assert await git_module.repair_mirror_hooks_path(mirror) is False
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
            # Prune cannot clear metadata whose gitdir back-reference stays
            # missing (the empty linked-worktree dir survives the prune).

        monkeypatch.setattr(git_module, "_repair_hooks_path_config", _repair_hooks_path_config)
        monkeypatch.setattr(git_module, "_run_git_worktree_prune", _run_git_worktree_prune)

        with pytest.raises(git_module.GitOperationError) as raised:
            await git_module.repair_mirror_hooks_path(mirror)

        assert prune_calls == 1
        assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
        assert raised.value.operation == "mirror.worktree_metadata_stale"

    @pytest.mark.unit
    async def test_run_git_worktree_prune_raises_when_prune_subprocess_fails(
        self, tmp_path: Path
    ) -> None:
        # A path that is not a git directory makes ``git worktree prune`` exit
        # non-zero, which must surface as a repair failure rather than being
        # swallowed.
        not_a_mirror = tmp_path / "not-a-git-dir"
        not_a_mirror.mkdir()

        with pytest.raises(git_module.GitOperationError) as raised:
            await git_module._run_git_worktree_prune(not_a_mirror)

        assert raised.value.operation == "mirror.worktree_prune"
        assert raised.value.returncode != 0
        assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"

    @pytest.mark.unit
    async def test_fails_closed_when_worktrees_dir_is_unreadable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mirror = tmp_path / "mirror.git"
        mirror.mkdir()
        subprocess.run(
            ["git", "init", "--bare", str(mirror)],
            check=True,
            capture_output=True,
        )
        worktrees_dir = mirror / "worktrees"
        worktrees_dir.mkdir()

        async def _repair_hooks_path_config(**_kwargs: object) -> bool:
            return False

        async def _run_git_worktree_prune(path: Path) -> None:
            assert path == mirror

        real_iterdir = Path.iterdir

        def _iterdir(self: Path):  # type: ignore[no-untyped-def]
            if self == worktrees_dir:
                raise OSError("permission denied scanning worktrees")
            return real_iterdir(self)

        monkeypatch.setattr(git_module, "_repair_hooks_path_config", _repair_hooks_path_config)
        monkeypatch.setattr(git_module, "_run_git_worktree_prune", _run_git_worktree_prune)
        monkeypatch.setattr(Path, "iterdir", _iterdir)

        # An unreadable ``worktrees`` directory persists across the prune retry,
        # so a poisoned worktree ``core.hooksPath`` cannot be verified-clean and
        # repair must fail closed.
        with pytest.raises(git_module.GitOperationError) as raised:
            await git_module.repair_mirror_hooks_path(mirror)

        assert raised.value.operation == "mirror.worktree_metadata_stale"
        assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"

    @pytest.mark.unit
    async def test_propagates_non_stale_linked_worktree_probe_error(
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

        async def _repair_hooks_path_config(**_kwargs: object) -> bool:
            return False

        probe_error = git_module.GitOperationError(
            operation="worktree.hooks_path_probe",
            returncode=1,
            stdout="",
            stderr="fatal: some other probe failure",
            reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
        )

        def linked_worktree_path_from_git_dir(_path: Path) -> Path:
            """Linked worktree path from git dir."""
            raise probe_error

        monkeypatch.setattr(git_module, "_repair_hooks_path_config", _repair_hooks_path_config)
        monkeypatch.setattr(
            git_module,
            "linked_worktree_path_from_git_dir",
            linked_worktree_path_from_git_dir,
        )

        # A probe error that is not the stale-metadata sentinel is a genuine
        # failure and must propagate instead of being treated as stale metadata.
        with pytest.raises(git_module.GitOperationError) as raised:
            await git_module.repair_mirror_hooks_path(mirror)

        assert raised.value is probe_error

    @pytest.mark.unit
    async def test_unreadable_live_worktree_gitdir_fails_closed_without_prune(
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
        gitdir_ref = linked_git_dir / "gitdir"
        gitdir_ref.write_text(str(tmp_path / "live-worktree" / ".git"), encoding="utf-8")
        prune_calls = 0

        async def _repair_hooks_path_config(**_kwargs: object) -> bool:
            return False

        async def _run_git_worktree_prune(path: Path) -> None:
            nonlocal prune_calls
            prune_calls += 1

        real_read_text = Path.read_text

        def _read_text(self: Path, *args: object, **kwargs: object) -> str:
            if self == gitdir_ref:
                raise PermissionError("unable to read gitdir file (Permission denied)")
            return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(git_module, "_repair_hooks_path_config", _repair_hooks_path_config)
        monkeypatch.setattr(git_module, "_run_git_worktree_prune", _run_git_worktree_prune)
        monkeypatch.setattr(Path, "read_text", _read_text)

        # A permission-denied gitdir back-reference belongs to a live worktree we
        # merely cannot inspect; it is NOT stale metadata. Pruning it would delete
        # the live worktree's admin files, so repair must fail closed and never
        # reach ``git worktree prune``.
        with pytest.raises(git_module.GitOperationError) as raised:
            await git_module.repair_mirror_hooks_path(mirror)

        assert prune_calls == 0
        assert raised.value.operation == "worktree.hooks_path_probe"
        assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
        assert "cannot access linked-worktree gitdir back-reference" in raised.value.stderr

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
        (worktree / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
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
                "linked_worktree",
            ),
            (
                (*safe_args, "-C", str(worktree)),
                ("--worktree",),
                linked_git_dir / "config.worktree",
                "worktree",
            ),
        ]

    @pytest.mark.unit
    async def test_existing_registered_worktree_missing_gitfile_is_stale_before_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify existing registered worktree missing gitfile is stale before probe."""
        mirror = tmp_path / "mirror.git"
        linked_git_dir = mirror / "worktrees" / "workspace"
        linked_git_dir.mkdir(parents=True)
        worktree = tmp_path / "workspace"
        worktree.mkdir()
        (linked_git_dir / "gitdir").write_text(str(worktree / ".git"), encoding="utf-8")
        repair_prefixes: list[str] = []
        prune_calls = 0

        async def _repair_hooks_path_config(
            *,
            git_args: tuple[str, ...],
            config_scope_args: tuple[str, ...],
            config_path: Path,
            operation_prefix: str,
        ) -> bool:
            """Test helper for repair hooks path config."""
            del git_args, config_scope_args, config_path
            repair_prefixes.append(operation_prefix)
            return False

        async def _run_git_worktree_prune(path: Path) -> None:
            """Test helper for run git worktree prune."""
            nonlocal prune_calls
            prune_calls += 1
            assert path == mirror
            shutil.rmtree(linked_git_dir)

        monkeypatch.setattr(git_module, "_repair_hooks_path_config", _repair_hooks_path_config)
        monkeypatch.setattr(git_module, "_run_git_worktree_prune", _run_git_worktree_prune)

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is False
        assert prune_calls == 1
        assert repair_prefixes == ["mirror", "mirror"]

    @pytest.mark.unit
    async def test_replacement_repo_at_worktree_path_is_stale_before_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify replacement repo at worktree path is stale before probe."""
        mirror, worktree = self._mirror_with_attached_worktree(tmp_path, create_hooks_dir=False)
        linked_git_dir = git_module.linked_worktree_git_dir(worktree)
        assert linked_git_dir is not None
        shutil.rmtree(worktree)
        worktree.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=worktree, check=True, capture_output=True)
        repair_prefixes: list[str] = []
        prune_calls = 0

        async def _repair_hooks_path_config(
            *,
            git_args: tuple[str, ...],
            config_scope_args: tuple[str, ...],
            config_path: Path,
            operation_prefix: str,
        ) -> bool:
            """Test helper for repair hooks path config."""
            del git_args, config_scope_args, config_path
            repair_prefixes.append(operation_prefix)
            return False

        async def _run_git_worktree_prune(path: Path) -> None:
            """Test helper for run git worktree prune."""
            nonlocal prune_calls
            prune_calls += 1
            assert path == mirror
            shutil.rmtree(linked_git_dir)

        monkeypatch.setattr(git_module, "_repair_hooks_path_config", _repair_hooks_path_config)
        monkeypatch.setattr(git_module, "_run_git_worktree_prune", _run_git_worktree_prune)

        result = await git_module.repair_mirror_hooks_path(mirror)

        assert result is False
        assert prune_calls == 1
        assert repair_prefixes == ["mirror", "mirror"]

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
