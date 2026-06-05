"""Shared-base + per-workspace overlay isolation for ``~/.claude`` auth (part 7).

Continuation of :mod:`test_claude_auth_overlay_part_001`; covers the
per-workspace overlay ``flock`` that serializes the #405 unpinned-upper discard
against a concurrent same-workspace provision's overlay mount (#418/#419). The
DB-CAS provisioning claim is the primary guard; this filesystem ``flock`` is the
backstop for the narrow stale-lease-recovery window in which two provisions of
the same workspace can overlap. Contention is simulated by the test holding the
real ``flock`` on a second file descriptor — exactly the technique the base-build
lock tests use (:func:`_claude_base_staging_build_is_live`), since ``flock``
denies a second open file description even within one process.

The shared :class:`FakeOverlayMounter` and ``_seed_host_claude`` helper are
imported from part 1 so every test in this package exercises the same fakes.
"""

from __future__ import annotations

import errno
import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from structlog.testing import capture_logs

# Patch the module that defines the overlay/Claude helpers (see part_001 header).
from awf.node import auth_mounts_claude as auth_mounts_mod
from awf.node.auth_mounts import (
    _host_claude_signature,
    _shared_claude_base_dir,
    resolve_service_auth_mounts,
)

from .test_claude_auth_overlay_part_001 import (
    FakeOverlayMounter as FakeOverlayMounter,
)
from .test_claude_auth_overlay_part_001 import (
    _seed_host_claude as _seed_host_claude,
)

_LOCK_NAME = auth_mounts_mod._OVERLAY_PROVISION_LOCK_NAME
_CONTENDED = "CLAUDE_AUTH_OVERLAY_DISCARD_LOCK_CONTENDED"
_SKIPPED_LIVE = "CLAUDE_AUTH_OVERLAY_DISCARD_SKIPPED_LIVE_MOUNT"
_UNAVAILABLE = "CLAUDE_AUTH_OVERLAY_PROVISION_LOCK_UNAVAILABLE"
_DISCARDED = "CLAUDE_AUTH_OVERLAY_UNPINNED_UPPER_DISCARDED_REBUILT"


@contextmanager
def _concurrent_provision_holding_overlay_lock(claude_root: Path) -> Iterator[None]:
    """Hold the per-workspace overlay ``flock`` to stand in for a racing provision.

    Opens ``<claude_root>/.overlay.lock`` on a *separate* file descriptor and takes
    the exclusive lock, so the production code's own ``flock`` attempt is denied
    (``EWOULDBLOCK``) — the stale-lease window a duplicate same-workspace provision
    would create. The lock is released on exit so the test cleans up after itself.
    """

    claude_root.mkdir(parents=True, exist_ok=True)
    holder_fd = os.open(claude_root / _LOCK_NAME, os.O_CREAT | os.O_WRONLY, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        yield
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)


@pytest.mark.unit
def test_unpinned_upper_discard_serialized_by_overlay_flock(tmp_path: Path) -> None:
    # The core #418/#419 regression. A non-empty, unpinned overlay ``upper`` carrying
    # the agent's edits survives, and a concurrent same-workspace provision holds the
    # overlay lock (the stale-lease window). Without serialization the #405 discard
    # would ``rmtree`` that ``upper``/``work`` — which the lock holder may have just
    # mounted as the live overlay's backing layers — destroying the agent's edits.
    # The lock forces this racing duplicate to skip the discard entirely.
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)

    claude_root = work_dir / "auth" / "ws_lock_serialized" / "claude"
    (claude_root / "upper").mkdir(parents=True)
    (claude_root / "work").mkdir(parents=True)
    # A non-empty, unpinned (no ``base.signature``) upper — the #405 discard candidate.
    overlay_data = claude_root / "upper" / "settings.json"
    overlay_data.write_text('{"theme": "agent-edited"}\n')

    mounter = FakeOverlayMounter(supported=True)

    with (
        _concurrent_provision_holding_overlay_lock(claude_root),
        capture_logs() as logs,
    ):
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_lock_serialized",
            host_env={},
            overlay_mounter=mounter,
        )

    # The agent's unpinned upper survives — the discard was serialized away from the
    # (possibly in-use) backing layer rather than racing the holder's mount.
    assert overlay_data.read_text() == '{"theme": "agent-edited"}\n'
    # No fresh off-lock mount was issued (an unserialized mount would reopen the race).
    assert mounter.mounts == []
    # The contention is observable, and the destructive discard is NOT emitted.
    assert any(entry.get("reason_code") == _CONTENDED for entry in logs)
    assert not any(entry.get("reason_code") == _DISCARDED for entry in logs)
    # Auth degrades to a fresh legacy ``.claude`` copy for this round; a later
    # provision (after the holder releases) pins/mounts the overlay correctly.
    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / ".claude")
    assert (claude_root / ".claude" / "settings.json").is_file()


