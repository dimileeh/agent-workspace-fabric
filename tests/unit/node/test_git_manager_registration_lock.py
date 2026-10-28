"""AWF linked-worktree registrations are locked against foreign ``worktree prune`` (#1133).

Every agent container mounts the whole bare mirror read-write but only its own
checkout, so a ``git worktree prune`` run inside any container sees every other
registration's ``gitdir`` target as missing. These tests use real ``git`` in
``tmp_path`` and simulate the container view by temporarily renaming the managed
worktrees root.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from structlog.testing import capture_logs

from awf.node import git_manager_registration as registration
from awf.node.cleanup import WorkspaceCleaner
from awf.node.git_manager import GitManager, WorktreeLayout, repair_mirror_hooks_path


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _admin(layout: WorktreeLayout) -> Path:
    return layout.mirror_path / "worktrees" / layout.worktree_path.name


def _registered_paths(mirror: Path) -> set[str]:
    porcelain = _git(["--git-dir", str(mirror), "worktree", "list", "--porcelain"]).stdout
    return {
        line.removeprefix("worktree ")
        for line in porcelain.splitlines()
        if line.startswith("worktree ") and line.removeprefix("worktree ") != str(mirror)
    }


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(["init", "-q", "-b", "development"], repo)
    _git(["config", "user.name", "AWF Test"], repo)
    _git(["config", "user.email", "awf@test.local"], repo)
    (repo / "README.md").write_text("first\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    return repo


@pytest.fixture
def manager(tmp_path: Path) -> GitManager:
    work_dir = tmp_path / "awf-work"
    work_dir.mkdir()
    return GitManager(work_dir)


async def _add(manager: GitManager, origin_repo: Path, workspace_id: str) -> WorktreeLayout:
    return await manager.add_worktree(
        workspace_id=workspace_id,
        repo_url=str(origin_repo),
        base_branch="development",
        new_branch=f"awf/{workspace_id}",
    )


def _commit_in(worktree: Path) -> None:
    _git(
        [
            "-c",
            "user.name=AWF Test",
            "-c",
            "user.email=awf@test.local",
            "-C",
            str(worktree),
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "still a repository",
        ]
    )


class TestLockedOnCreate:
    @pytest.mark.unit
    async def test_add_worktree_registers_locked_with_awf_reason(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        layout = await _add(manager, origin_repo, "ws_lock_a")

        locked = (_admin(layout) / "locked").read_text(encoding="utf-8").strip()
        assert locked == registration.awf_worktree_lock_reason("ws_lock_a")
        assert locked.startswith(registration.AWF_WORKTREE_LOCK_REASON_PREFIX)
        assert "ws_lock_a" in locked
        porcelain = _git(
            ["--git-dir", str(layout.mirror_path), "worktree", "list", "--porcelain"]
        ).stdout
        assert f"locked {locked}" in porcelain

    @pytest.mark.unit
    async def test_detached_trusted_base_worktree_registers_locked(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        base = await _add(manager, origin_repo, "ws_base")
        head = _git(["-C", str(base.worktree_path), "rev-parse", "HEAD"]).stdout.strip()

        detached = await manager.add_detached_worktree_at_commit(
            workspace_id="ws_base__trusted_base_profile",
            repo_url=str(origin_repo),
            commit_sha=head,
        )

        locked = (_admin(detached) / "locked").read_text(encoding="utf-8").strip()
        assert locked == registration.awf_worktree_lock_reason("ws_base__trusted_base_profile")

        await manager.remove_worktree(
            workspace_id="ws_base__trusted_base_profile", repo_url=str(origin_repo)
        )
        assert not _admin(detached).exists()
        assert not detached.worktree_path.exists()


class TestContainerViewPrune:
    @pytest.mark.unit
    async def test_prune_with_hidden_checkouts_keeps_every_awf_registration(
        self, manager: GitManager, origin_repo: Path, tmp_path: Path
    ) -> None:
        first = await _add(manager, origin_repo, "ws_live_1")
        second = await _add(manager, origin_repo, "ws_live_2")
        mirror = first.mirror_path
        # Control: a registration AWF did not create (unlocked) is pruned, proving
        # the hidden-root prune really reproduces the issue.
        foreign = tmp_path / "foreign-root" / "wt"
        _git(
            [
                "--git-dir",
                str(mirror),
                "worktree",
                "add",
                "-q",
                "--detach",
                str(foreign),
                "origin/development",
            ]
        )

        worktrees_root = manager.work_dir / "worktrees"
        hidden = worktrees_root.with_name("worktrees.hidden")
        foreign_hidden = foreign.parent.with_name("foreign-root.hidden")
        worktrees_root.rename(hidden)
        foreign.parent.rename(foreign_hidden)
        try:
            _git(["--git-dir", str(mirror), "worktree", "prune", "-v"])
        finally:
            hidden.rename(worktrees_root)
            foreign_hidden.rename(foreign.parent)

        assert _admin(first).is_dir()
        assert _admin(second).is_dir()
        assert not (mirror / "worktrees" / "wt").exists()
        _commit_in(first.worktree_path)
        _commit_in(second.worktree_path)


class TestLockOnReuse:
    @pytest.mark.unit
    async def test_ensure_worktree_locks_legacy_unlocked_registration(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        layout = await _add(manager, origin_repo, "ws_legacy")
        _git(
            ["--git-dir", str(layout.mirror_path), "worktree", "unlock", str(layout.worktree_path)]
        )
        assert not (_admin(layout) / "locked").exists()
        head_before = _git(["-C", str(layout.worktree_path), "rev-parse", "HEAD"]).stdout

        reused = await manager.ensure_worktree(
            workspace_id="ws_legacy",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_legacy",
        )

        assert reused == layout
        assert (_admin(layout) / "locked").read_text(
            encoding="utf-8"
        ).strip() == registration.awf_worktree_lock_reason("ws_legacy")
        assert _git(["-C", str(layout.worktree_path), "rev-parse", "HEAD"]).stdout == head_before

    @pytest.mark.unit
    async def test_lock_reused_registration_keeps_an_existing_lock(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        layout = await _add(manager, origin_repo, "ws_already")
        lock_file = _admin(layout) / "locked"
        lock_file.write_text("operator hold", encoding="utf-8")

        locked = await registration.lock_reused_registration(
            mirror_path=layout.mirror_path,
            worktree_path=layout.worktree_path,
            workspace_id="ws_already",
        )

        assert locked is True
        assert lock_file.read_text(encoding="utf-8") == "operator hold"

    @pytest.mark.unit
    async def test_lock_reused_registration_logs_reason_code_when_unregistered(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        layout = await _add(manager, origin_repo, "ws_present")
        missing = layout.worktree_path.with_name("ws_never_registered")

        with capture_logs() as logs:
            locked = await registration.lock_reused_registration(
                mirror_path=layout.mirror_path,
                worktree_path=missing,
                workspace_id="ws_never_registered",
            )

        assert locked is False
        assert [
            entry["reason_code"]
            for entry in logs
            if entry["event"] == "worktree.registration.lock_failed"
        ] == ["GIT_WORKTREE_REGISTRATION_LOCK_FAILED"]
        assert not (layout.mirror_path / "worktrees" / "ws_never_registered").exists()

    @pytest.mark.unit
    async def test_ensure_registration_locked_treats_already_locked_race_as_success(
        self, manager: GitManager, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        layout = await _add(manager, origin_repo, "ws_race")
        lock_file = _admin(layout) / "locked"
        # Hide the lock from the fast-path check so git itself reports "already locked".
        real_exists = Path.exists
        monkeypatch.setattr(
            Path,
            "exists",
            lambda path, **kwargs: False if path == lock_file else real_exists(path, **kwargs),
        )

        with capture_logs() as logs:
            locked = await registration.ensure_registration_locked(
                mirror_path=layout.mirror_path,
                worktree_path=layout.worktree_path,
                workspace_id="ws_race",
            )

        assert locked is True
        assert not [entry for entry in logs if entry["event"].startswith("worktree.registration")]

    @pytest.mark.unit
    async def test_cancelled_lock_terminates_git_before_releasing_mirror_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mirror = tmp_path / "mirrors" / "repo.git"
        mirror.mkdir(parents=True)
        pid_file = tmp_path / "git.pid"
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_git = fake_bin / "git"
        fake_git.write_text(f'#!/bin/sh\necho $$ > "{pid_file}"\nexec sleep 30\n', encoding="utf-8")
        fake_git.chmod(0o755)
        monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

        task = asyncio.create_task(
            registration.lock_reused_registration(
                mirror_path=mirror, worktree_path=tmp_path / "worktrees" / "wt", workspace_id="ws"
            )
        )
        for _ in range(500):
            if pid_file.is_file() and pid_file.read_text(encoding="utf-8").strip():
                break
            await asyncio.sleep(0.01)
        pid = int(pid_file.read_text(encoding="utf-8"))

        try:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            assert not GitManager._lock_for_mirror(mirror).locked()  # noqa: SLF001
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


class TestRemoveLockedRegistrations:
    @pytest.mark.unit
    async def test_remove_worktree_removes_locked_live_checkout(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        layout = await _add(manager, origin_repo, "ws_remove")
        (layout.worktree_path / "dirty.txt").write_text("uncommitted\n")

        await manager.remove_worktree(workspace_id="ws_remove", repo_url=str(origin_repo))

        assert not layout.worktree_path.exists()
        assert not _admin(layout).exists()
        assert _registered_paths(layout.mirror_path) == set()

    @pytest.mark.unit
    async def test_stale_reclaim_removes_locked_registration(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        layout = await _add(manager, origin_repo, "ws_stale")
        (layout.worktree_path / ".git").unlink()

        with capture_logs() as logs:
            await manager.remove_worktree(workspace_id="ws_stale", repo_url=str(origin_repo))

        assert not layout.worktree_path.exists()
        assert not _admin(layout).exists()
        reaped = [entry for entry in logs if entry["event"] == "worktree.registration.reaped"]
        assert [entry["reason_code"] for entry in reaped] == ["GIT_WORKTREE_REGISTRATION_REAPED"]

    @pytest.mark.unit
    async def test_remove_reaps_locked_registration_of_out_of_band_deleted_checkout(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        gone = await _add(manager, origin_repo, "ws_gone")
        kept = await _add(manager, origin_repo, "ws_kept")
        shutil.rmtree(gone.worktree_path)

        await manager.remove_worktree(workspace_id="ws_gone", repo_url=str(origin_repo))

        assert not _admin(gone).exists()
        assert (_admin(kept) / "locked").is_file()
        assert _registered_paths(kept.mirror_path) == {str(kept.worktree_path)}

    @pytest.mark.unit
    async def test_ensure_worktree_recreates_corrupt_locked_checkout(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        layout = await _add(manager, origin_repo, "ws_corrupt")
        (_admin(layout) / "HEAD").unlink()

        recreated = await manager.ensure_worktree(
            workspace_id="ws_corrupt",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_corrupt",
        )

        assert recreated.worktree_path == layout.worktree_path
        assert _registered_paths(layout.mirror_path) == {str(layout.worktree_path)}
        assert (_admin(layout) / "locked").read_text(
            encoding="utf-8"
        ).strip() == registration.awf_worktree_lock_reason("ws_corrupt")
        _commit_in(recreated.worktree_path)


class TestManagedReapScope:
    @pytest.mark.unit
    async def test_reap_skips_when_managed_root_is_not_visible(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        layout = await _add(manager, origin_repo, "ws_hidden_root")
        worktrees_root = manager.work_dir / "worktrees"
        hidden = worktrees_root.with_name("worktrees.hidden")
        worktrees_root.rename(hidden)
        try:
            reaped = await registration.unlock_dead_managed_registrations(
                mirror_path=layout.mirror_path, worktrees_root=worktrees_root
            )
        finally:
            hidden.rename(worktrees_root)

        assert reaped == ()
        assert (_admin(layout) / "locked").is_file()

    @pytest.mark.unit
    async def test_reap_skips_registrations_outside_managed_root_and_foreign_locks(
        self, manager: GitManager, origin_repo: Path, tmp_path: Path
    ) -> None:
        layout = await _add(manager, origin_repo, "ws_scope")
        mirror = layout.mirror_path
        outside = tmp_path / "elsewhere" / "ws_outside"
        _git(
            [
                "--git-dir",
                str(mirror),
                "worktree",
                "add",
                "-q",
                "--detach",
                str(outside),
                "origin/development",
            ]
        )
        _git(
            [
                "--git-dir",
                str(mirror),
                "worktree",
                "lock",
                "--reason",
                registration.awf_worktree_lock_reason("ws_outside"),
                str(outside),
            ]
        )
        operator_held = manager.work_dir / "worktrees" / "ws_operator"
        _git(
            [
                "--git-dir",
                str(mirror),
                "worktree",
                "add",
                "-q",
                "--detach",
                str(operator_held),
                "origin/development",
            ]
        )
        _git(["--git-dir", str(mirror), "worktree", "lock", str(operator_held)])
        unlocked = manager.work_dir / "worktrees" / "ws_unlocked"
        _git(
            [
                "--git-dir",
                str(mirror),
                "worktree",
                "add",
                "-q",
                "--detach",
                str(unlocked),
                "origin/development",
            ]
        )
        shutil.rmtree(outside)
        shutil.rmtree(operator_held)
        shutil.rmtree(unlocked)

        reaped = await registration.unlock_dead_managed_registrations(
            mirror_path=mirror, worktrees_root=manager.work_dir / "worktrees"
        )

        assert reaped == ()
        assert (mirror / "worktrees" / "ws_outside" / "locked").is_file()
        assert (mirror / "worktrees" / "ws_operator" / "locked").is_file()
        assert (_admin(layout) / "locked").is_file()

    @pytest.mark.unit
    async def test_reap_without_mirror_worktrees_dir_is_a_noop(self, tmp_path: Path) -> None:
        mirror = tmp_path / "mirrors" / "empty.git"
        _git(["init", "-q", "--bare", str(mirror)])
        (tmp_path / "worktrees").mkdir()

        assert (
            await registration.unlock_dead_managed_registrations(
                mirror_path=mirror, worktrees_root=tmp_path / "worktrees"
            )
            == ()
        )

    @pytest.mark.unit
    def test_managed_worktrees_root_follows_git_manager_layout(self, tmp_path: Path) -> None:
        assert registration.managed_worktrees_root_for_mirror(
            tmp_path / "git" / "mirrors" / "repo.git"
        ) == (tmp_path / "git" / "worktrees")
        assert registration.managed_worktrees_root_for_mirror(tmp_path / "mirror.git") is None


class TestCancelKeepsCheckout:
    @pytest.mark.unit
    async def test_kept_checkout_stays_registered_then_removes_cleanly(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        layout = await _add(manager, origin_repo, "ws_cancelled")
        cleaner = WorkspaceCleaner(git=manager, compose=AsyncMock())

        result = await cleaner.cleanup(
            workspace_id="ws_cancelled",
            repo_url=str(origin_repo),
            remove_worktree=False,
        )

        assert [step.reason_code for step in result.steps if step.name == "worktree_remove"] == [
            "WORKTREE_REMOVE_SKIPPED"
        ]
        assert (_admin(layout) / "locked").is_file()
        assert await repair_mirror_hooks_path(layout.mirror_path) is False
        assert (_admin(layout) / "locked").is_file()
        _commit_in(layout.worktree_path)

        await manager.remove_worktree(workspace_id="ws_cancelled", repo_url=str(origin_repo))

        assert not layout.worktree_path.exists()
        assert _registered_paths(layout.mirror_path) == set()


def _synthetic_admin(root: Path, name: str, *, gitdir: str, locked: str | None) -> Path:
    admin = root / "mirrors" / "repo.git" / "worktrees" / name
    admin.mkdir(parents=True)
    (admin / "gitdir").write_text(gitdir, encoding="utf-8")
    if locked is not None:
        (admin / "locked").write_text(locked, encoding="utf-8")
    return admin


class TestReapGuards:
    @pytest.mark.unit
    async def test_only_dead_direct_children_with_gitfile_backrefs_are_unlocked(
        self, tmp_path: Path
    ) -> None:
        worktrees_root = tmp_path / "worktrees"
        worktrees_root.mkdir()
        reason = registration.awf_worktree_lock_reason("ws")
        relative = _synthetic_admin(
            tmp_path, "ws_relative", gitdir="../../../../worktrees/ws_relative/.git", locked=reason
        )
        kept = [
            _synthetic_admin(tmp_path, "ws_empty", gitdir="\n", locked=reason),
            _synthetic_admin(
                tmp_path,
                "ws_not_gitfile",
                gitdir=str(worktrees_root / "ws_not_gitfile" / "HEAD"),
                locked=reason,
            ),
            _synthetic_admin(
                tmp_path,
                "ws_nested",
                gitdir=str(worktrees_root / "group" / "ws_nested" / ".git"),
                locked=reason,
            ),
        ]

        reaped = await registration.unlock_dead_managed_registrations(
            mirror_path=tmp_path / "mirrors" / "repo.git", worktrees_root=worktrees_root
        )

        assert reaped == ("ws_relative",)
        assert not (relative / "locked").exists()
        assert all((admin / "locked").is_file() for admin in kept)

    @pytest.mark.unit
    async def test_lock_that_cannot_be_removed_is_logged_and_left_for_fail_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktrees_root = tmp_path / "worktrees"
        worktrees_root.mkdir()
        admin = _synthetic_admin(
            tmp_path,
            "ws_stuck",
            gitdir=str(worktrees_root / "ws_stuck" / ".git"),
            locked=registration.awf_worktree_lock_reason("ws_stuck"),
        )
        real_unlink = Path.unlink

        def _denied(path: Path, missing_ok: bool = False) -> None:
            if path == admin / "locked":
                raise PermissionError(13, "Permission denied", str(path))
            real_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", _denied)

        with capture_logs() as logs:
            reaped = await registration.unlock_dead_managed_registrations(
                mirror_path=tmp_path / "mirrors" / "repo.git", worktrees_root=worktrees_root
            )

        assert reaped == ()
        assert (admin / "locked").is_file()
        assert [
            entry["reason_code"]
            for entry in logs
            if entry["event"] == "worktree.registration.reap_failed"
        ] == ["GIT_WORKTREE_REGISTRATION_REAP_FAILED"]

    @pytest.mark.unit
    async def test_unreadable_registration_dir_reaps_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktrees_root = tmp_path / "worktrees"
        worktrees_root.mkdir()
        admin_root = tmp_path / "mirrors" / "repo.git" / "worktrees"
        admin_root.mkdir(parents=True)
        real_iterdir = Path.iterdir

        def _denied(path: Path) -> object:
            if path == admin_root:
                raise PermissionError(13, "Permission denied", str(path))
            return real_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", _denied)

        assert (
            await registration.unlock_dead_managed_registrations(
                mirror_path=admin_root.parent, worktrees_root=worktrees_root
            )
            == ()
        )
