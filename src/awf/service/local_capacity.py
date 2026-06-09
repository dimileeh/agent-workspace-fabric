"""Detect local Docker capacity for reservation pressure reporting."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping
from typing import Literal, Protocol

from awf.common.config import Settings
from awf.service.resource_capacity import LocalCapacityLimits

_DETECT_TIMEOUT_SECONDS = 5.0
_BYTES_PER_GIB = 1024**3
_DETAIL_LIMIT = 240


class CompletedProcessLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


SubprocessRun = Callable[
    [
        list[str],
    ],
    CompletedProcessLike,
]


def detect_local_capacity(
    settings: Settings,
    *,
    run_subprocess: Callable[
        ...,
        CompletedProcessLike,
    ]
    | None = None,
) -> LocalCapacityLimits:
    """Detect capacity exposed by the Docker daemon used for workspaces."""

    run = run_subprocess or _run_subprocess
    env = {**os.environ, "DOCKER_HOST": settings.docker_host}
    try:
        result = run(
            ["docker", "info", "--format", "{{json .}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_DETECT_TIMEOUT_SECONDS,
            env=env,
        )
    except Exception as exc:
        return LocalCapacityLimits(
            source="docker",
            reason_code="DOCKER_INFO_UNAVAILABLE",
            detail=_truncate(f"{type(exc).__name__}: {exc}"),
        )

    if result.returncode != 0:
        return LocalCapacityLimits(
            source="docker",
            reason_code="DOCKER_INFO_UNAVAILABLE",
            detail=_truncate(result.stderr or result.stdout),
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return LocalCapacityLimits(
            source="docker",
            reason_code="DOCKER_INFO_PARSE_FAILED",
            detail=_truncate(str(exc)),
        )

    cpu_cores = _positive_float(payload.get("NCPU"))
    memory_gb = _bytes_to_gib(payload.get("MemTotal"))
    if cpu_cores is None and memory_gb is None:
        return LocalCapacityLimits(
            source="docker",
            reason_code="DOCKER_INFO_CAPACITY_MISSING",
            detail="docker info did not include NCPU or MemTotal.",
        )
    return LocalCapacityLimits(cpu_cores=cpu_cores, memory_gb=memory_gb, source="docker")


def _run_subprocess(
    args: list[str],
    *,
    check: bool,
    capture_output: bool,
    text: Literal[True],
    timeout: float,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        env=env,
    )


def _positive_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number > 0 else None


def _bytes_to_gib(value: object) -> float | None:
    number = _positive_float(value)
    if number is None:
        return None
    return round(number / _BYTES_PER_GIB, 3)


def _truncate(value: str) -> str:
    stripped = value.strip()
    if len(stripped) <= _DETAIL_LIMIT:
        return stripped
    return f"{stripped[: _DETAIL_LIMIT - 3]}..."
