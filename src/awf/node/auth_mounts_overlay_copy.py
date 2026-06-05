"""Symlink-safe overlay write primitives for Claude fallback-edit reconciliation.

Split out of :mod:`awf.node.auth_mounts_claude` to keep that module under the
first-party line limit. Holds the two provider-neutral helpers that
:func:`~awf.node.auth_mounts_claude._reconcile_fallback_edits_into_upper` uses to
forward fallback-era legacy edits into a live overlay without trusting an
agent-controlled tree: :func:`_safe_mtime_ns` reads a generation timestamp without
raising, and :func:`_safe_overlay_copy` copies a single file through the live
``merged`` mount with ``O_NOFOLLOW`` file descriptors so an agent-planted symlink
at any path component can never redirect the root write outside the ``.claude``
tree. :mod:`awf.node.auth_mounts_claude` re-imports both so its existing call sites
and ``awf.node.auth_mounts_claude.<name>`` test references stay unchanged.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
from pathlib import Path


def _safe_mtime_ns(path: Path, *, follow_symlinks: bool = True) -> int | None:
    """Return ``path``'s ``st_mtime_ns``, or ``None`` if it is absent/unstattable.

    Used by :func:`~awf.node.auth_mounts_claude._reconcile_fallback_edits_into_upper`
    to compare generations without raising on a file that exists in one tree but not
    another. The default ``follow_symlinks=True`` matches the legacy copy's
    ``copytree(symlinks=False)``, which materialized link targets as real files —
    correct for the ``legacy`` and ``base`` trees (the former pre-skips symlinks, the
    latter contains none).

    The agent-controlled ``upper`` tree can hold a planted symlink, so its generation
    must be read with ``follow_symlinks=False`` (``lstat``): the overlay entry's *own*
    mtime is when the agent made the overlay-era change, whereas the symlink target's
    mtime is unrelated and would make the "upper wins ties" comparison decide on the
    wrong file's metadata.
    """

    try:
        return path.stat(follow_symlinks=follow_symlinks).st_mtime_ns
    except OSError:
        return None


def _safe_overlay_copy(merged: Path, rel: Path, src: Path) -> None:
    """Copy ``src`` into ``merged / rel`` as root, never following an agent-planted
    symlink at *any* component — closing a TOCTOU symlink-injection escape.

    The destination lives under the live ``merged`` overlay whose upper layer is written
    by the (untrusted) agent. Resolving it by *name* — an ``is_symlink()``/``is_dir()``
    guard followed by ``shutil.copy2`` — is racy: a concurrent agent can swap a
    checked-clean component for a symlink in the window before the write. Worse,
    ``Path.mkdir(exist_ok=True)`` *itself* follows a symlink: its ``EEXIST`` fast path
    calls ``is_dir()``, which resolves a planted ``merged/.config -> /etc/sudoers.d``
    link and silently succeeds, after which every lexical ``Path.__truediv__`` resolves
    *through* the link and the root copy lands outside the ``.claude`` tree (an arbitrary
    root-write primitive).

    So the destination is never touched by name. Descend component-by-component with
    ``openat(O_NOFOLLOW | O_DIRECTORY)`` file descriptors: each ``open`` *atomically*
    refuses a symlinked component (``ELOOP``) and a non-directory one (``ENOTDIR``), with
    no check/use gap. Missing parents are created with ``os.mkdir(dir_fd=...)`` (a no-op
    ``FileExistsError`` if present) and then re-opened under ``O_NOFOLLOW`` so a link
    planted between the ``mkdir`` and the ``open`` is still rejected. The leaf is opened
    *relative to the parent ``dir_fd``* with ``O_WRONLY | O_CREAT | O_NOFOLLOW |
    O_NONBLOCK``:

    - ``O_NOFOLLOW`` fails (``ELOOP``) on a symlink leaf — the copy can never follow a
      planted link out of the tree.
    - ``O_WRONLY`` on a directory leaf fails (``EISDIR``); ``copy2`` would instead have
      written ``dst / basename(src)`` *inside* it, through any inner planted link.
    - ``O_NONBLOCK`` makes opening a reader-less FIFO fail (``ENXIO``) rather than block
      the root worker forever; the ``S_ISREG`` ``fstat`` guard rejects any other special
      file (e.g. a FIFO with a live reader) before a byte is written.

    The *source* (``src``) gets the symmetric treatment: the caller's
    ``is_symlink()``/``is_file()`` pre-checks are not atomic with this read, and the
    legacy copy is ``rw`` for the agent, so ``src`` is opened with ``O_RDONLY |
    O_NOFOLLOW | O_NONBLOCK`` and an ``S_ISREG`` ``fstat`` guard rather than a
    symlink-following ``src.open("rb")``. This closes the mirror-image escape — an
    agent-planted symlink would otherwise let the root worker read an arbitrary host
    path into the agent-visible overlay, and a FIFO would block it indefinitely.

    Best-effort: any structural conflict or ``OSError`` skips just this file and never
    raises, so reconciliation never blocks provisioning. Mode and mtime are preserved
    (matching ``copy2``) so the "upper wins ties" generation rule downstream still sees
    the forwarded edit's real mtime.
    """

    fds: list[int] = []
    try:
        dir_fd = os.open(merged, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(dir_fd)
        for part in rel.parent.parts:
            # ``FileExistsError`` means the parent already exists; re-open it under
            # ``O_NOFOLLOW`` below so a symlink occupying the component is still rejected
            # (``ELOOP``).
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, dir_fd=dir_fd)
            dir_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            fds.append(dir_fd)
        # Open and validate the *source* first, *before* the destination is created. The
        # caller's ``is_symlink()``/``is_file()`` guards in
        # :func:`~awf.node.auth_mounts_claude._reconcile_fallback_edits_into_upper` are
        # *not* atomic with this open, and the legacy copy is mounted ``rw`` for the
        # (untrusted) agent: it can swap the checked-clean regular file for a symlink or
        # FIFO in that window. A bare ``src.stat()`` + ``src.open("rb")`` follows symlinks
        # and blocks on FIFOs, so it would let the root worker (a) read an arbitrary host
        # path through an agent-planted ``ln -sf /etc/shadow src`` and surface it in the
        # agent-visible overlay, or (b) hang forever on a reader-less ``mkfifo src``.
        # ``O_NOFOLLOW`` rejects a planted symlink leaf (``ELOOP``); ``O_NONBLOCK`` makes a
        # reader-less FIFO fail (``ENXIO``) instead of blocking; and the ``S_ISREG``
        # ``fstat`` guard refuses any other non-regular leaf (e.g. a FIFO with a live
        # reader) before a byte is read — mirroring the destination protections.
        #
        # Source-before-destination ordering matters for correctness, not just security:
        # opening the destination with ``O_CREAT`` *first* would materialise a 0-byte
        # regular file in the overlay upper, and if the source open then failed (e.g. the
        # legacy file was unlinked in the non-atomic window above) the best-effort
        # ``except OSError`` return would leave that empty file shadowing the base
        # entry — a Claude config could read as empty to the agent. Validating the source
        # first means no destination is ever created unless there is real content to copy.
        src_fd = os.open(os.fspath(src), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        fds.append(src_fd)
        src_st = os.fstat(src_fd)
        if not stat.S_ISREG(src_st.st_mode):
            return
        dst_fd = os.open(
            rel.name,
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=dir_fd,
        )
        fds.append(dst_fd)
        if not stat.S_ISREG(os.fstat(dst_fd).st_mode):
            # A non-regular leaf the agent planted that still opened (e.g. a FIFO with a
            # live reader): never write to it — refuse the structural conflict.
            return
        # The read fd on the source content is already held, so ``ftruncate`` (below)
        # only runs once that fd is open: if the legacy file is unlinked in this window
        # the existing ``upper`` entry (the agent's overlay-era edit) is left intact
        # rather than silently zeroed.
        with os.fdopen(src_fd, "rb", closefd=False) as src_file:
            os.ftruncate(dst_fd, 0)
            with os.fdopen(dst_fd, "wb", closefd=False) as dst_file:
                shutil.copyfileobj(src_file, dst_file)
        os.fchmod(dst_fd, stat.S_IMODE(src_st.st_mode))
        os.utime(dst_fd, ns=(src_st.st_atime_ns, src_st.st_mtime_ns))
    except OSError:
        # Best-effort: a symlinked/non-dir component (``O_NOFOLLOW``/``O_DIRECTORY``), a
        # symlink/dir/reader-less-FIFO leaf (``ELOOP``/``EISDIR``/``ENXIO``), or any I/O
        # error skips just this file. Never blocks provisioning.
        return
    finally:
        for fd in fds:
            # Best-effort cleanup: a failing ``os.close`` (e.g. ``EBADF``) must not
            # abort the loop and leak the remaining descended fds. Matches the
            # "never blocks provisioning" contract above.
            with contextlib.suppress(OSError):
                os.close(fd)
