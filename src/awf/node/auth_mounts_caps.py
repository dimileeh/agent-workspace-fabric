"""Linux process-capability probes for the Claude auth overlay subsystem.

Split out of :mod:`awf.node.auth_mounts_claude` as a dependency-free leaf so the
overlay primitives, the fallback-edit reconcile pass, and the teardown path can
all share one ``CapEff`` parser without importing each other (which would form an
import cycle). :mod:`awf.node.auth_mounts_claude` re-exports these names, and
:mod:`awf.node.auth_mounts` re-exports them in turn, so
``awf.node.auth_mounts.<name>`` stays the stable import surface for callers and
tests.
"""

from __future__ import annotations

from pathlib import Path

_PROC_SELF_STATUS = Path("/proc/self/status")
_CAP_SYS_ADMIN_BIT = 21
# ``CAP_MKNOD`` (Linux capability bit 27) authorises ``mknod`` of a special file —
# here the char device ``0,0`` overlayfs reads as a whiteout. The worker that mounts
# overlays already holds it (it holds ``CAP_SYS_ADMIN``); the capability fallback in
# the deletion-forwarding pass exists for capability-less / hardened contexts.
_CAP_MKNOD_BIT = 27


def _capeff_has_bit(proc_status: Path, bit: int) -> bool:
    """Return whether the process's ``CapEff`` set in ``proc_status`` holds ``bit``.

    Shared parser for the capability probes below: reads the effective-capability
    bitmask from ``/proc/<pid>/status`` and tests a single bit. Any read/parse
    failure (missing file, absent ``CapEff:`` line, non-hex value) reads as "not
    held", so a probe never raises and the caller degrades conservatively.
    """

    try:
        contents = proc_status.read_text()
    except OSError:
        return False
    for line in contents.splitlines():
        if not line.startswith("CapEff:"):
            continue
        try:
            caps = int(line.split(":", 1)[1].strip(), 16)
        except ValueError:
            return False
        return bool(caps & (1 << bit))
    return False


def _has_cap_sys_admin(proc_status: Path = _PROC_SELF_STATUS) -> bool:
    """Return whether the current process holds ``CAP_SYS_ADMIN`` (needed to mount)."""

    return _capeff_has_bit(proc_status, _CAP_SYS_ADMIN_BIT)


def _has_cap_mknod(proc_status: Path = _PROC_SELF_STATUS) -> bool:
    """Return whether the current process holds ``CAP_MKNOD`` (needed for whiteouts).

    Mirrors :func:`_has_cap_sys_admin` for capability bit 27. The deletion-forwarding
    pass consults this before creating an overlayfs whiteout device so a
    capability-less context skips the whiteout (leaving the credential visible)
    rather than attempting a ``mknod`` that would fail.
    """

    return _capeff_has_bit(proc_status, _CAP_MKNOD_BIT)
