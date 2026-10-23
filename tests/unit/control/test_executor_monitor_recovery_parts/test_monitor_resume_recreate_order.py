"""Forced dependency recreates honor compose ``depends_on`` (PRRT_kwDOSJAM6s6vdv3T).

Each recreate runs with ``--no-deps``, which skips every depends_on health gate,
so recreating ``backend`` before the ``postgres`` it needs (alphabetical order)
let its initialization run against a database that was still being recreated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from awf.control.executor import monitor_resume_runtime
from awf.control.executor.constants import (
    MONITOR_RESUME_DEPENDENCY_PREREQUISITE_FAILED_REASON_CODE,
)
from awf.node.compose_manager import ComposeOperationError
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot

_CONDITIONS = {"backend": {"postgres": "service_healthy", "agent": "service_started"}}


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _service(
    name: str, *, state: str = "running", health: str | None = None, status: str | None = None
) -> RuntimeService:
    return RuntimeService(
        name=name,
        container_id=f"{name}-id",
        image=None,
        state=state,
        status=status,
        health=health,
    )


def _postgres(**kwargs: Any) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        stack_state="running",
        services=[_service("backend"), _service("postgres", **kwargs), _service("agent")],
    )


class _Runtime:
    """Scripted inspections that record how many compose calls preceded each one."""

    def __init__(self, *snapshots: RuntimeSnapshot | BaseException) -> None:
        self.snapshots = list(snapshots)
        self.calls: list[dict[str, Any]] = []
        self.inspected_after: list[int] = []

    async def ensure_project_up(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    async def inspect(self, compose_project: str) -> RuntimeSnapshot:
        assert compose_project == "awf_ws"
        item = self.snapshots[min(len(self.inspected_after), len(self.snapshots) - 1)]
        self.inspected_after.append(len(self.calls))
        if isinstance(item, BaseException):
            raise item
        return item


def _install(monkeypatch: pytest.MonkeyPatch, runtime: _Runtime) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(monitor_resume_runtime, "inspect_resume_runtime", runtime.inspect)
    monkeypatch.setattr(monitor_resume_runtime, "_monotonic", clock.monotonic)
    monkeypatch.setattr(monitor_resume_runtime, "_sleep", clock.sleep)
    return clock


async def _restart(runtime: _Runtime, tmp_path: Path, *, recreated: list[str]) -> None:
    await monitor_resume_runtime.restart_runtime_for_monitor_resume(
        runtime,
        compose_project="awf_ws",
        compose_file=tmp_path / "compose.yml",
        workspace_id="ws",
        budget_seconds=12,
        dependency_services=("agent", "backend", "postgres"),
        recreate_services=("backend", "postgres"),
        dependency_conditions=_CONDITIONS,
        on_recreated=recreated.append,
    )


def _recreates(runtime: _Runtime) -> list[tuple[str, ...]]:
    return [call["services"] for call in runtime.calls if call.get("force_recreate")]


@pytest.mark.unit
async def test_dependent_is_recreated_only_after_its_recreated_prerequisite_is_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _Runtime(
        _postgres(health="starting"), _postgres(health="healthy"), _postgres(health="healthy")
    )
    clock = _install(monkeypatch, runtime)
    recreated: list[str] = []

    await _restart(runtime, tmp_path, recreated=recreated)

    assert _recreates(runtime) == [("postgres",), ("backend",), ("agent",)]
    assert recreated == ["postgres", "backend"]
    # postgres is recreated before the first inspection; backend waits one poll for it.
    assert runtime.inspected_after[:3] == [1, 1, 2]
    assert clock.sleeps == [5.0]
    assert all(call["no_deps"] and not call["wait"] for call in runtime.calls[:2])


@pytest.mark.unit
async def test_recreate_wait_shares_the_budget_and_names_the_unsettled_prerequisite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _Runtime(_postgres(health="unhealthy"))
    clock = _install(monkeypatch, runtime)
    recreated: list[str] = []

    with pytest.raises(monitor_resume_runtime.DependencyWaitTimeoutError) as raised:
        await _restart(runtime, tmp_path, recreated=recreated)

    assert raised.value.unsettled_services == ("postgres",)
    assert clock.sleeps == [5.0, 5.0, 2.0]
    # backend was never recreated, so it stays pending for the next resume.
    assert recreated == ["postgres"]
    assert _recreates(runtime) == [("postgres",)]


@pytest.mark.unit
@pytest.mark.parametrize(
    "first_inspection",
    [
        OSError("docker missing"),
        RuntimeSnapshot(stack_state="unavailable", services=[]),
    ],
    ids=["inspect-error", "unreadable"],
)
async def test_unreadable_runtime_recreates_the_rest_in_dependency_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_inspection: RuntimeSnapshot | BaseException,
) -> None:
    """Without a readable runtime the recreate no longer waits; the wait and up follow."""
    runtime = _Runtime(first_inspection, _postgres(health="healthy"))
    clock = _install(monkeypatch, runtime)
    recreated: list[str] = []

    await _restart(runtime, tmp_path, recreated=recreated)

    assert recreated == ["postgres", "backend"]
    assert runtime.inspected_after[0] == 1
    assert _recreates(runtime) == [("postgres",), ("backend",), ("agent",)]
    assert clock.sleeps == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "postgres",
    [
        {"state": "exited", "status": "Exited (1) 1 second ago"},
        {"state": "created", "status": "Created"},
    ],
    ids=["exited-nonzero", "never-started"],
)
async def test_failed_prerequisite_fails_the_restart_before_its_dependent_is_recreated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, postgres: dict[str, str]
) -> None:
    """PRRT_kwDOSJAM6s6vewt7: a ``--no-deps`` recreate never bypasses a failed prerequisite.

    The later ``up --no-recreate`` can restart postgres but never recreates the
    backend, whose one-time initialization would already have failed against it.
    """
    runtime = _Runtime(_postgres(**postgres))
    clock = _install(monkeypatch, runtime)
    recreated: list[str] = []

    with pytest.raises(ComposeOperationError) as raised:
        await _restart(runtime, tmp_path, recreated=recreated)

    assert raised.value.reason_code == MONITOR_RESUME_DEPENDENCY_PREREQUISITE_FAILED_REASON_CODE
    assert f"backend needs postgres={postgres['status']}" in raised.value.stderr
    # backend was never recreated, so it stays pending for the next resume.
    assert recreated == ["postgres"]
    assert _recreates(runtime) == [("postgres",)]
    assert clock.sleeps == []


@pytest.mark.unit
async def test_depends_on_cycle_still_recreates_every_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _Runtime(_postgres(health="healthy"))
    _install(monkeypatch, runtime)

    await monitor_resume_runtime.restart_runtime_for_monitor_resume(
        runtime,
        compose_project="awf_ws",
        compose_file=tmp_path / "compose.yml",
        workspace_id="ws",
        budget_seconds=12,
        dependency_services=("agent", "backend", "postgres"),
        recreate_services=("backend", "postgres"),
        dependency_conditions={
            "backend": {"postgres": "service_started"},
            "postgres": {"backend": "service_started"},
        },
    )

    assert _recreates(runtime) == [("backend",), ("postgres",), ("agent",)]


@pytest.mark.unit
async def test_dependent_waits_for_an_unchanged_prerequisite_to_satisfy_its_condition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRRT_kwDOSJAM6s6veMdx: a prerequisite that is not recreated still gates the recreate."""
    runtime = _Runtime(
        _postgres(health="starting"), _postgres(health="healthy"), _postgres(health="healthy")
    )
    clock = _install(monkeypatch, runtime)
    recreated: list[str] = []

    await monitor_resume_runtime.restart_runtime_for_monitor_resume(
        runtime,
        compose_project="awf_ws",
        compose_file=tmp_path / "compose.yml",
        workspace_id="ws",
        budget_seconds=12,
        dependency_services=("agent", "backend", "postgres"),
        recreate_services=("backend",),
        dependency_conditions=_CONDITIONS,
        on_recreated=recreated.append,
    )

    assert recreated == ["backend"]
    assert _recreates(runtime) == [("backend",), ("agent",)]
    # backend is recreated only after the poll that finds postgres healthy.
    assert runtime.inspected_after[:2] == [0, 0]
    assert clock.sleeps == [5.0]


