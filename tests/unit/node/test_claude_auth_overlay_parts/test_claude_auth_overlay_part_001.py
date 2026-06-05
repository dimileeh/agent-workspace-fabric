"""Shared-base + per-workspace overlay isolation for ``~/.claude`` auth.

These tests inject a fake :class:`OverlayMounter` so the overlay/fallback/
teardown branches are exercised without root or a real overlayfs mount. True
kernel overlay semantics are validated operationally and guarded by the
copy fallback; here we assert mount layout, base reuse, isolation, chown
routing, fallback, and unmount-before-remove.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from structlog.testing import capture_logs

# The overlay primitives and Claude auth subsystem live in ``auth_mounts_claude``;
# ``auth_mounts`` re-exports them. Patch the module that *defines* the helpers so
# the consumers (which resolve them in that namespace) observe the override.
from awf.node import auth_mounts_claude as auth_mounts_mod
from awf.node import auth_mounts_claude_reconcile as reconcile_mod
from awf.node import auth_mounts_overlay_copy as overlay_copy_mod
from awf.node.auth_mounts import (
    _host_claude_signature,
    _shared_claude_base_dir,
    resolve_service_auth_mounts,
)


def _recording_mknod(recorded: list[dict[str, object]]):
    """Return a fake ``os.mknod`` recording its args and creating a placeholder.

    A real overlayfs whiteout is a char device 0,0 that ``mknod`` can only create with
    ``CAP_MKNOD``/root — unavailable in unit tests. The fake records the leaf name the
    production code requested and materializes a 0-byte placeholder at the same
    ``dir_fd``-relative location so callers can assert the whiteout landed in ``upper``
    without real privileges. Defined locally (not imported from part_003) because that
    module imports this one — importing back would form a circular import.
    """

    real_open = os.open

    def _fake_mknod(
        path: object, mode: int = 0o600, device: int = 0, *, dir_fd: int | None = None
    ) -> None:
        recorded.append({"name": os.fspath(path), "mode": mode, "device": device})
        fd = real_open(os.fspath(path), os.O_WRONLY | os.O_CREAT, 0o000, dir_fd=dir_fd)
        os.close(fd)

    return _fake_mknod


class FakeOverlayMounter:
    """In-memory :class:`OverlayMounter` recording mount/unmount calls."""

    def __init__(
        self,
        *,
        supported: bool = True,
        mount_error: Exception | None = None,
        unmount_error: Exception | None = None,
    ) -> None:
        self._supported = supported
        self._mount_error = mount_error
        self._unmount_error = unmount_error
        self.mounts: list[dict[str, Path]] = []
        self.unmounts: list[Path] = []
        self.mounted: set[Path] = set()

    def supported(self) -> bool:
        return self._supported

    def mount(self, *, lowerdir: Path, upperdir: Path, workdir: Path, merged: Path) -> None:
        if self._mount_error is not None:
            raise self._mount_error
        if Path(merged) in self.mounted:
            # Mirror the kernel: mounting onto a live overlay mountpoint is EBUSY.
            raise OSError("device or resource busy")
        self.mounts.append(
            {"lowerdir": lowerdir, "upperdir": upperdir, "workdir": workdir, "merged": merged}
        )
        self.mounted.add(Path(merged))

    def unmount(self, target: Path) -> None:
        self.unmounts.append(Path(target))
        if self._unmount_error is not None:
            raise self._unmount_error
        self.mounted.discard(Path(target))

    def is_mounted(self, target: Path) -> bool:
        return Path(target) in self.mounted

    def active_lowerdir(self, merged: Path) -> Path | None:
        # Mirror the kernel: only a live mountpoint has a lowerdir, recovered here
        # from the most recent recorded mount onto ``merged``.
        if Path(merged) not in self.mounted:
            return None
        for call in reversed(self.mounts):
            if call["merged"] == Path(merged):
                return call["lowerdir"]
        return None


def _seed_host_claude(host_home: Path) -> None:
    claude = host_home / ".claude"
    (claude / "skills" / "demo").mkdir(parents=True)
    (claude / "skills" / "demo" / "SKILL.md").write_text("# demo skill\n")
    (claude / "settings.json").write_text('{"theme": "dark"}\n')
    (claude / "projects" / "repo").mkdir(parents=True)
    (claude / "projects" / "repo" / "session.jsonl").write_text('{"usage": "historical"}\n')
    (host_home / ".claude.json").write_text("{}\n")


@pytest.mark.unit
def test_overlay_happy_path_mounts_shared_base_with_per_workspace_upper(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_overlay",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    claude_root = work_dir / "auth" / "ws_overlay" / "claude"
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    assert by_target["/home/agent/.claude"].mode == "rw"
    assert len(mounter.mounts) == 1
    call = mounter.mounts[0]
    assert call["lowerdir"] == _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    assert call["upperdir"] == claude_root / "upper"
    assert call["workdir"] == claude_root / "work"
    assert call["merged"] == claude_root / "merged"
    # ``~/.claude.json`` stays a tiny per-workspace file copy.
    assert by_target["/home/agent/.claude.json"].source == str(claude_root / ".claude.json")


@pytest.mark.unit
def test_shared_base_built_once_and_reused_across_workspaces(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    first = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_a",
        host_env={},
        overlay_mounter=mounter,
    )
    base = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    # A rebuild would replace ``base`` via os.replace and lose this marker; its
    # survival proves the second workspace (same host content) reuses the base.
    marker = base / ".reuse-marker"
    marker.write_text("kept")

    second = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_b",
        host_env={},
        overlay_mounter=mounter,
    )

    assert marker.read_text() == "kept"
    assert mounter.mounts[0]["lowerdir"] == base
    assert mounter.mounts[1]["lowerdir"] == base
    first_by_target = {m.target: m for m in first}
    second_by_target = {m.target: m for m in second}
    assert first_by_target["/home/agent/.claude"].source != (
        second_by_target["/home/agent/.claude"].source
    )


@pytest.mark.unit
def test_shared_base_rebuilt_when_host_claude_changes(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_a",
        host_env={},
        overlay_mounter=mounter,
    )
    base_a = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    assert (base_a / "settings.json").read_text() == '{"theme": "dark"}\n'

    # An operator updates ``~/.claude`` (here a setting) after the first
    # workspace; later workspaces must see the change, not a stale base.
    (host_home / ".claude" / "settings.json").write_text('{"theme": "light"}\n')

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_b",
        host_env={},
        overlay_mounter=mounter,
    )
    base_b = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))

    # The changed host yields a fresh, distinct base carrying the new content,
    # and the second workspace mounts it instead of the stale one.
    assert base_b != base_a
    assert (base_b / "settings.json").read_text() == '{"theme": "light"}\n'
    assert mounter.mounts[1]["lowerdir"] == base_b
    # The old base is immutable — left intact for any still-live overlay mount.
    assert (base_a / "settings.json").read_text() == '{"theme": "dark"}\n'


@pytest.mark.unit
def test_signature_tracks_file_mode_changes(tmp_path: Path) -> None:
    # ``copytree`` preserves permission bits, so making a hook/plugin script
    # executable changes what the copied base contains. ``chmod`` bumps ctime but
    # not size or mtime, so the signature must key off ``st_mode`` to rebuild.
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    script = host_home / ".claude" / "hooks" / "hook.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\necho hi\n")

    before = _host_claude_signature(host_home)
    script.chmod(0o755)
    after = _host_claude_signature(host_home)
    assert after != before


@pytest.mark.unit
def test_signature_tracks_root_directory_mode_changes(tmp_path: Path) -> None:
    # ``copytree`` preserves the top-level directory's mode, so an operator who
    # fixes ``~/.claude`` itself from an inaccessible mode to ``0700`` changes
    # what the copied base looks like at its root. ``chmod`` on the root bumps
    # ctime but not size or mtime, and the walk's per-child entries never cover
    # the root dir, so the signature must sign the root's own ``st_mode`` to
    # rebuild instead of reusing a base with stale root permissions.
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    claude = host_home / ".claude"

    claude.chmod(0o700)
    before = _host_claude_signature(host_home)
    claude.chmod(0o755)
    after = _host_claude_signature(host_home)
    assert after != before


@pytest.mark.unit
def test_signature_follows_symlink_target_content(tmp_path: Path) -> None:
    # An operator keeps ``~/.claude`` config as symlinks into a dotfiles repo.
    # ``copytree(symlinks=False)`` copies the *targets'* contents into the base,
    # so the signature must track those targets — not the (unchanging) links.
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    claude = host_home / ".claude"
    dotfiles = tmp_path / "dotfiles"
    (dotfiles / "skills" / "linked").mkdir(parents=True)
    (dotfiles / "skills" / "linked" / "SKILL.md").write_text("v1\n")
    (dotfiles / "settings.local.json").write_text('{"v": 1}\n')

    # A file symlink and a directory symlink, mirroring real dotfiles setups.
    (claude / "settings.local.json").symlink_to(dotfiles / "settings.local.json")
    (claude / "skills" / "linked").symlink_to(
        dotfiles / "skills" / "linked", target_is_directory=True
    )

    before_file = _host_claude_signature(host_home)
    # Update a file symlink's target in place; the link itself is untouched.
    (dotfiles / "settings.local.json").write_text('{"v": 2, "added": true}\n')
    after_file = _host_claude_signature(host_home)
    assert after_file != before_file

    # Update content *inside* a directory symlink's target; the link is untouched.
    (dotfiles / "skills" / "linked" / "SKILL.md").write_text("v2 — longer\n")
    after_dir = _host_claude_signature(host_home)
    assert after_dir != after_file


@pytest.mark.unit
def test_signature_terminates_on_circular_symlink(tmp_path: Path) -> None:
    # ``os.walk(followlinks=True)`` does not detect symlink cycles, so a circular
    # link in ``~/.claude`` (e.g. a child linked back to a parent) would loop
    # forever — and this runs on every provision call. The ``visited`` inode set
    # must bound the walk so signing terminates instead of hanging the worker.
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    claude = host_home / ".claude"
    loop_dir = claude / "skills" / "demo"
    # Link ``demo/cycle`` back to its own parent ``skills``; followlinks would
    # otherwise descend skills→demo→cycle→skills→… without limit.
    (loop_dir / "cycle").symlink_to(claude / "skills", target_is_directory=True)

    # Terminates (no hang) and returns a stable 16-char digest.
    signature = _host_claude_signature(host_home)
    assert len(signature) == 16
    assert signature == _host_claude_signature(host_home)


@pytest.mark.unit
def test_signature_keeps_dir_whose_stat_races_to_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``os.walk(followlinks=True)`` classifies a directory from ``scandir`` (no stat
    # call on a populated ``d_type``), but the per-child ``child.stat()`` in the dirs
    # loop can still raise — a directory deleted/permission-flipped in the window
    # (a TOCTOU), or an ``EACCES`` race. That ``except`` must keep the entry so the
    # files loop signs it as ``missing`` and record an ``ancestors_by_path`` entry so
    # the "every walked path has an entry" invariant holds for any descendant that
    # resolves before the walk descends. Signing must terminate and stay stable.
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    (host_home / ".claude" / "racingdir").mkdir()

    base_path_cls = type(Path())

    class _StatRacingPath(base_path_cls):  # type: ignore[valid-type, misc]
        def stat(self, *args: object, **kwargs: object) -> os.stat_result:
            if self.name == "racingdir":
                raise PermissionError("simulated stat race")
            return super().stat(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(auth_mounts_mod, "Path", _StatRacingPath)

    signature = _host_claude_signature(host_home)
    assert len(signature) == 16
    assert signature == _host_claude_signature(host_home)


@pytest.mark.unit
def test_signature_tracks_duplicate_symlinks_to_same_target(tmp_path: Path) -> None:
    # Two directory symlinks to the same target are NOT a cycle: ``copytree(
    # symlinks=False)`` copies each linked path separately, so the base gains both
    # ``alpha/`` and ``beta/``. Deduping by inode identity would prune the second
    # path entirely, leaving the signature blind to it — a new ``beta`` link could
    # then reuse a stale base missing skills/plugins reachable through it. The walk
    # must bound only true ancestor cycles, signing each distinct path.
    host_home = tmp_path / "host-home"
    _seed_host_claude(host_home)
    claude = host_home / ".claude"
    dotfiles = tmp_path / "dotfiles"
    (dotfiles / "shared").mkdir(parents=True)
    (dotfiles / "shared" / "SKILL.md").write_text("v1\n")

    # Links placed directly under ``~/.claude`` (whose own mtime is never part of
    # the signature) so the duplicate-path coverage — not an incidental parent
    # mtime bump — is what the assertion exercises.
    (claude / "alpha").symlink_to(dotfiles / "shared", target_is_directory=True)
    before = _host_claude_signature(host_home)

    (claude / "beta").symlink_to(dotfiles / "shared", target_is_directory=True)
    after = _host_claude_signature(host_home)
    assert after != before


@pytest.mark.unit
def test_shared_base_content_excludes_history_and_skips_dangling_links(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    host_claude = host_home / ".claude"
    (host_claude / "todos").mkdir()
    (host_claude / "shell-snapshots").mkdir()
    (host_claude / "statsig").mkdir()
    stale_skill = host_claude / "skills" / "stale"
    stale_skill.mkdir()
    (stale_skill / "SKILL.md").symlink_to(host_home / "removed" / "SKILL.md")

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_base",
        host_env={},
        overlay_mounter=FakeOverlayMounter(supported=True),
    )

    base = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    assert (base / "skills" / "demo" / "SKILL.md").read_text() == "# demo skill\n"
    assert (base / "settings.json").read_text() == '{"theme": "dark"}\n'
    for excluded in ("projects", "todos", "shell-snapshots", "statsig"):
        assert not (base / excluded).exists()
    assert not (base / "skills" / "stale" / "SKILL.md").exists()


@pytest.mark.unit
def test_overlay_isolation_only_chowns_upper_and_work_not_merged_or_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    chowned: list[Path] = []
    monkeypatch.setattr(
        auth_mounts_mod.os,
        "chown",
        lambda path, _uid, _gid: chowned.append(Path(path)),
    )
    monkeypatch.setattr(
        auth_mounts_mod.os,
        "lchown",
        lambda path, _uid, _gid: chowned.append(Path(path)),
    )

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_a",
        host_env={},
        workspace_owner_uid=1000,
        workspace_owner_gid=1000,
        overlay_mounter=mounter,
    )
    base = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    chowned.clear()  # discard the one-time base build chowns

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_b",
        host_env={},
        workspace_owner_uid=1000,
        workspace_owner_gid=1000,
        overlay_mounter=mounter,
    )

    claude_root_b = work_dir / "auth" / "ws_b" / "claude"
    # The second resolution chowns only B's writable upper/work — never the
    # shared base and never the live ``merged`` overlay mount.
    assert (claude_root_b / "upper") in chowned
    assert (claude_root_b / "work") in chowned
    assert (claude_root_b / "merged") not in chowned
    shared_root = work_dir / "auth" / "_shared"
    assert not any(path.is_relative_to(shared_root) for path in chowned)
    assert not any(path.is_relative_to(base) for path in chowned)


@pytest.mark.unit
@pytest.mark.parametrize(
    "mounter",
    [
        FakeOverlayMounter(supported=False),
        FakeOverlayMounter(supported=True, mount_error=PermissionError("no CAP_SYS_ADMIN")),
        FakeOverlayMounter(supported=True, mount_error=subprocess.CalledProcessError(32, "mount")),
    ],
)
def test_overlay_unavailable_falls_back_to_legacy_copy(
    tmp_path: Path,
    mounter: FakeOverlayMounter,
) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)

    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_fallback",
            host_env={},
            overlay_mounter=mounter,
        )

    by_target = {m.target: m for m in mounts}
    claude_root = work_dir / "auth" / "ws_fallback" / "claude"
    # Legacy full copy: the mount source is the copied ``.claude`` tree.
    assert by_target["/home/agent/.claude"].source == str(claude_root / ".claude")
    assert (claude_root / ".claude" / "settings.json").read_text() == '{"theme": "dark"}\n'
    assert not (claude_root / ".claude" / "projects").exists()
    # No stray ``merged`` mountpoint is left behind on fallback.
    assert not (claude_root / "merged").exists()
    # Every fallback path — overlay unsupported and mount failure alike — emits a
    # clear reason so the copy fallback is never silent (issue #361 requirement).
    assert any(entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_UNAVAILABLE" for entry in logs)


@pytest.mark.unit
@pytest.mark.parametrize("reserved", [",", ":"])
def test_reserved_char_work_dir_falls_back_to_legacy_copy(tmp_path: Path, reserved: str) -> None:
    """A ``,``/``:`` in the work dir degrades to copy instead of a broken mount.

    overlayfs's ``mount -o lowerdir=..,upperdir=..,workdir=..`` payload splits on
    ``,`` and stacks lower layers on ``:`` — neither is escapable in that legacy API.
    A literal comma/colon in ``AWF_WORK_DIR`` would tear the option string apart or
    be misread as an extra lower layer, so the overlay branch must never attempt the
    mount: it falls back to the per-workspace copy (PRRT_kwDOSJAM6s6HOnML).
    """

    host_home = tmp_path / "host-home"
    work_dir = tmp_path / f"wo{reserved}rk"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_reserved",
            host_env={},
            overlay_mounter=mounter,
        )

    by_target = {m.target: m for m in mounts}
    claude_root = work_dir / "auth" / "ws_reserved" / "claude"
    # The mount is never attempted — the paths cannot be expressed as overlay options.
    assert mounter.mounts == []
    # Legacy full copy took over: the mount source is the copied ``.claude`` tree.
    assert by_target["/home/agent/.claude"].source == str(claude_root / ".claude")
    assert (claude_root / ".claude" / "settings.json").read_text() == '{"theme": "dark"}\n'
    assert not (claude_root / "merged").exists()
    # The degrade is logged with a distinct reason so it is never silent.
    assert any(
        entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_UNAVAILABLE"
        and entry.get("reason") == "overlay_path_reserved_chars"
        for entry in logs
    )


@pytest.mark.unit
def test_mount_failure_forwards_called_process_stderr(tmp_path: Path) -> None:
    """The copy-fallback warning carries ``mount(8)`` stderr, not just the rc.

    ``str(CalledProcessError)`` emits only the command + return code, so the
    kernel reason an operator needs to diagnose why every workspace degraded to
    the full-copy path would otherwise be silently dropped.
    """

    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    kernel_reason = "special device overlay does not exist"
    mounter = FakeOverlayMounter(
        supported=True,
        mount_error=subprocess.CalledProcessError(32, "mount", stderr=kernel_reason),
    )

    with capture_logs() as logs:
        resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_stderr",
            host_env={},
            overlay_mounter=mounter,
        )

    fallback = next(
        entry
        for entry in logs
        if entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_UNAVAILABLE"
        and entry.get("event") == "claude_auth_overlay_unavailable"
    )
    assert fallback.get("stderr") == kernel_reason


@pytest.mark.unit
def test_shared_base_build_oserror_falls_back_to_legacy_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)

    def _disk_full(**_kwargs: object) -> Path:
        raise OSError("No space left on device")

    # Building the shared base can fail with OSError (disk full, permissions).
    # That must degrade to the legacy copy, not hard-fail provisioning.
    monkeypatch.setattr(auth_mounts_mod, "_ensure_shared_claude_base", _disk_full)

    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_base_fail",
            host_env={},
            overlay_mounter=FakeOverlayMounter(supported=True),
        )

    by_target = {m.target: m for m in mounts}
    claude_root = work_dir / "auth" / "ws_base_fail" / "claude"
    # Legacy full copy took over: the mount source is the copied ``.claude`` tree.
    assert by_target["/home/agent/.claude"].source == str(claude_root / ".claude")
    assert (claude_root / ".claude" / "settings.json").read_text() == '{"theme": "dark"}\n'
    assert not (claude_root / "merged").exists()
    assert any(entry.get("reason_code") == "CLAUDE_AUTH_SHARED_BASE_FAILED" for entry in logs)


@pytest.mark.unit
def test_overlay_scratch_dir_oserror_falls_back_to_legacy_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)

    real_mkdir = auth_mounts_mod.Path.mkdir

    def _mkdir(self: Path, *args: object, **kwargs: object) -> None:
        # Disk full after the base copytree: creating the per-workspace overlay
        # scratch dirs (``<claude_root>/{upper,work,merged}``) fails. The legacy
        # copy's own dirs must still be created.
        if self.name in {"upper", "work", "merged"} and self.parent.name == "claude":
            raise OSError("No space left on device")
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(auth_mounts_mod.Path, "mkdir", _mkdir)

    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_scratch_fail",
            host_env={},
            overlay_mounter=FakeOverlayMounter(supported=True),
        )

    by_target = {m.target: m for m in mounts}
    claude_root = work_dir / "auth" / "ws_scratch_fail" / "claude"
    # A scratch-dir OSError degrades to the legacy full copy, not a hard fail.
    assert by_target["/home/agent/.claude"].source == str(claude_root / ".claude")
    assert (claude_root / ".claude" / "settings.json").read_text() == '{"theme": "dark"}\n'
    assert any(entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_UNAVAILABLE" for entry in logs)


@pytest.mark.unit
def test_overlay_signature_write_oserror_keeps_live_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)

    def _write_fails(self: Path, *args: object, **kwargs: object) -> int:
        # The only ``write_text`` in the resolve path is the base-signature marker,
        # now written *after* a successful mount; a disk-full filesystem fails it.
        raise OSError("No space left on device")

    monkeypatch.setattr(auth_mounts_mod.Path, "write_text", _write_fails)

    # A single shared mounter so the live overlay this provision mounts survives into
    # the second provision below (#405: the pin must become durable on the *next* run).
    mounter = FakeOverlayMounter(supported=True)
    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_sig_fail",
            host_env={},
            overlay_mounter=mounter,
        )

    by_target = {m.target: m for m in mounts}
    claude_root = work_dir / "auth" / "ws_sig_fail" / "claude"
    # The mount already succeeded, so a marker-write OSError keeps the live overlay
    # (it is correct for this provision) rather than discarding it for a needless
    # full copy — it must not hard-fail provisioning either. The pin marker is just
    # absent, so a future teardown+retry recomputes the base from the host.
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    assert not (claude_root / ".claude").exists()
    assert not (claude_root / "base.signature").exists()
    # The mount succeeded, so the failure must surface as a base-pin-write event,
    # *not* a mount-unavailable one — operators grepping for the latter to diagnose
    # real mount failures must not see this harmless metadata-write failure.
    assert any(
        entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_BASE_PIN_WRITE_FAILED"
        and entry.get("event") == "claude_auth_overlay_base_pin_write_failed"
        for entry in logs
    )
    assert not any(entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_UNAVAILABLE" for entry in logs)

    # #405: keep-live is non-negotiable, but the residual cross-reboot wrong-base risk
    # of an absent pin is mitigated on the *next* provision rather than by failing over
    # at write time. The agent accumulated overlay data; the disk-full condition then
    # clears. A second provision reuses the still-live overlay and re-pins ``base.signature``
    # to the base it is actually mounted against — never hard-failing, never dropping the
    # agent's upper data.
    # Clear the disk-full condition first (the patch shadows *all* ``Path.write_text``,
    # including this test's own writes and the next provision's re-pin).
    monkeypatch.undo()
    overlay_data = claude_root / "upper" / "settings.json"
    overlay_data.write_text('{"theme": "agent-edited"}\n')

    second = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_sig_fail",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target_2 = {m.target: m for m in second}
    assert by_target_2["/home/agent/.claude"].source == str(claude_root / "merged")
    # The live mount was reused (no remount onto the busy mountpoint) ...
    assert len(mounter.mounts) == 1
    # ... the pin is now durable, so a later teardown+remount reuses the correct base ...
    assert (claude_root / "base.signature").read_text() == _host_claude_signature(host_home)
    # ... and the agent's overlay data was preserved throughout.
    assert overlay_data.read_text() == '{"theme": "agent-edited"}\n'


@pytest.mark.unit
def test_overlay_reprovision_reuses_live_mount_without_data_loss(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_retry",
        host_env={},
        overlay_mounter=mounter,
    )
    claude_root = work_dir / "auth" / "ws_retry" / "claude"
    # The agent's writable overlay data accumulated in ``upper`` during the run.
    overlay_data = claude_root / "upper" / "settings.json"
    overlay_data.write_text('{"theme": "agent-edited"}\n')

    # A second provision on the same workspace (auth dir survived a failed stack
    # launch, overlay still mounted) must reuse the live mount, not remount.
    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_retry",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    # No second mount onto the busy mountpoint, so no EBUSY-triggered rmtree.
    assert len(mounter.mounts) == 1
    # The writable upper layer (the agent's overlay data) is preserved, and no
    # full-copy fallback tree was written.
    assert overlay_data.read_text() == '{"theme": "agent-edited"}\n'
    assert not (claude_root / ".claude").exists()


@pytest.mark.unit
def test_live_mount_reuse_records_pin_when_marker_missing(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_nopin",
        host_env={},
        overlay_mounter=mounter,
    )
    claude_root = work_dir / "auth" / "ws_nopin" / "claude"
    # Simulate a worker killed after ``mount()`` succeeded but before (or while)
    # the base-signature marker was written: the overlay stays live on disk while
    # ``base.signature`` is missing.
    (claude_root / "base.signature").unlink()
    overlay_data = claude_root / "upper" / "settings.json"
    overlay_data.write_text('{"theme": "agent-edited"}\n')

    # The retry reaches the idempotent live-mount branch (no remount) but must
    # still persist the pin so a later teardown + host change remounts the
    # surviving upper against this exact base rather than a recomputed one.
    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_nopin",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    # No second mount onto the busy mountpoint — the live mount was reused.
    assert len(mounter.mounts) == 1
    # The pin is now recorded, matching the base the live overlay runs against.
    assert (claude_root / "base.signature").read_text() == _host_claude_signature(host_home)
    assert overlay_data.read_text() == '{"theme": "agent-edited"}\n'


@pytest.mark.unit
def test_live_mount_reuse_pins_actual_base_when_host_changed(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_killpin",
        host_env={},
        overlay_mounter=mounter,
    )
    claude_root = work_dir / "auth" / "ws_killpin" / "claude"
    signature_a = _host_claude_signature(host_home)
    base_a = _shared_claude_base_dir(work_dir, signature_a)
    overlay_data = claude_root / "upper" / "settings.json"
    overlay_data.write_text('{"theme": "agent-edited"}\n')
    # Worker killed after ``mount()`` (against base A) but before the pin: the
    # overlay stays live on disk while ``base.signature`` is missing.
    (claude_root / "base.signature").unlink()

    # The operator edits ``~/.claude`` before the retry, so a signature recomputed
    # from the host now names a *different* base than the one the live overlay is
    # actually mounted against.
    (host_home / ".claude" / "settings.json").write_text('{"theme": "light"}\n')
    signature_b = _host_claude_signature(host_home)
    assert signature_b != signature_a

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_killpin",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    # The live mount was reused — no remount onto the busy mountpoint.
    assert len(mounter.mounts) == 1
    # The pin records the base the live overlay is *actually* mounted against
    # (the original base A recovered from the live mount), never the guessed base B
    # recomputed from the changed host. A later teardown + remount therefore reuses
    # the correct lowerdir instead of tripping an upper/base mismatch.
    assert (claude_root / "base.signature").read_text() == signature_a
    assert base_a != _shared_claude_base_dir(work_dir, signature_b)
    assert overlay_data.read_text() == '{"theme": "agent-edited"}\n'


@pytest.mark.unit
def test_live_mount_reuse_reconciles_against_actual_base_when_host_changed(
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    # An extra host file present at provision time becomes part of base A.
    (host_home / ".claude" / "keeper.json").write_text('{"k": 1}\n')
    mounter = FakeOverlayMounter(supported=True)

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_recon",
        host_env={},
        overlay_mounter=mounter,
    )
    claude_root = work_dir / "auth" / "ws_recon" / "claude"
    signature_a = _host_claude_signature(host_home)
    base_a = _shared_claude_base_dir(work_dir, signature_a)

    # The agent accumulated writable overlay data, making ``upper`` non-empty so the
    # surviving overlay overrides the legacy-copy guard on the retry.
    (claude_root / "upper" / "agent.json").write_text('{"agent": true}\n')

    # A transient remount failure on a prior provision degraded to a *legacy full
    # copy* the agent then mutated. Reconstruct that copy: an unedited baseline file
    # matching base A (``copy2`` preserves its mtime), plus one genuine fallback edit
    # that is absent from base A.
    legacy = claude_root / ".claude"
    legacy.mkdir(parents=True)
    shutil.copy2(base_a / "keeper.json", legacy / "keeper.json")
    (legacy / "edited.json").write_text('{"fallback": "edit"}\n')

    # Worker killed after ``mount()`` (against base A) but before the pin write.
    (claude_root / "base.signature").unlink()

    # The operator *removed* ``keeper.json`` from the host before the retry, so a base
    # recomputed from the current host (base B) lacks it entirely and has a different
    # signature than the live overlay's actual base A.
    (host_home / ".claude" / "keeper.json").unlink()
    signature_b = _host_claude_signature(host_home)
    assert signature_b != signature_a

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_recon",
        host_env={},
        overlay_mounter=mounter,
    )

    upper = claude_root / "upper"
    merged = claude_root / "merged"
    # Reconciliation compared legacy files against the base the live mount *actually*
    # uses (base A, recovered from the mount), not the freshly recomputed base B that
    # is missing ``keeper.json``. The unedited baseline file therefore stays out of the
    # overlay — comparing against base B would have mis-copied it as a "new" edit.
    assert not (merged / "keeper.json").exists()
    assert not (upper / "keeper.json").exists()
    # A genuine fallback edit (absent from base A) is still forwarded — written through
    # the live ``merged`` mount (in production the kernel copies it up into ``upper``;
    # the fake mounter has no real copy-up, so it lands in ``merged`` here).
    assert (merged / "edited.json").read_text() == '{"fallback": "edit"}\n'
    # The legacy copy is reaped once reconciled.
    assert not legacy.exists()
    # The pin records the base the live overlay is actually mounted against.
    assert (claude_root / "base.signature").read_text() == signature_a


@pytest.mark.unit
def test_live_mount_reuse_skips_pin_when_lowerdir_unrecoverable(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)

    class UnrecoverableLowerdirMounter(FakeOverlayMounter):
        """A live overlay whose lowerdir cannot be recovered from the mount table."""

        def active_lowerdir(self, merged: Path) -> Path | None:
            return None

    mounter = UnrecoverableLowerdirMounter(supported=True)
    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_norecover",
        host_env={},
        overlay_mounter=mounter,
    )
    claude_root = work_dir / "auth" / "ws_norecover" / "claude"
    (claude_root / "base.signature").unlink()

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_norecover",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    # No pin is guessed when the live overlay's base cannot be recovered; a later
    # teardown + retry recomputes from the host instead of locking to a guess.
    assert not (claude_root / "base.signature").exists()


@pytest.mark.unit
def test_live_mount_reuse_skips_pin_when_lowerdir_not_a_shared_base(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)

    class StrayLowerdirMounter(FakeOverlayMounter):
        """A live overlay reporting a lowerdir outside the shared-base layout."""

        def active_lowerdir(self, merged: Path) -> Path | None:
            return Path("/somewhere/unexpected/.claude")

    mounter = StrayLowerdirMounter(supported=True)
    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_stray",
        host_env={},
        overlay_mounter=mounter,
    )
    claude_root = work_dir / "auth" / "ws_stray" / "claude"
    (claude_root / "base.signature").unlink()

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_stray",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    # A recovered lowerdir that does not resolve to a shared base under ``work_dir``
    # is not trusted as a pin: write nothing rather than a path we cannot verify.
    assert not (claude_root / "base.signature").exists()


@pytest.mark.unit
def test_live_mount_reuse_defers_reconcile_when_lowerdir_unrecoverable(
    tmp_path: Path,
) -> None:
    """Live overlay reused but its real base is unrecoverable: do not reconcile.

    When ``_live_overlay_pin_signature`` cannot recover the live mount's lowerdir, the
    host-recomputed base is *not* the tree the overlay is mounted against (the host
    changed since the kill). Reconciling the legacy copy's fallback edits against that
    wrong base could copy baseline noise into the overlay or skip a real edit before
    the legacy copy is reaped. So both the reconcile and the reap must be deferred,
    leaving the legacy copy intact for a later provision that can pin the true base.
    """

    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    # An extra host file present at provision time becomes part of base A.
    (host_home / ".claude" / "keeper.json").write_text('{"k": 1}\n')

    class UnrecoverableLowerdirMounter(FakeOverlayMounter):
        """A live overlay whose lowerdir cannot be recovered from the mount table."""

        def active_lowerdir(self, merged: Path) -> Path | None:
            return None

    mounter = UnrecoverableLowerdirMounter(supported=True)
    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_defer",
        host_env={},
        overlay_mounter=mounter,
    )
    claude_root = work_dir / "auth" / "ws_defer" / "claude"
    signature_a = _host_claude_signature(host_home)
    base_a = _shared_claude_base_dir(work_dir, signature_a)

    # The agent accumulated writable overlay data, making ``upper`` non-empty so the
    # surviving overlay overrides the legacy-copy guard on the retry.
    (claude_root / "upper" / "agent.json").write_text('{"agent": true}\n')

    # A transient remount failure on a prior provision degraded to a *legacy full
    # copy* the agent then mutated: an unedited baseline file matching base A plus a
    # genuine fallback edit absent from base A.
    legacy = claude_root / ".claude"
    legacy.mkdir(parents=True)
    shutil.copy2(base_a / "keeper.json", legacy / "keeper.json")
    (legacy / "edited.json").write_text('{"fallback": "edit"}\n')

    # Worker killed after ``mount()`` (against base A) but before the pin write.
    (claude_root / "base.signature").unlink()

    # The operator changed the host so a base recomputed now (base B) differs from the
    # live overlay's actual base A — exactly the case where reconciling against the
    # host guess would be wrong.
    (host_home / ".claude" / "keeper.json").unlink()
    assert _host_claude_signature(host_home) != signature_a

    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_defer",
            host_env={},
            overlay_mounter=mounter,
        )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    # The legacy copy is NOT reaped: its fallback edits are preserved on disk for a
    # later provision that can recover/pin the true base and reconcile correctly.
    assert legacy.exists()
    assert (legacy / "edited.json").read_text() == '{"fallback": "edit"}\n'
    # No reconcile ran against the wrong (host-recomputed) base: neither the genuine
    # edit nor baseline noise was copied into the live overlay.
    merged = claude_root / "merged"
    upper = claude_root / "upper"
    assert not (merged / "edited.json").exists()
    assert not (merged / "keeper.json").exists()
    assert not (upper / "edited.json").exists()
    assert not (upper / "keeper.json").exists()
    # No pin is guessed when the live overlay's base cannot be recovered.
    assert not (claude_root / "base.signature").exists()
    # The deferral is logged (not silent) so an operator can see the legacy copy lingers.
    assert any(e.get("event") == "claude_auth_overlay_reconcile_deferred" for e in logs)


@pytest.mark.unit
def test_live_mount_reuse_defers_reconcile_when_recovered_base_dir_is_gone(
    tmp_path: Path,
) -> None:
    """Live overlay reused but its recovered lowerdir no longer exists on disk.

    A live overlay keeps serving off kernel-held inodes even after its lowerdir
    *path* is removed/renamed on the host, and ``active_lowerdir`` still reports that
    stale path. ``_live_overlay_pin_signature`` resolves it with ``strict=False`` (no
    existence proof), so without the ``is_dir`` guard ``base`` would be realigned to a
    vanished tree and ``_reconcile_fallback_edits_into_upper`` — reading every missing
    ``base[rel]`` as "legacy is newer than base" — would copy the *whole* legacy tree
    into the live overlay. The recovered base must still be a real directory; otherwise
    the reconcile and reap are deferred (and no pin is written).
    """

    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    # An extra host file present at provision time becomes part of base A.
    (host_home / ".claude" / "keeper.json").write_text('{"k": 1}\n')
    mounter = FakeOverlayMounter(supported=True)

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_gone",
        host_env={},
        overlay_mounter=mounter,
    )
    claude_root = work_dir / "auth" / "ws_gone" / "claude"
    signature_a = _host_claude_signature(host_home)
    base_a = _shared_claude_base_dir(work_dir, signature_a)

    # The agent accumulated writable overlay data, making ``upper`` non-empty so the
    # surviving overlay overrides the legacy-copy guard on the retry.
    (claude_root / "upper" / "agent.json").write_text('{"agent": true}\n')

    # A transient remount failure on a prior provision degraded to a *legacy full copy*
    # the agent then mutated: an unedited baseline file matching base A plus a genuine
    # fallback edit absent from base A.
    legacy = claude_root / ".claude"
    legacy.mkdir(parents=True)
    shutil.copy2(base_a / "keeper.json", legacy / "keeper.json")
    (legacy / "edited.json").write_text('{"fallback": "edit"}\n')

    # Worker killed after ``mount()`` (against base A) but before the pin write.
    (claude_root / "base.signature").unlink()

    # The live overlay's lowerdir *path* was removed on the host (the mount lives on via
    # kernel-held inodes, and ``active_lowerdir`` still reports the now-stale path). The
    # operator also changed ``~/.claude`` so a base recomputed now (base B) differs from
    # base A — base A therefore stays gone across the retry rather than being rebuilt.
    (host_home / ".claude" / "keeper.json").unlink()
    assert _host_claude_signature(host_home) != signature_a
    shutil.rmtree(base_a)
    assert not base_a.is_dir()

    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_gone",
            host_env={},
            overlay_mounter=mounter,
        )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    # The legacy copy is NOT reaped and its whole tree is NOT copied into the overlay:
    # a vanished recovered base is treated as untrustworthy, so the reconcile is deferred.
    assert legacy.exists()
    assert (legacy / "edited.json").read_text() == '{"fallback": "edit"}\n'
    merged = claude_root / "merged"
    upper = claude_root / "upper"
    assert not (merged / "keeper.json").exists()
    assert not (merged / "edited.json").exists()
    assert not (upper / "keeper.json").exists()
    assert not (upper / "edited.json").exists()
    # No pin is guessed against a base directory that no longer exists.
    assert not (claude_root / "base.signature").exists()
    # The deferral is logged (not silent) so an operator can see the legacy copy lingers.
    assert any(e.get("event") == "claude_auth_overlay_reconcile_deferred" for e in logs)


@pytest.mark.unit
def test_live_mount_reuse_pins_when_work_dir_is_symlinked(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    # ``AWF_WORK_DIR`` reached through a symlink (e.g. a bind-mount alias): the kernel
    # records the live overlay lowerdir in ``/proc/mounts`` in resolved form, while
    # ``_shared_claude_base_dir`` builds the unresolved (symlinked) path.
    real_work = tmp_path / "real-work"
    real_work.mkdir()
    work_dir = tmp_path / "work"
    work_dir.symlink_to(real_work, target_is_directory=True)
    _seed_host_claude(host_home)

    class ResolvedLowerdirMounter(FakeOverlayMounter):
        """Mirror the kernel: ``/proc/mounts`` reports the lowerdir resolved."""

        def active_lowerdir(self, merged: Path) -> Path | None:
            live = super().active_lowerdir(merged)
            return live.resolve(strict=False) if live is not None else None

    mounter = ResolvedLowerdirMounter(supported=True)
    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_symlink",
        host_env={},
        overlay_mounter=mounter,
    )
    claude_root = work_dir / "auth" / "ws_symlink" / "claude"
    signature = _host_claude_signature(host_home)
    # Worker killed after ``mount()`` but before the pin write: overlay stays live
    # while ``base.signature`` is missing.
    (claude_root / "base.signature").unlink()

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_symlink",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    # The live mount was reused — no remount onto the busy mountpoint.
    assert len(mounter.mounts) == 1
    # The pin is recorded even though the resolved live lowerdir and the unresolved
    # ``_shared_claude_base_dir`` path diverge string-wise: both sides are resolved
    # before comparing, so the symlinked ``work_dir`` no longer drops a valid base.
    assert (claude_root / "base.signature").read_text() == signature


# --- legacy-copy completeness marker (#414 PRRT_kwDOSJAM6s6HRNkk) -------------------------


@pytest.mark.unit
def test_legacy_fallback_copy_writes_completeness_marker(tmp_path: Path) -> None:
    # When overlayfs is unavailable the legacy full-copy branch materializes ``.claude``
    # atomically (staging dir + ``replace``) and, because that copy is provably whole,
    # drops the completeness marker. A later overlay-reconcile consults the marker to
    # decide whether the copy's absences are confident agent deletions (safe to whiteout)
    # or possibly never-copied files of a partial copy (must stay visible).
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_fallback_marker",
        host_env={},
        overlay_mounter=FakeOverlayMounter(supported=False),
    )

    claude_root = work_dir / "auth" / "ws_fallback_marker" / "claude"
    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / ".claude")
    assert (claude_root / ".claude").is_dir()
    assert (claude_root / auth_mounts_mod._CLAUDE_LEGACY_COMPLETE_MARKER).is_file()


@pytest.mark.unit
def test_write_legacy_complete_marker_swallows_oserror(tmp_path: Path) -> None:
    # ``touch`` on a marker whose parent directory does not exist raises OSError. The
    # helper swallows it: a missing marker only forgoes whiteouting confident deletions
    # (fail-safe — the credential stays visible), so a write fault must never propagate
    # and break provisioning.
    missing_root = tmp_path / "does-not-exist"

    auth_mounts_mod._write_legacy_complete_marker(missing_root)  # must not raise

    assert not (missing_root / auth_mounts_mod._CLAUDE_LEGACY_COMPLETE_MARKER).exists()


def _seed_overlay_reuse_with_deletion_shaped_legacy(
    tmp_path: Path, workspace_id: str
) -> tuple[Path, Path, Path]:
    """Provision an overlay, then stage a legacy ``.claude`` missing a base credential.

    Leaves the workspace one step before a second provision: a surviving non-empty
    ``upper`` (so the legacy-copy guard is overridden and the live overlay remounted),
    a legacy copy that *lacks* ``secret.json`` (present in base + on the unchanged host,
    so its absence reads as a confident agent deletion to the #402 pass), plus a genuine
    fallback edit. Whether that absence is a real deletion (complete copy) or a
    never-copied file (partial copy) is exactly what the completeness marker — which the
    caller writes or omits — disambiguates. Returns ``(host_home, work_dir, claude_root)``.
    """

    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    # A credential present at provision time becomes part of base A and stays on the
    # unchanged host, so the deletion guard's "host == base" precondition holds for it.
    (host_home / ".claude" / "secret.json").write_text("token\n")
    mounter = FakeOverlayMounter(supported=True)

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id=workspace_id,
        host_env={},
        overlay_mounter=mounter,
    )
    claude_root = work_dir / "auth" / workspace_id / "claude"
    base_a = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))

    # Agent accumulated writable overlay data → non-empty ``upper`` overrides the
    # legacy-copy guard so the surviving overlay is remounted and reconciled on retry.
    (claude_root / "upper" / "agent.json").write_text('{"agent": true}\n')

    # A prior transient-fallback provision left a legacy ``.claude`` the agent mutated.
    # Build it as a faithful copy of base A (``copy2`` preserves mtimes so the edit pass
    # skips the unchanged baseline) with exactly ONE difference that reads as a confident
    # deletion: ``secret.json`` removed (present in base + on the unchanged host). Add a
    # genuine fallback edit too. The completeness marker the caller writes/omits decides
    # whether that single absence is forwarded as a whiteout or kept visible.
    legacy = claude_root / ".claude"
    shutil.copytree(base_a, legacy)
    (legacy / "secret.json").unlink()
    (legacy / "edited.json").write_text('{"fallback": "edit"}\n')

    # Worker killed after ``mount()`` (against base A) but before the pin write, so the
    # retry recovers base A from the live mount and reconciles against it.
    (claude_root / "base.signature").unlink()
    return host_home, work_dir, claude_root


@pytest.mark.unit
def test_reconcile_skips_deletions_for_unmarked_partial_legacy_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reviewer's #414 case end-to-end: a reused legacy ``.claude`` with NO completeness
    # marker (it may be a partial pre-atomic-staging copy) whose missing ``secret.json``
    # would otherwise read as a confident deletion. The reconcile must skip the destructive
    # whiteout pass so the still-valid lower credential stays visible, while the always-safe
    # edit forwarding still runs.
    host_home, work_dir, claude_root = _seed_overlay_reuse_with_deletion_shaped_legacy(
        tmp_path, "ws_unmarked_recon"
    )
    assert not (claude_root / auth_mounts_mod._CLAUDE_LEGACY_COMPLETE_MARKER).exists()

    monkeypatch.setattr(reconcile_mod, "_has_cap_mknod", lambda: True)
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(overlay_copy_mod.os, "mknod", _recording_mknod(recorded))

    with capture_logs() as logs:
        resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_unmarked_recon",
            host_env={},
            overlay_mounter=FakeOverlayMounter(supported=True),
        )

    upper = claude_root / "upper"
    # No whiteout was attempted at all — the never-copied credential is NOT hidden.
    assert recorded == []
    assert not (upper / "secret.json").exists()
    # Edits are always forwarded (they can never hide a lower credential).
    assert (claude_root / "merged" / "edited.json").read_text() == '{"fallback": "edit"}\n'
    # The conservative skip is surfaced once for diagnosability.
    assert any(
        entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_DELETION_SKIPPED_INCOMPLETE_LEGACY"
        for entry in logs
    )
    # The legacy copy is still reaped once reconciled.
    assert not (claude_root / ".claude").exists()


@pytest.mark.unit
def test_reconcile_forwards_deletions_for_marked_complete_legacy_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The complement: an identical setup but the legacy copy carries the completeness
    # marker (it was materialized atomically, so it is provably whole and its missing
    # ``secret.json`` is a genuine agent deletion). The reconcile forwards the deletion as
    # a whiteout — the gate suppresses the pass only for unproven copies, never regressing
    # the #402 deletion-forwarding for a proven-complete one.
    host_home, work_dir, claude_root = _seed_overlay_reuse_with_deletion_shaped_legacy(
        tmp_path, "ws_marked_recon"
    )
    (claude_root / auth_mounts_mod._CLAUDE_LEGACY_COMPLETE_MARKER).touch()

    monkeypatch.setattr(reconcile_mod, "_has_cap_mknod", lambda: True)
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(overlay_copy_mod.os, "mknod", _recording_mknod(recorded))

    with capture_logs() as logs:
        resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_marked_recon",
            host_env={},
            overlay_mounter=FakeOverlayMounter(supported=True),
        )

    upper = claude_root / "upper"
    # The genuine agent deletion is forwarded as an overlayfs whiteout.
    assert [entry["name"] for entry in recorded] == ["secret.json"]
    assert (upper / "secret.json").exists()
    # The skip path did not fire for a proven-complete copy.
    assert not any(
        entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_DELETION_SKIPPED_INCOMPLETE_LEGACY"
        for entry in logs
    )


@pytest.mark.unit
def test_overlay_reap_removes_stale_completeness_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reviewer's #414 PRRT_kwDOSJAM6s6HRwl_ case: reaping the reconciled legacy copy
    # when the overlay wins must ALSO remove the completeness marker that lived beside it.
    # The marker's invariant is "present ⟹ *this* ``.claude`` copy is provably complete";
    # leaving it dangling after the copy is gone would let a later partial ``.claude`` (one
    # that never went through the atomic-write path — e.g. a concurrent older-code provision)
    # be falsely vouched for, so ``forward_deletions`` would stay true and the whiteout pass
    # could hide still-valid lower credentials.
    host_home, work_dir, claude_root = _seed_overlay_reuse_with_deletion_shaped_legacy(
        tmp_path, "ws_reap_marker"
    )
    (claude_root / auth_mounts_mod._CLAUDE_LEGACY_COMPLETE_MARKER).touch()

    monkeypatch.setattr(reconcile_mod, "_has_cap_mknod", lambda: True)
    monkeypatch.setattr(overlay_copy_mod.os, "mknod", _recording_mknod([]))

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_reap_marker",
        host_env={},
        overlay_mounter=FakeOverlayMounter(supported=True),
    )

    # The legacy copy is reaped, and so is its now-dangling completeness marker.
    assert not (claude_root / ".claude").exists()
    assert not (claude_root / auth_mounts_mod._CLAUDE_LEGACY_COMPLETE_MARKER).exists()
