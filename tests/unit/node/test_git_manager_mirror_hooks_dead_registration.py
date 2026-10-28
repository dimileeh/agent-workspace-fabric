"""Hooks-path repair tolerates dead linked-worktree metadata without failing the mirror (#1133).

A registration git rejects as "not a git repository" (half-removed admin dir), or one
that vanishes between enumeration and probe, cannot carry ``core.hooksPath``
poisoning. It must be logged and skipped or reaped instead of failing repair for every
other workspace on the mirror. Real poisoning and unreadable metadata stay fail-closed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from awf.node import git_manager as git_module
from awf.node import git_manager_registration as registration
from awf.node.git_manager import GitManager, GitOperationError, WorktreeLayout


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _admin(layout: WorktreeLayout) -> Path:
    return layout.mirror_path / "worktrees" / layout.worktree_path.name


def _hooks_path(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args, "core.hooksPath"], check=False, capture_output=True, text=True
    )


def _reason_codes(logs: list[dict[str, object]], event: str) -> list[object]:
    return [entry["reason_code"] for entry in logs if entry["event"] == event]


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


def _half_remove(layout: WorktreeLayout) -> None:
    """Leave only ``gitdir`` behind, the shape seen in production."""
    (_admin(layout) / "HEAD").unlink()
    (_admin(layout) / "commondir").unlink()


def _poison_worktree_config(layout: WorktreeLayout) -> None:
    _git(["--git-dir", str(layout.mirror_path), "config", "extensions.worktreeConfig", "true"])
    _git(["-C", str(layout.worktree_path), "config", "--worktree", "core.hooksPath", "/dev/null"])


class TestDeadRegistrationDoesNotFailMirror:
    @pytest.mark.unit
    async def test_half_removed_registration_is_skipped_and_healthy_one_still_repaired(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        dead = await _add(manager, origin_repo, "ws_0_dead")
        healthy = await _add(manager, origin_repo, "ws_1_healthy")
        _poison_worktree_config(healthy)
        _half_remove(dead)

        with capture_logs() as logs:
            repaired = await git_module.repair_mirror_hooks_path(healthy.mirror_path)

        assert repaired is True
        assert _hooks_path("-C", str(healthy.worktree_path), "config", "--worktree").returncode
        assert _reason_codes(logs, "mirror.hooks_path.dead_worktree_registration_skipped") == [
            "MIRROR_WORKTREE_REGISTRATION_DEAD"
        ]
        # The dead workspace still fails on its own.
        status = subprocess.run(
            ["git", "-C", str(dead.worktree_path), "status"], capture_output=True, text=True
        )
        assert status.returncode == 128
        assert "not a git repository" in status.stderr
        assert (_admin(healthy) / "locked").is_file()

    @pytest.mark.unit
    async def test_garbage_head_registration_is_skipped_and_healthy_one_still_repaired(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        healthy = await _add(manager, origin_repo, "ws_0_healthy")
        corrupt = await _add(manager, origin_repo, "ws_1_corrupt")
        _poison_worktree_config(healthy)
        (_admin(corrupt) / "HEAD").write_text("garbage\n", encoding="utf-8")

        with capture_logs() as logs:
            repaired = await git_module.repair_mirror_hooks_path(healthy.mirror_path)

        assert repaired is True
        assert _hooks_path("-C", str(healthy.worktree_path), "config", "--worktree").returncode
        assert _reason_codes(logs, "mirror.hooks_path.dead_worktree_registration_skipped") == [
            "MIRROR_WORKTREE_REGISTRATION_DEAD"
        ]
        assert _admin(corrupt).is_dir()

    @pytest.mark.unit
    async def test_locked_registration_with_gone_checkout_is_reaped(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        gone = await _add(manager, origin_repo, "ws_0_gone")
        healthy = await _add(manager, origin_repo, "ws_1_healthy")
        shutil.rmtree(gone.worktree_path)

        with capture_logs() as logs:
            repaired = await git_module.repair_mirror_hooks_path(healthy.mirror_path)

        assert repaired is False
        assert not _admin(gone).exists()
        assert _reason_codes(logs, "worktree.registration.reaped") == [
            "GIT_WORKTREE_REGISTRATION_REAPED"
        ]
        assert (_admin(healthy) / "locked").is_file()

    @pytest.mark.unit
    @pytest.mark.parametrize("missing", ["HEAD", "commondir"])
    async def test_locked_registration_without_backref_or_admin_file_is_skipped(
        self, missing: str, manager: GitManager, origin_repo: Path
    ) -> None:
        dead = await _add(manager, origin_repo, "ws_0_dead")
        healthy = await _add(manager, origin_repo, "ws_1_healthy")
        _poison_worktree_config(healthy)
        # Half-removed, but still locked: git prune never reaps it.
        (_admin(dead) / "gitdir").unlink()
        (_admin(dead) / missing).unlink()

        with capture_logs() as logs:
            repaired = await git_module.repair_mirror_hooks_path(healthy.mirror_path)

        assert repaired is True
        assert _hooks_path("-C", str(healthy.worktree_path), "config", "--worktree").returncode
        assert _reason_codes(logs, "mirror.hooks_path.dead_worktree_registration_skipped") == [
            "MIRROR_WORKTREE_REGISTRATION_DEAD"
        ]
        assert (_admin(dead) / "locked").is_file()
        assert (_admin(healthy) / "locked").is_file()

    @pytest.mark.unit
    async def test_registration_vanishing_during_probe_is_reaped_and_retried(
        self, manager: GitManager, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        racing = await _add(manager, origin_repo, "ws_0_racing")
        healthy = await _add(manager, origin_repo, "ws_1_healthy")
        real_repair = git_module._repair_hooks_path_config
        vanished = False

        async def _repair_then_vanish(**kwargs: object) -> bool:
            nonlocal vanished
            git_args = kwargs["git_args"]
            assert isinstance(git_args, tuple)
            if not vanished and str(racing.worktree_path) in git_args:
                vanished = True
                shutil.rmtree(racing.worktree_path)
            return await real_repair(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(git_module, "_repair_hooks_path_config", _repair_then_vanish)

        with capture_logs() as logs:
            repaired = await git_module.repair_mirror_hooks_path(healthy.mirror_path)

        assert vanished is True
        assert repaired is False
        assert _reason_codes(logs, "mirror.hooks_path.dead_worktree_registration_skipped") == [
            "MIRROR_WORKTREE_REGISTRATION_VANISHED"
        ]
        assert not _admin(racing).exists()
        assert _admin(healthy).is_dir()

    @pytest.mark.unit
    async def test_issue_reproduction_container_prune_then_stale_admin_dir(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        legacy = await _add(manager, origin_repo, "ws_0_legacy")
        healthy = await _add(manager, origin_repo, "ws_1_healthy")
        mirror = healthy.mirror_path
        # Pre-fix shape: unlocked registrations reaped by a container-view prune.
        for layout in (legacy, healthy):
            _git(["--git-dir", str(mirror), "worktree", "unlock", str(layout.worktree_path)])
        gitdir_backref = (_admin(legacy) / "gitdir").read_text(encoding="utf-8")
        root = manager.work_dir / "worktrees"
        root.rename(root.with_name("worktrees.hidden"))
        try:
            _git(["--git-dir", str(mirror), "worktree", "prune"])
        finally:
            root.with_name("worktrees.hidden").rename(root)
        assert not (mirror / "worktrees" / "ws_0_legacy").exists()
        # A half-rebuilt admin dir (only ``gitdir``) is what made repair fail closed.
        _admin(legacy).mkdir(parents=True)
        (_admin(legacy) / "gitdir").write_text(gitdir_backref, encoding="utf-8")
        # Restart recovery re-registers the healthy workspace (now locked).
        healthy = await manager.ensure_worktree(
            workspace_id="ws_1_healthy",
            repo_url=str(origin_repo),
            base_branch="development",
            new_branch="awf/ws_1_healthy",
        )

        with capture_logs() as logs:
            assert await git_module.repair_mirror_hooks_path(mirror) is False

        assert _reason_codes(logs, "mirror.hooks_path.dead_worktree_registration_skipped") == [
            "MIRROR_WORKTREE_REGISTRATION_DEAD"
        ]
        assert (_admin(healthy) / "locked").is_file()


class TestPoisoningStaysFailClosed:
    @pytest.mark.unit
    async def test_mirror_config_poisoning_is_repaired_next_to_dead_registration(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        dead = await _add(manager, origin_repo, "ws_0_dead")
        _half_remove(dead)
        _git(["--git-dir", str(dead.mirror_path), "config", "core.hooksPath", "/dev/null"])

        assert await git_module.repair_mirror_hooks_path(dead.mirror_path) is True
        assert _hooks_path("--git-dir", str(dead.mirror_path), "config", "--local").returncode

    @pytest.mark.unit
    async def test_unrepairable_worktree_config_poisoning_still_raises(
        self, manager: GitManager, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dead = await _add(manager, origin_repo, "ws_0_dead")
        healthy = await _add(manager, origin_repo, "ws_1_healthy")
        _poison_worktree_config(healthy)
        _half_remove(dead)
        real_run_git_config = git_module._run_git_config

        async def _unset_fails(**kwargs: object) -> tuple[int, str, str]:
            args = kwargs["args"]
            assert isinstance(args, tuple)
            if "--unset-all" in args:
                return 1, "", "error: could not lock config file"
            return await real_run_git_config(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(git_module, "_run_git_config", _unset_fails)

        with pytest.raises(GitOperationError) as raised:
            await git_module.repair_mirror_hooks_path(healthy.mirror_path)

        assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
        assert raised.value.operation == "worktree.hooks_path_repair"

    @pytest.mark.unit
    async def test_unreadable_dead_registration_still_fails_closed(
        self, manager: GitManager, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dead = await _add(manager, origin_repo, "ws_0_dead")
        (_admin(dead) / "commondir").unlink()
        head = _admin(dead) / "HEAD"
        head.write_text("garbage\n", encoding="utf-8")
        real_read_bytes = Path.read_bytes

        def _denied(path: Path) -> bytes:
            if path == head:
                raise PermissionError(13, "Permission denied", str(path))
            return real_read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", _denied)

        with pytest.raises(GitOperationError) as raised:
            await git_module.repair_mirror_hooks_path(dead.mirror_path)

        assert raised.value.reason_code == "MIRROR_HOOKS_PATH_REPAIR_FAILED"
        assert raised.value.returncode == 128

    @pytest.mark.unit
    async def test_locked_registration_without_backref_but_openable_still_fails_closed(
        self, manager: GitManager, origin_repo: Path
    ) -> None:
        live = await _add(manager, origin_repo, "ws_0_live")
        # ``HEAD`` and ``commondir`` intact: git can still open it, so it may
        # carry poisoning and must not be skipped.
        (_admin(live) / "gitdir").unlink()

        with pytest.raises(GitOperationError) as raised:
            await git_module.repair_mirror_hooks_path(live.mirror_path)

        assert raised.value.operation == "mirror.worktree_metadata_stale"
        assert (_admin(live) / "locked").is_file()

    @pytest.mark.unit
    async def test_locked_registration_without_backref_and_unreadable_head_fails_closed(
        self, manager: GitManager, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dead = await _add(manager, origin_repo, "ws_0_dead")
        (_admin(dead) / "gitdir").unlink()
        (_admin(dead) / "commondir").unlink()
        head = _admin(dead) / "HEAD"
        real_read_bytes = Path.read_bytes

        def _denied(path: Path) -> bytes:
            if path == head:
                raise PermissionError(13, "Permission denied", str(path))
            return real_read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", _denied)

        with pytest.raises(GitOperationError) as raised:
            await git_module.repair_mirror_hooks_path(dead.mirror_path)

        assert raised.value.operation == "mirror.worktree_metadata_stale"


def _probe_error(
    *, returncode: int = 128, stderr: str = "fatal: not a git repository: x", operation: str = ""
) -> GitOperationError:
    return GitOperationError(
        operation=operation or "linked_worktree.hooks_path_probe",
        returncode=returncode,
        stdout="",
        stderr=stderr,
        reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
    )


class TestClassifyLinkedProbeFailure:
    @pytest.fixture
    def registered(self, tmp_path: Path) -> tuple[Path, Path]:
        admin = tmp_path / "mirror.git" / "worktrees" / "ws_x"
        admin.mkdir(parents=True)
        (admin / "HEAD").write_text("ref: refs/heads/x\n", encoding="utf-8")
        (admin / "commondir").write_text("../..\n", encoding="utf-8")
        checkout = tmp_path / "worktrees" / "ws_x"
        checkout.mkdir(parents=True)
        (checkout / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
        return admin, checkout

    @staticmethod
    def _classify(exc: GitOperationError, registered: tuple[Path, Path]) -> str | None:
        admin, checkout = registered
        return registration.classify_linked_probe_failure(
            exc, linked_git_dir=admin, worktree_path=checkout
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "exc",
        [
            _probe_error(returncode=1),
            _probe_error(stderr="fatal: bad config line 1 in file config"),
            _probe_error(operation="linked_worktree.hooks_path_repair"),
            _probe_error(operation="mirror.hooks_path_probe"),
        ],
    )
    def test_other_failures_are_not_classified(
        self, exc: GitOperationError, registered: tuple[Path, Path]
    ) -> None:
        (registered[0] / "HEAD").unlink()
        assert self._classify(exc, registered) is None

    @pytest.mark.unit
    def test_corrupt_but_readable_registration_is_dead(self, registered: tuple[Path, Path]) -> None:
        assert self._classify(_probe_error(), registered) == "dead"

    @pytest.mark.unit
    def test_unreadable_head_fails_closed(
        self, registered: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        head = registered[0] / "HEAD"
        real_read_bytes = Path.read_bytes

        def _denied(path: Path) -> bytes:
            if path == head:
                raise PermissionError(13, "Permission denied", str(path))
            return real_read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", _denied)
        assert self._classify(_probe_error(), registered) is None

    @pytest.mark.unit
    @pytest.mark.parametrize("missing", ["HEAD", "commondir"])
    def test_missing_admin_file_is_dead(self, missing: str, registered: tuple[Path, Path]) -> None:
        (registered[0] / missing).unlink()
        assert self._classify(_probe_error(operation="worktree.hooks_path_probe"), registered) == (
            "dead"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("gone", ["checkout", "gitfile", "admin"])
    def test_vanished_paths(self, gone: str, registered: tuple[Path, Path]) -> None:
        admin, checkout = registered
        if gone == "checkout":
            shutil.rmtree(checkout)
        elif gone == "gitfile":
            (checkout / ".git").unlink()
        else:
            shutil.rmtree(admin)
        exc = _probe_error(stderr="fatal: cannot change to 'x': No such file or directory")
        assert self._classify(exc, registered) == "vanished"

    @pytest.mark.unit
    def test_unreadable_checkout_path_fails_closed(
        self, registered: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_lstat = Path.lstat

        def _denied(path: Path) -> object:
            if path == registered[1]:
                raise PermissionError(13, "Permission denied", str(path))
            return real_lstat(path)

        monkeypatch.setattr(Path, "lstat", _denied)
        assert self._classify(_probe_error(), registered) is None
