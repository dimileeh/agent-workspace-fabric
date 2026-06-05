"""Shared-base + per-workspace overlay isolation for ``~/.claude`` auth (part 3).

Continuation of :mod:`test_claude_auth_overlay_part_002`; covers the
``/proc/mounts`` lowerdir-discovery helpers (now in
:mod:`awf.node.auth_mounts_overlay`, re-exported through ``auth_mounts``) and the
fallback-edit reconcile branches. The shared :class:`FakeOverlayMounter` and
``_seed_host_claude`` helper are imported from :mod:`test_claude_auth_overlay_part_001`.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

# Patch the module that defines the overlay/Claude helpers (see part_001 header).
from awf.node import auth_mounts_claude as auth_mounts_mod
from awf.node import auth_mounts_claude_reconcile as reconcile_mod
from awf.node import auth_mounts_overlay_copy as overlay_copy_mod
from awf.node.auth_mounts import (
    _reconcile_fallback_edits_into_upper,
)

from .test_claude_auth_overlay_part_001 import (
    FakeOverlayMounter as FakeOverlayMounter,
)
from .test_claude_auth_overlay_part_001 import (
    _seed_host_claude as _seed_host_claude,
)


def _recording_mknod(recorded: list[dict[str, object]]):
    """Return a fake ``os.mknod`` recording its args and creating a placeholder.

    A real overlayfs whiteout is a char device 0,0, which ``mknod`` can only create
    with ``CAP_MKNOD``/root — unavailable in unit tests. The fake records the mode /
    device / leaf name the production code requested and materializes a 0-byte
    placeholder at the same ``dir_fd``-relative location so callers can assert the
    whiteout landed in ``upper`` without real privileges.
    """

    real_open = os.open

    def _fake_mknod(
        path: object, mode: int = 0o600, device: int = 0, *, dir_fd: int | None = None
    ) -> None:
        recorded.append({"name": os.fspath(path), "mode": mode, "device": device})
        fd = real_open(os.fspath(path), os.O_WRONLY | os.O_CREAT, 0o000, dir_fd=dir_fd)
        os.close(fd)

    return _fake_mknod


@pytest.mark.unit
def test_iter_overlay_lowerdirs_collects_all_overlays(tmp_path: Path) -> None:
    base_one = tmp_path / "_shared" / "claude-base" / "sig1" / ".claude"
    base_two = tmp_path / "_shared" / "claude-base" / "sig2" / ".claude"
    # A space in a path is octal-escaped in ``/proc/mounts`` and must be decoded.
    spaced = tmp_path / "weird dir" / ".claude"
    escaped_spaced = str(spaced).replace(" ", "\\040")
    proc_mounts = tmp_path / "mounts"
    proc_mounts.write_text(
        "proc /proc proc rw 0 0\n"
        f"overlay {tmp_path / 'm1'} overlay rw,lowerdir={base_one},upperdir=/u,workdir=/w 0 0\n"
        f"ext4 /data ext4 rw 0 0\n"
        f"overlay {tmp_path / 'm2'} overlay rw,lowerdir={base_two}:{escaped_spaced} 0 0\n"
    )

    from awf.node.auth_mounts import iter_overlay_lowerdirs

    assert iter_overlay_lowerdirs(proc_mounts) == {base_one, base_two, spaced}


@pytest.mark.unit
def test_iter_overlay_lowerdirs_decodes_octal_colon_in_path(tmp_path: Path) -> None:
    # A literal colon in a lowerdir path is octal-escaped as ``\072`` by overlayfs
    # (its raw ``:`` is reserved for joining layers), so the split-on-``:`` keeps the
    # path whole and the escape must be decoded back to ``:``. Otherwise GC-B's
    # protected set holds ``...\072...`` instead of the real path and reaps a base
    # still backing a live overlay. (PRRT_kwDOSJAM6s6HN8ld)
    colon_base = Path("/srv/weird:dir/_shared/claude-base/sig/.claude")
    escaped = str(colon_base).replace(":", "\\072")
    proc_mounts = tmp_path / "mounts"
    proc_mounts.write_text(
        f"overlay {tmp_path / 'm'} overlay rw,lowerdir={escaped},upperdir=/u,workdir=/w 0 0\n"
    )

    from awf.node.auth_mounts import iter_overlay_lowerdirs

    assert iter_overlay_lowerdirs(proc_mounts) == {colon_base}


@pytest.mark.unit
def test_iter_overlay_lowerdirs_decodes_octal_comma_in_path(tmp_path: Path) -> None:
    # The mount-option field is comma-separated, so a literal comma in a lowerdir
    # path is octal-escaped as ``\054`` by overlayfs. The split-on-``,`` over the
    # options keeps the path whole, but the escape must be decoded back to ``,`` or
    # GC-B's protected set holds ``...\054...`` instead of the real path and reaps a
    # base still backing a live overlay. (PRRT_kwDOSJAM6s6HOMbH)
    comma_base = Path("/srv/weird,dir/_shared/claude-base/sig/.claude")
    escaped = str(comma_base).replace(",", "\\054")
    proc_mounts = tmp_path / "mounts"
    proc_mounts.write_text(
        f"overlay {tmp_path / 'm'} overlay rw,lowerdir={escaped},upperdir=/u,workdir=/w 0 0\n"
    )

    from awf.node.auth_mounts import iter_overlay_lowerdirs

    assert iter_overlay_lowerdirs(proc_mounts) == {comma_base}


@pytest.mark.unit
def test_iter_overlay_lowerdirs_handles_missing_and_optionless(tmp_path: Path) -> None:
    from awf.node.auth_mounts import iter_overlay_lowerdirs

    # Missing ``/proc/mounts`` → empty set.
    assert iter_overlay_lowerdirs(tmp_path / "absent") == set()

    # An overlay line carrying no ``lowerdir=`` option contributes nothing.
    proc_mounts = tmp_path / "mounts"
    proc_mounts.write_text(f"overlay {tmp_path / 'm'} overlay rw,upperdir=/u,workdir=/w 0 0\n")
    assert iter_overlay_lowerdirs(proc_mounts) == set()


@pytest.mark.unit
def test_unescape_proc_mount_field_decodes_backslash_last() -> None:
    # ``/proc/mounts`` encodes a literal backslash as ``\134``. A path holding a
    # backslash immediately followed by the digits ``040``/``011``/``012`` —
    # ``/foo\040bar`` — is therefore written ``/foo\134040bar``. Decoding the
    # backslash escape first would leave ``/foo\040bar`` and the space pass would
    # then corrupt it into ``/foo bar``; the non-backslash escapes must decode
    # first so a decoded backslash is never re-read as another escape.
    from awf.node.auth_mounts_claude import _unescape_proc_mount_field

    assert _unescape_proc_mount_field("/foo\\134040bar") == "/foo\\040bar"
    assert _unescape_proc_mount_field("/foo\\134011bar") == "/foo\\011bar"
    assert _unescape_proc_mount_field("/foo\\134012bar") == "/foo\\012bar"
    # The plain escapes still decode correctly.
    assert _unescape_proc_mount_field("a\\040b\\011c\\012d\\134e") == "a b\tc\nd\\e"


@pytest.mark.unit
def test_unescape_proc_mount_field_decodes_octal_colon() -> None:
    # overlayfs uses a raw ``:`` to join layered lowerdirs, so a literal colon
    # inside a single lowerdir path is octal-escaped as ``\072`` (the split-on-``:``
    # in the lowerdir parsers therefore never tears such a path apart). The escape
    # must still be decoded back to ``:`` here, or a base path containing a colon
    # would come back as ``...\072...`` and fail the ``base_root`` match in
    # ``_protected_signature_dirs`` — letting GC-B reap a base that still backs a
    # live overlay. (PRRT_kwDOSJAM6s6HN8ld)
    from awf.node.auth_mounts_claude import _unescape_proc_mount_field

    assert _unescape_proc_mount_field("/foo\\072bar") == "/foo:bar"
    # A literal backslash followed by the text ``072`` is written ``\134072`` and
    # must round-trip to ``/foo\072bar`` (the backslash, not a decoded colon) —
    # decoding the colon escape before the backslash keeps that distinction.
    assert _unescape_proc_mount_field("/foo\\134072bar") == "/foo\\072bar"
    # A literal backslash immediately followed by a colon is ``\134\072`` and must
    # decode to a backslash then a colon.
    assert _unescape_proc_mount_field("/foo\\134\\072bar") == "/foo\\:bar"


@pytest.mark.unit
def test_unescape_proc_mount_field_decodes_octal_comma() -> None:
    # The mount-option field is comma-separated, so a literal comma inside a single
    # lowerdir path is octal-escaped as ``\054`` (the split-on-``,`` over options
    # therefore never tears such a path apart). The escape must still be decoded
    # back to ``,`` here, or a base path containing a comma would come back as
    # ``...\054...`` and fail the ``base_root`` match in ``_protected_signature_dirs``
    # — letting GC-B reap a base that still backs a live overlay. (PRRT_kwDOSJAM6s6HOMbH)
    from awf.node.auth_mounts_claude import _unescape_proc_mount_field

    assert _unescape_proc_mount_field("/foo\\054bar") == "/foo,bar"
    # A literal backslash followed by the text ``054`` is written ``\134054`` and
    # must round-trip to ``/foo\054bar`` (the backslash, not a decoded comma) —
    # decoding the comma escape before the backslash keeps that distinction.
    assert _unescape_proc_mount_field("/foo\\134054bar") == "/foo\\054bar"
    # A literal backslash immediately followed by a comma is ``\134\054`` and must
    # decode to a backslash then a comma.
    assert _unescape_proc_mount_field("/foo\\134\\054bar") == "/foo\\,bar"


@pytest.mark.unit
def test_reconcile_skips_unstattable_legacy_file(tmp_path: Path) -> None:
    # A legacy entry whose ``stat`` fails (a dangling symlink) is skipped, never
    # fatal, and copies nothing.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    (legacy / "dangling").symlink_to(tmp_path / "missing-target")

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    assert list(merged.iterdir()) == []
    assert list(upper.iterdir()) == []


@pytest.mark.unit
def test_reconcile_upper_wins_ties(tmp_path: Path) -> None:
    # The base lacks the file (so it reads as a fallback edit), but ``upper`` already
    # holds an equal-or-newer version — the agent's authoritative overlay-era change.
    # Only a *strictly newer* legacy edit may overwrite it, so the tie leaves ``upper``
    # untouched and nothing is forwarded through ``merged``.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    (legacy / "f").write_text("legacy edit\n")
    (upper / "f").write_text("upper edit\n")
    legacy_mtime_ns = (legacy / "f").stat().st_mtime_ns
    os.utime(upper / "f", ns=(legacy_mtime_ns, legacy_mtime_ns))  # equal mtimes

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    assert (upper / "f").read_text() == "upper edit\n"
    assert not (merged / "f").exists()


@pytest.mark.unit
def test_reconcile_compares_upper_symlink_own_mtime_not_target(tmp_path: Path) -> None:
    # ``upper`` is agent-controlled and can hold a planted symlink. The generation
    # comparison for the "upper wins ties" rule must use the overlay entry's *own*
    # mtime (``lstat``), not the symlink target's (``stat``): the target's mtime is
    # unrelated to when the agent made the overlay-era change. Here the upper symlink's
    # own mtime is older than the legacy fallback edit while its target is newer, so the
    # strictly-newer legacy edit must win and be forwarded. With the buggy ``stat`` the
    # target's newer mtime would wrongly suppress the edit.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    outside = tmp_path / "outside"
    for directory in (legacy, merged, upper, base, outside):
        directory.mkdir()
    (legacy / "f").write_text("legacy edit\n")
    legacy_mtime_ns = (legacy / "f").stat().st_mtime_ns
    # The symlink target carries a *newer* mtime — it must NOT be what we compare.
    target = outside / "target"
    target.write_text("newer target\n")
    os.utime(target, ns=(legacy_mtime_ns + 1_000_000, legacy_mtime_ns + 1_000_000))
    # The agent-planted upper symlink, whose own (lstat) mtime predates the legacy edit.
    (upper / "f").symlink_to(target)
    os.utime(
        upper / "f",
        ns=(legacy_mtime_ns - 1_000_000, legacy_mtime_ns - 1_000_000),
        follow_symlinks=False,
    )

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    # The strictly-newer legacy edit wins and is forwarded through ``merged``.
    assert (merged / "f").read_text() == "legacy edit\n"


@pytest.mark.unit
def test_reconcile_per_file_copy_error_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A per-file copy failure is best-effort: it is swallowed so reconciliation never
    # blocks provisioning.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    (legacy / "f").write_text("fallback edit\n")  # base lacks it, upper lacks it

    def _copy_fails(src: object, dst: object, *args: object, **kwargs: object) -> object:
        raise OSError("No space left on device")

    # The fd-based safe copy streams content via ``shutil.copyfileobj``; a mid-stream
    # write failure must be swallowed so reconciliation never blocks provisioning.
    monkeypatch.setattr(auth_mounts_mod.shutil, "copyfileobj", _copy_fails)

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    # Best-effort: the failure is swallowed and nothing is forwarded into the live
    # overlay's underlying ``upper`` (as with a partial ``copy2``, an empty leaf may be
    # left in ``merged`` — never any content, never an exception).
    assert not (upper / "f").exists()
    assert (merged / "f").read_text() == ""


@pytest.mark.unit
def test_reconcile_preserves_destination_when_source_vanishes_after_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression for the ``_safe_overlay_copy`` ftruncate-ordering fix: if the legacy
    # source disappears in the window after the atomic source ``os.open`` would have run
    # but before its read fd is acquired, the pre-existing destination (the agent's
    # overlay-era edit, visible through the live ``merged`` mount) must be left intact,
    # never zeroed by an early ``ftruncate``.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    (legacy / "f").write_text("legacy edit\n")  # base lacks it -> fallback edit
    # An older ``upper`` entry so the strictly-newer legacy edit is selected to forward.
    (upper / "f").write_text("agent overlay edit\n")
    # Pre-existing destination content seen through the live overlay's ``merged`` mount.
    (merged / "f").write_text("agent overlay edit\n")
    legacy_mtime_ns = (legacy / "f").stat().st_mtime_ns
    os.utime(upper / "f", ns=(legacy_mtime_ns - 1_000_000, legacy_mtime_ns - 1_000_000))

    real_os_open = os.open

    def _os_open_source_vanished(path: object, flags: int, *args: object, **kwargs: object) -> int:
        # Simulate the source being removed in the window before its read fd is held.
        # The source leaf is opened by bare name (``rel.name``) relative to its descended
        # parent ``dir_fd``: it is the only read-only, non-directory ``os.open`` of ``f``
        # (the dir descents use ``O_DIRECTORY``; the destination leaf open uses ``O_CREAT``).
        if os.fspath(path) == "f" and not (flags & (os.O_DIRECTORY | os.O_CREAT)):
            raise OSError("source vanished")
        return real_os_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(auth_mounts_mod.os, "open", _os_open_source_vanished)

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    # The source open failed and was swallowed best-effort; the destination keeps its
    # original content instead of being truncated to empty.
    assert (merged / "f").read_text() == "agent overlay edit\n"


@pytest.mark.unit
def test_reconcile_does_not_create_empty_destination_when_source_open_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression for the source-before-destination open ordering: for a *new* fallback
    # edit (no prior ``upper``/``merged`` entry) the destination must not be opened with
    # ``O_CREAT`` before the source is validated. Otherwise a source-open failure (e.g.
    # the legacy file unlinked in the non-atomic window after the caller's ``is_file()``
    # pre-check) would leave a 0-byte regular file in the overlay upper that silently
    # shadows the base entry — a Claude config could read as empty to the agent.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    (legacy / "f").write_text("legacy edit\n")  # base/upper lack it -> new fallback edit

    real_os_open = os.open

    def _os_open_source_vanished(path: object, flags: int, *args: object, **kwargs: object) -> int:
        # Fail only the source read leaf open — opened by bare name (``rel.name``) relative
        # to its descended parent ``dir_fd`` (the dir descents use ``O_DIRECTORY``; the
        # destination leaf open uses ``O_CREAT``). If the destination were opened first it
        # would already have created the empty file before this fires.
        if os.fspath(path) == "f" and not (flags & (os.O_DIRECTORY | os.O_CREAT)):
            raise OSError("source vanished")
        return real_os_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(auth_mounts_mod.os, "open", _os_open_source_vanished)

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    # No empty file was materialised in the overlay upper to shadow the base entry.
    assert not (merged / "f").exists()


@pytest.mark.unit
def test_reconcile_writes_through_merged_not_directly_into_upper(tmp_path: Path) -> None:
    # A genuine fallback edit (absent from base, absent from upper) is forwarded
    # *through* the live ``merged`` mount, not poked straight into the underlying
    # ``upper`` dir — overlayfs treats external writes to a live overlay's upper tree
    # as undefined behavior that can read back stale/invisible through ``merged``. The
    # real kernel then copies the write up into ``upper``; the test has no real overlay,
    # so the file lands in ``merged`` and ``upper`` stays untouched, proving the write
    # target is ``merged``.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    (legacy / "nested").mkdir()
    (legacy / "nested" / "f").write_text("fallback edit\n")

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    # Forwarded through ``merged`` (including the copied-up parent dir)...
    assert (merged / "nested" / "f").read_text() == "fallback edit\n"
    # ...never written directly into ``upper`` under the live overlay.
    assert not (upper / "nested").exists()


@pytest.mark.unit
def test_reconcile_refuses_symlinked_destination(tmp_path: Path) -> None:
    # The live ``merged`` overlay's upper layer is written by the (untrusted) agent.
    # A prior overlay run may have left an agent-created symlink at the destination
    # ``rel``. ``shutil.copy2`` follows destination symlinks by default, so as the root
    # worker it would write the legacy file's content *through* the link to a target
    # outside the ``.claude`` tree — an arbitrary root-write primitive. The reconcile
    # must refuse to follow it and leave the out-of-tree target untouched.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    outside = tmp_path / "outside"
    for directory in (legacy, merged, upper, base, outside):
        directory.mkdir()
    secret = outside / "secret"
    secret.write_text("original\n")
    # A genuine fallback edit (absent from base and upper) the attacker wants written.
    (legacy / "f").write_text("attacker payload\n")
    # The agent-planted destination symlink, surviving in the live merged mount.
    (merged / "f").symlink_to(secret)

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    # copy2 must NOT have followed the symlink to overwrite the out-of-tree target.
    assert secret.read_text() == "original\n"
    # The planted symlink is skipped, never materialized into a real file.
    assert (merged / "f").is_symlink()


@pytest.mark.unit
def test_reconcile_skips_source_symlink_so_target_is_not_disclosed(tmp_path: Path) -> None:
    # A *fresh* legacy copy is materialized via ``copytree(symlinks=False)`` and so
    # never contains symlinks; any symlink in the legacy tree was planted by the
    # (untrusted) agent during the fallback session (the legacy copy is mounted ``rw``
    # into the agent). ``shutil.copy2`` follows *source* symlinks by default, so as the
    # root worker reconciling later it would read the link target — possibly outside the
    # ``.claude`` tree — and write its contents into the agent-visible overlay, an
    # arbitrary root-read primitive. The destination guard only blocks writes *through* a
    # dest link, so a source link must be skipped: read nothing, copy nothing.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    outside = tmp_path / "outside"
    for directory in (legacy, merged, upper, base, outside):
        directory.mkdir()
    secret = outside / "root-only-secret"
    secret.write_text("root-only contents\n")
    # The agent plants a legacy-copy symlink at a path absent from base/upper pointing at
    # an out-of-tree root-readable target, so it reads as a brand-new "fallback edit".
    (legacy / "stolen").symlink_to(secret)

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    # The link target's contents must NOT be surfaced through the overlay (merged/upper).
    assert not (merged / "stolen").exists()
    assert not (upper / "stolen").exists()
    # The out-of-tree secret is untouched and was never read into the tree.
    assert secret.read_text() == "root-only contents\n"


@pytest.mark.unit
def test_reconcile_refuses_symlinked_parent_component(tmp_path: Path) -> None:
    # The same escape, planted one level up: an agent-created symlink at a *parent*
    # directory of ``rel``. ``mkdir(parents=True)`` would traverse through it and
    # ``copy2`` would then write inside the escaped directory. The reconcile must
    # refuse the symlinked parent and write nothing outside the tree.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    outside = tmp_path / "outside"
    for directory in (legacy, merged, upper, base, outside):
        directory.mkdir()
    (legacy / "nested").mkdir()
    (legacy / "nested" / "f").write_text("attacker payload\n")
    # The agent planted ``nested`` as a symlink to an out-of-tree dir in the upper layer.
    (merged / "nested").symlink_to(outside)

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    # The escaped directory must not receive the forwarded file.
    assert not (outside / "f").exists()
    assert list(outside.iterdir()) == []


@pytest.mark.unit
def test_reconcile_refuses_directory_destination(tmp_path: Path) -> None:
    # The escape via a *directory* destination: the agent (writing the live ``merged``
    # upper layer) created a directory at the same relative path as a legacy regular
    # file, holding an inner symlink whose name matches the legacy basename. ``shutil.copy2``
    # treats a directory ``dst`` as a *containing* directory and writes ``dst / basename(src)``
    # — i.e. through that planted inner symlink — letting the root copy escape the ``.claude``
    # tree. The fd-based open of the leaf is ``O_WRONLY``, so a directory dest fails with
    # ``EISDIR``: it must refuse the directory destination and write nothing outside the tree.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    outside = tmp_path / "outside"
    for directory in (legacy, merged, upper, base, outside):
        directory.mkdir()
    secret = outside / "secret"
    secret.write_text("original\n")
    # A genuine fallback edit (absent from base and upper) the attacker wants written.
    (legacy / "f").write_text("attacker payload\n")
    # The agent planted ``merged/f`` as a directory containing ``f`` -> out-of-tree secret;
    # ``copy2`` into the directory targets ``merged/f/f`` and would follow that inner link.
    (merged / "f").mkdir()
    (merged / "f" / "f").symlink_to(secret)

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    # copy2 must NOT have written through the inner symlink to the out-of-tree target.
    assert secret.read_text() == "original\n"
    # The planted inner symlink is left intact, never materialized into a real file.
    assert (merged / "f" / "f").is_symlink()


@pytest.mark.unit
def test_reconcile_skips_source_fifo_so_root_worker_cannot_block(tmp_path: Path) -> None:
    # The agent can ``mkfifo`` in the ``rw`` legacy copy during the fallback session. A
    # FIFO ``stat``s fine, so ``_safe_mtime_ns`` would treat it as a brand-new fallback
    # edit (absent from base/upper) — but ``shutil.copy2`` then ``open``s the source for
    # reading, and a FIFO with no peer writer blocks the root worker *indefinitely*,
    # hanging provisioning. The source must be skipped before any open: a regular source
    # file is required. (If this regressed, the test itself would hang.)
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    os.mkfifo(legacy / "pipe")

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    # Nothing was read from or forwarded for the FIFO, and it is left intact.
    assert list(merged.iterdir()) == []
    assert list(upper.iterdir()) == []
    assert stat.S_ISFIFO((legacy / "pipe").stat().st_mode)


@pytest.mark.unit
def test_reconcile_refuses_fifo_destination(tmp_path: Path) -> None:
    # The agent (writing the live ``merged`` upper layer) planted a FIFO at the same
    # relative path as a legacy regular-file edit. ``shutil.copy2`` ``open``s ``dst`` for
    # writing, and a FIFO with no peer reader blocks the root worker indefinitely, hanging
    # provisioning. The fd-based open uses ``O_NONBLOCK``, so a reader-less FIFO fails with
    # ``ENXIO`` instead of blocking: it must refuse the FIFO destination and forward
    # nothing. (If this regressed, the test itself would hang.)
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    # A genuine fallback edit (absent from base and upper).
    (legacy / "f").write_text("fallback edit\n")
    # The agent-planted destination FIFO surviving in the live merged mount.
    os.mkfifo(merged / "f")

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    # The FIFO is left intact, never opened/clobbered, and nothing reached ``upper``.
    assert stat.S_ISFIFO((merged / "f").stat().st_mode)
    assert not (upper / "f").exists()


@pytest.mark.unit
def test_reconcile_refuses_fifo_destination_with_a_live_reader(tmp_path: Path) -> None:
    # A FIFO with a *live reader* opens successfully even under ``O_WRONLY | O_NONBLOCK``
    # (the reader-less ``ENXIO`` guard does not catch it), so the ``S_ISREG`` ``fstat``
    # guard is what must refuse it: the root worker only ever writes a regular file, never
    # forwarding content into an agent-planted FIFO. (If this regressed, the test could
    # hang on the write — there is no peer draining the pipe.)
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    (legacy / "f").write_text("fallback edit\n")
    fifo = merged / "f"
    os.mkfifo(fifo)
    # Hold the read end open so the write-side open below succeeds instead of ENXIO.
    reader_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        _reconcile_fallback_edits_into_upper(
            legacy=legacy,
            merged=merged,
            upper=upper,
            base=base,
            host_claude=tmp_path / "host-claude",
        )
    finally:
        os.close(reader_fd)

    # The FIFO is left intact (never written through) and nothing reached ``upper``.
    assert stat.S_ISFIFO(os.lstat(fifo).st_mode)
    assert not (upper / "f").exists()


@pytest.mark.unit
def test_reconcile_forwards_into_a_preexisting_merged_parent_dir(tmp_path: Path) -> None:
    # When the destination's parent directory already exists in the live ``merged``
    # overlay, the fd-based descent's ``os.mkdir`` raises ``FileExistsError`` and is a
    # no-op — it must still re-open the existing real directory under ``O_NOFOLLOW`` and
    # forward the edit into it (a non-symlink existing parent is not a structural
    # conflict).
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    (legacy / "nested").mkdir()
    (legacy / "nested" / "f").write_text("fallback edit\n")
    # The parent dir already exists in ``merged`` (e.g. copied up by an earlier file).
    (merged / "nested").mkdir()

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    assert (merged / "nested" / "f").read_text() == "fallback edit\n"


@pytest.mark.unit
def test_reconcile_skips_usage_history_dirs_absent_from_base(tmp_path: Path) -> None:
    # The shared base is built with ``ignore_patterns(*_CLAUDE_USAGE_HISTORY_DIRS)``, so
    # ``projects/``, ``todos/``, ``shell-snapshots/`` and ``statsig/`` never exist in it.
    # A fallback session's Claude process re-creates them and can fill ``projects/`` with
    # large transcripts; because the base lacks them they all read as ``base_mtime_ns is
    # None`` fallback edits. Reconcile must mirror the base exclusion and not forward those
    # subtrees into the overlay upper, or the shared-base disk savings are negated.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    for directory in (legacy, merged, upper, base):
        directory.mkdir()
    # A genuine edit outside the usage-history dirs is still forwarded.
    (legacy / "settings.json").write_text("real fallback edit\n")
    # Agent-accumulated usage-history subtrees that the base does not track.
    for history_dir in ("projects", "todos", "shell-snapshots", "statsig"):
        (legacy / history_dir).mkdir()
        (legacy / history_dir / "big").write_text("multi-GB transcript\n")

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=tmp_path / "host-claude",
    )

    assert (merged / "settings.json").read_text() == "real fallback edit\n"
    for history_dir in ("projects", "todos", "shell-snapshots", "statsig"):
        assert not (merged / history_dir).exists()
        assert not (upper / history_dir).exists()


@pytest.mark.unit
def test_safe_overlay_copy_refuses_source_symlink_swapped_after_caller_checks(
    tmp_path: Path,
) -> None:
    # Source-side TOCTOU guard: the caller's ``is_symlink()``/``is_file()`` checks in
    # ``_reconcile_fallback_edits_into_upper`` are not atomic with the source open inside
    # ``_safe_overlay_copy``, and the legacy copy is ``rw`` for the agent — so it can swap
    # a checked-clean regular file for a symlink before the open. Calling
    # ``_safe_overlay_copy`` directly with a symlink source models that post-check swap:
    # the atomic ``O_NOFOLLOW`` source open must refuse it (``ELOOP``) so the root worker
    # never follows the link to read an arbitrary host path into the agent-visible tree.
    merged = tmp_path / "merged"
    merged.mkdir()
    src_root = tmp_path / "legacy"
    src_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "root-only-secret"
    secret.write_text("root-only contents\n")
    # The agent swapped the checked-clean ``src_root/f`` leaf for a symlink to an
    # out-of-tree secret after the caller's checks but before this open.
    (src_root / "f").symlink_to(secret)

    auth_mounts_mod._safe_overlay_copy(merged, Path("f"), src_root)

    # The link target's contents are never read into the destination tree. With the
    # source validated *before* the destination is opened, the refused source leaves no
    # 0-byte leaf behind at all, and the out-of-tree secret is untouched.
    assert not (merged / "f").exists()
    assert secret.read_text() == "root-only contents\n"


@pytest.mark.unit
def test_safe_overlay_copy_refuses_source_fifo_swapped_after_caller_checks(
    tmp_path: Path,
) -> None:
    # Source-side TOCTOU guard (FIFO variant): a ``mkfifo`` planted after the caller's
    # ``is_file()`` check must not block the root worker. The atomic source open uses
    # ``O_NONBLOCK`` and the ``S_ISREG`` ``fstat`` guard refuses the FIFO before any read.
    # (If this regressed, the test itself would hang — there is no peer writer.)
    merged = tmp_path / "merged"
    merged.mkdir()
    src_root = tmp_path / "legacy"
    src_root.mkdir()
    src_fifo = src_root / "f"
    os.mkfifo(src_fifo)

    auth_mounts_mod._safe_overlay_copy(merged, Path("f"), src_root)

    # Nothing was read from the FIFO and it never blocked. The source is refused before
    # the destination is opened, so no dest leaf is created at all.
    assert not (merged / "f").exists()
    assert stat.S_ISFIFO(os.lstat(src_fifo).st_mode)


@pytest.mark.unit
def test_safe_overlay_copy_refuses_symlinked_source_parent_component(
    tmp_path: Path,
) -> None:
    # Source-side TOCTOU guard, one level up (PRRT_kwDOSJAM6s6HOvV4): the caller walks a
    # *real* directory ``src_root/nested`` and yields ``nested/f``, but the legacy copy is
    # ``rw`` for the agent, which can swap ``nested`` for ``nested -> /outside`` before the
    # source is read. Opening ``src_root / rel`` by full path would apply ``O_NOFOLLOW``
    # only to the ``f`` leaf, so the redirected ``nested`` parent would still resolve by
    # name and the root worker would read ``/outside/f`` into the agent-visible overlay —
    # an arbitrary root-read primitive. The component-by-component ``O_NOFOLLOW`` descent
    # of the source must refuse the symlinked parent (``ELOOP``) and copy nothing.
    merged = tmp_path / "merged"
    merged.mkdir()
    src_root = tmp_path / "legacy"
    src_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "f"
    secret.write_text("root-only contents\n")
    # The agent planted ``nested`` as a symlink to the out-of-tree dir; the leaf ``f``
    # behind it is a genuine regular file, so only the parent guard can catch this.
    (src_root / "nested").symlink_to(outside)

    auth_mounts_mod._safe_overlay_copy(merged, Path("nested/f"), src_root)

    # The out-of-tree file is never read into the destination tree, and no dest leaf
    # (nor its parent) is materialized through the escaped parent.
    assert not (merged / "nested" / "f").exists()
    assert secret.read_text() == "root-only contents\n"


# --- #404: host-origin vs agent-origin newer legacy mtime ----------------------------


def _mkdirs(*paths: Path) -> None:
    for path in paths:
        path.mkdir()


@pytest.mark.unit
def test_reconcile_keeps_upper_edit_over_unedited_host_changed_legacy(tmp_path: Path) -> None:
    # #404: the host ``~/.claude`` changed *after* the agent made an overlay-era ``upper``
    # edit, then a transient remount failure created a legacy copy *from that changed
    # host*. The legacy copy is byte/mtime-identical to the new host (the agent never
    # touched it) and carries a newer host mtime than the agent's older ``upper`` edit.
    # The mtime-only rule would treat it as a fallback edit and overwrite the agent's
    # overlay-era change. With the host-divergence guard it is recognised as an unedited
    # host copy and the ``upper`` edit is preserved — nothing is forwarded.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    host = tmp_path / "host"
    _mkdirs(legacy, merged, upper, base, host)
    # The agent's overlay-era edit, older than the (later) host change.
    (upper / "settings.json").write_text('{"theme": "agent-overlay-edit"}\n')
    upper_mtime_ns = (upper / "settings.json").stat().st_mtime_ns
    # The host changed after that edit (newer mtime) ...
    (host / "settings.json").write_text('{"theme": "host-changed"}\n')
    os.utime(
        host / "settings.json",
        ns=(upper_mtime_ns + 5_000_000, upper_mtime_ns + 5_000_000),
    )
    host_stat = (host / "settings.json").stat()
    # ... and the legacy copy is a byte/mtime-identical, unedited copy of that new host.
    (legacy / "settings.json").write_text('{"theme": "host-changed"}\n')
    os.utime(legacy / "settings.json", ns=(host_stat.st_mtime_ns, host_stat.st_mtime_ns))

    _reconcile_fallback_edits_into_upper(
        legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
    )

    # The agent's overlay-era edit stands; the unedited host copy was not forwarded.
    assert (upper / "settings.json").read_text() == '{"theme": "agent-overlay-edit"}\n'
    assert not (merged / "settings.json").exists()


@pytest.mark.unit
def test_reconcile_still_forwards_agent_edited_legacy_diverging_from_host(tmp_path: Path) -> None:
    # #404 must not regress the tested fallback re-edit forwarding: when the agent
    # *did* edit the legacy copy during the fallback session, the legacy file diverges
    # from the live host (different content/mtime) and is strictly newer than the
    # ``upper`` edit, so it is forwarded over it exactly as before.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    host = tmp_path / "host"
    _mkdirs(legacy, merged, upper, base, host)
    (upper / "settings.json").write_text('{"theme": "old-overlay-edit"}\n')
    upper_mtime_ns = (upper / "settings.json").stat().st_mtime_ns
    # The live host holds one version ...
    (host / "settings.json").write_text('{"theme": "host"}\n')
    os.utime(host / "settings.json", ns=(upper_mtime_ns, upper_mtime_ns))
    # ... but the agent edited the legacy copy to something else, strictly newer.
    (legacy / "settings.json").write_text('{"theme": "agent-fallback-edit"}\n')
    os.utime(
        legacy / "settings.json",
        ns=(upper_mtime_ns + 5_000_000, upper_mtime_ns + 5_000_000),
    )

    _reconcile_fallback_edits_into_upper(
        legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
    )

    # The agent's strictly-newer fallback edit diverges from the host and is forwarded.
    assert (merged / "settings.json").read_text() == '{"theme": "agent-fallback-edit"}\n'


@pytest.mark.unit
def test_reconcile_forwards_agent_edit_when_host_lacks_file(tmp_path: Path) -> None:
    # When the live host does not hold the path at all, divergence cannot be disproven,
    # so a strictly-newer legacy file is treated as an agent edit and forwarded over an
    # existing ``upper`` regular file — fail safe toward preserving an edit, never
    # dropping/hiding one. (Host-lacking is the common shape the existing fallback
    # forwarding relies on.)
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    host = tmp_path / "host"
    _mkdirs(legacy, merged, upper, base, host)
    (upper / "settings.json").write_text('{"theme": "old"}\n')
    upper_mtime_ns = (upper / "settings.json").stat().st_mtime_ns
    (legacy / "settings.json").write_text('{"theme": "agent-edit"}\n')
    os.utime(
        legacy / "settings.json",
        ns=(upper_mtime_ns + 5_000_000, upper_mtime_ns + 5_000_000),
    )

    _reconcile_fallback_edits_into_upper(
        legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
    )

    assert (merged / "settings.json").read_text() == '{"theme": "agent-edit"}\n'


@pytest.mark.unit
def test_reconcile_forwards_agent_edit_matching_host_mtime_size_but_not_bytes(
    tmp_path: Path,
) -> None:
    # #404 host-divergence guard must compare *content*, not just mtime + size. The agent
    # edited the legacy copy during the fallback session to a *same-length* credential and
    # the edit's timestamp happens to align with the live host's mtime (e.g. ``touch -r``
    # or a coincidental ns match on a fixed-width token rewrite). mtime + size alone read
    # the legacy file as an "unedited host copy" and skip forwarding, reaping the agent's
    # newer credential so it never reaches ``upper``. Byte-for-byte comparison against the
    # live host proves the legacy file diverges, so it is correctly forwarded — symmetric
    # with the #402 deletion pass's content check.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    host = tmp_path / "host"
    _mkdirs(legacy, merged, upper, base, host)
    (upper / "settings.json").write_text('{"theme": "old-overlay-edit-X"}\n')
    upper_mtime_ns = (upper / "settings.json").stat().st_mtime_ns
    # The live host holds one same-length version ...
    (host / "settings.json").write_text('{"theme": "host-credential-A"}\n')
    os.utime(
        host / "settings.json",
        ns=(upper_mtime_ns + 5_000_000, upper_mtime_ns + 5_000_000),
    )
    host_stat = (host / "settings.json").stat()
    # ... but the agent edited the legacy copy to a *different, same-length* credential,
    # strictly newer than ``upper`` yet with an mtime + size identical to the live host.
    (legacy / "settings.json").write_text('{"theme": "host-credential-B"}\n')
    assert (legacy / "settings.json").stat().st_size == host_stat.st_size
    os.utime(legacy / "settings.json", ns=(host_stat.st_mtime_ns, host_stat.st_mtime_ns))

    _reconcile_fallback_edits_into_upper(
        legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
    )

    # The legacy bytes diverge from the host, so the agent's newer credential is forwarded
    # rather than discarded as a phantom "unedited host copy".
    assert (merged / "settings.json").read_text() == '{"theme": "host-credential-B"}\n'


@pytest.mark.unit
def test_safe_agent_file_equal_content_guards_agent_path(tmp_path: Path) -> None:
    # The agent-controlled path is descended from the trusted ``agent_root`` and the leaf
    # opened with ``O_NOFOLLOW | O_NONBLOCK`` + an ``S_ISREG`` ``fstat`` guard so a swapped
    # symlink/FIFO can never follow out of tree or block the root worker; the trusted host
    # arg keeps the plain read.
    root = tmp_path / "root"
    root.mkdir()
    host = tmp_path / "host"
    host.write_bytes(b"token-aaaa\n")
    rel = Path("agent")
    agent = root / rel
    agent.write_bytes(b"token-aaaa\n")
    # Byte-identical regular files compare equal.
    assert overlay_copy_mod._safe_agent_file_equal_content(root, rel, host) is True

    agent.write_bytes(b"token-bbbb\n")  # same length, different bytes
    assert overlay_copy_mod._safe_agent_file_equal_content(root, rel, host) is False

    # A reader-less FIFO swapped in for the agent leaf fails safe to ``False`` WITHOUT
    # blocking: the ``O_RDONLY | O_NONBLOCK`` open succeeds immediately and the ``S_ISREG``
    # ``fstat`` guard rejects it. If this regressed to a plain ``open("rb")`` the read-side
    # open would block forever — the test itself would hang.
    agent.unlink()
    os.mkfifo(agent)
    assert overlay_copy_mod._safe_agent_file_equal_content(root, rel, host) is False

    # A symlink swapped in for the agent leaf is refused by ``O_NOFOLLOW`` (``ELOOP`` ->
    # ``False``), never followed to read the link target.
    agent.unlink()
    agent.symlink_to(host)
    assert overlay_copy_mod._safe_agent_file_equal_content(root, rel, host) is False

    # An absent/unreadable *trusted* path fails safe to ``False`` (the inner ``OSError``).
    agent.unlink()
    agent.write_bytes(b"token-aaaa\n")
    assert overlay_copy_mod._safe_agent_file_equal_content(root, rel, tmp_path / "missing") is False


@pytest.mark.unit
def test_safe_agent_file_equal_content_guards_swapped_parent_component(tmp_path: Path) -> None:
    # PR #414 review (PRRT_kwDOSJAM6s6HQvpP): a full-path ``os.open(..., O_NOFOLLOW)`` only
    # guards the *leaf*, so an agent that swaps an already-walked *parent* directory of the
    # legacy file for a symlink before this read would have the root worker resolve through
    # it and read a file OUTSIDE the ``.claude`` tree. If that out-of-tree file's bytes
    # match the trusted host, the function returned a false ``True`` and the #404 caller
    # silently dropped the genuine fallback edit. The fd-anchored component descent must
    # reject the swapped parent (``ELOOP``) and fail safe to ``False``.
    root = tmp_path / "root"
    real_parent = root / "sub"
    real_parent.mkdir(parents=True)
    rel = Path("sub") / "agent"
    (root / rel).write_bytes(b"agent-edit\n")  # the genuine agent fallback edit

    host = tmp_path / "host"
    host.write_bytes(b"host-bytes\n")
    # An out-of-tree file whose bytes match the host — what the swapped parent redirects to.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "agent").write_bytes(b"host-bytes\n")

    # The agent swaps the walked parent ``root/sub`` for ``sub -> outside`` after the
    # caller's check. A leaf-only ``O_NOFOLLOW`` would follow it and read ``outside/agent``
    # (== host) -> false ``True``; the per-component descent rejects ``sub`` as a symlink.
    import shutil as _shutil

    _shutil.rmtree(real_parent)
    (root / "sub").symlink_to(outside)
    assert overlay_copy_mod._safe_agent_file_equal_content(root, rel, host) is False


@pytest.mark.unit
def test_safe_agent_file_equal_content_closes_fds_on_partial_descent_failure(
    tmp_path: Path,
) -> None:
    # PR #414 review (PRRT_kwDOSJAM6s6HQ6h7): when the agent-path descent fails *after*
    # opening one or more directory fds (e.g. a swapped-symlink leaf several levels deep),
    # the early ``return False`` must still close the directory fds already collected. The
    # cleanup ``finally`` previously covered only the inner read ``try``, so a partial
    # descent leaked every opened dir fd during #404 reconciliation. Repeatedly forcing a
    # mid-descent failure must not grow the process fd table.
    root = tmp_path / "root"
    (root / "a" / "b").mkdir(parents=True)
    rel = Path("a") / "b" / "agent"
    # A symlink leaf: the descent opens the root, ``a`` and ``b`` directory fds, then the
    # ``O_NOFOLLOW`` leaf open fails (``ELOOP``) -> the first ``except`` returns ``False``
    # with three directory fds already collected in ``fds``.
    (root / rel).symlink_to(root / "a" / "b")  # target irrelevant; never followed
    host = tmp_path / "host"
    host.write_bytes(b"x\n")

    fd_dir = Path("/proc/self/fd")
    before = len(list(fd_dir.iterdir()))
    for _ in range(100):
        assert overlay_copy_mod._safe_agent_file_equal_content(root, rel, host) is False
    after = len(list(fd_dir.iterdir()))
    # Every directory fd opened during the failed descent was closed: no fd growth. Without
    # the outer ``try``/``finally`` this leaks three fds per call (~300 over the loop).
    assert after <= before


@pytest.mark.unit
def test_legacy_is_unedited_host_copy_refuses_fifo_swapped_after_caller_check(
    tmp_path: Path,
) -> None:
    # TOCTOU (PR #414 review): the caller's ``is_file()`` pre-check in
    # ``_reconcile_fallback_edits_into_upper`` is not atomic with the #404 content read
    # here, and ``legacy_file`` lives under the agent-writable legacy copy. An agent can
    # ``unlink`` + ``mkfifo`` it in that window; a reader-less FIFO opened with a plain
    # ``open("rb")`` would block the root provisioning worker forever. Calling
    # ``_legacy_is_unedited_host_copy`` directly with the captured regular-file stat but a
    # now-FIFO path models that post-check swap: the stat-gate (mtime + size) still passes,
    # so control reaches the content compare, which must fail safe to ``False`` (legacy not
    # provably unedited -> eligible to forward) WITHOUT hanging. (If this regressed, the
    # test itself would hang.)
    host = tmp_path / "host-file"
    host.write_bytes(b"credential\n")
    host_stat = host.stat()
    legacy_root = tmp_path / "legacy-root"
    legacy_root.mkdir()
    rel = Path("legacy-file")
    legacy = legacy_root / rel
    legacy.write_bytes(b"credential\n")  # same content + size as the host
    os.utime(legacy, ns=(host_stat.st_mtime_ns, host_stat.st_mtime_ns))  # align mtime
    legacy_stat = legacy.stat()  # the regular-file stat the caller captured pre-swap
    legacy.unlink()
    os.mkfifo(legacy)  # the agent swaps the checked-clean file for a reader-less FIFO

    assert reconcile_mod._legacy_is_unedited_host_copy(legacy_root, rel, legacy_stat, host) is False
    # The FIFO is left intact — never read through, never blocked on.
    assert stat.S_ISFIFO(os.lstat(legacy).st_mode)


# --- #414: host-origin guard also protects an ``upper`` whiteout (agent deletion) -----


def _whiteout_stat(mtime_ns: int):
    """A synthetic ``stat`` for an overlayfs whiteout (char device ``0,0``).

    A real whiteout can only be created with ``CAP_MKNOD``/root (unavailable in unit
    tests), so model the ``upper`` entry the #404/#414 guard inspects: a char-device
    ``0,0`` carrying the whiteout's creation mtime. Only the fields the reconcile reads
    on the ``upper`` entry are populated.
    """

    import types

    return types.SimpleNamespace(
        st_mode=stat.S_IFCHR,
        st_rdev=os.makedev(0, 0),
        st_mtime_ns=mtime_ns,
        st_size=0,
    )


def _patch_upper_whiteout_stat(
    monkeypatch: pytest.MonkeyPatch, upper_path: Path, mtime_ns: int
) -> None:
    """Make ``reconcile_mod._safe_stat(upper_path, follow_symlinks=False)`` read a whiteout.

    Delegates every other call (the real legacy/host stats) to the genuine helper so
    only the agent-controlled ``upper`` leaf reports as a char-device ``0,0``.
    """

    real_safe_stat = reconcile_mod._safe_stat

    def _fake(path: Path, *, follow_symlinks: bool = True):
        if not follow_symlinks and Path(path) == upper_path:
            return _whiteout_stat(mtime_ns)
        return real_safe_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(reconcile_mod, "_safe_stat", _fake)


@pytest.mark.unit
def test_reconcile_keeps_whiteout_over_unedited_host_changed_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #414: the agent deleted a file in the overlay era (whiteout char device ``0,0`` in
    # ``upper``), then the host changed that path *after* the deletion and a transient
    # remount created a legacy copy *from the changed host*. The legacy copy is an
    # unedited, byte/mtime-identical copy of the live host and carries a newer mtime than
    # the whiteout's creation mtime. The mtime-only rule plus the old ``S_ISREG``-gated
    # #404 guard would skip the whiteout and forward the legacy file through ``merged``,
    # silently un-deleting what the agent removed. The host-origin guard now protects the
    # whiteout too: an unedited host copy is recognised and the deletion is preserved.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    host = tmp_path / "host"
    _mkdirs(legacy, merged, upper, base, host)
    # The agent's overlay-era deletion whiteout, older than the (later) host change.
    whiteout_mtime_ns = 1_000_000_000_000_000_000
    # The host changed after that deletion (newer mtime) ...
    (host / "settings.json").write_text('{"theme": "host-changed-after-delete"}\n')
    os.utime(
        host / "settings.json",
        ns=(whiteout_mtime_ns + 5_000_000, whiteout_mtime_ns + 5_000_000),
    )
    host_stat = (host / "settings.json").stat()
    # ... and the legacy copy is a byte/mtime-identical, unedited copy of that new host.
    (legacy / "settings.json").write_text('{"theme": "host-changed-after-delete"}\n')
    os.utime(legacy / "settings.json", ns=(host_stat.st_mtime_ns, host_stat.st_mtime_ns))
    _patch_upper_whiteout_stat(monkeypatch, upper / "settings.json", whiteout_mtime_ns)

    _reconcile_fallback_edits_into_upper(
        legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
    )

    # The whiteout (agent deletion) stands; the unedited host copy was not forwarded, so
    # nothing was un-deleted through ``merged``.
    assert not (merged / "settings.json").exists()


@pytest.mark.unit
def test_reconcile_forwards_agent_recreated_legacy_over_whiteout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #414 must not over-protect: when the legacy file actually *diverges* from the live
    # host (the agent re-created the file during the fallback session), it is a genuine
    # fallback-era edit and is forwarded over the whiteout — correctly un-deleting it,
    # exactly as before. Only a provably-unedited host copy is held back.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    host = tmp_path / "host"
    _mkdirs(legacy, merged, upper, base, host)
    whiteout_mtime_ns = 1_000_000_000_000_000_000
    # The live host holds one version ...
    (host / "settings.json").write_text('{"theme": "host"}\n')
    os.utime(
        host / "settings.json",
        ns=(whiteout_mtime_ns + 5_000_000, whiteout_mtime_ns + 5_000_000),
    )
    # ... but the agent re-created the legacy copy to something else, strictly newer.
    (legacy / "settings.json").write_text('{"theme": "agent-recreated"}\n')
    os.utime(
        legacy / "settings.json",
        ns=(whiteout_mtime_ns + 9_000_000, whiteout_mtime_ns + 9_000_000),
    )
    _patch_upper_whiteout_stat(monkeypatch, upper / "settings.json", whiteout_mtime_ns)

    _reconcile_fallback_edits_into_upper(
        legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
    )

    # The legacy bytes diverge from the host, so the agent's fallback re-creation is
    # forwarded over the whiteout (the deletion is un-done, as intended for a real edit).
    assert (merged / "settings.json").read_text() == '{"theme": "agent-recreated"}\n'


# --- #402: forward fallback-era deletions as overlayfs whiteouts ----------------------


@pytest.mark.unit
def test_reconcile_forwards_agent_deletion_as_whiteout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The agent deleted a Claude auth/config file present in ``base`` during the
    # fallback session; the live host still holds it unchanged (host == base), so the
    # legacy absence is a confident agent deletion. With CAP_MKNOD present it is
    # forwarded as an overlayfs whiteout (char device 0,0) into ``upper`` so the
    # deletion survives the next remount instead of the lower re-exposing it.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    host = tmp_path / "host"
    _mkdirs(legacy, merged, upper, base, host)
    (base / "secret.json").write_text("token\n")
    base_stat = (base / "secret.json").stat()
    (host / "secret.json").write_text("token\n")  # same content -> same size
    os.utime(host / "secret.json", ns=(base_stat.st_atime_ns, base_stat.st_mtime_ns))
    # ``legacy`` LACKS ``secret.json`` -> the agent removed it. ``upper`` is empty.

    monkeypatch.setattr(reconcile_mod, "_has_cap_mknod", lambda: True)
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(overlay_copy_mod.os, "mknod", _recording_mknod(recorded))

    _reconcile_fallback_edits_into_upper(
        legacy=legacy,
        merged=merged,
        upper=upper,
        base=base,
        host_claude=host,
        forward_deletions=True,
    )

    assert len(recorded) == 1
    # A char device 0,0 written at the deleted path's leaf, inside ``upper``.
    assert recorded[0]["name"] == "secret.json"
    assert recorded[0]["mode"] == stat.S_IFCHR
    assert recorded[0]["device"] == os.makedev(0, 0)
    assert (upper / "secret.json").exists()


@pytest.mark.unit
def test_reconcile_ambiguous_host_removed_file_is_not_whiteouted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The file is in ``base`` but absent from BOTH the legacy copy and the live host:
    # the host may itself have removed it, so the legacy absence is host-explained, not
    # confidently agent-attributable. Fail safe — never whiteout (the credential stays
    # visible). This is the credential-safety guarantee: ambiguity never hides a file.
    legacy = tmp_path / "legacy"
    merged = tmp_path / "merged"
    upper = tmp_path / "upper"
    base = tmp_path / "base"
    host = tmp_path / "host"
    _mkdirs(legacy, merged, upper, base, host)
    (base / "secret.json").write_text("token\n")  # host lacks it entirely

    monkeypatch.setattr(reconcile_mod, "_has_cap_mknod", lambda: True)
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(overlay_copy_mod.os, "mknod", _recording_mknod(recorded))

    _reconcile_fallback_edits_into_upper(
        legacy=legacy, merged=merged, upper=upper, base=base, host_claude=host
    )

    assert recorded == []
    assert not (upper / "secret.json").exists()
