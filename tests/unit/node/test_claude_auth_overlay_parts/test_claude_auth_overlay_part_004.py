"""Shared-base + per-workspace overlay isolation for ``~/.claude`` auth (part 4).

Continuation of :mod:`test_claude_auth_overlay_part_001`; covers overlay
retry/teardown and concurrent-mount (EBUSY) pin-recovery branches. The shared
:class:`FakeOverlayMounter` and ``_seed_host_claude`` helper are imported from
that part so every test in this package exercises the same fakes.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from structlog.testing import capture_logs

# The overlay primitives and Claude auth subsystem live in ``auth_mounts_claude``;
# ``auth_mounts`` re-exports them. Patch the module that *defines* the helpers so
# the consumers (which resolve them in that namespace) observe the override.
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
    # #405: a stale pin pointing at a now-vanished base is not trusted — the rebuild
    # re-pins ``base.signature`` to the freshly built base so future remounts are durable.
    assert (claude_root / "base.signature").read_text() == _host_claude_signature(host_home)
    assert overlay_data.read_text() == '{"theme": "agent-edited"}\n'


@pytest.mark.unit
def test_overlay_reboot_without_pin_rebuilds_and_repins(tmp_path: Path) -> None:
    # #405 (owner decision, supersedes the original "preserve the surviving upper"
    # encoding): a prior provision's base-pin WRITE failed (keep-live, no hard-fail), so
    # the overlay went live with no ``base.signature``. The agent mutated ``upper``; then
    # the host rebooted (the merged mount is gone) while ``upper``/``work`` survive on
    # disk. The next provision has no pin to trust and no live mount to recover the base
    # from, so the surviving NON-EMPTY upper is *unverifiable*: mounting it over a base
    # rebuilt from the current host would stack it over a GUESSED lower (a wrong-base
    # correctness gap if ``~/.claude`` had changed). The owner ruled wrong-base-correctness
    # WINS over credential-preservation for unverifiable bases, so the unpinned upper is
    # DISCARDED and a fresh empty upper is mounted over the current-host base. There is no
    # operator-visible credential loss: ``~/.claude`` (credentials included) is re-derived
    # from the CURRENT host base, which is exactly what the operator refreshed. The discard
    # is made LOUD via ``CLAUDE_AUTH_OVERLAY_UNPINNED_UPPER_DISCARDED_REBUILT`` so it is
    # auditable and never silent.
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_reboot_nopin",
        host_env={},
        overlay_mounter=mounter,
    )
    claude_root = work_dir / "auth" / "ws_reboot_nopin" / "claude"
    base_a = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    overlay_data = claude_root / "upper" / "settings.json"
    overlay_data.write_text('{"theme": "agent-edited"}\n')
    # Model the earlier pin-write failure: the overlay is live but carries no pin.
    (claude_root / "base.signature").unlink()
    # Reboot: the merged mount is gone; ``upper``/``work`` persist; host unchanged.
    mounter.mounted.clear()

    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_reboot_nopin",
            host_env={},
            overlay_mounter=mounter,
        )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    # A fresh empty upper is mounted over the base rebuilt from the current host ...
    assert mounter.mounts[-1]["lowerdir"] == base_a
    assert mounter.mounts[-1]["upperdir"] == claude_root / "upper"
    # ... the unpinned/unverifiable upper was discarded (its agent edit is gone) ...
    assert not overlay_data.exists()
    # ... the discard is loud and auditable (never silent) ...
    assert any(
        entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_UNPINNED_UPPER_DISCARDED_REBUILT"
        for entry in logs
    )
    # ... and the pin is now durable for future remounts (recorded post-mount).
    assert (claude_root / "base.signature").read_text() == _host_claude_signature(host_home)
    # No operator-visible credential loss: the mount is backed by the current-host base,
    # so ~/.claude (credentials included) is re-derived fresh from the current host.
    assert base_a.is_dir()


@pytest.mark.unit
def test_unpinned_upper_after_host_change_discarded_and_rebuilt(tmp_path: Path) -> None:
    # #405 owner decision, point 6 — the exact deferred edge case: an unpinned NON-EMPTY
    # overlay ``upper`` survives a reboot AND the operator changed ``~/.claude`` in the
    # meantime. There is no pin and no live mount to recover the true base, so the only
    # bases available are GUESSED from the changed host. Mounting the stale upper over such
    # a guessed base is the wrong-base correctness gap #405 escalated. Owner ruling:
    # discard the unverifiable upper, rebuild from the CURRENT host base, and emit the loud
    # reason code. From the operator's perspective there is no credential loss — credentials
    # are re-pulled fresh from the current host (which they just refreshed).
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    # Provision 1: a live overlay against base A; the agent mutates the writable upper.
    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_unpinned_hostchange",
        host_env={},
        overlay_mounter=mounter,
    )
    claude_root = work_dir / "auth" / "ws_unpinned_hostchange" / "claude"
    base_a = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    overlay_data = claude_root / "upper" / "settings.json"
    overlay_data.write_text('{"theme": "agent-edited"}\n')
    # Model the pin-write failure / pre-pin state: live but no ``base.signature``.
    (claude_root / "base.signature").unlink()

    # The operator changes the host ``~/.claude`` so a base rebuilt from it differs.
    (host_home / ".claude" / "settings.json").write_text('{"theme": "light"}\n')
    base_b = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    assert base_b != base_a

    # Reboot: the merged mount is gone; ``upper``/``work`` persist on disk.
    mounter.mounted.clear()

    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_unpinned_hostchange",
            host_env={},
            overlay_mounter=mounter,
        )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    # The mount is backed by the base rebuilt from the CURRENT host (base B), never the
    # stale upper over a guessed base — so credentials are re-derived from the current host.
    assert mounter.mounts[-1]["lowerdir"] == base_b
    assert mounter.mounts[-1]["lowerdir"] != base_a
    # The stale unpinned upper edit is discarded.
    assert not overlay_data.exists()
    # The discard is loud and auditable.
    assert any(
        entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_UNPINNED_UPPER_DISCARDED_REBUILT"
        for entry in logs
    )
    # The pin is re-recorded to the NEW host signature (durable, post-mount).
    assert (claude_root / "base.signature").read_text() == _host_claude_signature(host_home)


@pytest.mark.unit
def test_pinned_upper_with_unmatchable_signature_discarded(tmp_path: Path) -> None:
    # #405 owner decision, point 1(b): "a pin that cannot be matched to an available/known
    # base". A non-empty upper carries a ``base.signature`` naming a base that is neither on
    # disk nor equal to the current-host signature — the base is unverifiable, so a rebuild
    # would mount the stale upper over a GUESSED base. The owner ruling applies: discard +
    # rebuild from the current host + loud reason code.
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    # A surviving overlay whose pin names a foreign/stale base that does not exist on disk
    # and does not equal the current host signature.
    claude_root = work_dir / "auth" / "ws_unmatchable_pin" / "claude"
    (claude_root / "upper").mkdir(parents=True)
    (claude_root / "work").mkdir(parents=True)
    overlay_data = claude_root / "upper" / "settings.json"
    overlay_data.write_text('{"theme": "agent-edited"}\n')
    foreign_signature = "deadbeefdeadbeef"
    assert foreign_signature != _host_claude_signature(host_home)
    assert not _shared_claude_base_dir(work_dir, foreign_signature).is_dir()
    (claude_root / "base.signature").write_text(foreign_signature)

    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_unmatchable_pin",
            host_env={},
            overlay_mounter=mounter,
        )

    by_target = {m.target: m for m in mounts}
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    # Rebuilt from the current host (the foreign pin is not trusted) ...
    base = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    assert mounter.mounts[-1]["lowerdir"] == base
    # ... the stale upper edit is discarded ...
    assert not overlay_data.exists()
    # ... loudly ...
    assert any(
        entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_UNPINNED_UPPER_DISCARDED_REBUILT"
        for entry in logs
    )
    # ... and the pin is corrected to the current-host signature.
    assert (claude_root / "base.signature").read_text() == _host_claude_signature(host_home)


@pytest.mark.unit
def test_corrupted_signature_marker_discarded_not_crash(tmp_path: Path) -> None:
    # A ``base.signature`` marker corrupted into non-UTF-8 bytes must not crash
    # provisioning. ``Path.read_text()`` raises ``UnicodeDecodeError`` (a
    # ``ValueError``, NOT an ``OSError``) on such bytes; both ``_pinned_overlay_base``
    # and ``_pin_matches_signature`` must swallow it so the unverifiable non-empty
    # upper is discarded + rebuilt from the current host rather than propagating the
    # crash up through provisioning.
    host_home = tmp_path / "host-home"
    work_dir = tmp_path / "work"
    _seed_host_claude(host_home)
    mounter = FakeOverlayMounter(supported=True)

    claude_root = work_dir / "auth" / "ws_corrupt_pin" / "claude"
    (claude_root / "upper").mkdir(parents=True)
    (claude_root / "work").mkdir(parents=True)
    overlay_data = claude_root / "upper" / "settings.json"
    overlay_data.write_text('{"theme": "agent-edited"}\n')
    # Invalid UTF-8 continuation bytes — ``read_text()`` raises ``UnicodeDecodeError``.
    (claude_root / "base.signature").write_bytes(b"\xff\xfe\x00corrupt")

    with capture_logs() as logs:
        mounts = resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_corrupt_pin",
            host_env={},
            overlay_mounter=mounter,
        )

    by_target = {m.target: m for m in mounts}
    # Provisioning completed (no crash) and rebuilt from the current host ...
    assert by_target["/home/agent/.claude"].source == str(claude_root / "merged")
    base = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    assert mounter.mounts[-1]["lowerdir"] == base
    # ... the unverifiable upper edit is discarded loudly ...
    assert not overlay_data.exists()
    assert any(
        entry.get("reason_code") == "CLAUDE_AUTH_OVERLAY_UNPINNED_UPPER_DISCARDED_REBUILT"
        for entry in logs
    )
    # ... and the corrupted pin is rewritten to the current-host signature.
    assert (claude_root / "base.signature").read_text() == _host_claude_signature(host_home)


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
