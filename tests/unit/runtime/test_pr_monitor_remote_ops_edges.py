"""Focused edge coverage for PR monitor remote operations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from awf.runtime.pr_monitor_runner.remote_ops import _refresh_staleness_after_sync_base


@pytest.mark.unit
async def test_refresh_staleness_after_sync_base_treats_session_failure_as_best_effort(
    tmp_path: Path,
) -> None:
    class _BrokenDeps:
        def session_factory(self) -> object:
            raise RuntimeError("session factory unavailable")

    class _Runner:
        _deps = _BrokenDeps()
        _worktrees_root = tmp_path

    runner: Any = _Runner()
    await _refresh_staleness_after_sync_base(
        runner,
        workspace_id="ws_sync_base_refresh",
        base_branch="development",
    )
