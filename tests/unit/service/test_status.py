"""Service status disk-pressure checks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from awf.service.config import ServiceSettings
from awf.service.status import collect_service_status


def _settings(tmp_path: Path, *, min_free_disk_bytes: int) -> ServiceSettings:
    return ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url="sqlite+aiosqlite:///:memory:",
        docker_host=f"unix://{tmp_path / 'docker.sock'}",
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work"),
        api_token=None,
        github_token=None,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        min_free_disk_bytes=min_free_disk_bytes,
    )


class _Response:
    status_code = 200

    def json(self) -> dict[str, str]:
        return {"status": "ok"}

    def raise_for_status(self) -> None:
        return None


class _DiskUsage:
    def __init__(self, *, total: int, used: int, free: int) -> None:
        self.total = total
        self.used = used
        self.free = free


async def _api_get(_url: str, *, timeout: float) -> _Response:
    return _Response()


async def _db_probe(_database_url: str) -> dict[str, Any]:
    return {"ok": True, "status": "ok"}


def _run_subprocess(args: list[str], **_kwargs: object) -> Any:
    if args[:2] == ["docker", "info"]:
        return type("Completed", (), {"returncode": 0, "stdout": "27.0.3\n", "stderr": ""})()
    if args[:3] == ["docker", "image", "inspect"]:
        return type("Completed", (), {"returncode": 0, "stdout": "sha256:deadbeef\n", "stderr": ""})()
    raise AssertionError(f"unexpected subprocess call: {args}")


@pytest.mark.unit
def test_service_status_includes_ok_disk_check_from_mocked_usage(tmp_path: Path) -> None:
    status = asyncio.run(
        collect_service_status(
            _settings(tmp_path, min_free_disk_bytes=200),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_run_subprocess,
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
        )
    )

    assert status["status"] == "ok"
    disk = status["checks"]["disk"]
    assert disk["ok"] is True
    assert disk["status"] == "ok"
    assert disk["reason"] == "SUFFICIENT_DISK"
    assert disk["total_bytes"] == 1000
    assert disk["used_bytes"] == 700
    assert disk["free_bytes"] == 300
    assert disk["percent_free"] == 30.0
    assert disk["threshold_bytes"] == 200


@pytest.mark.unit
def test_service_status_fails_when_disk_is_below_threshold(tmp_path: Path) -> None:
    status = asyncio.run(
        collect_service_status(
            _settings(tmp_path, min_free_disk_bytes=400),
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_run_subprocess,
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
        )
    )

    assert status["status"] == "fail"
    disk = status["checks"]["disk"]
    assert disk["ok"] is False
    assert disk["status"] == "fail"
    assert disk["reason"] == "INSUFFICIENT_DISK"
    assert disk["threshold_bytes"] == 400
    assert "AWF_MIN_FREE_DISK_BYTES" in str(disk["detail"])
