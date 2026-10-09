"""Regression tests for shared-mirror ref-namespace agent writability (#1033).

Root-side control-plane git activity can prune and recreate
``refs/heads/<namespace>/`` in the shared bare mirror ``755 root:root`` *after*
the per-attempt chown has already run, which makes the agent's own
``git commit`` fail with ``EACCES`` on the ref lock. These tests lock the three
defences:

1. ``gc.packRefs=false`` on every AWF-managed mirror (created or repaired), so
   auto-gc never empties a loose-ref directory in the first place.
2. a default POSIX ACL on ``refs/``/``logs/`` so directories root creates later
   inherit agent write access, with a graceful logged fallback when ``setfacl``
   or filesystem ACL support is missing.
3. the cheap refs/logs ownership repair re-running after a root-side ref write.

CI does not run as root and the test image has no ``setfacl``, so uid, chown and
the ``setfacl`` subprocess are monkeypatched; the git repositories are real.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

import awf.node.git_manager as git_manager
import awf.node.git_manager_mirror as git_manager_mirror
from awf.node.git_manager import GitManager, GitOperationError
from awf.runtime.ownership import repair_agent_runtime_ownership

_AGENT_UID = 1000
_AGENT_GID = 1000
_SETFACL = "/usr/bin/setfacl"


def _git(args: list[str], cwd: Path) -> None:
    """Run a git command in ``cwd``, failing loudly."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    """A tiny real origin repository to mirror."""
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "user.email", "t@t"], repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    return repo


@pytest.fixture(autouse=True)
def _reset_setfacl_lookup() -> Iterator[None]:
    """Keep the process-wide ``setfacl`` lookup from leaking between tests."""
    git_manager_mirror._setfacl_executable.cache_clear()  # noqa: SLF001
    yield
    git_manager_mirror._setfacl_executable.cache_clear()  # noqa: SLF001


class _WarningLog:
    """Collect the structured warnings the hardening helpers emit."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def warning(self, event: str, **fields: object) -> None:
        """Record a structured warning."""
        self.events.append((event, fields))

    def names(self) -> list[str]:
        """Return the recorded event names."""
        return [event for event, _fields in self.events]


def _open_fd_count() -> int:
    """How many descriptors this process holds open right now."""
    return len(list(Path("/proc/self/fd").iterdir()))


def _resolved(paths: set[Path]) -> set[Path]:
    """Resolve expected directories the way a pinned descriptor reports them."""
    return {path.resolve() for path in paths}


def _capture_warnings(monkeypatch: pytest.MonkeyPatch) -> _WarningLog:
    """Swap the module logger for a recorder."""
    log = _WarningLog()
    monkeypatch.setattr(git_manager_mirror, "_log", log)
    return log