@pytest.mark.unit
async def test_absent_prerequisite_is_started_before_its_recreated_dependent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRRT_kwDOSJAM6s6vebKx: a released runtime's database starts before the backend."""
    released = RuntimeSnapshot(
        stack_state="running", services=[_service("backend"), _service("agent")]
    )
    runtime = _Runtime(
        released,
        _postgres(health="starting"),
        _postgres(health="healthy"),
        _postgres(health="healthy"),
    )
    clock = _install(monkeypatch, runtime)
    recreated: list[str] = []

    await monitor_resume_runtime.restart_runtime_for_monitor_resume(
        runtime,
        compose_project="awf_ws",
        compose_file=tmp_path / "compose.yml",
        workspace_id="ws",
        budget_seconds=12,
        dependency_services=("agent", "backend", "postgres"),
        recreate_services=("backend",),
        dependency_conditions=_CONDITIONS,
        on_recreated=recreated.append,
    )

    start, backend, up, agent = runtime.calls
    assert start["services"] == ("postgres",)
    assert start["no_recreate"] and start["no_deps"] and not start["wait"]
    assert backend["services"] == ("backend",) and backend["force_recreate"]
    assert up["services"] == ("backend", "postgres") and up["no_recreate"]
    assert agent["services"] == ("agent",)
    assert recreated == ["backend"]
    # backend is recreated only after the poll that finds the started postgres healthy.
    assert runtime.inspected_after == [0, 1, 1, 2]
    assert clock.sleeps == [5.0]
