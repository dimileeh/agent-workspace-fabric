"""Small executor protocols used to avoid concrete runtime cycles."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class _MonitorRunnerProto(Protocol):
    """Minimum surface the executor needs from a PR monitor runner.

    Declared as a Protocol so the executor doesn't structurally depend
    on ``PullRequestMonitorRunner`` — tests can pass a tiny stub, and
    the monitor stage is a clean extension seam for Phase 2 variants
    (merge queue, release-PR monitor, etc.)."""

    async def run(
        self: Any,
        *,
        workspace_id: str,
        compose_project: str,
        compose_file: Path,
        monitor_owner_id: str | None = None,
    ) -> None: ...
