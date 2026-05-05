"""Local Docker capacity detection tests."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from awf.common.config import Settings
from awf.service.local_capacity import detect_local_capacity


class _Completed:
    def __init__(self, *, returncode: int, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.mark.unit
def test_detect_local_capacity_reads_docker_desktop_cpu_and_memory() -> None:
    calls: list[dict[str, Any]] = []

    def run_subprocess(
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: float,
        env: dict[str, str],
    ) -> _Completed:
        calls.append(
            {
                "args": args,
                "check": check,
                "capture_output": capture_output,
                "text": text,
                "timeout": timeout,
                "env": env,
            }
        )
        return _Completed(
            returncode=0,
            stdout='{"NCPU":8,"MemTotal":25162375168,"OperatingSystem":"Docker Desktop"}',
        )

    settings = Settings(_env_file=None, docker_host="unix:///tmp/docker.sock")

    capacity = detect_local_capacity(settings, run_subprocess=run_subprocess)

    assert capacity.cpu_cores == 8.0
    assert capacity.memory_gb == pytest.approx(23.44, abs=0.01)
    assert capacity.source == "docker"
    assert capacity.reason_code is None
    assert calls[0]["args"] == ["docker", "info", "--format", "{{json .}}"]
    assert calls[0]["env"]["DOCKER_HOST"] == "unix:///tmp/docker.sock"


@pytest.mark.unit
def test_detect_local_capacity_returns_unknown_when_docker_info_fails() -> None:
    def run_subprocess(*args: Any, **kwargs: Any) -> _Completed:
        return _Completed(returncode=1, stdout="", stderr="Cannot connect to Docker")

    capacity = detect_local_capacity(Settings(_env_file=None), run_subprocess=run_subprocess)

    assert capacity.cpu_cores is None
    assert capacity.memory_gb is None
    assert capacity.source == "docker"
    assert capacity.reason_code == "DOCKER_INFO_UNAVAILABLE"
    assert "Cannot connect to Docker" in (capacity.detail or "")


@pytest.mark.unit
def test_detect_local_capacity_truncates_long_docker_error_detail() -> None:
    long_error = "docker unavailable: " + ("x" * 400)

    def run_subprocess(*args: Any, **kwargs: Any) -> _Completed:
        return _Completed(returncode=1, stdout="", stderr=long_error)

    capacity = detect_local_capacity(Settings(_env_file=None), run_subprocess=run_subprocess)

    assert capacity.reason_code == "DOCKER_INFO_UNAVAILABLE"
    assert capacity.detail is not None
    assert len(capacity.detail) == 240
    assert capacity.detail.endswith("...")


@pytest.mark.unit
def test_detect_local_capacity_returns_unknown_on_timeout() -> None:
    def run_subprocess(*args: Any, **kwargs: Any) -> _Completed:
        raise subprocess.TimeoutExpired(cmd=["docker", "info"], timeout=5)

    capacity = detect_local_capacity(Settings(_env_file=None), run_subprocess=run_subprocess)

    assert capacity.cpu_cores is None
    assert capacity.memory_gb is None
    assert capacity.reason_code == "DOCKER_INFO_UNAVAILABLE"


@pytest.mark.unit
def test_detect_local_capacity_reports_json_parse_failure() -> None:
    def run_subprocess(*args: Any, **kwargs: Any) -> _Completed:
        return _Completed(returncode=0, stdout="{not valid json")

    capacity = detect_local_capacity(Settings(_env_file=None), run_subprocess=run_subprocess)

    assert capacity.cpu_cores is None
    assert capacity.memory_gb is None
    assert capacity.source == "docker"
    assert capacity.reason_code == "DOCKER_INFO_PARSE_FAILED"
    assert "Expecting property name" in (capacity.detail or "")


@pytest.mark.unit
def test_detect_local_capacity_reports_missing_capacity_fields() -> None:
    def run_subprocess(*args: Any, **kwargs: Any) -> _Completed:
        return _Completed(returncode=0, stdout="{}")

    capacity = detect_local_capacity(Settings(_env_file=None), run_subprocess=run_subprocess)

    assert capacity.cpu_cores is None
    assert capacity.memory_gb is None
    assert capacity.reason_code == "DOCKER_INFO_CAPACITY_MISSING"
    assert "NCPU or MemTotal" in (capacity.detail or "")


@pytest.mark.unit
def test_detect_local_capacity_rejects_non_numeric_capacity_values() -> None:
    def run_subprocess(*args: Any, **kwargs: Any) -> _Completed:
        return _Completed(returncode=0, stdout='{"NCPU": true, "MemTotal": "unknown"}')

    capacity = detect_local_capacity(Settings(_env_file=None), run_subprocess=run_subprocess)

    assert capacity.cpu_cores is None
    assert capacity.memory_gb is None
    assert capacity.reason_code == "DOCKER_INFO_CAPACITY_MISSING"