@pytest.mark.unit
def test_contended_lock_reuses_live_overlay(tmp_path: Path) -> None:
    # The lock is contended AND the holder has already mounted ``merged``: the racing
    # duplicate must reuse the live overlay read-through rather than discard, remount,
    # or pin (the holder owns pinning). This is the safe path that keeps the holder's
    # backing layers and the agent's edits intact.
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)

    claude_root = work_dir / "auth" / "ws_lock_reuse" / "claude"
    (claude_root / "upper").mkdir(parents=True)
    (claude_root / "work").mkdir(parents=True)
    overlay_data = claude_root / "upper" / "settings.json"
    overlay_data.write_text('{"theme": "agent-edited"}\n')
    merged = claude_root / "merged"

    mounter = FakeOverlayMounter(supported=True)
    # The lock holder already mounted the overlay (pre-populate the fake's live set).
    mounter.mounted.add(merged)

    with (
        _concurrent_provision_holding_overlay_lock(claude_root),
        capture_logs() as logs,
    ):
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_lock_reuse",
            host_env={},
            overlay_mounter=mounter,
        )

    by_target = {m.target: m for m in mounts}
    # The live overlay is reused, not re-mounted ...
    assert by_target["/home/agent/.claude"].source == str(merged)
    assert mounter.mounts == []
    # ... the holder's backing upper survives ...
    assert overlay_data.read_text() == '{"theme": "agent-edited"}\n'
    # ... no pin is written (the holder owns pinning) ...
    assert not (claude_root / "base.signature").exists()
    # ... and the contention is observable.
    assert any(entry.get("reason_code") == _CONTENDED for entry in logs)


@pytest.mark.unit
def test_overlay_provision_acquires_and_releases_flock(tmp_path: Path) -> None:
    # The happy path: with no concurrent holder the provision acquires the lock,
    # mounts the overlay, and releases the lock in its ``finally``. None of the
    # lock-specific reason codes are emitted on this uncontended path.
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_lock_happy",
            host_env={},
            overlay_mounter=mounter,
        )

    claude_root = work_dir / "auth" / "ws_lock_happy" / "claude"
    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    assert len(mounter.mounts) == 1

    # The lock was released in ``finally``: the test can now take it itself. If the
    # production code had leaked the descriptor's lock this non-blocking acquire
    # would raise ``BlockingIOError``.
    verify_fd = os.open(claude_root / _LOCK_NAME, os.O_RDONLY)
    try:
        fcntl.flock(verify_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(verify_fd, fcntl.LOCK_UN)
    finally:
        os.close(verify_fd)

    for code in (_CONTENDED, _SKIPPED_LIVE, _UNAVAILABLE):
        assert not any(entry.get("reason_code") == code for entry in logs)


@pytest.mark.unit
def test_unpinned_upper_discard_runs_under_lock_when_uncontended(tmp_path: Path) -> None:
    # Guard that the lock did not regress #405/#416: with the lock acquired (no
    # concurrent holder) a surviving non-empty unverifiable upper is still discarded
    # and rebuilt over the current-host base, exactly as before the lock was added.
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    # Provision 1: a live overlay against base A; the agent mutates the upper.
    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_lock_discard",
        host_env={},
        overlay_mounter=mounter,
    )
    claude_root = work_dir / "auth" / "ws_lock_discard" / "claude"
    base_a = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    overlay_data = claude_root / "upper" / "settings.json"
    overlay_data.write_text('{"theme": "agent-edited"}\n')
    # Model the pre-pin / pin-write-failure state: live but no ``base.signature``.
    (claude_root / "base.signature").unlink()

    # The operator changes the host ``~/.claude`` so a rebuilt base differs, and a
    # reboot drops the merged mount while ``upper``/``work`` persist on disk.
    (host_home / ".claude" / "settings.json").write_text('{"theme": "light"}\n')
    base_b = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    assert base_b != base_a
    mounter.mounted.clear()

    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_lock_discard",
            host_env={},
            overlay_mounter=mounter,
        )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    # Rebuilt over the CURRENT host base (base B), never the stale upper over a guess.
    assert mounter.mounts[-1]["lowerdir"] == base_b
    assert not overlay_data.exists()
    # The discard is loud (#416), and no lock-contention / skip codes fired (it ran
    # under an uncontended, acquired lock).
    assert any(entry.get("reason_code") == _DISCARDED for entry in logs)
    for code in (_CONTENDED, _SKIPPED_LIVE, _UNAVAILABLE):
        assert not any(entry.get("reason_code") == code for entry in logs)


