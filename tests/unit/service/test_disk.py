"""Disk admission check behavior."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from awf.service.disk import _nearest_existing_path, check_disk_space


@dataclass(frozen=True)
class _Usage:
    total: int
    used: int
    free: int


@pytest.mark.unit
def test_nearest_existing_path_returns_path_itself(tmp_path: Path) -> None:
    assert _nearest_existing_path(tmp_path) == tmp_path


@pytest.mark.unit
def test_nearest_existing_path_walks_up_to_existing_parent(tmp_path: Path) -> None:
    result = _nearest_existing_path(tmp_path / "a" / "b")
    assert result == tmp_path


@pytest.mark.unit
def test_nearest_existing_path_returns_root_when_nothing_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "exists", lambda _self: False)
    result = _nearest_existing_path(Path("/nowhere/fake"))
    assert result == Path("/")


@pytest.mark.unit
def test_disk_check_uses_nearest_existing_parent_for_missing_work_dir(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "awf"
    existing.mkdir()
    checked_paths: list[Path] = []

    def usage(path: Path) -> _Usage:
        checked_paths.append(path)
        return _Usage(total=1000, used=100, free=900)

    check = check_disk_space(
        existing / "missing" / "nested",
        min_free_bytes=800,
        disk_usage=usage,
    )

    assert checked_paths == [existing]
    assert check.path == str(existing / "missing" / "nested")
    assert check.checked_path == str(existing)
    assert check.ok is True
    assert check.percent_free == 90.0


@pytest.mark.unit
def test_disk_check_reports_usage_exceptions_as_unavailable(tmp_path: Path) -> None:
    def usage(_path: Path) -> _Usage:
        raise PermissionError("not allowed")

    check = check_disk_space(tmp_path / "work", min_free_bytes=1, disk_usage=usage)

    assert check.ok is False
    assert check.status == "fail"
    assert check.reason == "DISK_USAGE_UNAVAILABLE"
    assert check.total_bytes == 0
    assert "PermissionError: not allowed" in str(check.detail)


@pytest.mark.unit
def test_disk_check_handles_zero_total_usage_without_division_error(tmp_path: Path) -> None:
    check = check_disk_space(
        tmp_path,
        min_free_bytes=0,
        disk_usage=lambda _path: _Usage(total=0, used=0, free=0),
    )

    assert check.ok is True
    assert check.percent_free == 0.0
    assert check.reason == "SUFFICIENT_DISK"


@pytest.mark.unit
def test_disk_check_insufficient_detail_includes_free_and_threshold_bytes(
    tmp_path: Path,
) -> None:
    check = check_disk_space(
        tmp_path,
        min_free_bytes=400,
        disk_usage=lambda _path: _Usage(total=1000, used=700, free=300),
    )

    assert check.ok is False
    assert check.reason == "INSUFFICIENT_DISK"
    assert check.free_bytes == 300
    assert check.threshold_bytes == 400
    assert "free_bytes=300 threshold_bytes=400" in str(check.detail)


@pytest.mark.unit
def test_disk_check_can_use_real_shutil_disk_usage(tmp_path: Path) -> None:
    check = check_disk_space(tmp_path, min_free_bytes=0)

    assert check.ok is True
    assert check.checked_path == str(tmp_path)
    assert check.total_bytes >= check.free_bytes
