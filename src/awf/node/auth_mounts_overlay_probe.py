"""Truthful overlayfs preflight: one real scratch mount, cached per process (#874).

The pre-#874 preflight asked two cheap questions — does ``/proc/filesystems``
advertise ``overlay``, and does the process hold ``CAP_SYS_ADMIN`` — and answered
"supported" whenever both held. Both are blind to the LSM layer. AWF's worker
container runs under Docker's ``docker-default`` AppArmor profile in *enforce*
mode, whose plain (therefore non-auditing) ``deny mount,`` rule blocks
``mount(2)`` regardless of capability. The worker *did* hold ``CAP_SYS_ADMIN``
(``CapEff`` bit 21), the work dir *was* ext4 and ``rshared``, and kernel
overlayfs *was* present — yet every mount failed with ``EACCES``, surfacing as
util-linux's read-only retry (``cannot mount overlay read-only``, exit 32) with
no overlayfs ``dmesg`` record and no AppArmor audit line. Because the preflight
lied, each provision first built a ~1.9 GB shared base, then failed the mount,
then wrote another ~1.7 GB legacy copy. Probing for real removes the wasted base
build entirely and lets the failure be reported honestly.

The probe performs one scratch ``mount``/``umount`` of an overlay under
``<work_dir>/auth/_shared`` and reports:

- ``OVERLAY_PROBE_OK`` — the mount succeeded (whether or not the umount did).
- ``REFUSED`` — ``mount(8)`` failed. On a host that already passed every cheap
  gate this is the AppArmor/seccomp case: unexpected, and worth surfacing.
- ``TIMEOUT`` — ``mount(8)`` did not return within ``timeout_seconds``. Also
  unexpected. The staging tree is retained, because a timed-out helper may still
  have completed the mount.
- ``MOUNT_BINARY_MISSING`` — no ``mount(8)`` on ``PATH``.
- ``SCRATCH_UNAVAILABLE`` — the scratch staging tree could not be created.
- ``PATH_RESERVED_CHARS`` — the scratch path carries a ``,`` or ``:`` that
  overlayfs's unescapable ``-o`` payload cannot encode.

**Expected vs unexpected is the load-bearing distinction.** Only ``REFUSED`` and
``TIMEOUT`` are unexpected; every other non-ok reason is a legitimate platform
property. Combined with the fact that the probe only runs *after* the cheap gates
pass (force-copy not requested, kernel overlayfs present, ``CAP_SYS_ADMIN`` held,
no reserved chars), this is what keeps the copy fallback a first-class,
non-alarming path: a hosted/GKE control plane — where a pod-level overlay mount
is typically impossible and the copy fallback is the correct posture — never
reaches the probe, therefore never writes evidence, therefore never warns.
**Absence of evidence means silence, by construction.**

The JSON evidence file is the worker→api channel: the worker is the only service
that provisions and mounts, but ``awf service status`` / provider readiness are
usually collected from the API process. The work dir is bind-mounted at the same
absolute path into both containers, so ``<work_dir>/auth/_shared/overlay-probe.json``
is readable from either. Writing it is best-effort (``OSError`` suppressed),
mirroring :func:`awf.node.auth_mounts_claude._record_overlay_base_pin`: losing
the evidence forfeits observability, never provisioning.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from awf.common.logging import get_logger
from awf.node.auth_mounts_claude_base import _SHARED_AUTH_DIRNAME

# A literal ``,`` would split the ``lowerdir=..,upperdir=..,workdir=..`` option
# string into spurious options, and a literal ``:`` inside ``lowerdir`` would be
# misread as the separator between stacked lower layers. ``mount(8)`` offers no
# escaping for either, so a path carrying one forces the copy fallback.
# Re-exported as ``_OVERLAY_OPTION_RESERVED_CHARS`` by
# :mod:`awf.node.auth_mounts_claude` so both the probe and the production mount
# path share one definition.
OVERLAY_OPTION_RESERVED_CHARS = (",", ":")

_log = get_logger(__name__)

OVERLAY_PROBE_EVIDENCE_NAME = "overlay-probe.json"

# Logged at INFO whenever the per-workspace ``~/.claude`` overlay is unavailable
# and the copy fallback is taken. This is a fully supported posture — hosted/GKE,
# ``AWF_CLAUDE_AUTH_FORCE_COPY``, a worker without ``CAP_SYS_ADMIN`` — so it is
# deliberately NOT in the reason catalog: documenting it as a fault would make a
# correct platform choice read as a standing failure.
CLAUDE_AUTH_OVERLAY_UNAVAILABLE = "CLAUDE_AUTH_OVERLAY_UNAVAILABLE"
# Logged at WARNING only when the probe actually ran (every cheap gate passed)
# and the scratch mount was REFUSED or timed out: overlay *should* work on this
# host and an LSM is blocking it. This one IS cataloged.
CLAUDE_AUTH_OVERLAY_UNEXPECTEDLY_UNAVAILABLE = "CLAUDE_AUTH_OVERLAY_UNEXPECTEDLY_UNAVAILABLE"
_PROBE_STAGING_PREFIX = ".overlay-probe-"
_DEFAULT_TIMEOUT_SECONDS = 10.0
_DETAIL_LIMIT = 512
_DEFAULT_RUN: Callable[..., object] = subprocess.run

# Non-ok reasons that indicate a host which *should* have been able to overlay
# (every cheap gate passed) but was refused anyway — the AppArmor/seccomp case.
# Every other reason is an expected platform property and stays silent.
_UNEXPECTED_PROBE_REASONS = frozenset({"REFUSED", "TIMEOUT"})


@dataclass(frozen=True)
class OverlayProbeResult:
    """Outcome of one scratch overlay mount attempt."""

    ok: bool
    reason: str
    detail: str = ""


def overlay_probe_expected(result: OverlayProbeResult) -> bool:
    """Return whether ``result`` is an expected posture for this platform.

    ``True`` for success and for every non-ok reason that describes the host
    rather than a misconfiguration. Only an unexpected result may ever produce an
    operator-visible warning.
    """

    return result.ok or result.reason not in _UNEXPECTED_PROBE_REASONS


def overlay_probe_scratch_root(work_dir: Path) -> Path:
    """Return the shared auth dir the probe stages under and writes evidence to."""

    return work_dir / "auth" / _SHARED_AUTH_DIRNAME


def overlay_probe_evidence_path(scratch_root: Path) -> Path:
    """Return the JSON evidence path inside ``scratch_root``."""

    return scratch_root / OVERLAY_PROBE_EVIDENCE_NAME


def _truncate(text: str) -> str:
    return text[:_DETAIL_LIMIT]


def _refused_detail(exc: subprocess.CalledProcessError) -> str:
    stderr = exc.stderr if isinstance(exc.stderr, str) else ""
    return _truncate(f"exit={exc.returncode}: {stderr.strip()}")


def probe_overlay_mount(
    *,
    scratch_root: Path,
    run: Callable[..., object] = _DEFAULT_RUN,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> OverlayProbeResult:
    """Mount and unmount a scratch overlay under ``scratch_root``; report the outcome.

    ``run`` is injected so unit tests exercise every branch without a real mount
    (the test container holds no ``CAP_SYS_ADMIN`` and is AppArmor-confined).
    """

    if any(char in os.fspath(scratch_root) for char in OVERLAY_OPTION_RESERVED_CHARS):
        return OverlayProbeResult(
            ok=False,
            reason="PATH_RESERVED_CHARS",
            detail=_truncate(f"scratch path is unencodable in mount -o: {scratch_root}"),
        )

    try:
        scratch_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=_PROBE_STAGING_PREFIX, dir=scratch_root))
    except OSError as exc:
        return OverlayProbeResult(
            ok=False, reason="SCRATCH_UNAVAILABLE", detail=_truncate(str(exc))
        )

    lower = staging / "lower"
    upper = staging / "upper"
    work = staging / "work"
    merged = staging / "merged"
    try:
        for layer in (lower, upper, work, merged):
            layer.mkdir()
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return OverlayProbeResult(
            ok=False, reason="SCRATCH_UNAVAILABLE", detail=_truncate(str(exc))
        )

    options = f"lowerdir={lower},upperdir={upper},workdir={work}"
    try:
        run(
            ["mount", "-t", "overlay", "overlay", "-o", options, str(merged)],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        shutil.rmtree(staging, ignore_errors=True)
        return OverlayProbeResult(
            ok=False, reason="MOUNT_BINARY_MISSING", detail="mount(8) not found on PATH"
        )
    except subprocess.TimeoutExpired:
        # Killing the timed-out helper does NOT undo a ``mount(2)`` that already
        # landed, so ``merged`` may be a live overlay pinning lower/upper/work.
        # Same reasoning — and the same choice — as the umount-failure branch
        # below: retaining one empty staging dir is strictly safer than a
        # recursive delete that would descend through a live merged view, tear
        # out the pinned layers, and swallow the resulting ``EBUSY``. The
        # retained path is recorded so an operator can reclaim it.
        return OverlayProbeResult(
            ok=False,
            reason="TIMEOUT",
            detail=_truncate(
                f"mount did not return within {timeout_seconds}s; retained staging dir {staging}"
            ),
        )
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return OverlayProbeResult(ok=False, reason="REFUSED", detail=_refused_detail(exc))
    except (subprocess.SubprocessError, OSError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return OverlayProbeResult(ok=False, reason="REFUSED", detail=_truncate(str(exc)))

    try:
        run(
            ["umount", str(merged)],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        # The mount itself worked, which is the whole question the probe asks, so
        # the answer stays ``ok``. Deliberately do NOT ``rmtree``: the overlay may
        # still be live, and a recursive delete would descend through ``merged``
        # into the (empty, but conceptually real) layers underneath. Leaking one
        # empty staging dir is strictly safer, and the retained path is recorded
        # so an operator can reclaim it.
        return OverlayProbeResult(
            ok=True,
            reason="OVERLAY_PROBE_OK",
            detail=_truncate(f"umount failed ({exc}); retained staging dir {staging}"),
        )

    shutil.rmtree(staging, ignore_errors=True)
    return OverlayProbeResult(ok=True, reason="OVERLAY_PROBE_OK")


def write_overlay_probe_evidence(
    scratch_root: Path,
    result: OverlayProbeResult,
    *,
    now: datetime | None = None,
) -> None:
    """Persist ``result`` as the worker→api evidence file (best-effort)."""

    payload = {
        "ok": result.ok,
        "reason": result.reason,
        "expected": overlay_probe_expected(result),
        "detail": result.detail,
        "checked_at": (now or datetime.now(UTC)).isoformat(),
    }
    with contextlib.suppress(OSError):
        overlay_probe_evidence_path(scratch_root).write_text(json.dumps(payload))


def read_overlay_probe_evidence(work_dir: Path) -> Mapping[str, object] | None:
    """Return the probe evidence recorded under ``work_dir``, or ``None``.

    ``None`` covers every degraded case — no file (the common one: a host that
    never probed), an unreadable file, malformed JSON, or JSON that is not an
    object. All of them must read as "no signal", never as a fault.
    """

    path = overlay_probe_evidence_path(overlay_probe_scratch_root(work_dir))
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping):
        return None
    return payload


def overlay_unexpectedly_unavailable(work_dir: Path) -> bool:
    """Return whether recorded evidence says overlay failed where it should not have.

    The single predicate the service-layer surfaces (``provider_readiness``,
    ``status``) share. Strict identity checks (``is False``) so a truthy-but-
    non-boolean JSON value never trips a warning.
    """

    evidence = read_overlay_probe_evidence(work_dir)
    if evidence is None:
        return False
    return evidence.get("ok") is False and evidence.get("expected") is False


_PROBE_CACHE: dict[str, OverlayProbeResult] = {}


def cached_overlay_probe(
    *,
    scratch_root: Path,
    run: Callable[..., object] = _DEFAULT_RUN,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> OverlayProbeResult:
    """Probe once per process per ``scratch_root``, recording evidence on the miss.

    ``supported()`` is consulted on every provision; a real mount+umount per call
    would be wasteful and, worse, would race the shared-base staging sweep. The
    evidence write happens only on the miss path, so a host that never probes
    never creates the file.
    """

    key = os.fspath(scratch_root)
    cached = _PROBE_CACHE.get(key)
    if cached is not None:
        return cached
    result = probe_overlay_mount(
        scratch_root=scratch_root, run=run, timeout_seconds=timeout_seconds
    )
    _PROBE_CACHE[key] = result
    write_overlay_probe_evidence(scratch_root, result)
    return result


def reset_overlay_probe_cache() -> None:
    """Clear the per-process probe cache (the test seam)."""

    _PROBE_CACHE.clear()


def log_overlay_unavailable(*, claude_root: Path, work_dir: Path) -> None:
    """Report an unavailable Claude auth overlay at the severity evidence justifies.

    INFO when there is no probe evidence or the probe passed; WARNING only on
    recorded unexpected failure. Absence of evidence is silence by construction:
    hosts that fail a cheap gate never probe and never write evidence.
    """

    if not overlay_unexpectedly_unavailable(work_dir):
        _log.info(
            "claude_auth_overlay_unavailable",
            reason_code=CLAUDE_AUTH_OVERLAY_UNAVAILABLE,
            workspace_auth_root=str(claude_root),
            reason="overlayfs_unsupported",
        )
        return
    evidence = read_overlay_probe_evidence(work_dir) or {}
    _log.warning(
        "claude_auth_overlay_unexpectedly_unavailable",
        reason_code=CLAUDE_AUTH_OVERLAY_UNEXPECTEDLY_UNAVAILABLE,
        workspace_auth_root=str(claude_root),
        reason="overlay_probe_failed",
        probe_reason=str(evidence.get("reason", "")),
        probe_detail=str(evidence.get("detail", "")),
    )