@pytest.mark.unit
def test_overlay_provision_lock_unavailable_proceeds_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The lock file cannot be hosted (an FS fault: ENOSPC/EROFS/EPERM). Provisioning
    # must proceed best-effort exactly as before the lock — discard/mount unserialized
    # — and log the unavailability exactly once so the degraded posture is auditable.
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    real_os_open = os.open

    def _open_failing_only_lock_file(path: object, *args: object, **kwargs: object) -> int:
        # Fail solely for the overlay lock file; delegate everything else (the
        # shared-base ``.build.lock``, scratch dirs, the mknod whiteout fake) so the
        # rest of provisioning is exercised unchanged.
        if os.fspath(path).endswith(_LOCK_NAME):
            raise OSError("read-only file system")
        return real_os_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(auth_mounts_mod.os, "open", _open_failing_only_lock_file)

    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_lock_unavailable",
            host_env={},
            overlay_mounter=mounter,
        )

    claude_root = work_dir / "auth" / "ws_lock_unavailable" / "claude"
    by_target = {m.target: m for m in mounts}
    # Provisioning still produced a live overlay (best-effort, unserialized).
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    assert len(mounter.mounts) == 1
    # The unavailability is logged exactly once, and no contention was reported.
    assert sum(1 for entry in logs if entry.get("reason_code") == _UNAVAILABLE) == 1
    assert not any(entry.get("reason_code") == _CONTENDED for entry in logs)


