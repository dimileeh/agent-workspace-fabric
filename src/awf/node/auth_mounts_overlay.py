"""Generic overlayfs ``/proc/mounts`` parsing primitives for Claude auth isolation.

Split out of :mod:`awf.node.auth_mounts_claude` to keep that module under the
first-party line limit. Holds the provider-neutral lowerdir-discovery helpers
that parse ``/proc/mounts`` to recover live overlay ``lowerdir`` paths. Both the
:class:`~awf.node.auth_mounts_claude.OverlayMounter` implementation that resolves
a single mount's lowerdir and the shared-base GC (#389) that protects every live
lower depend on them. :mod:`awf.node.auth_mounts_claude` (and in turn
:mod:`awf.node.auth_mounts`) re-export every public name here so
``awf.node.auth_mounts.<name>`` stays the stable import surface for callers/tests.
"""

from __future__ import annotations

import os
from pathlib import Path

_PROC_MOUNTS = Path("/proc/mounts")


def _unescape_proc_mount_field(field: str) -> str:
    """Decode the octal escapes ``/proc/mounts`` uses for space/tab/newline/colon/comma/backslash.

    overlayfs reserves a raw ``:`` to join layered lowerdirs, so a literal colon
    *inside* a single lowerdir path is octal-escaped as ``\\072`` (the comment on
    the lowerdir parsers calls this out). The mount-option field is likewise
    comma-separated, so a literal comma in a path is octal-escaped as ``\\054``.
    Callers split on the raw ``:``/``,`` separators first — which never tear such a
    path apart — and then decode each field here, so ``\\072``/``\\054`` must
    round-trip back to ``:``/``,``. Skipping either leaves a base path as
    ``...\\072...``/``...\\054...``, which fails the ``base_root`` match in
    ``_protected_signature_dirs`` and lets GC-B reap a base still backing a live
    overlay (PRRT_kwDOSJAM6s6HN8ld).
    """

    # Decode the backslash escape *last*: a path containing a literal backslash
    # followed by ``040``/``011``/``012``/``072``/``054`` is encoded as ``\134040``
    # etc., and decoding ``\134`` first would leave ``\040`` that the next pass would
    # wrongly turn into a space. Decoding the non-backslash escapes first means a
    # decoded backslash can never be re-read as the start of another escape.
    return (
        field.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\072", ":")
        .replace("\\054", ",")
        .replace("\\134", "\\")
    )


def _overlay_lowerdir_from_proc_mounts(proc_mounts: Path, merged: Path) -> Path | None:
    """Return the ``lowerdir`` of the overlay mounted at ``merged`` per ``/proc/mounts``.

    ``None`` when ``merged`` is not a live overlay there, the file is unreadable, or
    the entry carries no ``lowerdir=`` option. Lets a retry that reuses a still-live
    overlay recover the base it is *actually* mounted against instead of guessing
    from the current host content.
    """

    try:
        contents = proc_mounts.read_text()
    except OSError:
        return None
    target = os.fspath(merged)
    for line in contents.splitlines():
        fields = line.split(" ")
        if len(fields) < 4 or fields[2] != "overlay":
            continue
        if _unescape_proc_mount_field(fields[1]) != target:
            continue
        for option in fields[3].split(","):
            if option.startswith("lowerdir="):
                # ``lowerdir`` may layer multiple lowers as ``a:b:c`` (overlayfs's
                # ``:`` separator, distinct from the ``\072`` octal escape a literal
                # colon in a path takes). Pin to the first/primary lower — the one
                # closest to the upper — instead of treating the whole colon-joined
                # string as a single directory path, which would never resolve to a
                # shared base and would silently drop the pin. AWF's Claude overlay
                # uses a single lower today; splitting keeps pin recovery correct if
                # that ever changes, matching :func:`iter_overlay_lowerdirs`.
                for entry in option[len("lowerdir=") :].split(":"):
                    if entry:
                        return Path(_unescape_proc_mount_field(entry))
                return None
        return None
    return None


def iter_overlay_lowerdirs(proc_mounts: Path = _PROC_MOUNTS) -> set[Path]:
    """Return every live overlay ``lowerdir=`` path from ``/proc/mounts``.

    Generalizes :func:`_overlay_lowerdir_from_proc_mounts` (which resolves a single
    overlay's lowerdir by mountpoint) to enumerate *all* live overlays' lowers.
    GC-B (#389) intersects this with the on-disk shared-base dirs so a base still
    backing a live overlay as its ``lowerdir`` is never reaped — overlayfs forbids
    removing a live lower. Reuses :func:`_unescape_proc_mount_field` for the octal
    escapes ``/proc/mounts`` uses; an unreadable file yields an empty set.

    A multi-layer ``lowerdir=a:b:c`` is split on ``:`` so every contributing lower
    is captured. The Claude overlay uses a single lower today, but splitting keeps
    the protection correct if that ever changes.
    """

    try:
        contents = proc_mounts.read_text()
    except OSError:
        return set()
    lowerdirs: set[Path] = set()
    for line in contents.splitlines():
        fields = line.split(" ")
        if len(fields) < 4 or fields[2] != "overlay":
            continue
        for option in fields[3].split(","):
            if not option.startswith("lowerdir="):
                continue
            for entry in option[len("lowerdir=") :].split(":"):
                if entry:
                    lowerdirs.add(Path(_unescape_proc_mount_field(entry)))
    return lowerdirs
