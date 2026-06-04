"""Shared-base + per-workspace overlay isolation for ``~/.claude`` auth.

These tests inject a fake :class:`OverlayMounter` so the overlay/fallback/
teardown branches are exercised without root or a real overlayfs mount. True
kernel overlay semantics are validated operationally and guarded by the
copy fallback; here we assert mount layout, base reuse, isolation, chown
routing, fallback, and unmount-before-remove.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from awf.node import auth_mounts as auth_mounts_mod
from awf.node.auth_mounts import (
    _host_claude_signature,
    _shared_claude_base_dir,
    resolve_service_auth_mounts,
)


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

    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_sig_fail",
            host_env={},
            overlay_mounter=FakeOverlayMounter(supported=True),
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
def test_overlay_retry_after_teardown_pins_original_base_when_host_changed(
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_reboot",
        host_env={},
        overlay_mounter=mounter,
    )
    base_a = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    claude_root = work_dir / "auth" / "ws_reboot" / "claude"
    # The agent accumulated writable overlay data in ``upper`` during the run.
    overlay_data = claude_root / "upper" / "settings.json"
    overlay_data.write_text('{"theme": "agent-edited"}\n')

    # The merged mount is gone (e.g. a host reboot) but ``upper``/``work`` survive
    # on disk, and the operator updated ``~/.claude`` before the retry.
    mounter.mounted.clear()
    (host_home / ".claude" / "settings.json").write_text('{"theme": "light"}\n')
    base_b = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    assert base_b != base_a

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_reboot",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    # The remount pins the *original* base the surviving upper was built against,
    # never the recomputed base from the changed host — so no config leak and no
    # upper/work mismatch that would rmtree the agent's mutations.
    assert mounter.mounts[-1]["lowerdir"] == base_a
    assert mounter.mounts[-1]["lowerdir"] != base_b
    # The changed-host base is never even built on the pinned retry.
    assert not base_b.is_dir()
    assert overlay_data.read_text() == '{"theme": "agent-edited"}\n'
    assert not (claude_root / ".claude").exists()


@pytest.mark.unit
def test_overlay_retry_rebuilds_when_pinned_base_missing(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_basegone",
        host_env={},
        overlay_mounter=mounter,
    )
    base_a = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    claude_root = work_dir / "auth" / "ws_basegone" / "claude"
    overlay_data = claude_root / "upper" / "settings.json"
    overlay_data.write_text('{"theme": "agent-edited"}\n')

    # The overlay is torn down and the pinned base no longer exists on disk (a
    # future reaper removed it). With nothing to pin to, the retry must rebuild a
    # fresh base from the current host rather than failing.
    mounter.mounted.clear()
    shutil.rmtree(base_a)

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_basegone",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    # A fresh base (current signature, same content) is rebuilt and used.
    assert mounter.mounts[-1]["lowerdir"] == base_a
    assert base_a.is_dir()
    assert overlay_data.read_text() == '{"theme": "agent-edited"}\n'


@pytest.mark.unit
def test_overlay_retry_without_pin_marker_recomputes_base(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    # An overlay left behind by a pre-pin build: ``upper``/``work`` exist but no
    # base-signature marker was recorded, so the original base is unknowable.
    claude_root = work_dir / "auth" / "ws_nomarker" / "claude"
    (claude_root / "upper").mkdir(parents=True)
    (claude_root / "work").mkdir(parents=True)

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_nomarker",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    # Falls back to the current-host base and records the marker for next time.
    base = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    assert mounter.mounts[-1]["lowerdir"] == base
    assert (claude_root / "base.signature").read_text() == _host_claude_signature(host_home)


@pytest.mark.unit
def test_overlay_capable_retry_preserves_prior_legacy_copy(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)

    # First provision predates overlay support (legacy/pre-upgrade): a
    # per-workspace ``.claude`` copy is written and the agent mutates it.
    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_upgrade",
        host_env={},
        overlay_mounter=FakeOverlayMounter(supported=False),
    )
    claude_root = work_dir / "auth" / "ws_upgrade" / "claude"
    legacy_copy = claude_root / ".claude" / "settings.json"
    assert legacy_copy.read_text() == '{"theme": "dark"}\n'
    legacy_copy.write_text('{"theme": "agent-edited"}\n')

    # AWF is upgraded and overlay support becomes available on the retry. The
    # existing legacy copy (with the agent's mutations) must be reused, not
    # dropped for a fresh shared-base overlay that would seed from the host.
    mounter = FakeOverlayMounter(supported=True)
    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_upgrade",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    # The mount keeps pointing at the legacy copy and no overlay is mounted.
    assert by_target["/home/agent/.claude"].source == str(claude_root / ".claude")
    assert mounter.mounts == []
    assert not (claude_root / "merged").exists()
    # The agent's mutation survives the retry rather than being overwritten.
    assert legacy_copy.read_text() == '{"theme": "agent-edited"}\n'


@pytest.mark.unit
def test_mount_ebusy_after_concurrent_mount_reuses_live_overlay(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)

    class RacingOverlayMounter(FakeOverlayMounter):
        """Models a concurrent provision winning the mount race.

        ``is_mounted`` is false at the pre-check, then ``mount`` simulates a
        concurrent caller having mounted the same ``merged`` path in the window
        (the overlay becomes live) and our own attempt colliding with EBUSY.
        """

        def mount(self, *, lowerdir: Path, upperdir: Path, workdir: Path, merged: Path) -> None:
            # The racing winner's overlay is now live at ``merged`` ...
            self.mounted.add(Path(merged))
            # ... and our attempt onto the busy mountpoint fails.
            raise OSError("device or resource busy")

    mounter = RacingOverlayMounter(supported=True)
    claude_root = work_dir / "auth" / "ws_race_mount" / "claude"
    # Stand in for the writable layer the racing winner accumulated.
    upper_data = claude_root / "upper" / "settings.json"

    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_race_mount",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    # The live overlay is reused rather than torn down on EBUSY ...
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    assert by_target["/home/agent/.claude"].mode == "rw"
    # ... so the writable upper layer survives and no full-copy fallback ran.
    assert (claude_root / "upper").is_dir()
    assert (claude_root / "work").is_dir()
    assert not (claude_root / ".claude").exists()
    # Sanity: a marker written into ``upper`` would not be deleted by the handler.
    upper_data.write_text('{"theme": "race-winner"}\n')
    assert upper_data.read_text() == '{"theme": "race-winner"}\n'


@pytest.mark.unit
def test_mount_ebusy_after_concurrent_mount_pins_actual_base_when_host_changed(
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)

    # First provision establishes a live overlay against base A, then the racing
    # winner is modelled as killed before its post-mount pin write: ``upper`` is
    # live on disk and ``base.signature`` is missing when the retry runs.
    seed_mounter = FakeOverlayMounter(supported=True)
    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_race_pin",
        host_env={},
        overlay_mounter=seed_mounter,
    )
    claude_root = work_dir / "auth" / "ws_race_pin" / "claude"
    signature_a = _host_claude_signature(host_home)
    base_a = _shared_claude_base_dir(work_dir, signature_a)
    (claude_root / "base.signature").unlink()

    # The operator edits ``~/.claude`` before the retry, so a signature recomputed
    # from the host now names a *different* base than the one the live overlay (the
    # racing winner's) is actually mounted against.
    (host_home / ".claude" / "settings.json").write_text('{"theme": "light"}\n')
    signature_b = _host_claude_signature(host_home)
    assert signature_b != signature_a

    class RacingOverlayMounter(FakeOverlayMounter):
        """The pre-check sees ``merged`` unmounted; our ``mount`` then loses the
        race to a concurrent provision that wins the mount (against base A) and
        collides with EBUSY."""

        def mount(self, *, lowerdir: Path, upperdir: Path, workdir: Path, merged: Path) -> None:
            # The racing winner's live overlay runs against the original base A,
            # not the base recomputed from the since-changed host.
            self.mounts.append(
                {"lowerdir": base_a, "upperdir": upperdir, "workdir": workdir, "merged": merged}
            )
            self.mounted.add(Path(merged))
            raise OSError("device or resource busy")

    mounter = RacingOverlayMounter(supported=True)
    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_race_pin",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    # The pin records the base the live overlay is *actually* mounted against
    # (base A recovered from the mount), never the guessed base B from the changed
    # host — so a later teardown + remount reuses the correct lowerdir instead of
    # tripping the upper/base mismatch whose failure path ``rmtree``s the agent's
    # overlay mutations.
    assert (claude_root / "base.signature").read_text() == signature_a
    assert base_a != _shared_claude_base_dir(work_dir, signature_b)


@pytest.mark.unit
def test_mount_ebusy_after_concurrent_mount_skips_pin_when_lowerdir_unrecoverable(
    tmp_path: Path,
) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)

    seed_mounter = FakeOverlayMounter(supported=True)
    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_race_norecover",
        host_env={},
        overlay_mounter=seed_mounter,
    )
    claude_root = work_dir / "auth" / "ws_race_norecover" / "claude"
    (claude_root / "base.signature").unlink()

    class UnrecoverableRacingMounter(FakeOverlayMounter):
        """Wins the race (overlay live, EBUSY for us) but exposes no lowerdir."""

        def mount(self, *, lowerdir: Path, upperdir: Path, workdir: Path, merged: Path) -> None:
            self.mounted.add(Path(merged))
            raise OSError("device or resource busy")

        def active_lowerdir(self, merged: Path) -> Path | None:
            return None

    mounter = UnrecoverableRacingMounter(supported=True)
    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_race_norecover",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    # No pin is guessed when the racing winner's base cannot be recovered; a later
    # teardown + retry recomputes from the host instead of locking to a guess.
    assert not (claude_root / "base.signature").exists()


@pytest.mark.unit
def test_transient_mount_failure_preserves_surviving_upper(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_transient",
        host_env={},
        overlay_mounter=mounter,
    )
    claude_root = work_dir / "auth" / "ws_transient" / "claude"
    # The agent accumulated writable overlay data in ``upper`` during the run.
    overlay_data = claude_root / "upper" / "settings.json"
    overlay_data.write_text('{"theme": "agent-edited"}\n')

    # The overlay is torn down normally (``upper``/``work`` persist on disk) and a
    # later provision retry hits a *transient* mount failure with ``merged`` not
    # mounted. The cleanup must not wipe the surviving ``upper``/``work`` layers
    # (the agent's mutations); only the unused ``merged`` mountpoint is removed and
    # we degrade to the legacy copy so the retry can later recover the overlay.
    mounter.mounted.clear()
    mounter._mount_error = OSError("transient remount failure")

    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_transient",
            host_env={},
            overlay_mounter=mounter,
        )

    by_target = {m.target: m for m in mounts}
    # Degraded to the legacy full copy for this provision ...
    assert by_target["/home/agent/.claude"].source == str(claude_root / ".claude")
    assert any(entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_UNAVAILABLE" for entry in logs)
    # ... but the agent's surviving overlay mutations are intact for a future
    # retry to remount, and the unused mountpoint is cleaned up.
    assert overlay_data.read_text() == '{"theme": "agent-edited"}\n'
    assert (claude_root / "work").is_dir()
    assert not (claude_root / "merged").exists()


@pytest.mark.unit
def test_retry_after_transient_fallback_remounts_surviving_upper(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    # Provision 1: overlay succeeds and the agent mutates the writable ``upper``.
    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_recover",
        host_env={},
        overlay_mounter=mounter,
    )
    claude_root = work_dir / "auth" / "ws_recover" / "claude"
    overlay_data = claude_root / "upper" / "settings.json"
    overlay_data.write_text('{"theme": "agent-edited"}\n')

    # Provision 2: teardown leaves ``upper``/``work`` on disk, then a transient
    # remount failure degrades to a *fresh* legacy ``.claude`` copy (no mutations).
    mounter.mounted.clear()
    mounter._mount_error = OSError("transient remount failure")
    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_recover",
        host_env={},
        overlay_mounter=mounter,
    )
    # The fresh legacy copy now exists alongside the surviving overlay ``upper``.
    assert (claude_root / ".claude").is_dir()
    assert overlay_data.read_text() == '{"theme": "agent-edited"}\n'

    # Provision 3: the mount works again. The surviving overlay ``upper`` must be
    # remounted (recovering the agent's mutations) rather than skipped in favor of
    # the stale fresh legacy copy created by the transient failure.
    mounter.mounted.clear()
    mounter._mount_error = None
    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_recover",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    # Auth is served from the live overlay (``merged``), not the stale legacy copy.
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    # The remount reused the surviving ``upper`` carrying the agent's mutations.
    call = mounter.mounts[-1]
    assert call["upperdir"] == claude_root / "upper"
    assert overlay_data.read_text() == '{"theme": "agent-edited"}\n'
    # The stale fresh legacy copy from provision 2 is now unmounted dead weight
    # (~1.7 GB) superseded by the live overlay — it must be reaped, not orphaned.
    assert not (claude_root / ".claude").exists()


@pytest.mark.unit
def test_empty_surviving_upper_does_not_shadow_mutated_legacy_copy(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)

    # Provision 1: the very first overlay attempt fails its mount, so ``upper`` is
    # created on disk but never goes live — it stays *empty*. Provisioning degrades
    # to a fresh legacy ``.claude`` copy, which the agent then mutates.
    mounter = FakeOverlayMounter(supported=True, mount_error=OSError("transient mount failure"))
    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_empty_upper",
        host_env={},
        overlay_mounter=mounter,
    )
    claude_root = work_dir / "auth" / "ws_empty_upper" / "claude"
    # An empty leftover upper survives alongside the mutated legacy copy.
    assert (claude_root / "upper").is_dir()
    assert not any((claude_root / "upper").iterdir())
    legacy_copy = claude_root / ".claude" / "settings.json"
    assert legacy_copy.read_text() == '{"theme": "dark"}\n'
    legacy_copy.write_text('{"theme": "agent-edited"}\n')

    # Provision 2: the mount works again. The empty surviving ``upper`` carries no
    # agent data, so it must NOT override the legacy-copy guard and shadow the
    # mutated legacy copy behind a fresh shared-base overlay. The legacy copy (with
    # the agent's mutations) must keep serving auth.
    mounter._mount_error = None
    mounter.mounted.clear()
    mounts = resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_empty_upper",
        host_env={},
        overlay_mounter=mounter,
    )

    by_target = {m.target: m for m in mounts}
    # Auth keeps pointing at the legacy copy; no overlay is mounted over the empty
    # upper, so the agent's mutations are not hidden.
    assert by_target["/home/agent/.claude"].source == str(claude_root / ".claude")
    assert mounter.mounts == []
    assert legacy_copy.read_text() == '{"theme": "agent-edited"}\n'
