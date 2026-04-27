"""Disk pressure checks for local AWF workspace admission."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class DiskUsageStats(Protocol):
    @property
    def total(self) -> int: ...  # pragma: no cover - Protocol property declaration only.

    @property
    def used(self) -> int: ...  # pragma: no cover - Protocol property declaration only.

    @property
    def free(self) -> int: ...  # pragma: no cover - Protocol property declaration only.


class DiskUsage(Protocol):
    def __call__(  # pragma: no cover - Protocol method declaration only.
        self,
        path: Path,
    ) -> DiskUsageStats: ...


@dataclass(frozen=True, kw_only=True)
class DiskCheck:
    path: str
    checked_path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent_free: float
    threshold_bytes: int
    ok: bool
    status: str
    reason: str
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "path": self.path,
            "checked_path": self.checked_path,
            "total_bytes": self.total_bytes,
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
            "percent_free": self.percent_free,
            "threshold_bytes": self.threshold_bytes,
            "ok": self.ok,
            "status": self.status,
            "reason": self.reason,
        }
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


def check_disk_space(
    work_dir: str | Path,
    *,
    min_free_bytes: int,
    disk_usage: DiskUsage | None = None,
) -> DiskCheck:
    """Return a structured disk-pressure check for the AWF work directory.

    ``shutil.disk_usage`` requires an existing path. The configured work
    directory may not exist on a fresh node, so the check walks up to the nearest
    existing parent while still reporting the configured target path.
    """

    target_path = Path(work_dir).expanduser().resolve(strict=False)
    checked_path = _nearest_existing_path(target_path)
    usage_fn = disk_usage or _shutil_disk_usage
    try:
        usage = usage_fn(checked_path)
    except Exception as exc:
        return DiskCheck(
            path=str(target_path),
            checked_path=str(checked_path),
            total_bytes=0,
            used_bytes=0,
            free_bytes=0,
            percent_free=0.0,
            threshold_bytes=min_free_bytes,
            ok=False,
            status="fail",
            reason="DISK_USAGE_UNAVAILABLE",
            detail=(
                "Could not inspect free disk for the AWF work directory; "
                f"verify the path is accessible: {type(exc).__name__}: {exc}"
            ),
        )

    total = int(usage.total)
    used = int(usage.used)
    free = int(usage.free)
    percent_free = round((free / total) * 100, 2) if total > 0 else 0.0
    if free >= min_free_bytes:
        return DiskCheck(
            path=str(target_path),
            checked_path=str(checked_path),
            total_bytes=total,
            used_bytes=used,
            free_bytes=free,
            percent_free=percent_free,
            threshold_bytes=min_free_bytes,
            ok=True,
            status="ok",
            reason="SUFFICIENT_DISK",
        )

    return DiskCheck(
        path=str(target_path),
        checked_path=str(checked_path),
        total_bytes=total,
        used_bytes=used,
        free_bytes=free,
        percent_free=percent_free,
        threshold_bytes=min_free_bytes,
        ok=False,
        status="fail",
        reason="INSUFFICIENT_DISK",
        detail=(
            "Free disk is below AWF_MIN_FREE_DISK_BYTES for the AWF work directory; "
            "free disk before creating new workspaces or intentionally lower the "
            f"threshold. free_bytes={free} threshold_bytes={min_free_bytes}"
        ),
    )


def _nearest_existing_path(path: Path) -> Path:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            return current
        current = parent
    return current


def _shutil_disk_usage(path: Path) -> DiskUsageStats:
    return shutil.disk_usage(path)
