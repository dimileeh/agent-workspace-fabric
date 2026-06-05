"""Fallback-edit/deletion reconciliation for the Claude auth overlay (#381/#402/#404).

Split out of :mod:`awf.node.auth_mounts_claude` as a dependency-free leaf (it
imports only the capability probes and the symlink-safe overlay copy/stat
helpers, never the orchestrator module) to keep that module under the first-party
line limit. When a transient remount failure degrades a provision to the legacy
full copy, the agent may mutate Claude auth/config in that copy; once the overlay
later remounts, ``merged`` (``upper`` over ``base``) shadows the legacy copy and
the caller reaps it. These passes forward the fallback-era *edits* and confident
*deletions* into the overlay ``upper`` first, so they survive the remount.

:mod:`awf.node.auth_mounts_claude` re-exports the public reconcile entry point and
:mod:`awf.node.auth_mounts` re-exports it in turn, so
``awf.node.auth_mounts.<name>`` stays the stable import surface for callers/tests.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from awf.common.logging import get_logger
from awf.node.auth_mounts_caps import _has_cap_mknod
from awf.node.auth_mounts_overlay_copy import _safe_files_equal_content as _safe_files_equal_content
from awf.node.auth_mounts_overlay_copy import _safe_mtime_ns as _safe_mtime_ns
from awf.node.auth_mounts_overlay_copy import _safe_overlay_copy as _safe_overlay_copy
from awf.node.auth_mounts_overlay_copy import _safe_overlay_whiteout as _safe_overlay_whiteout
from awf.node.auth_mounts_overlay_copy import _safe_stat as _safe_stat

_log = get_logger(__name__)

# Per-workspace Claude/Gemini auth copies must exclude historical usage/transcript
# dirs. Otherwise ``ccusage`` (which reads these local files) would attribute the
# host's prior usage to the workspace run; baseline/delta still guards totals, but
# excluding the copy keeps the workspace from seeing unrelated host transcripts and
# avoids copying potentially large history trees.
_CLAUDE_USAGE_HISTORY_DIRS = ("projects", "todos", "shell-snapshots", "statsig")
# Logged once per reconcile when a fallback-era deletion *could* be forwarded as an
# overlayfs whiteout (host confirms the base copy is unchanged, so the legacy
# absence is a confident agent deletion) but the worker lacks ``CAP_MKNOD`` to create
# the whiteout device. The deletion is simply not forwarded — the credential stays
# visible (fail-safe) — but the gap is surfaced so a capability misconfiguration on a
# host that should hold ``CAP_MKNOD`` is diagnosable rather than silent.
_CLAUDE_AUTH_OVERLAY_WHITEOUT_INCAPABLE = "CLAUDE_AUTH_OVERLAY_WHITEOUT_INCAPABLE"
# Logged once per reconcile when a fallback-era deletion *could* be forwarded and
# ``CAP_MKNOD`` is present, yet the ``mknod`` is still refused at the filesystem level
# (e.g. an ``upper`` tmpfs/filesystem without char-device support). Distinct from the
# capability-missing case above: here the probe passed, so the failure would otherwise
# be silent. The deletion is not forwarded — the credential stays visible (fail-safe) —
# but the gap is surfaced so the un-forwarded deletion is diagnosable.
_CLAUDE_AUTH_OVERLAY_WHITEOUT_FAILED = "CLAUDE_AUTH_OVERLAY_WHITEOUT_FAILED"


def _legacy_is_unedited_host_copy(legacy_stat: os.stat_result, host_file: Path) -> bool:
    """True when ``legacy_stat`` is byte/mtime-identical to the live host ``host_file``.

    The legacy ``~/.claude`` copy is materialized via ``copytree(symlinks=False)``
    using ``copy2``, which preserves the host's ``st_mtime_ns`` and ``st_size``. So a
    legacy file whose mtime *and* size still match the live host is an **unedited**
    host-origin copy the agent never touched; one that diverges (different mtime or
    size) is an agent fallback-era edit. #404 uses this to refuse overwriting an
    agent's overlay-era ``upper`` edit with an unedited copy of a host that merely
    changed *after* the edit — the reviewer's case, where the fallback copy carries a
    newer host mtime yet the agent never edited it.

    Returns ``False`` when the host file is absent or unstattable: divergence then
    cannot be *disproven*, so the caller treats the legacy file as an agent edit
    (eligible to forward, preserving the tested fallback re-edit forwarding). The
    fail-safe that protects the agent's ``upper`` edit fires only on a *positive*
    host match — it never hides or drops a credential, it only declines to clobber a
    provably-unedited file over an existing overlay-era edit.
    """

    host_stat = _safe_stat(host_file)
    if host_stat is None:
        return False
    return (
        host_stat.st_mtime_ns == legacy_stat.st_mtime_ns
        and host_stat.st_size == legacy_stat.st_size
    )


def _forward_fallback_deletions_as_whiteouts(
    *, legacy: Path, upper: Path, base: Path, host_claude: Path
) -> None:
    """Forward fallback-era *deletions* into the overlay ``upper`` as whiteouts (#402).

    The edit/addition walk in :func:`_reconcile_fallback_edits_into_upper` only sees
    files that still *exist* under ``legacy``; a Claude auth/config file the agent
    *removed* during a transient-fallback session is invisible there, and after the
    overlay remounts the lower ``base`` re-exposes it. This second pass walks ``base``
    for entries the legacy copy no longer holds and, when the removal can be
    **confidently** attributed to the agent, records an overlayfs whiteout in
    ``upper`` so the deletion survives the remount.

    Credential safety is paramount: a misclassified deletion must never *hide* a
    still-needed credential, so every ambiguous case fails safe toward leaving the
    base copy visible. A removal is whiteouted only when *all* hold:

    - ``upper`` has no entry for the path (any type). The agent's overlay state is
      otherwise authoritative — never double-handle it.
    - The live host ``~/.claude`` still holds the path **unchanged** from ``base`` —
      same ``st_mtime_ns`` + ``st_size`` **and** byte-for-byte identical content (see
      :func:`_safe_files_equal_content`). That proves the base copy is current, so the
      legacy copy's absence is a genuine agent deletion — mirroring normal overlay
      semantics where an agent deletion through ``merged`` hides the lower. The content
      check is not redundant with mtime + size: a host that rotates a credential to
      same-length bytes and restores the old timestamp (``touch -r``, or a
      timestamp-preserving sync) would otherwise read as "unchanged" and have its
      *new, valid* credential hidden by the whiteout — the content compare closes that
      window in this credential-hiding direction.
    - ``CAP_MKNOD`` is available to create the whiteout device.

    Every other shape is ambiguous and **not** whiteouted: the host *lacking* the
    path (the host may itself have removed it — the legacy absence is then
    host-explained, not agent-attributable), the host holding a *changed* version
    (re-added / edited — detected by a differing mtime, size, *or* content), or a
    missing capability. In each case the deletion is simply not forwarded and the
    base's copy stays visible — the only safe direction.

    ``os.walk`` here uses the default ``followlinks=False`` and the base tree holds no
    symlinks (it is built via ``copytree(symlinks=False)``), so no agent-controlled
    link is ever traversed; the whiteout itself is created with a symlink-safe,
    ``CAP_MKNOD``-guarded descent in :func:`_safe_overlay_whiteout`. Best-effort: a
    capability-less worker that *would* have whiteouted at least one confident
    deletion logs once (``CLAUDE_AUTH_OVERLAY_WHITEOUT_INCAPABLE``), and a worker that
    *has* ``CAP_MKNOD`` yet still has ``mknod`` refused by the filesystem logs once too
    (``CLAUDE_AUTH_OVERLAY_WHITEOUT_FAILED``) so neither gap is silent, but neither ever
    blocks provisioning.

    Scope — *whole-directory* deletions are not fully hidden. This walks ``base`` for
    *files* only (``for name in files``) and writes a per-file char-device whiteout for
    each. When the agent deleted an entire directory during the fallback session (so the
    directory is absent from ``legacy`` altogether), each contained file is whiteouted
    individually, which first ``mkdir``s the directory in ``upper``; after the remount
    overlayfs shows ``merged/dir`` as an *empty* directory rather than hiding it (fully
    hiding a lower directory needs a char-device ``0,0`` placed *at* ``upper/dir`` itself,
    replacing the entry). This is fail-safe (it never exposes a credential the agent
    deleted — at most an empty husk remains) and, for Claude's auth layout — individual
    credential files, not whole directories, are the deletion target — does not matter in
    practice. Partial-directory deletions are handled correctly.
    """

    excluded = frozenset(_CLAUDE_USAGE_HISTORY_DIRS)
    has_cap_mknod = _has_cap_mknod()
    skipped_for_capability = False
    whiteout_failed_despite_cap = False
    for root, dirs, files in os.walk(base):
        root_path = Path(root)
        # Mirror the base copy's usage-history exclusion (kept for symmetry: those
        # dirs are absent from base anyway, so this never prunes a real candidate).
        dirs[:] = [d for d in dirs if d not in excluded]
        for name in files:
            rel = (root_path / name).relative_to(base)
            # Present in legacy (as anything) → not a deletion; the edit walk owns it.
            if _safe_stat(legacy / rel, follow_symlinks=False) is not None:
                continue
            # ``upper`` already holds an entry (file or whiteout): the agent's overlay
            # state is authoritative — never double-handle it.
            if _safe_stat(upper / rel, follow_symlinks=False) is not None:
                continue
            # Host-divergence disambiguation: only a base still matched byte-for-byte
            # by the live host proves the legacy absence is a confident agent deletion.
            base_stat = _safe_stat(base / rel)
            host_stat = _safe_stat(host_claude / rel)
            if base_stat is None or host_stat is None:
                # Base unstattable (raced) or host lacks the path (ambiguous: the host
                # may have removed it). Fail safe — keep the credential visible.
                continue
            if (
                host_stat.st_mtime_ns != base_stat.st_mtime_ns
                or host_stat.st_size != base_stat.st_size
            ):
                # Host changed the path since the base was built (re-added / edited):
                # the legacy absence is no longer a confident agent deletion. Fail safe.
                continue
            if not _safe_files_equal_content(base / rel, host_claude / rel):
                # mtime + size matched but the *bytes* differ: the host rotated the
                # credential to same-length content while preserving the old timestamp
                # (e.g. a ``touch -r`` after a same-length rewrite, or a timestamp-
                # preserving sync). The live host therefore holds a different, currently
                # valid credential — whiteouting would *hide* it. The legacy absence is
                # no longer a confident agent deletion. Fail safe — keep it visible.
                continue
            if not has_cap_mknod:
                # A confident agent deletion we cannot forward without ``CAP_MKNOD``.
                # Leave the credential visible and surface the capability gap once.
                skipped_for_capability = True
                continue
            if not _safe_overlay_whiteout(upper, rel):
                # ``CAP_MKNOD`` is present but the ``mknod`` was still refused by the
                # filesystem (e.g. an ``upper`` tmpfs without char-device support). The
                # deletion is not forwarded — the credential stays visible (fail-safe) —
                # but, unlike the missing-capability path, this would otherwise be
                # entirely silent. Flag it for one diagnostic log below so an operator
                # investigating an un-forwarded deletion has a signal beyond the probe.
                whiteout_failed_despite_cap = True
    if skipped_for_capability:
        _log.info(
            "claude_auth_overlay_whiteout_incapable",
            reason_code=_CLAUDE_AUTH_OVERLAY_WHITEOUT_INCAPABLE,
            workspace_auth_root=str(upper.parent),
        )
    if whiteout_failed_despite_cap:
        _log.warning(
            "claude_auth_overlay_whiteout_failed",
            reason_code=_CLAUDE_AUTH_OVERLAY_WHITEOUT_FAILED,
            workspace_auth_root=str(upper.parent),
        )


def _reconcile_fallback_edits_into_upper(
    *, legacy: Path, merged: Path, upper: Path, base: Path, host_claude: Path
) -> None:
    """Forward fallback-era legacy edits into the overlay before reaping it.

    Closes #381: when a surviving non-empty ``upper`` exists but a transient remount
    failure degraded a provision to the legacy full copy, the agent may have mutated
    Claude auth/config in that legacy copy. When the overlay later remounts,
    ``merged`` (``upper`` over ``base``) shadows the legacy copy and the caller
    ``rmtree``s it — dropping those fallback edits. This reconciles them first.

    The forwarded copies are written **through the live ``merged`` mount**, never
    directly into the underlying ``upper`` dir. By the time this runs the overlay is
    already mounted, and overlayfs treats external writes to the upper/lower trees of
    a live mount as undefined behavior — files poked straight into ``upper`` can read
    back stale or invisible through ``merged`` (what the agent actually sees). Writing
    via ``merged`` lets the kernel perform the copy-up coherently, so the reconciled
    edit lands in ``upper`` *and* is immediately visible to the agent. The generation
    comparisons below only *stat* the underlying ``upper``/``base`` (reads are safe);
    only the copy itself routes through ``merged``.

    Generation/mtime rule, walking every regular file ``rel`` under ``legacy``:

    - It is a **fallback edit** iff ``base[rel]`` is absent OR
      ``legacy[rel].st_mtime_ns > base[rel].st_mtime_ns``. A *fresh* legacy copy
      preserves host mtimes via ``copy2``, so an unedited file matches the base and
      is skipped — this filters the whole baseline tree out, preserving the
      overlay's disk savings (only the small edited set is ever copied).
    - A fallback edit is forwarded (via ``merged``) only when ``upper[rel]`` is absent
      OR ``legacy[rel].st_mtime_ns > upper[rel].st_mtime_ns``. **``upper`` wins ties:**
      an equal-or-newer upper edit is the agent's authoritative overlay-era change,
      and only a *strictly newer* legacy edit overwrites it.
    - **#404 host-divergence guard:** even a strictly-newer legacy file does not
      overwrite an existing ``upper`` *regular file* unless it provably diverges from
      the live host ``~/.claude`` (different mtime or size — see
      :func:`_legacy_is_unedited_host_copy`). The legacy copy preserves host mtimes via
      ``copy2``, so a fallback copy that is byte/mtime-identical to a host that merely
      changed *after* the agent's overlay edit is an *unedited* host copy, not an agent
      edit; forwarding it would silently drop the agent's overlay-era change. When
      ``upper[rel]`` is absent, a whiteout, or any non-regular entry there is nothing to
      protect, so the existing fallback-edit behaviour stands (this preserves the tested
      fallback-session re-edit forwarding, where the agent-edited legacy diverges).

    ``host_claude`` is the **live** host ``~/.claude`` (the production caller passes
    ``host_home / ".claude"``); it is the trusted reference for both the #404 guard
    above and the #402 deletion-forwarding pass below.

    Per-file ``OSError`` is caught and skipped — best-effort, never blocks
    provisioning. Never logs file contents (no secret leakage). Each copy goes through
    :func:`_safe_overlay_copy`, which descends with ``O_NOFOLLOW`` file descriptors and
    opens the leaf relative to the parent ``dir_fd``: ``merged``'s upper layer is
    agent-controlled, and a name-based copy would follow a planted symlink at any
    component, so an fd-based descent (no check/use gap) is required to keep this root
    write inside the ``.claude`` tree.

    *Source* symlinks are likewise refused: a fresh legacy copy is materialized via
    ``copytree(symlinks=False)`` so it never contains symlinks, but the agent can plant
    one in its ``rw`` legacy copy during the fallback session. ``copy2`` follows source
    symlinks by default, which would let this root worker read the link target (possibly
    outside the ``.claude`` tree) and surface its contents through the agent-visible
    overlay. Any symlink in the legacy tree is therefore skipped before it is stat'd.

    Known limitation: if the host ``~/.claude`` changed between when the overlay's
    base was built and when the fallback copy was taken, unedited legacy files may
    read as "newer than base" and be copied. There is still no data loss (an
    equal/newer ``upper`` always wins, and the copy is bounded to the differing
    set); the mtime comparison is intentionally conservative toward preserving edits.

    Fallback-era *deletions* (#402): a separate pass,
    :func:`_forward_fallback_deletions_as_whiteouts`, runs after this edit walk. It
    diffs ``base`` against ``legacy`` and synthesizes an overlayfs whiteout in
    ``upper`` for each file the agent confidently removed (the live host still matches
    ``base`` by mtime, size, *and* content), so the removal survives the next remount
    instead of the lower re-exposing it. Ambiguous removals (host lacks the path, or
    host changed it — including a same-length, timestamp-preserving credential
    rotation) and a missing ``CAP_MKNOD`` fail safe toward keeping the credential
    visible — a misclassified deletion can never *hide* a still-needed credential.

    Known limitation (edits inside an agent-planted directory symlink are not
    forwarded): ``os.walk`` runs with the default ``followlinks=False``, so if the
    agent replaced a subdirectory with a symlink (e.g. ``legacy/.config ->
    /other/dir``) during the fallback session, the walk lists it in ``_dirs`` but
    never descends, and files reachable only *through* that link are not yielded or
    forwarded. This is deliberate, not a regression: descending (``followlinks=True``)
    would let this root worker read content at the link target — possibly outside the
    ``.claude`` tree — and surface it through the agent-visible overlay, the same
    arbitrary root-read primitive the per-file source-symlink skip above guards
    against. The only "lost" data is content that never lived in the ``.claude`` copy
    to begin with (it lives at the link target); genuine edits to files in *real*
    subdirectories are walked and forwarded normally.

    Known limitation (a newer host write can un-delete an overlay deletion): an
    overlay-era deletion is recorded in ``upper`` as a whiteout character device,
    and ``_safe_mtime_ns`` returns that whiteout's *creation* mtime (when the agent
    deleted the file). The "upper wins ties" rule only guards equal mtimes, so if
    the host updates the same path *after* teardown and bumps the legacy copy's
    mtime strictly past the whiteout's creation mtime, ``legacy_mtime_ns >
    upper_mtime_ns`` holds and ``copy2`` replaces the whiteout with a regular file —
    silently restoring what the agent had intentionally removed. This is an inherent
    edge of the mtime-only approach (whiteouts carry no "deleted at" semantics stat
    can read) and is accepted as best-effort.
    """

    excluded = frozenset(_CLAUDE_USAGE_HISTORY_DIRS)
    for root, dirs, files in os.walk(legacy):
        root_path = Path(root)
        # Mirror the base copy's ``ignore_patterns(*_CLAUDE_USAGE_HISTORY_DIRS)``: the
        # shared base never holds these usage-history subtrees, so every file in one
        # reads as ``base_mtime_ns is None`` (an unconditional "fallback edit") and would
        # be forwarded whole. A long fallback session that filled ``projects/`` with
        # multi-GB transcripts would otherwise materialise all of it in the overlay upper,
        # negating the shared-base disk-savings goal. Prune them in place so the walk does
        # not descend, bounding reconcile to the same scope as the base itself.
        dirs[:] = [d for d in dirs if d not in excluded]
        for name in files:
            legacy_file = root_path / name
            if legacy_file.is_symlink():
                # A fresh legacy copy is materialized via ``copytree(symlinks=False)`` and
                # so never holds symlinks; any symlink here was planted by the (untrusted)
                # agent in the fallback session (the legacy copy is mounted ``rw`` for it).
                # ``shutil.copy2`` follows *source* symlinks by default, so the root worker
                # would read the link target — possibly outside the ``.claude`` tree — and
                # write its contents into the agent-visible overlay, an arbitrary root-read
                # primitive. The destination guard below only blocks writes *through* a
                # dest link, so a source link must be skipped here. Best-effort: drop just
                # this entry, never block provisioning.
                continue
            if not legacy_file.is_file():
                # Not a regular file: the agent can ``mkfifo`` (or plant a socket/device)
                # in the ``rw`` legacy copy during the fallback session. ``_safe_mtime_ns``
                # ``stat``s a FIFO fine, so it would otherwise slip through — but
                # ``shutil.copy2`` then ``open``s the source for reading, and a FIFO with no
                # peer writer blocks the root worker *indefinitely*, hanging provisioning
                # rather than completing the copy. (Not a symlink here — that is skipped
                # above.) Best-effort: drop just this entry.
                continue
            legacy_stat = _safe_stat(legacy_file)
            if legacy_stat is None:
                continue
            legacy_mtime_ns = legacy_stat.st_mtime_ns
            rel = legacy_file.relative_to(legacy)
            base_mtime_ns = _safe_mtime_ns(base / rel)
            is_fallback_edit = base_mtime_ns is None or legacy_mtime_ns > base_mtime_ns
            if not is_fallback_edit:
                continue
            upper_file = upper / rel
            # ``lstat`` the overlay entry: ``upper`` is agent-controlled and may hold a
            # planted symlink. Comparing the symlink's *own* mtime (not its target's)
            # is the semantically correct generation for the "upper wins ties" rule.
            upper_stat = _safe_stat(upper_file, follow_symlinks=False)
            if upper_stat is not None:
                if legacy_mtime_ns <= upper_stat.st_mtime_ns:
                    # ``upper`` wins ties: an equal-or-newer overlay-era edit stands.
                    continue
                if stat.S_ISREG(upper_stat.st_mode) and _legacy_is_unedited_host_copy(
                    legacy_stat, host_claude / rel
                ):
                    # #404: the strictly-newer legacy file is byte/mtime-identical to
                    # the live host, so it is an *unedited* copy of a host that changed
                    # after the agent's overlay edit — not an agent fallback-era edit.
                    # Forwarding it would silently drop the agent's overlay-era ``upper``
                    # edit, so fail safe and keep that edit. (A non-regular ``upper``
                    # entry — symlink/whiteout — has nothing to protect and falls through
                    # to the existing forwarding, preserving the tested re-edit case.)
                    continue
            # Copy via :func:`_safe_overlay_copy`, which descends *both* the destination
            # (under ``merged``) and the source (under the trusted ``legacy`` root) with
            # ``O_NOFOLLOW`` file descriptors and opens each leaf relative to its parent
            # ``dir_fd`` — so an agent-planted symlink at *any* component of either path
            # cannot redirect this root copy outside the ``.claude`` tree (no name-based
            # check/use gap). Passing ``legacy`` (not ``legacy_file``) lets the source be
            # descended component-by-component from a trusted anchor. Best-effort: it
            # swallows any structural conflict or ``OSError`` and drops just this file.
            _safe_overlay_copy(merged, rel, legacy)

    # Second pass (#402): forward fallback-era *deletions* as overlayfs whiteouts.
    _forward_fallback_deletions_as_whiteouts(
        legacy=legacy, upper=upper, base=base, host_claude=host_claude
    )
