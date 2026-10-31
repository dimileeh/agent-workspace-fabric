"""Shared fixtures for MCP server unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.service.disk import DiskCheck


def _ok_admission_check_disk_space(
    work_dir: str | Path,
    *,
    min_free_bytes: int,
) -> DiskCheck:
    free = min_free_bytes + 1
    return DiskCheck(
        path=str(work_dir),
        checked_path=str(work_dir),
        total_bytes=free,
        used_bytes=0,
        free_bytes=free,
        percent_free=100.0,
        threshold_bytes=min_free_bytes,
        ok=True,
        status="ok",
        reason="SUFFICIENT_DISK",
    )


@pytest.fixture(autouse=True)
def _pin_mcp_workspace_admission_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep MCP workspace admission independent of the host's real free disk.

    ``build_mcp_server`` falls back to ``check_disk_space(settings.work_dir)``
    when no ``disk_check_provider`` is injected, so on a CI runner with less
    free disk than ``AWF_MIN_FREE_DISK_BYTES`` every ``awf_create_workspace``
    call returned ``INSUFFICIENT_DISK``. This mirrors the REST ``client``
    fixture, which pins ``app.state.workspace_admission_disk_check``. Tests
    that exercise low-disk admission still inject their own provider, which
    takes precedence over this fallback.
    """
    monkeypatch.setattr("awf.mcp.server.check_disk_space", _ok_admission_check_disk_space)
