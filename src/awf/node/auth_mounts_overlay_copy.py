"""Symlink-safe overlay write primitives for Claude fallback-edit reconciliation.

Split out of :mod:`awf.node.auth_mounts_claude` to keep that module under the
first-party line limit. Holds the provider-neutral helpers that
:func:`~awf.node.auth_mounts_claude._reconcile_fallback_edits_into_upper` uses to
forward fallback-era legacy edits and deletions into a live overlay without trusting
an agent-controlled tree: :func:`_safe_mtime_ns` / :func:`_safe_stat` read a
generation timestamp (and size) without raising, :func:`_safe_overlay_copy` copies a
single file through the live ``merged`` mount with ``O_NOFOLLOW`` file descriptors on
*both* the source and the destination paths so an agent-planted symlink at any path
component can never redirect the root write outside — nor the root read in from
outside — the ``.claude`` tree, and :func:`_safe_overlay_whiteout` creates an
overlayfs whiteout device directly in the ``upper`` layer with the same symlink-safe
descent so a fallback-era deletion survives the next remount.
:mod:`awf.node.auth_mounts_claude` re-imports them so its existing call sites
and ``awf.node.auth_mounts_claude.<name>`` test references stay unchanged.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
from pathlib import Path
from typing import IO


def _safe_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result | None:
    """Return ``path``'s :class:`os.stat_result`, or ``None`` if it is absent/unstattable.

    A superset of :func:`_safe_mtime_ns` used where the reconcile needs both the
    generation timestamp *and* the size of an entry (the host-divergence
    disambiguation for #402/#404): the legacy ``~/.claude`` copy is materialized via
    ``copytree(symlinks=False)`` using ``copy2``, which preserves the host's
    ``st_mtime_ns`` and ``st_size``, so a legacy file still byte/mtime-identical to
    the live host is an unedited host-origin copy. Never raises — a missing entry in
    one tree but not another reads as ``None`` so the caller can fail safe.
    """

    try:
        return path.stat(follow_symlinks=follow_symlinks)
    except OSError:
        return None


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


def _legacy_path_confidently_absent(root: Path, rel: Path) -> bool:
    """True only when ``root / rel`` is genuinely absent under a chain of *real* dirs.

    The #402 deletion-forwarding pass treats a path present in ``base`` but absent from
    the agent-writable legacy copy as a confident agent deletion (eligible for a
    whiteout once the host still matches ``base``). The naive
    ``_safe_stat(root / rel, follow_symlinks=False)`` check is **not** symmetric with the
    edit walk's ``os.walk(legacy, followlinks=False)``: ``lstat`` only declines to follow
    the *leaf* symlink — every *intermediate* component of ``root / rel`` is still
    resolved by the OS. So if the agent replaced a subdirectory with a symlink during the
    fallback session (e.g. ``legacy/subdir -> /empty_dir``), the lstat for
    ``rel = subdir/secret.json`` resolves *through* that link and stats
    ``/empty_dir/secret.json``; its absence then reads as a "deletion" and a whiteout
    would *hide* a credential the agent never explicitly removed.

    This descends ``rel``'s parents from the trusted ``root`` component-by-component with
    ``openat(O_NOFOLLOW | O_DIRECTORY)`` — the same fd-anchored descent
    :func:`_safe_overlay_copy` / :func:`_safe_overlay_whiteout` use — and only then
    ``lstat``s the leaf relative to the final parent ``dir_fd``. The path is *confidently
    absent* (returns ``True``) only when:

    - every parent component is a **real directory** (so an agent-planted intermediate
      symlink or a directory-replaced-by-a-file is *not* walked), **and**
    - the leaf does not exist there (``lstat`` raises ``ENOENT``).

    A parent that is genuinely missing (``ENOENT``) means the whole subtree was removed
    (a whole-directory deletion the pass forwards file-by-file), so it counts as
    confidently absent. Every other shape — the leaf present (any type, including a
    symlink), an intermediate symlink/non-directory (``ELOOP`` / ``ENOTDIR``), or any
    other stat/open error — returns ``False`` ("not a confident deletion"), so the caller
    fails safe and keeps the credential visible. ``root`` itself is the trusted legacy
    copy root the caller passes (opened with ``O_NOFOLLOW`` for symmetry).
    """

    fds: list[int] = []
    try:
        dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(dir_fd)
        for part in rel.parent.parts:
            try:
                dir_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            except FileNotFoundError:
                # The parent subtree is genuinely gone (a whole-directory deletion): the
                # leaf is absent too. Confidently absent — the per-file whiteout pass owns
                # forwarding it.
                return True
            except OSError:
                # ``ELOOP`` (an agent-planted intermediate symlink), ``ENOTDIR`` (a parent
                # replaced with a non-directory), or any other open failure: the path
                # *through* this component cannot be walked as real directories, so the
                # leaf's apparent absence cannot be attributed to a deletion. Not
                # confidently absent — fail safe (keep the credential visible).
                return False
            fds.append(dir_fd)
        try:
            os.stat(rel.name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        except OSError:
            # Any other leaf stat failure is ambiguous, not a proven deletion. Fail safe.
            return False
        # The leaf exists (any type, including a symlink/FIFO): present, not a deletion.
        return False
    except OSError:
        # ``root`` itself unopenable (absent or symlinked): ambiguous. Fail safe.
        return False
    finally:
        for fd in fds:
            with contextlib.suppress(OSError):
                os.close(fd)


def _streams_equal_bytes(fa: IO[bytes], fb: IO[bytes]) -> bool:
    """True iff two already-open binary streams yield identical bytes through EOF.

    The shared chunked-compare loop behind both :func:`_safe_files_equal_content` (two
    non-agent trees) and :func:`_safe_agent_file_equal_content` (an agent-controlled
    first path). Each caller owns opening — and *safely* opening — its file descriptors;
    this only walks them in lock-step so the two open paths cannot drift apart.
    """

    while True:
        chunk_a = fa.read(65536)
        chunk_b = fb.read(65536)
        if chunk_a != chunk_b:
            return False
        if not chunk_a:
            return True


def _safe_files_equal_content(a: Path, b: Path) -> bool:
    """True iff ``a`` and ``b`` are both readable and byte-for-byte identical.

    Used by the #402 deletion-forwarding pass to confirm — *beyond* a matching
    ``st_mtime_ns`` + ``st_size`` — that the live host copy is genuinely unchanged
    from ``base`` before a whiteout *hides* it. mtime + size alone can collide when a
    host rotates a credential to same-length bytes and restores the old timestamp
    (e.g. a ``touch -r`` after a same-length token rewrite, or a copy/sync that
    preserves mtimes): the stat-only check would then read "host == base" and
    whiteout a path the host has since re-populated with a *different, currently
    valid* credential — hiding it. Comparing content closes that window in this
    safety-sensitive, credential-hiding direction.

    **Both arguments must be *non-agent* trees** — the read-only overlay ``base`` lower
    (materialized via ``copytree(symlinks=False)``, so no symlinks) and the trusted
    live host ``~/.claude`` — because the plain ``open("rb")`` here follows symlinks
    and *blocks* on a reader-less FIFO. A call site whose argument lives under an
    agent-writable tree (e.g. the ``rw`` legacy copy) must use
    :func:`_safe_agent_file_equal_content` instead, which adds the ``O_NOFOLLOW |
    O_NONBLOCK`` + ``S_ISREG`` guard against a swapped symlink/FIFO; without that an
    agent could hang the root provisioning worker forever. The fd-based symlink-safe
    descent that :func:`_safe_overlay_copy` needs for the agent-controlled
    ``merged``/``legacy`` trees is otherwise unnecessary for these two trusted trees.
    Any read error (a racing unlink, a permission failure) fails safe to ``False`` —
    "not provably equal", so the caller declines the whiteout and the credential stays
    visible.
    """

    try:
        with a.open("rb") as fa, b.open("rb") as fb:
            return _streams_equal_bytes(fa, fb)
    except OSError:
        return False


def _safe_agent_file_equal_content(agent_root: Path, rel: Path, trusted_file: Path) -> bool:
    """True iff ``agent_root / rel`` and ``trusted_file`` are both readable and byte-identical.

    Content-equality variant of :func:`_safe_files_equal_content` for the case where the
    *first* path lives under an **agent-controlled** tree — the ``rw`` legacy
    ``~/.claude`` copy that #404's
    :func:`~awf.node.auth_mounts_claude_reconcile._legacy_is_unedited_host_copy` reads.
    The caller's ``is_file()`` pre-check in
    :func:`~awf.node.auth_mounts_claude_reconcile._reconcile_fallback_edits_into_upper`
    is *not* atomic with this read, and the agent can swap the agent file — or one of its
    *parent* directories — for a symlink or a reader-less FIFO in that window: the plain
    ``open("rb")`` that :func:`_safe_files_equal_content` uses for its two non-agent trees
    would then follow the link out of the ``.claude`` tree or block the root provisioning
    worker forever (the same DoS that :func:`_safe_overlay_copy` already defends against on
    its agent-controlled source).

    A full-path ``os.open(agent_root / rel, O_NOFOLLOW)`` is *not* enough: ``O_NOFOLLOW``
    only guards the *leaf*, so a parent component such as ``agent_root/a`` in
    ``agent_root/a/file`` would still be resolved by name. An agent that swaps a real
    walked directory ``a`` for ``a -> /outside`` between the caller's ``os.walk`` /
    ``is_file()`` and this read would redirect the root worker to ``/outside/<file>``; if
    that out-of-tree file's bytes happen to match ``trusted_file`` the function returns a
    *false* ``True``, the #404 caller treats the legacy file as an unedited host copy,
    *skips* forwarding it (so :func:`_safe_overlay_copy` never re-validates it), and the
    genuine agent fallback edit is lost when the legacy tree is reaped. So the agent path
    is descended component-by-component from the trusted ``agent_root`` with
    ``openat(O_NOFOLLOW | O_DIRECTORY)`` — the same fd-anchored descent
    :func:`_safe_overlay_copy` uses for its agent-controlled source — and the leaf is
    opened relative to its parent ``dir_fd`` with ``O_RDONLY | O_NOFOLLOW | O_NONBLOCK``
    plus an ``S_ISREG`` ``fstat`` guard: ``O_NOFOLLOW`` rejects a swapped symlink at the
    leaf *or any descended parent* (``ELOOP``), ``O_NONBLOCK`` guarantees the open
    itself never blocks on a special leaf (a device/FIFO open can otherwise wait), and
    the ``S_ISREG`` ``fstat`` guard is what actually refuses any non-regular leaf — a
    FIFO (reader-less *or* with a live reader), device, or socket — before a byte is
    read. (A read-only FIFO open succeeds regardless of whether a writer is attached;
    ``ENXIO`` only applies to a *write*-side open of a reader-less FIFO, so
    ``O_NONBLOCK`` is a non-blocking safeguard here, not the FIFO rejection itself.)
    ``trusted_file`` is the live host ``~/.claude`` copy (non-agent), so it keeps the
    plain streamed read.

    Any open/read error — a symlinked/non-dir component, a swapped special file, a racing
    unlink, a permission failure — fails safe to ``False`` ("not provably equal"), exactly
    as :func:`_safe_files_equal_content` does. The #404 caller then treats the legacy file
    as eligible to forward (where :func:`_safe_overlay_copy` independently re-validates
    it with the same fd-based descent), so this never silently hides or drops a
    credential — it only declines the "unedited host copy" protection.
    """

    fds: list[int] = []
    try:
        try:
            dir_fd = os.open(agent_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            fds.append(dir_fd)
            for part in rel.parent.parts:
                # No parent is ever created — the agent file must already exist.
                # ``O_NOFOLLOW`` rejects a symlink swapped in for any descended parent
                # component (``ELOOP``), ``O_DIRECTORY`` rejects a non-directory one
                # (``ENOTDIR``); both atomic, no check/use gap.
                dir_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
                fds.append(dir_fd)
            agent_fd = os.open(rel.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
            fds.append(agent_fd)
        except OSError:
            # A symlinked/non-dir parent component, a swapped symlink leaf (``ELOOP``), a
            # racing unlink, or a permission failure. (A reader-less FIFO leaf is *not*
            # caught here — a read-only FIFO open succeeds even with no writer; the
            # ``S_ISREG`` guard below rejects it.) Fail safe. The outer ``finally`` still
            # closes any directory fds opened during a *partial* descent before the error.
            return False
        if not stat.S_ISREG(os.fstat(agent_fd).st_mode):
            # A FIFO (reader-less or with a live reader), device, socket, or directory the
            # agent swapped in after the caller's ``is_file()`` check: never read it.
            return False
        with os.fdopen(agent_fd, "rb", closefd=False) as fa, trusted_file.open("rb") as fb:
            return _streams_equal_bytes(fa, fb)
    except OSError:
        return False
    finally:
        for fd in fds:
            with contextlib.suppress(OSError):
                os.close(fd)


def _safe_overlay_copy(merged: Path, rel: Path, src_root: Path) -> None:
    """Copy ``src_root / rel`` into ``merged / rel`` as root, never following an
    agent-planted symlink at *any* component of *either* path — closing a TOCTOU
    symlink-injection escape on the destination *and* the source.

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

    The *source* gets the fully symmetric treatment, anchored at the trusted
    ``src_root`` (the legacy copy root the caller walked). The caller's
    ``is_symlink()``/``is_file()`` pre-checks are not atomic with this read, and the
    legacy copy is ``rw`` for the agent, so the source is *also* descended
    component-by-component with ``openat(O_NOFOLLOW | O_DIRECTORY)`` from ``src_root``
    rather than opened by full path. Opening ``src_root / rel`` by name would apply
    ``O_NOFOLLOW`` only to the *leaf*: a parent component such as ``src_root/a`` would
    still be resolved by name, so an agent that swaps a real walked directory
    ``a`` for ``a -> /outside`` between the caller's ``os.walk``/``is_file()`` and this
    read would redirect the root worker to ``/outside/<file>`` and surface it in the
    agent-visible overlay — the same arbitrary root-read primitive the leaf guard
    blocks, one level up. After the safe descent the leaf is opened relative to its
    parent ``dir_fd`` with ``O_RDONLY | O_NOFOLLOW | O_NONBLOCK`` and an ``S_ISREG``
    ``fstat`` guard rather than a symlink-following ``open("rb")``. This closes the
    mirror-image escape — an agent-planted symlink at *any* source component would
    otherwise let the root worker read an arbitrary host path into the agent-visible
    overlay, and a FIFO would block it indefinitely.

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
        # (untrusted) agent: it can swap the checked-clean regular file — or one of its
        # *parent* directories — for a symlink or FIFO in that window. A bare
        # ``src.stat()`` + ``src.open("rb")`` follows symlinks and blocks on FIFOs, so it
        # would let the root worker (a) read an arbitrary host path through an agent-planted
        # ``ln -sf /etc/shadow src`` and surface it in the agent-visible overlay, or
        # (b) hang forever on a reader-less ``mkfifo src``. Opening ``src_root / rel`` by
        # full path is not enough: ``O_NOFOLLOW`` would then only guard the *leaf*, leaving
        # every parent component (``src_root/a`` in ``src_root/a/file``) resolved by name —
        # so a swapped ``a -> /outside`` parent would still redirect the read out of tree.
        # Descend from the trusted ``src_root`` component-by-component with
        # ``openat(O_NOFOLLOW | O_DIRECTORY)`` (no parent is ever created — the source must
        # already exist), then open the leaf relative to its parent ``dir_fd`` with
        # ``O_RDONLY | O_NOFOLLOW | O_NONBLOCK``: ``O_NOFOLLOW`` rejects a planted symlink
        # at the leaf or any descended parent (``ELOOP``); ``O_NONBLOCK`` makes a
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
        src_dir_fd = os.open(src_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(src_dir_fd)
        for part in rel.parent.parts:
            src_dir_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=src_dir_fd
            )
            fds.append(src_dir_fd)
        src_fd = os.open(rel.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src_dir_fd)
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


def _safe_overlay_whiteout(upper: Path, rel: Path) -> bool:
    """Create an overlayfs whiteout for ``rel`` in the overlay ``upper`` layer.

    An overlayfs whiteout is a character device with device number ``0,0``; it
    hides the same-named entry in the lower (``base``) layer. The reconcile uses it
    to preserve a fallback-era *deletion* of a Claude auth/config file (#402): once
    the overlay remounts, ``upper`` over ``base`` would otherwise re-expose the file
    the agent removed. Unlike :func:`_safe_overlay_copy` (which writes *through* the
    live ``merged`` mount so the kernel copies up coherently), a whiteout is
    meaningful only in the ``upper`` layer itself — overlayfs reads whiteouts there,
    and there is no ``merged``-routed equivalent — so it is created directly under
    ``upper``. The reconcile runs at provision time *before* the agent attaches, and
    a not-yet-coherent whiteout merely fails to hide (the safe direction), so the
    direct ``upper`` write is acceptable here.

    The descent mirrors :func:`_safe_overlay_copy`'s destination handling: each
    component of ``rel.parent`` is created (if missing) and re-opened under
    ``openat(O_NOFOLLOW | O_DIRECTORY)`` so an agent-planted symlink at *any*
    component (the overlay ``upper`` is agent-controlled) can never redirect the
    ``mknod`` outside the ``.claude`` tree. The leaf device is created relative to
    the validated parent ``dir_fd`` so it, too, cannot escape via a swapped name.

    Requires ``CAP_MKNOD``. **Fail-safe toward keeping the credential visible:** any
    ``OSError`` — a missing capability or unsupported filesystem (``EPERM`` /
    ``ENOSYS``), a symlinked/non-dir component (``ELOOP`` / ``ENOTDIR``), or an
    already-present leaf (``EEXIST``) — skips the whiteout and returns ``False``
    without raising. A deletion that cannot be safely forwarded therefore degrades
    to "the lower's copy stays visible", never to "a still-needed credential is
    hidden". Returns ``True`` only when the whiteout device was actually created.
    """

    fds: list[int] = []
    try:
        dir_fd = os.open(upper, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(dir_fd)
        for part in rel.parent.parts:
            # ``FileExistsError`` means the parent already exists; re-open it under
            # ``O_NOFOLLOW`` below so a symlink occupying the component is still
            # rejected (``ELOOP``), exactly as in :func:`_safe_overlay_copy`.
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, dir_fd=dir_fd)
            dir_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            fds.append(dir_fd)
        # The overlayfs whiteout marker: a char device 0,0 with no permission bits.
        # Created relative to the validated parent ``dir_fd`` so a name swapped for a
        # symlink mid-descent cannot redirect it. ``EEXIST`` (a leaf the agent already
        # planted) is part of the fail-safe ``OSError`` catch — never clobber it.
        os.mknod(
            rel.name,
            mode=stat.S_IFCHR | 0o000,
            device=os.makedev(0, 0),
            dir_fd=dir_fd,
        )
    except OSError:
        # Best-effort fail-safe: a missing ``CAP_MKNOD`` (``EPERM``), an unsupported
        # filesystem (``ENOSYS``), a symlinked/non-dir component (``ELOOP`` /
        # ``ENOTDIR``), or an existing leaf (``EEXIST``) all degrade to "not hidden".
        return False
    finally:
        for fd in fds:
            with contextlib.suppress(OSError):
                os.close(fd)
    return True
