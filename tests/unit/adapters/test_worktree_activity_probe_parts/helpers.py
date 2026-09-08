"""Shared helpers for the worktree activity probe tests (issue #932).

Split out of ``tests/unit/adapters/test_worktree_activity_probe.py`` so each
part module stays under the first-party 1500-line maintainability guardrail.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from awf.adapters import worktree_activity
from awf.adapters.worktree_activity import WorktreeActivityProbe

# The subprocess regression in ``test_worktree_activity_probe_part_003`` must
# exercise the same source tree the rest of these modules import, not whatever
# ``awf`` a bare interpreter would resolve.
SRC_ROOT = Path(worktree_activity.__file__).parents[2]


def _age(path: Path, *, seconds: float = 3600.0) -> None:
    """Push ``path``'s mtime into the past so it predates any probe baseline."""
    stat_result = path.stat()
    os.utime(path, (stat_result.st_atime - seconds, stat_result.st_mtime - seconds))


def _age_tree(root: Path) -> None:
    for current, dirnames, filenames in os.walk(root):
        for name in filenames:
            _age(Path(current) / name)
        for name in dirnames:
            _age(Path(current) / name)
    _age(root)


async def _await_scan_gate(probe: WorktreeActivityProbe) -> None:
    """Wait for an abandoned scan's thread to settle, reopening the probe's gate.

    Only the thread's own completion reopens it — nothing can cancel a scan — so
    a test that releases a stalled walk and then probes has to let the released
    thread publish its result first.
    """
    for _ in range(500):
        if not probe._scan_gate.is_busy():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the abandoned scan never finished")


def _prime_scan_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make only the priming walk truncate, leaving the probe without a baseline."""
    real_scan = WorktreeActivityProbe._scan
    scans = 0

    def _first_scan_truncates(self: WorktreeActivityProbe) -> object:
        nonlocal scans
        scans += 1
        return None if scans == 1 else real_scan(self)

    monkeypatch.setattr(WorktreeActivityProbe, "_scan", _first_scan_truncates)