class _RecordedSetfacl:
    """Record ``setfacl`` invocations; pass every other command through."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.pass_fds: list[tuple[int, ...]] = []
        self.pinned: list[Path] = []
        self._returncode = returncode
        self._stderr = stderr
        self._passthrough = subprocess.run

    def __call__(
        self,
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        """Intercept ``setfacl`` and record it with the directories it pinned."""
        if Path(args[0]).name != "setfacl":
            return self._passthrough(args, **kwargs)  # type: ignore[call-overload,no-any-return]
        self.calls.append(list(args))
        self.pass_fds.append(tuple(kwargs.get("pass_fds") or ()))
        # The fake runs in-process while the descriptors are still open, so the
        # magic links still name the directories the walk pinned.
        self.pinned.extend(Path(arg).readlink() for arg in args[4:])
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=self._returncode,
            stdout="",
            stderr=self._stderr,
        )

    def acl_paths(self) -> set[Path]:
        """Every directory a default ACL was requested for."""
        return set(self.pinned)

    def argument_fds(self) -> list[int]:
        """The descriptor numbers named by the recorded ``/proc/self/fd`` paths."""
        return [int(Path(arg).name) for call in self.calls for arg in call[4:]]


def _install_fake_setfacl(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stderr: str = "",
) -> _RecordedSetfacl:
    """Pretend ``setfacl`` exists and capture its invocations."""
    recorder = _RecordedSetfacl(returncode=returncode, stderr=stderr)
    monkeypatch.setattr(
        git_manager_mirror.shutil,
        "which",
        lambda name: _SETFACL if name == "setfacl" else None,
    )
    monkeypatch.setattr(git_manager_mirror.subprocess, "run", recorder)
    return recorder


def _mirror_config(mirror: Path, key: str) -> tuple[int, str]:
    """Read a config key straight out of the mirror's local config."""
    result = subprocess.run(
        ["git", "--git-dir", str(mirror), "config", "--local", "--get", key],
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout.strip()


@pytest.mark.unit
async def test_fresh_mirror_clone_disables_root_side_ref_packing(
    tmp_path: Path, origin_repo: Path
) -> None:
    """A freshly cloned mirror must never let auto-gc pack (and prune) its refs."""
    manager = GitManager(tmp_path / "work")

    mirror = await manager.ensure_mirror(str(origin_repo))

    assert _mirror_config(mirror, "gc.packRefs") == (0, "false")


@pytest.mark.unit
async def test_existing_mirror_is_rehardened_on_ensure(tmp_path: Path, origin_repo: Path) -> None:
    """Pre-#1033 mirrors get the ref-storage setting on the next ensure."""
    manager = GitManager(tmp_path / "work")
    mirror = await manager.ensure_mirror(str(origin_repo))
    subprocess.run(
        ["git", "--git-dir", str(mirror), "config", "--local", "--unset", "gc.packRefs"],
        check=True,
        capture_output=True,
    )
    assert _mirror_config(mirror, "gc.packRefs")[0] != 0

    reused = await manager.ensure_mirror(str(origin_repo))

    assert reused == mirror
    assert _mirror_config(mirror, "gc.packRefs") == (0, "false")


@pytest.mark.unit
async def test_ref_packing_config_failure_is_reported_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed hardening is logged with its reason code and reported as False."""
    mirror = tmp_path / "mirror.git"
    mirror.mkdir()
    log = _capture_warnings(monkeypatch)

    async def _failing_run(args: list[str], *, operation: str) -> object:
        del args
        raise GitOperationError(
            operation=operation,
            returncode=1,
            stdout="",
            stderr="could not lock config file",
        )

    hardened = await git_manager_mirror.ensure_mirror_ref_packing_disabled(mirror, _failing_run)

    assert hardened is False
    assert log.names() == ["mirror.ref_packing_config_failed"]
    fields = log.events[0][1]
    assert fields["reason_code"] == git_manager_mirror.MIRROR_REF_PACKING_CONFIG_FAILED_REASON
    assert fields["config_key"] == "gc.packRefs"


@pytest.mark.unit
async def test_ensure_mirror_survives_ref_packing_config_failure(
    tmp_path: Path, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hardening is advisory: provisioning must not fail when it cannot be set."""

    async def _failing_hardening(mirror_path: Path, run: object) -> bool:
        del mirror_path, run
        return False

    monkeypatch.setattr(git_manager, "ensure_mirror_ref_packing_disabled", _failing_hardening)
    manager = GitManager(tmp_path / "work")

    mirror = await manager.ensure_mirror(str(origin_repo))

    assert mirror.exists()


@pytest.mark.unit
def test_default_acls_cover_ref_and_log_directories_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default ACL set is exactly the refs/ and logs/ directory trees."""
    mirror = tmp_path / "mirror.git"
    namespace = mirror / "refs" / "heads" / "feature-sync"
    namespace.mkdir(parents=True)
    (namespace / "ws_abc").write_text(f"{'0' * 40}\n", encoding="utf-8")
    log_namespace = mirror / "logs" / "refs" / "heads"
    log_namespace.mkdir(parents=True)
    (mirror / "objects" / "pack").mkdir(parents=True)
    (mirror / "packed-refs").write_text("", encoding="utf-8")
    recorder = _install_fake_setfacl(monkeypatch)

    applied = git_manager_mirror.apply_mirror_ref_default_acls(mirror, _AGENT_UID)

    assert applied is True
    assert recorder.acl_paths() == _resolved(
        {
            mirror / "refs",
            mirror / "refs" / "heads",
            namespace,
            mirror / "logs",
            mirror / "logs" / "refs",
            log_namespace,
        }
    )
    for call in recorder.calls:
        assert call[0] == _SETFACL
        assert call[1:4] == ["-d", "-m", f"u:{_AGENT_UID}:rwx"]
        # Pinned descriptors, not mutable pathnames, and handed to the child.
        assert all(arg.startswith("/proc/self/fd/") for arg in call[4:])
    assert recorder.pass_fds == [tuple(recorder.argument_fds())]


@pytest.mark.unit
def test_default_acls_never_follow_a_symlinked_ref_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``setfacl`` follows symlinks, so a ref symlink must not grant outside."""
    mirror = tmp_path / "mirror.git"
    (mirror / "refs" / "heads").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (mirror / "refs" / "linked").symlink_to(outside, target_is_directory=True)
    recorder = _install_fake_setfacl(monkeypatch)

    assert git_manager_mirror.apply_mirror_ref_default_acls(mirror, _AGENT_UID) is True
    assert recorder.acl_paths() == _resolved({mirror / "refs", mirror / "refs" / "heads"})


@pytest.mark.unit
def test_default_acls_pin_directories_against_a_post_check_symlink_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ref directory swapped for a symlink after the walk must not be granted.

    The agent owns these directories, so it can replace one — or an ancestor of
    one — with a symlink between the walk and the root-side ``setfacl``, which
    follows symlinks given on its command line. The ACL must still land on the
    directory that was validated, never on the symlink target.
    """
    mirror = tmp_path / "mirror.git"
    heads = mirror / "refs" / "heads"
    heads.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    heads_inode = heads.stat().st_ino
    pinned_inodes: list[int] = []

    def _swap_then_record(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Swap the walked directory for an outside symlink, then read the argv."""
        if not heads.is_symlink():
            heads.rmdir()
            heads.symlink_to(outside, target_is_directory=True)
        pinned_inodes.extend(os.fstat(int(Path(arg).name)).st_ino for arg in args[4:])
        return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

    monkeypatch.setattr(git_manager_mirror.shutil, "which", lambda _name: _SETFACL)
    monkeypatch.setattr(git_manager_mirror.subprocess, "run", _swap_then_record)

    assert git_manager_mirror.apply_mirror_ref_default_acls(mirror, _AGENT_UID) is True
    assert heads_inode in pinned_inodes
    assert outside.stat().st_ino not in pinned_inodes


@pytest.mark.unit
def test_default_acls_batch_pinned_descriptors_without_leaking_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batching bounds argv and open descriptors, and releases them as it goes."""
    mirror = tmp_path / "mirror.git"
    for namespace in ("alpha", "beta"):
        (mirror / "refs" / "heads" / namespace).mkdir(parents=True)
    monkeypatch.setattr(git_manager_mirror, "_SETFACL_PATH_BATCH", 2)
    recorder = _install_fake_setfacl(monkeypatch)
    open_descriptors = _open_fd_count()

    applied = git_manager_mirror.apply_mirror_ref_default_acls(mirror, _AGENT_UID)

    assert applied is True
    assert [len(call) - 4 for call in recorder.calls] == [2, 2]
    assert recorder.acl_paths() == _resolved(
        {
            mirror / "refs",
            mirror / "refs" / "heads",
            mirror / "refs" / "heads" / "alpha",
            mirror / "refs" / "heads" / "beta",
        }
    )
    assert _open_fd_count() == open_descriptors


@pytest.mark.unit
def test_default_acls_report_a_failing_batch_without_stopping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch that ``setfacl`` rejects is reported, and later batches still run."""
    mirror = tmp_path / "mirror.git"
    for namespace in ("alpha", "beta"):
        (mirror / "refs" / "heads" / namespace).mkdir(parents=True)
    monkeypatch.setattr(git_manager_mirror, "_SETFACL_PATH_BATCH", 2)
    recorder = _install_fake_setfacl(monkeypatch, returncode=1, stderr="Operation not supported")
    log = _capture_warnings(monkeypatch)

    applied = git_manager_mirror.apply_mirror_ref_default_acls(mirror, _AGENT_UID)

    assert applied is False
    assert len(recorder.calls) == 2
    assert log.names() == ["mirror.ref_default_acl_failed"] * 2


@pytest.mark.unit
def test_pinned_walk_closes_descriptors_when_the_caller_stops_early(tmp_path: Path) -> None:
    """Abandoning the walk must not leak the descriptors it is still holding."""
    mirror = tmp_path / "mirror.git"
    (mirror / "refs" / "heads" / "feature-sync").mkdir(parents=True)
    open_descriptors = _open_fd_count()

    directories = git_manager_mirror._walk_pinned_acl_dir_fds(mirror)  # noqa: SLF001
    first = next(directories)
    os.close(first)
    directories.close()

    assert _open_fd_count() == open_descriptors


@pytest.mark.unit
def test_default_acls_skip_a_mirror_that_vanished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mirror removed before the repair runs is nothing to grant, not a crash."""
    recorder = _install_fake_setfacl(monkeypatch)

    assert git_manager_mirror.apply_mirror_ref_default_acls(tmp_path / "gone.git", 1000) is True
    assert recorder.calls == []


@pytest.mark.unit
def test_default_acls_skip_a_ref_directory_that_becomes_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable pinned directory is skipped; the walk must not raise."""
    mirror = tmp_path / "mirror.git"
    (mirror / "refs" / "heads").mkdir(parents=True)
    recorder = _install_fake_setfacl(monkeypatch)

    def _unreadable(_dir_fd: int) -> list[str]:
        raise OSError("input/output error")

    monkeypatch.setattr(git_manager_mirror.os, "listdir", _unreadable)

    assert git_manager_mirror.apply_mirror_ref_default_acls(mirror, _AGENT_UID) is True
    assert recorder.acl_paths() == _resolved({mirror / "refs"})


@pytest.mark.unit
def test_default_acls_are_skipped_when_mirror_has_no_ref_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to grant means no subprocess and no failure."""
    mirror = tmp_path / "mirror.git"
    mirror.mkdir()
    recorder = _install_fake_setfacl(monkeypatch)

    assert git_manager_mirror.apply_mirror_ref_default_acls(mirror, _AGENT_UID) is True
    assert recorder.calls == []


@pytest.mark.unit
def test_missing_setfacl_degrades_to_a_logged_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``setfacl`` the chown baseline stands and nothing is executed."""
    mirror = tmp_path / "mirror.git"
    (mirror / "refs" / "heads").mkdir(parents=True)
    recorder = _install_fake_setfacl(monkeypatch)
    monkeypatch.setattr(git_manager_mirror.shutil, "which", lambda _name: None)
    log = _capture_warnings(monkeypatch)

    applied = git_manager_mirror.apply_mirror_ref_default_acls(mirror, _AGENT_UID)

    assert applied is False
    assert recorder.calls == []
    assert log.names() == ["mirror.ref_default_acl_unsupported"]
    assert (
        log.events[0][1]["reason_code"]
        == git_manager_mirror.MIRROR_REF_DEFAULT_ACL_UNSUPPORTED_REASON
    )


@pytest.mark.unit
def test_setfacl_nonzero_exit_is_reported_with_reason_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filesystem without ACL support is a warning, not a provision failure."""
    mirror = tmp_path / "mirror.git"
    (mirror / "refs" / "heads").mkdir(parents=True)
    _install_fake_setfacl(monkeypatch, returncode=1, stderr="Operation not supported")
    log = _capture_warnings(monkeypatch)

    applied = git_manager_mirror.apply_mirror_ref_default_acls(mirror, _AGENT_UID)

    assert applied is False
    assert log.names() == ["mirror.ref_default_acl_failed"]
    fields = log.events[0][1]
    assert fields["reason_code"] == git_manager_mirror.MIRROR_REF_DEFAULT_ACL_FAILED_REASON
    assert "Operation not supported" in str(fields["stderr"])


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    [
        OSError("setfacl vanished"),
        subprocess.TimeoutExpired(cmd=["setfacl"], timeout=20.0),
    ],
)
def test_setfacl_execution_errors_are_reported_with_reason_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """``setfacl`` blowing up must not raise out of the ownership repair."""
    mirror = tmp_path / "mirror.git"
    (mirror / "refs" / "heads").mkdir(parents=True)
    monkeypatch.setattr(git_manager_mirror.shutil, "which", lambda _name: _SETFACL)

    def _raise(*_args: object, **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr(git_manager_mirror.subprocess, "run", _raise)
    log = _capture_warnings(monkeypatch)

    applied = git_manager_mirror.apply_mirror_ref_default_acls(mirror, _AGENT_UID)

    assert applied is False
    assert log.names() == ["mirror.ref_default_acl_failed"]
    assert (
        log.events[0][1]["reason_code"] == git_manager_mirror.MIRROR_REF_DEFAULT_ACL_FAILED_REASON
    )


def _linked_worktree(tmp_path: Path, workspace_id: str = "ws_race") -> tuple[Path, Path]:
    """Build the on-disk mirror + linked-worktree shape the repair expects."""
    mirror = tmp_path / "work" / "mirrors" / "repo.git"
    linked_git_dir = mirror / "worktrees" / workspace_id
    linked_git_dir.mkdir(parents=True)
    (mirror / "refs" / "heads" / "feature-sync").mkdir(parents=True)
    worktree = tmp_path / "work" / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {linked_git_dir}\n", encoding="utf-8")
    (linked_git_dir / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    return mirror, worktree


@pytest.mark.unit
def test_repair_applies_default_acls_for_the_resolved_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared-metadata repair grants the inheritable ACL alongside the chown."""
    mirror, worktree = _linked_worktree(tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda *_args: None)
    recorder = _install_fake_setfacl(monkeypatch)

    git_manager.repair_agent_writable_worktree(mirror, worktree, _AGENT_UID, _AGENT_GID)

    assert mirror / "refs" / "heads" / "feature-sync" in recorder.acl_paths()


@pytest.mark.unit
def test_repair_skips_default_acls_without_shared_metadata_or_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolated-worktree and non-root repairs keep their existing narrow surface."""
    mirror, worktree = _linked_worktree(tmp_path)
    monkeypatch.setattr(os, "chown", lambda *_args: None)
    recorder = _install_fake_setfacl(monkeypatch)

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    git_manager.repair_agent_writable_worktree(
        mirror,
        worktree,
        _AGENT_UID,
        _AGENT_GID,
        repair_shared_git_metadata=False,
    )
    assert recorder.calls == []

    monkeypatch.setattr(git_manager, "mirror_path_for_worktree", lambda _path: None)
    git_manager.repair_agent_writable_worktree(None, worktree, _AGENT_UID, _AGENT_GID)
    assert recorder.calls == []

    monkeypatch.setattr(os, "geteuid", lambda: _AGENT_UID)
    git_manager.repair_agent_writable_worktree(mirror, worktree, _AGENT_UID, _AGENT_GID)
    assert recorder.calls == []


@pytest.mark.unit
async def test_root_recreated_ref_namespace_is_repaired_again_after_a_ref_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1033: a namespace recreated root-owned mid-attempt is repaired on re-run.

    Models the observed race: the prelaunch repair runs, root-side git activity
    prunes and recreates ``refs/heads/feature-sync/`` ``root:root``, and the
    repair that now follows every control-plane ref write has to hand the
    directory back to the agent (chown) *and* re-arm the inheritable ACL.
    """
    mirror, worktree = _linked_worktree(tmp_path)
    namespace = mirror / "refs" / "heads" / "feature-sync"
    chowned: list[tuple[Path, int, int]] = []

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        os,
        "chown",
        lambda path, uid, gid: chowned.append((Path(path), uid, gid)),
    )
    recorder = _install_fake_setfacl(monkeypatch)

    git_manager.repair_agent_writable_worktree(mirror, worktree, _AGENT_UID, _AGENT_GID)
    assert (namespace, _AGENT_UID, _AGENT_GID) in chowned

    # Root packs the refs, git prunes the emptied directory, and the next
    # root-side ref write recreates it ``root:root``.
    shutil.rmtree(namespace)
    namespace.mkdir()
    chowned.clear()
    recorder.calls.clear()

    class _FailLog:
        def exception(self, event: str, **fields: object) -> None:
            """Fail the test on an unexpected ownership-repair failure."""
            pytest.fail(f"unexpected ownership-repair failure: {event} {fields}")

    repaired = await repair_agent_runtime_ownership(
        logger=_FailLog(),
        workspace_id="ws_race",
        worktree_path=worktree,
        reason="sync_base_post_merge_ref_write",
        event_name="monitor.agent_runtime_ownership_repair_failed",
    )

    assert repaired is True
    assert (namespace, _AGENT_UID, _AGENT_GID) in chowned
    assert namespace in recorder.acl_paths()


@pytest.mark.unit
async def test_worktree_add_applies_default_acls_for_the_new_namespace(
    tmp_path: Path, origin_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``worktree add`` is itself a root-side ref write; it must arm the ACL."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda *_args: None)
    monkeypatch.setattr(os, "lchown", lambda *_args: None)
    recorder = _install_fake_setfacl(monkeypatch)
    manager = GitManager(
        tmp_path / "work",
        worktree_owner_uid=_AGENT_UID,
        worktree_owner_gid=_AGENT_GID,
    )

    layout = await manager.add_worktree(
        workspace_id="ws_acl_worktree",
        repo_url=str(origin_repo),
        base_branch="main",
        new_branch="feature-sync/ws_acl_worktree",
    )

    acl_paths = recorder.acl_paths()
    assert layout.mirror_path / "refs" / "heads" / "feature-sync" in acl_paths
    assert not any(path.is_relative_to(layout.mirror_path / "objects") for path in acl_paths)