@pytest.mark.unit
def test_flock_unsupported_proceeds_best_effort_not_contended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The lock file is created, but the FS does not support advisory locking, so
    # ``fcntl.flock`` raises a non-blocking ``OSError`` (ENOTSUP/ENOSYS/EINVAL).
    # This must be treated as ``"unavailable"`` (best-effort, unserialized overlay),
    # NOT ``"contended"`` — otherwise overlays would be permanently disabled and
    # degrade to the legacy copy on every provision on such a filesystem.
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    # ``fcntl`` is the same module object everywhere, so patching its ``flock`` would
    # also break the shared-base ``.build.lock``. Scope the failure to *only* the
    # overlay provision lock's fd by tracking which fd was opened for ``.overlay.lock``.
    real_os_open = os.open
    real_flock = fcntl.flock
    overlay_lock_fds: set[int] = set()

    def _track_overlay_lock_fd(path: object, *args: object, **kwargs: object) -> int:
        fd = real_os_open(path, *args, **kwargs)  # type: ignore[arg-type]
        if os.fspath(path).endswith(_LOCK_NAME):
            overlay_lock_fds.add(fd)
        return fd

    def _flock_unsupported(fd: int, operation: int) -> None:
        # Only the overlay provision lock's non-blocking acquire raises; the release
        # in ``finally`` and the base-build ``flock`` pass through unchanged.
        if fd in overlay_lock_fds and operation == (fcntl.LOCK_EX | fcntl.LOCK_NB):
            raise OSError(errno.ENOTSUP, "operation not supported")
        return real_flock(fd, operation)

    monkeypatch.setattr(auth_mounts_mod.os, "open", _track_overlay_lock_fd)
    monkeypatch.setattr(auth_mounts_mod.fcntl, "flock", _flock_unsupported)

    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_flock_unsupported",
            host_env={},
            overlay_mounter=mounter,
        )

    claude_root = work_dir / "auth" / "ws_flock_unsupported" / "claude"
    by_target = {m.target: m for m in mounts}
    # Provisioning still produced a live overlay (best-effort, unserialized) — it did
    # not degrade to the legacy copy.
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    assert len(mounter.mounts) == 1
    # The unavailability is logged exactly once, and contention was NOT reported.
    assert sum(1 for entry in logs if entry.get("reason_code") == _UNAVAILABLE) == 1
    assert not any(entry.get("reason_code") == _CONTENDED for entry in logs)


@pytest.mark.unit
def test_close_oserror_does_not_mask_body_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If ``os.close`` raises ``OSError`` in the lock's ``finally`` (e.g. ``EIO`` on a
    # flush-on-close over NFS), it must NOT mask an exception already propagating from
    # the ``with`` body — the close is suppressed exactly like the ``flock(LOCK_UN)``
    # release above it (PRRT_kwDOSJAM6s6HX0fq).
    claude_root = tmp_path / "claude"
    claude_root.mkdir(parents=True)

    real_os_close = os.close
    closed_fds: list[int] = []

    def _close_raising(fd: int) -> None:
        # Close for real so the descriptor does not leak, then raise to model a
        # flush-on-close fault surfacing from the ``finally``.
        real_os_close(fd)
        closed_fds.append(fd)
        raise OSError(errno.EIO, "input/output error")

    monkeypatch.setattr(auth_mounts_mod.os, "close", _close_raising)

    with (
        pytest.raises(RuntimeError, match="body boom"),
        auth_mounts_mod._overlay_provision_lock(claude_root) as state,
    ):
        assert state == "acquired"
        raise RuntimeError("body boom")

    # The close ran (and raised, then was suppressed) — the body's RuntimeError, not
    # the swallowed OSError, is what propagated.
    assert len(closed_fds) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "would_block_errno",
    [errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK],
)
def test_flock_eacces_treated_as_contended_not_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, would_block_errno: int
) -> None:
    # Some systems/filesystems report a non-blocking ``flock`` conflict as ``EACCES``
    # (and Python's ``fcntl`` docs call out both ``EACCES`` and ``EAGAIN``). Such a
    # conflict arrives as a plain ``OSError`` rather than ``BlockingIOError``, so it
    # must still be treated as genuine contention (``"contended"``) — NOT
    # ``"unavailable"`` — otherwise the caller proceeds unserialized and reopens the
    # stale-lease discard-vs-mount race the lock exists to close
    # (PRRT_kwDOSJAM6s6HYm1i).
    claude_root = tmp_path / "claude"
    claude_root.mkdir(parents=True)

    real_flock = fcntl.flock

    def _flock_would_block(fd: int, operation: int) -> None:
        if operation == (fcntl.LOCK_EX | fcntl.LOCK_NB):
            raise OSError(would_block_errno, os.strerror(would_block_errno))
        return real_flock(fd, operation)

    monkeypatch.setattr(auth_mounts_mod.fcntl, "flock", _flock_would_block)

    with (
        capture_logs() as logs,
        auth_mounts_mod._overlay_provision_lock(claude_root) as state,
    ):
        assert state == "contended"

    # Contention must not be misreported as the unsupported/unavailable path.
    assert not any(entry.get("reason_code") == _UNAVAILABLE for entry in logs)
