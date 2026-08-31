"""Real scratch overlay mount probe and its worker→api evidence channel (#874).

The pre-#874 preflight grepped ``/proc/filesystems`` and the ``CAP_SYS_ADMIN``
bit, both blind to the LSM layer. On a worker confined by Docker's
``docker-default`` AppArmor profile — whose plain, non-auditing ``deny mount,``
rule blocks ``mount(2)`` regardless of capability — it reported "supported" and
every mount then failed, after a ~1.9 GB shared-base copy had already been built.

No test here performs a real mount: the container running these tests has no
``CAP_SYS_ADMIN`` and is itself AppArmor-confined. Everything is driven through
the injected ``run`` callable, ``monkeypatch`` and ``tmp_path``.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from awf.node import auth_mounts_overlay_probe as probe_mod
from awf.node.auth_mounts_overlay_probe import (
    OverlayProbeResult,
    cached_overlay_probe,
    discard_overlay_probe_evidence,
    force_copy_isolation_requested,
    overlay_probe_evidence_path,
    overlay_probe_expected,
    overlay_probe_scratch_root,
    overlay_unexpectedly_unavailable,
    probe_overlay_mount,
    read_overlay_probe_evidence,
    reset_overlay_probe_cache,
    write_overlay_probe_evidence,
)


class _FakeRun:
    """Records ``mount``/``umount`` invocations and raises the configured error."""

    def __init__(
        self,
        *,
        mount_error: BaseException | None = None,
        umount_error: BaseException | None = None,
    ) -> None:
        self.mount_error = mount_error
        self.umount_error = umount_error
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        if args[0] == "mount" and self.mount_error is not None:
            raise self.mount_error
        if args[0] == "umount" and self.umount_error is not None:
            raise self.umount_error
        return subprocess.CompletedProcess(list(args), 0, "", "")

    @property
    def commands(self) -> list[str]:
        return [call[0] for call in self.calls]


def _staging_dirs(scratch_root: Path) -> list[Path]:
    if not scratch_root.is_dir():
        return []
    return sorted(scratch_root.glob(".overlay-probe-*"))


def _refused_error() -> subprocess.CalledProcessError:
    # util-linux retries read-only after ``EACCES``; exit 32 with this stderr is
    # exactly what the operator's AppArmor-confined worker produced.
    return subprocess.CalledProcessError(
        32,
        ["mount"],
        output="",
        stderr="mount: /scratch/merged: cannot mount overlay read-only.\n",
    )


@pytest.mark.unit
def test_probe_reports_ok_and_cleans_up_staging(tmp_path: Path) -> None:
    scratch_root = tmp_path / "auth" / "_shared"
    run = _FakeRun()

    result = probe_overlay_mount(scratch_root=scratch_root, run=run)

    assert result == OverlayProbeResult(ok=True, reason="OVERLAY_PROBE_OK", detail="")
    assert run.commands == ["mount", "umount"]
    assert _staging_dirs(scratch_root) == []


@pytest.mark.unit
def test_probe_mount_options_reference_the_staging_layers(tmp_path: Path) -> None:
    scratch_root = tmp_path / "auth" / "_shared"
    run = _FakeRun()

    probe_overlay_mount(scratch_root=scratch_root, run=run)

    mount_call = run.calls[0]
    assert mount_call[:5] == ["mount", "-t", "overlay", "overlay", "-o"]
    options = mount_call[5]
    assert "lowerdir=" in options
    assert "upperdir=" in options
    assert "workdir=" in options
    assert mount_call[6].endswith("/merged")


@pytest.mark.unit
def test_probe_reports_refused_with_stderr_detail(tmp_path: Path) -> None:
    scratch_root = tmp_path / "auth" / "_shared"
    run = _FakeRun(mount_error=_refused_error())

    result = probe_overlay_mount(scratch_root=scratch_root, run=run)

    assert result.ok is False
    assert result.reason == "REFUSED"
    assert "cannot mount overlay read-only" in result.detail
    assert "32" in result.detail
    assert run.commands == ["mount"]
    assert _staging_dirs(scratch_root) == []


@pytest.mark.unit
def test_probe_refused_detail_is_truncated(tmp_path: Path) -> None:
    scratch_root = tmp_path / "auth" / "_shared"
    run = _FakeRun(
        mount_error=subprocess.CalledProcessError(32, ["mount"], stderr="x" * 5_000),
    )

    result = probe_overlay_mount(scratch_root=scratch_root, run=run)

    assert result.reason == "REFUSED"
    assert len(result.detail) <= 512


@pytest.mark.unit
def test_probe_refused_on_os_error(tmp_path: Path) -> None:
    scratch_root = tmp_path / "auth" / "_shared"
    run = _FakeRun(mount_error=PermissionError("operation not permitted"))

    result = probe_overlay_mount(scratch_root=scratch_root, run=run)

    assert result.ok is False
    assert result.reason == "REFUSED"
    assert "operation not permitted" in result.detail


@pytest.mark.unit
@pytest.mark.parametrize(
    "mount_error",
    [
        pytest.param(subprocess.CalledProcessError(-9, ["mount"], stderr=""), id="signalled"),
        pytest.param(PermissionError("operation not permitted"), id="os-error"),
    ],
)
def test_probe_keeps_staging_when_failed_mount_left_merged_mounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mount_error: BaseException
) -> None:
    # A ``mount(8)`` that reports failure normally means ``mount(2)`` never
    # landed, but the two are not the same event: a helper killed by a signal
    # after the syscall succeeded still exits non-zero. Recursively deleting then
    # would descend through a live overlay, so staging is retained whenever
    # ``merged`` is still a mount point.
    scratch_root = tmp_path / "auth" / "_shared"
    monkeypatch.setattr(probe_mod, "_is_mounted", lambda path: path.name == "merged")
    run = _FakeRun(mount_error=mount_error)

    result = probe_overlay_mount(scratch_root=scratch_root, run=run)

    assert result.ok is False
    assert result.reason == "REFUSED"
    retained = _staging_dirs(scratch_root)
    assert len(retained) == 1
    assert str(retained[0]) in result.detail


@pytest.mark.unit
@pytest.mark.parametrize(
    "umount_error",
    [
        pytest.param(subprocess.CalledProcessError(1, ["umount"], stderr="busy"), id="refused"),
        pytest.param(subprocess.TimeoutExpired(["umount"], 10.0), id="timeout"),
        pytest.param(FileNotFoundError("umount"), id="binary-missing"),
    ],
)
def test_probe_rejects_a_mount_it_cannot_unmount(
    tmp_path: Path, umount_error: BaseException
) -> None:
    # A mounter that cannot tear the scratch overlay down cannot tear production
    # overlays down either, so reporting ``ok`` would enable overlays that leak
    # live mounts at workspace cleanup. Never ``rmtree`` under a possibly-live
    # mount either: the staging tree is retained and its path recorded.
    scratch_root = tmp_path / "auth" / "_shared"
    run = _FakeRun(umount_error=umount_error)

    result = probe_overlay_mount(scratch_root=scratch_root, run=run)

    assert result.ok is False
    assert result.reason == "UMOUNT_FAILED"
    retained = _staging_dirs(scratch_root)
    assert len(retained) == 1
    assert str(retained[0]) in result.detail


@pytest.mark.unit
def test_probe_rejects_a_mount_still_live_after_a_successful_umount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``umount(8)`` exiting 0 is not the same event as the mount being gone (a
    # lazy/deferred detach still leaves it live). The mount must be *confirmed*
    # unmounted before the probe blesses production overlays — and before the
    # staging tree is recursively deleted through the merged view.
    scratch_root = tmp_path / "auth" / "_shared"
    monkeypatch.setattr(probe_mod, "_is_mounted", lambda path: path.name == "merged")
    run = _FakeRun()

    result = probe_overlay_mount(scratch_root=scratch_root, run=run)

    assert result.ok is False
    assert result.reason == "UMOUNT_FAILED"
    assert run.commands == ["mount", "umount"]
    retained = _staging_dirs(scratch_root)
    assert len(retained) == 1
    assert str(retained[0]) in result.detail


@pytest.mark.unit
def test_probe_reports_missing_mount_binary(tmp_path: Path) -> None:
    scratch_root = tmp_path / "auth" / "_shared"
    run = _FakeRun(mount_error=FileNotFoundError("mount"))

    result = probe_overlay_mount(scratch_root=scratch_root, run=run)

    assert result.ok is False
    assert result.reason == "MOUNT_BINARY_MISSING"
    assert _staging_dirs(scratch_root) == []


@pytest.mark.unit
def test_probe_keeps_staging_when_mount_times_out(tmp_path: Path) -> None:
    # Killing the timed-out ``mount(8)`` does not undo a ``mount(2)`` that already
    # landed, so ``merged`` may be a live overlay pinning lower/upper/work. Same
    # reasoning as the umount-failure branch: retain rather than recursively
    # delete through a possibly-live merged view.
    scratch_root = tmp_path / "auth" / "_shared"
    run = _FakeRun(mount_error=subprocess.TimeoutExpired(["mount"], 10.0))

    result = probe_overlay_mount(scratch_root=scratch_root, run=run)

    assert result.ok is False
    assert result.reason == "TIMEOUT"
    retained = _staging_dirs(scratch_root)
    assert len(retained) == 1
    assert str(retained[0]) in result.detail


@pytest.mark.unit
def test_probe_reports_scratch_unavailable_when_root_is_a_file(tmp_path: Path) -> None:
    scratch_root = tmp_path / "not-a-dir"
    scratch_root.write_text("occupied\n")
    run = _FakeRun()

    result = probe_overlay_mount(scratch_root=scratch_root, run=run)

    assert result.ok is False
    assert result.reason == "SCRATCH_UNAVAILABLE"
    assert run.calls == []


@pytest.mark.unit
def test_probe_reports_scratch_unavailable_when_mkdtemp_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch_root = tmp_path / "auth" / "_shared"

    def _boom(*_args: object, **_kwargs: object) -> str:
        raise OSError("no space left on device")

    monkeypatch.setattr(probe_mod.tempfile, "mkdtemp", _boom)
    run = _FakeRun()

    result = probe_overlay_mount(scratch_root=scratch_root, run=run)

    assert result.ok is False
    assert result.reason == "SCRATCH_UNAVAILABLE"
    assert "no space left on device" in result.detail
    assert run.calls == []


@pytest.mark.unit
def test_probe_reports_scratch_unavailable_when_layer_creation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch_root = tmp_path / "auth" / "_shared"
    vanished = tmp_path / "vanished-staging"

    monkeypatch.setattr(probe_mod.tempfile, "mkdtemp", lambda **_kwargs: str(vanished))
    run = _FakeRun()

    result = probe_overlay_mount(scratch_root=scratch_root, run=run)

    assert result.ok is False
    assert result.reason == "SCRATCH_UNAVAILABLE"
    assert run.calls == []


@pytest.mark.unit
@pytest.mark.parametrize("segment", ["comma,dir", "colon:dir"])
def test_probe_reports_reserved_chars_without_touching_the_filesystem(
    tmp_path: Path, segment: str
) -> None:
    # ``mount -o`` offers no escaping for ``,``/``:``; such a host is a *expected*
    # copy-fallback platform, not a misconfiguration.
    scratch_root = tmp_path / segment / "auth" / "_shared"
    run = _FakeRun()

    result = probe_overlay_mount(scratch_root=scratch_root, run=run)

    assert result.ok is False
    assert result.reason == "PATH_RESERVED_CHARS"
    assert run.calls == []
    assert not scratch_root.exists()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("reason", "ok", "expected"),
    [
        ("OVERLAY_PROBE_OK", True, True),
        ("REFUSED", False, False),
        ("TIMEOUT", False, False),
        ("UMOUNT_FAILED", False, False),
        ("MOUNT_BINARY_MISSING", False, True),
        ("SCRATCH_UNAVAILABLE", False, True),
        ("PATH_RESERVED_CHARS", False, True),
    ],
)
def test_expected_classification(reason: str, ok: bool, expected: bool) -> None:
    # Only a refusal/timeout on a host that passed every cheap gate is
    # *unexpected*. Everything else is a legitimate platform property and must
    # stay silent (the awf-cloud/GKE constraint).
    assert overlay_probe_expected(OverlayProbeResult(ok=ok, reason=reason)) is expected


@pytest.mark.unit
def test_cached_probe_runs_once_per_scratch_root(tmp_path: Path) -> None:
    scratch_root = tmp_path / "auth" / "_shared"
    other_root = tmp_path / "other" / "_shared"
    run = _FakeRun()

    first = cached_overlay_probe(scratch_root=scratch_root, run=run)
    second = cached_overlay_probe(scratch_root=scratch_root, run=run)

    assert first == second
    assert run.commands == ["mount", "umount"]

    cached_overlay_probe(scratch_root=other_root, run=run)

    assert run.commands == ["mount", "umount", "mount", "umount"]


@pytest.mark.unit
def test_reset_overlay_probe_cache_forces_a_reprobe(tmp_path: Path) -> None:
    scratch_root = tmp_path / "auth" / "_shared"
    run = _FakeRun()

    cached_overlay_probe(scratch_root=scratch_root, run=run)
    reset_overlay_probe_cache()
    cached_overlay_probe(scratch_root=scratch_root, run=run)

    assert run.commands == ["mount", "umount", "mount", "umount"]


@pytest.mark.unit
def test_cached_probe_writes_evidence_once(tmp_path: Path) -> None:
    scratch_root = tmp_path / "auth" / "_shared"
    run = _FakeRun(mount_error=_refused_error())

    cached_overlay_probe(scratch_root=scratch_root, run=run)
    evidence_path = overlay_probe_evidence_path(scratch_root)
    first = evidence_path.read_text()
    cached_overlay_probe(scratch_root=scratch_root, run=run)

    assert evidence_path.read_text() == first
    payload = json.loads(first)
    assert payload["ok"] is False
    assert payload["reason"] == "REFUSED"
    assert payload["expected"] is False


@pytest.mark.unit
def test_evidence_round_trip_records_injected_timestamp(tmp_path: Path) -> None:
    scratch_root = tmp_path / "auth" / "_shared"
    scratch_root.mkdir(parents=True)
    checked_at = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)

    write_overlay_probe_evidence(
        scratch_root,
        OverlayProbeResult(ok=False, reason="REFUSED", detail="exit=32"),
        now=checked_at,
    )
    evidence = read_overlay_probe_evidence(tmp_path)

    assert evidence == {
        "ok": False,
        "reason": "REFUSED",
        "expected": False,
        "detail": "exit=32",
        "checked_at": "2026-08-30T12:00:00+00:00",
    }


@pytest.mark.unit
def test_evidence_defaults_checked_at_to_now(tmp_path: Path) -> None:
    scratch_root = overlay_probe_scratch_root(tmp_path)
    scratch_root.mkdir(parents=True)

    write_overlay_probe_evidence(
        scratch_root, OverlayProbeResult(ok=True, reason="OVERLAY_PROBE_OK")
    )
    evidence = read_overlay_probe_evidence(tmp_path)

    assert evidence is not None
    assert datetime.fromisoformat(str(evidence["checked_at"])).tzinfo is not None


@pytest.mark.unit
def test_evidence_write_is_suppressed_when_the_directory_is_missing(tmp_path: Path) -> None:
    # Best-effort, mirroring ``_record_overlay_base_pin``: a failed write only
    # forfeits observability; it must never fail provisioning.
    scratch_root = tmp_path / "absent" / "_shared"

    write_overlay_probe_evidence(
        scratch_root, OverlayProbeResult(ok=True, reason="OVERLAY_PROBE_OK")
    )

    assert read_overlay_probe_evidence(tmp_path) is None


@pytest.mark.unit
def test_read_evidence_returns_none_when_absent(tmp_path: Path) -> None:
    assert read_overlay_probe_evidence(tmp_path) is None


@pytest.mark.unit
def test_read_evidence_returns_none_on_invalid_json(tmp_path: Path) -> None:
    scratch_root = overlay_probe_scratch_root(tmp_path)
    scratch_root.mkdir(parents=True)
    overlay_probe_evidence_path(scratch_root).write_text("{not json")

    assert read_overlay_probe_evidence(tmp_path) is None


@pytest.mark.unit
def test_read_evidence_returns_none_on_non_mapping_json(tmp_path: Path) -> None:
    scratch_root = overlay_probe_scratch_root(tmp_path)
    scratch_root.mkdir(parents=True)
    overlay_probe_evidence_path(scratch_root).write_text("[1, 2]")

    assert read_overlay_probe_evidence(tmp_path) is None


@pytest.mark.unit
def test_read_evidence_returns_none_on_unreadable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force a reader failure instead of chmod(0o000): root bypasses mode bits, so
    # a permission-only fixture can still decode the JSON and fail the assertion.
    scratch_root = overlay_probe_scratch_root(tmp_path)
    scratch_root.mkdir(parents=True)
    evidence_path = overlay_probe_evidence_path(scratch_root)
    evidence_path.write_text("{}")
    original_read_text = Path.read_text

    def _raise_for_evidence(self: Path, *args: object, **kwargs: object) -> str:
        if self == evidence_path:
            raise PermissionError(13, "Permission denied", str(self))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _raise_for_evidence)

    assert read_overlay_probe_evidence(tmp_path) is None


@pytest.mark.unit
def test_read_evidence_returns_none_on_invalid_utf8(tmp_path: Path) -> None:
    # ``Path.read_text()`` raises ``UnicodeDecodeError`` (a ``ValueError``, not an
    # ``OSError``) on non-UTF-8 bytes; that must degrade to "no signal" the same way
    # as malformed JSON, or readiness/status checks raise instead of warning.
    scratch_root = overlay_probe_scratch_root(tmp_path)
    scratch_root.mkdir(parents=True)
    overlay_probe_evidence_path(scratch_root).write_bytes(b"\xff\xfe\x00corrupt")

    assert read_overlay_probe_evidence(tmp_path) is None
    assert overlay_unexpectedly_unavailable(tmp_path) is False


def _write_evidence(work_dir: Path, payload: object) -> None:
    scratch_root = overlay_probe_scratch_root(work_dir)
    scratch_root.mkdir(parents=True, exist_ok=True)
    overlay_probe_evidence_path(scratch_root).write_text(json.dumps(payload))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("payload", "unexpected"),
    [
        ({"ok": False, "expected": False}, True),
        ({"ok": False, "expected": True}, False),
        ({"ok": True, "expected": True}, False),
        ({"ok": True, "expected": False}, False),
        ({"reason": "REFUSED"}, False),
        ({"ok": "false", "expected": "false"}, False),
    ],
)
def test_overlay_unexpectedly_unavailable_truth_table(
    tmp_path: Path, payload: dict[str, object], unexpected: bool
) -> None:
    _write_evidence(tmp_path, payload)

    assert overlay_unexpectedly_unavailable(tmp_path) is unexpected


@pytest.mark.unit
def test_overlay_unexpectedly_unavailable_is_false_without_evidence(tmp_path: Path) -> None:
    # The awf-cloud/GKE regression, at its source: absence of evidence means
    # silence, by construction.
    assert overlay_unexpectedly_unavailable(tmp_path) is False


@pytest.mark.unit
def test_overlay_unexpectedly_unavailable_is_false_on_corrupt_evidence(tmp_path: Path) -> None:
    scratch_root = overlay_probe_scratch_root(tmp_path)
    scratch_root.mkdir(parents=True)
    overlay_probe_evidence_path(scratch_root).write_text("nonsense")

    assert overlay_unexpectedly_unavailable(tmp_path) is False


@pytest.mark.unit
@pytest.mark.parametrize("flag", ["1", "true", "TRUE", " yes ", "on"])
def test_force_copy_posture_ignores_evidence_from_an_earlier_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    # A host refused once, then restarted under force-copy: it fails the first
    # cheap gate and never probes again, so the file on disk describes a posture
    # it no longer runs. The copy fallback it now takes is fully supported and
    # must not surface as CLAUDE_AUTH_OVERLAY_UNEXPECTEDLY_UNAVAILABLE.
    _write_evidence(tmp_path, {"ok": False, "expected": False, "reason": "REFUSED"})
    assert overlay_unexpectedly_unavailable(tmp_path) is True

    monkeypatch.setenv("AWF_CLAUDE_AUTH_FORCE_COPY", flag)

    assert overlay_unexpectedly_unavailable(tmp_path) is False
    assert overlay_unexpectedly_unavailable(tmp_path, host_env={}) is True
    assert (
        overlay_unexpectedly_unavailable(tmp_path, host_env={"AWF_CLAUDE_AUTH_FORCE_COPY": "true"})
        is False
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "requested"),
    [("1", True), ("on", True), ("Yes", True), ("false", False), ("", False), ("0", False)],
)
def test_force_copy_isolation_requested_parses_the_flag(value: str, requested: bool) -> None:
    assert force_copy_isolation_requested({"AWF_CLAUDE_AUTH_FORCE_COPY": value}) is requested
    assert force_copy_isolation_requested({}) is False


@pytest.mark.unit
def test_discard_evidence_removes_the_file_and_tolerates_absence(tmp_path: Path) -> None:
    scratch_root = overlay_probe_scratch_root(tmp_path)
    _write_evidence(tmp_path, {"ok": False, "expected": False, "reason": "REFUSED"})

    discard_overlay_probe_evidence(scratch_root)

    assert not overlay_probe_evidence_path(scratch_root).exists()
    assert overlay_unexpectedly_unavailable(tmp_path) is False
    # Idempotent: the common case is a host that never wrote evidence at all.
    discard_overlay_probe_evidence(scratch_root)
    discard_overlay_probe_evidence(tmp_path / "never-created")


@pytest.mark.unit
def test_discard_evidence_suppresses_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Best-effort, exactly like the write: losing the delete forfeits accuracy of
    # an advisory signal, never provisioning.
    scratch_root = overlay_probe_scratch_root(tmp_path)
    _write_evidence(tmp_path, {"ok": False, "expected": False, "reason": "REFUSED"})

    def _boom(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("read-only mount")

    monkeypatch.setattr(Path, "unlink", _boom)

    discard_overlay_probe_evidence(scratch_root)

    assert overlay_probe_evidence_path(scratch_root).exists()


@pytest.mark.unit
def test_scratch_root_is_the_shared_auth_dir(tmp_path: Path) -> None:
    # The work dir is bind-mounted at the same absolute path into worker and api,
    # which is what makes this a valid cross-container evidence channel.
    assert overlay_probe_scratch_root(tmp_path) == tmp_path / "auth" / "_shared"
    assert (
        overlay_probe_evidence_path(overlay_probe_scratch_root(tmp_path))
        == tmp_path / "auth" / "_shared" / "overlay-probe.json"
    )


@pytest.mark.unit
def test_probe_result_is_frozen() -> None:
    result = OverlayProbeResult(ok=True, reason="OVERLAY_PROBE_OK")

    with pytest.raises(AttributeError):
        result.ok = False  # type: ignore[misc]


@pytest.mark.unit
def test_probe_passes_the_timeout_through(tmp_path: Path) -> None:
    captured: list[object] = []

    def _run(args: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess(list(args), 0, "", "")

    probe_overlay_mount(scratch_root=tmp_path / "s", run=_run, timeout_seconds=2.5)

    assert captured == [2.5, 2.5]


@pytest.mark.unit
def test_probe_default_run_is_subprocess_run() -> None:
    # Guards the injection seam: production must not silently lose the real
    # ``mount(8)`` call, and tests must always be the ones overriding it.
    assert probe_mod._DEFAULT_RUN is subprocess.run  # noqa: SLF001


@pytest.mark.unit
def test_module_documents_every_reason() -> None:
    docstring = probe_mod.__doc__ or ""
    for reason in (
        "OVERLAY_PROBE_OK",
        "REFUSED",
        "TIMEOUT",
        "UMOUNT_FAILED",
        "MOUNT_BINARY_MISSING",
        "SCRATCH_UNAVAILABLE",
        "PATH_RESERVED_CHARS",
    ):
        assert reason in docstring
