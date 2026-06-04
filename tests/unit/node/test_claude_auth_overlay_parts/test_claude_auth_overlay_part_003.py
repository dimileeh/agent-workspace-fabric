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
from awf.node.auth_mounts import (
    _reconcile_fallback_edits_into_upper,
)

from .test_claude_auth_overlay_part_001 import (
    FakeOverlayMounter as FakeOverlayMounter,
)
from .test_claude_auth_overlay_part_001 import (
    _seed_host_claude as _seed_host_claude,
)


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

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

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

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

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

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

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

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

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
        # The source is the only non-directory ``os.open`` of ``legacy/f`` (the dir
        # descent uses ``O_DIRECTORY``); the destination open targets ``merged``.
        if os.fspath(path) == os.fspath(legacy / "f") and not (flags & os.O_DIRECTORY):
            raise OSError("source vanished")
        return real_os_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(auth_mounts_mod.os, "open", _os_open_source_vanished)

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

    # The source open failed and was swallowed best-effort; the destination keeps its
    # original content instead of being truncated to empty.
    assert (merged / "f").read_text() == "agent overlay edit\n"


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

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

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

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

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

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

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

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

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

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

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

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

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

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

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
        _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)
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

    _reconcile_fallback_edits_into_upper(legacy=legacy, merged=merged, upper=upper, base=base)

    assert (merged / "nested" / "f").read_text() == "fallback edit\n"


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
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "root-only-secret"
    secret.write_text("root-only contents\n")
    src_link = tmp_path / "src"
    src_link.symlink_to(secret)

    auth_mounts_mod._safe_overlay_copy(merged, Path("f"), src_link)

    # The link target's contents are never read into the destination tree (the leaf may
    # exist as an empty file from the dest ``O_CREAT`` before the source open failed —
    # never any content), and the out-of-tree secret is untouched.
    assert (merged / "f").read_text() == ""
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
    src_fifo = tmp_path / "pipe"
    os.mkfifo(src_fifo)

    auth_mounts_mod._safe_overlay_copy(merged, Path("f"), src_fifo)

    # Nothing was read from the FIFO; an empty dest leaf at most, never blocking.
    assert (merged / "f").read_text() == ""
    assert stat.S_ISFIFO(os.lstat(src_fifo).st_mode)
