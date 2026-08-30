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
def test_probe_keeps_staging_when_umount_fails(tmp_path: Path) -> None:
    # Never ``rmtree`` under a possibly-live mount: the probe succeeded, so the
    # host *can* overlay; leaking one empty staging dir is strictly safer than
    # recursively deleting through a live overlay's merged view.
    scratch_root = tmp_path / "auth" / "_shared"
    run = _FakeRun(umount_error=subprocess.CalledProcessError(1, ["umount"], stderr="busy"))

    result = probe_overlay_mount(scratch_root=scratch_root, run=run)

    assert result.ok is True
    assert result.reason == "OVERLAY_PROBE_OK"
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
def test_read_evidence_returns_none_on_unreadable_file(tmp_path: Path) -> None:
    scratch_root = overlay_probe_scratch_root(tmp_path)
    scratch_root.mkdir(parents=True)
    evidence_path = overlay_probe_evidence_path(scratch_root)
    evidence_path.write_text("{}")
    evidence_path.chmod(0o000)

    assert read_overlay_probe_evidence(tmp_path) is None


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
        "MOUNT_BINARY_MISSING",
        "SCRATCH_UNAVAILABLE",
        "PATH_RESERVED_CHARS",
    ):
        assert reason in docstring
