"""Monitor resume restarts only the agent and waits out recovering dependencies (#936).

Resume used to force-recreate the whole compose project, restarting a dependency
that was still in its own crash recovery (Postgres fsync after an unclean stop).
It now starts exited dependencies one by one without a compose health gate,
waits for running dependencies that are ``starting``/``unhealthy``, brings
the project up with ``--no-recreate`` and recreates only the agent with
``--no-deps``. The wait is bounded by the profile budget, not by the healthcheck
retries baked into an already-rendered compose.yml.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from awf.control.executor import monitor_resume_runtime
from awf.control.executor.constants import MONITOR_RESUME_DEPENDENCY_WAIT_TIMEOUT_REASON_CODE
from awf.node.compose_manager import ComposeOperationError
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot

# The unit conftest replaces the Docker-backed seam; keep the real one for its own tests.
_REAL_INSPECT = monitor_resume_runtime.inspect_resume_runtime


class _RecordingCompose:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def ensure_project_up(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _service(
    name: str,
    *,
    state: str = "running",
    health: str | None = None,
    one_off: bool = False,
    status: str | None = None,
) -> RuntimeService:
    return RuntimeService(
        name=name,
        container_id=f"{name}-id",
        image=None,
        state=state,
        status=status,
        health=health,
        one_off=one_off,
    )


def _running(*services: RuntimeService) -> RuntimeSnapshot:
    return RuntimeSnapshot(stack_state="running", services=list(services))


class _ScriptedInspector:
    """Return snapshots in order; the last one repeats."""

    def __init__(self, *snapshots: RuntimeSnapshot | BaseException) -> None:
        self.snapshots = list(snapshots)
        self.calls = 0

    async def __call__(self, compose_project: str) -> RuntimeSnapshot:
        assert compose_project == "awf_ws"
        item = self.snapshots[min(self.calls, len(self.snapshots) - 1)]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        return item


def _install_inspector(
    monkeypatch: pytest.MonkeyPatch, inspector: _ScriptedInspector
) -> _ScriptedInspector:
    monkeypatch.setattr(monitor_resume_runtime, "inspect_resume_runtime", inspector)
    return inspector


async def _restart(
    compose: _RecordingCompose,
    tmp_path: Path,
    *,
    budget: int = 300,
    dependency_services: tuple[str, ...] | None = None,
    runtime_usable: Callable[[], Awaitable[bool]] | None = None,
) -> ComposeOperationError | None:
    return await monitor_resume_runtime.restart_runtime_for_monitor_resume(
        compose,
        compose_project="awf_ws",
        compose_file=tmp_path / "compose.yml",
        workspace_id="ws",
        budget_seconds=budget,
        dependency_services=dependency_services,
        runtime_usable=runtime_usable,
    )


@pytest.mark.unit
async def test_resume_recreates_only_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_inspector(
        monkeypatch,
        _ScriptedInspector(_running(_service("postgres", health="healthy"), _service("agent"))),
    )
    compose = _RecordingCompose()

    await _restart(compose, tmp_path, budget=900)

    common = {
        "project_name": "awf_ws",
        "compose_file": tmp_path / "compose.yml",
        "workspace_id": "ws",
        "wait": True,
        "compose_up_timeout_seconds": 900,
    }
    assert compose.calls == [
        {**common, "no_recreate": True},
        {**common, "force_recreate": True, "no_deps": True, "services": ("agent",)},
    ]
    # No call ever force-recreates anything but the agent.
    for call in compose.calls:
        if call.get("force_recreate"):
            assert call["services"] == ("agent",)
            assert call["no_deps"] is True


@pytest.mark.unit
async def test_dependency_up_leaves_the_stale_agent_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stale agent is neither started nor waited on before it is recreated.

    A whole-project ``up --wait`` started the old agent container, so a failure
    there (a vanished auth overlay bind source) skipped the agent recreate.
    """
    _install_inspector(
        monkeypatch,
        _ScriptedInspector(
            _running(
                _service("postgres", health="healthy"),
                _service("redis", health="healthy"),
                _service("agent", state="exited"),
            )
        ),
    )
    compose = _RecordingCompose()

    await _restart(compose, tmp_path, dependency_services=("agent", "postgres", "redis"))

    assert [(call.get("no_recreate"), call.get("services")) for call in compose.calls] == [
        (True, ("postgres", "redis")),
        (None, ("agent",)),
    ]


@pytest.mark.unit
async def test_agent_only_project_skips_the_dependency_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_inspector(monkeypatch, _ScriptedInspector(_running(_service("agent"))))
    compose = _RecordingCompose()

    await _restart(compose, tmp_path, dependency_services=("agent",))

    assert [call.get("services") for call in compose.calls] == [("agent",)]
    assert compose.calls[0]["force_recreate"] is True


@pytest.mark.unit
async def test_starting_dependency_is_waited_on_and_never_recreated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    starting = _running(_service("postgres", health="starting"), _service("agent"))
    healthy = _running(_service("postgres", health="healthy"), _service("agent"))
    inspector = _install_inspector(monkeypatch, _ScriptedInspector(starting, starting, healthy))
    compose = _RecordingCompose()
    order: list[str] = []

    async def _ensure(**kwargs: Any) -> None:
        # Compose is only reached once the dependency finished its recovery.
        order.append(f"compose@{inspector.calls}")
        compose.calls.append(kwargs)

    monkeypatch.setattr(compose, "ensure_project_up", _ensure)
    clock = _FakeClock()
    monkeypatch.setattr(monitor_resume_runtime, "_monotonic", clock.monotonic)
    monkeypatch.setattr(monitor_resume_runtime, "_sleep", clock.sleep)

    await _restart(compose, tmp_path)

    assert inspector.calls == 3
    assert clock.sleeps == [5.0, 5.0]
    assert order == ["compose@3", "compose@3"]
    assert compose.calls[0]["no_recreate"] is True
    assert "force_recreate" not in compose.calls[0]
    assert compose.calls[1]["services"] == ("agent",)


@pytest.mark.unit
async def test_unhealthy_running_dependency_is_still_waited_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docker keeps probing an ``unhealthy`` container and flips it back to healthy."""
    unhealthy = _running(_service("postgres", health="unhealthy"), _service("agent"))
    healthy = _running(_service("postgres", health="healthy"), _service("agent"))
    inspector = _install_inspector(monkeypatch, _ScriptedInspector(unhealthy, healthy))
    clock = _FakeClock()
    monkeypatch.setattr(monitor_resume_runtime, "_monotonic", clock.monotonic)
    monkeypatch.setattr(monitor_resume_runtime, "_sleep", clock.sleep)

    await monitor_resume_runtime.wait_for_recovering_dependencies(
        "awf_ws", workspace_id="ws", budget_seconds=300
    )

    assert inspector.calls == 2
    assert clock.sleeps == [5.0]


@pytest.mark.unit
async def test_wait_budget_follows_profile_not_rendered_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compose.yml rendered before #936 (``retries: 60``) still gets the profile budget."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        "services:\n"
        "  postgres:\n"
        "    healthcheck:\n"
        "      test: ['CMD-SHELL', 'pg_isready']\n"
        "      interval: 5s\n"
        "      retries: 60\n"
        "  agent:\n"
        "    image: agent\n",
        encoding="utf-8",
    )
    recovering = _running(_service("postgres", health="unhealthy"), _service("agent"))
    healthy = _running(_service("postgres", health="healthy"), _service("agent"))
    # Unhealthy for 600 s of fake time: past the rendered 60 x 5 s, inside 900 s.
    inspector = _install_inspector(monkeypatch, _ScriptedInspector(*([recovering] * 121), healthy))
    clock = _FakeClock()
    monkeypatch.setattr(monitor_resume_runtime, "_monotonic", clock.monotonic)
    monkeypatch.setattr(monitor_resume_runtime, "_sleep", clock.sleep)
    compose = _RecordingCompose()

    await _restart(compose, tmp_path, budget=900)

    assert clock.now == pytest.approx(605.0)
    assert inspector.calls == 122
    assert len(compose.calls) == 2


@pytest.mark.unit
async def test_wait_budget_exhaustion_raises_dependency_wait_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_inspector(
        monkeypatch,
        _ScriptedInspector(
            _running(
                _service("postgres", health="starting"),
                _service("redis", health="healthy"),
                _service("agent"),
            )
        ),
    )
    clock = _FakeClock()
    monkeypatch.setattr(monitor_resume_runtime, "_monotonic", clock.monotonic)
    monkeypatch.setattr(monitor_resume_runtime, "_sleep", clock.sleep)
    compose = _RecordingCompose()

    with pytest.raises(ComposeOperationError) as excinfo:
        await _restart(compose, tmp_path, budget=12)

    error = excinfo.value
    assert error.reason_code == MONITOR_RESUME_DEPENDENCY_WAIT_TIMEOUT_REASON_CODE
    assert error.operation == "dependency_wait"
    assert "postgres" in error.stderr
    assert "starting" in error.stderr
    assert "redis" not in error.stderr
    # The last sleep is clipped to the remaining budget.
    assert clock.sleeps == [5.0, 5.0, 2.0]
    assert compose.calls == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "inspection",
    [
        RuntimeSnapshot(stack_state="unavailable", reason="docker daemon down"),
        RuntimeSnapshot(stack_state="unknown"),
        TimeoutError(),
        OSError("docker missing"),
    ],
)
async def test_unreadable_runtime_does_not_block_compose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inspection: RuntimeSnapshot | BaseException,
) -> None:
    _install_inspector(monkeypatch, _ScriptedInspector(inspection))
    compose = _RecordingCompose()

    await _restart(compose, tmp_path)

    assert [call.get("no_recreate") for call in compose.calls] == [True, None]
    assert compose.calls[1]["services"] == ("agent",)


@pytest.mark.unit
async def test_agent_health_is_not_waited_on_and_stopped_dependencies_are_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dependency that stays exited after its start is left to the project ``up``."""
    inspector = _install_inspector(
        monkeypatch,
        _ScriptedInspector(
            _running(
                _service("agent", health="unhealthy"),
                _service("postgres", state="exited", health="unhealthy"),
                _service("redis"),
            )
        ),
    )
    compose = _RecordingCompose()

    await _restart(compose, tmp_path)

    assert inspector.calls == 2
    assert [call.get("services") for call in compose.calls] == [("postgres",), None, ("agent",)]


@pytest.mark.unit
async def test_stopped_legacy_dependency_gets_the_profile_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a host restart, a pre-#936 dependency must not fail on its stored ``retries: 60``.

    Each stopped dependency is started alone with ``--no-deps`` and no ``--wait``, so
    no compose health gate can fail it; the budgeted wait then covers its recovery.
    """
    stopped = _running(
        _service("postgres", state="exited"),
        _service("redis", state="created"),
        _service("agent", state="exited"),
    )
    recovering = _running(
        _service("postgres", health="unhealthy"),
        _service("redis", health="healthy"),
        _service("agent", state="exited"),
    )
    healthy = _running(
        _service("postgres", health="healthy"),
        _service("redis", health="healthy"),
        _service("agent", state="exited"),
    )
    # Unhealthy for 600 s of fake time: past the rendered 60 x 5 s, inside 900 s.
    inspector = _install_inspector(
        monkeypatch, _ScriptedInspector(stopped, *([recovering] * 121), healthy)
    )
    clock = _FakeClock()
    monkeypatch.setattr(monitor_resume_runtime, "_monotonic", clock.monotonic)
    monkeypatch.setattr(monitor_resume_runtime, "_sleep", clock.sleep)
    compose = _RecordingCompose()

    await _restart(compose, tmp_path, budget=900)

    assert clock.now == pytest.approx(605.0)
    assert inspector.calls == 123
    common = {
        "project_name": "awf_ws",
        "compose_file": tmp_path / "compose.yml",
        "workspace_id": "ws",
        "compose_up_timeout_seconds": 900,
    }
    start = {**common, "wait": False, "no_recreate": True, "no_deps": True}
    assert compose.calls == [
        {**start, "services": ("postgres",)},
        {**start, "services": ("redis",)},
        {**common, "wait": True, "no_recreate": True},
        {**common, "wait": True, "force_recreate": True, "no_deps": True, "services": ("agent",)},
    ]


@pytest.mark.unit
async def test_absent_legacy_dependency_is_created_outside_the_health_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dependency whose container is gone (released runtime) also gets the profile budget.

    Creating it in the ``up --wait`` would gate it on the legacy ``retries: 60``, so
    it is created alone without ``--wait``; a one-off container does not count as
    its container, and a dependency the resume force-recreates is recreated first.
    """
    absent = RuntimeSnapshot(
        stack_state="stopped",
        services=[_service("postgres", state="exited", one_off=True)],
    )
    recovering = _running(_service("postgres", health="starting"))
    healthy = _running(_service("postgres", health="healthy"))
    inspector = _install_inspector(
        monkeypatch, _ScriptedInspector(absent, *([recovering] * 121), healthy)
    )
    clock = _FakeClock()
    monkeypatch.setattr(monitor_resume_runtime, "_monotonic", clock.monotonic)
    monkeypatch.setattr(monitor_resume_runtime, "_sleep", clock.sleep)
    compose = _RecordingCompose()

    await monitor_resume_runtime.restart_runtime_for_monitor_resume(
        compose,
        compose_project="awf_ws",
        compose_file=tmp_path / "compose.yml",
        workspace_id="ws",
        budget_seconds=900,
        dependency_services=("agent", "postgres", "redis"),
        recreate_services=("redis",),
    )

    assert clock.now == pytest.approx(605.0)
    assert inspector.calls == 123
    common = {
        "project_name": "awf_ws",
        "compose_file": tmp_path / "compose.yml",
        "workspace_id": "ws",
        "compose_up_timeout_seconds": 900,
    }
    assert compose.calls[0] == {
        **common,
        "wait": False,
        "force_recreate": True,
        "no_deps": True,
        "services": ("redis",),
    }
    assert compose.calls[1] == {
        **common,
        "wait": False,
        "no_recreate": True,
        "no_deps": True,
        "services": ("postgres",),
    }
    assert [call.get("services") for call in compose.calls] == [
        ("redis",),
        ("postgres",),
        ("postgres", "redis"),
        ("agent",),
    ]


@pytest.mark.unit
async def test_stopped_recreated_companion_is_recreated_before_the_dependency_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running dependency that needs a stopped recreated companion can still settle.

    Skipping the companion until the final recreate left ``web`` unhealthy until
    the wait timed out, so repeated attempts parked a recoverable workspace. It is
    recreated rather than started, then waited on like any running dependency.
    """
    recreated = _running(
        _service("web", health="unhealthy"),
        _service("api", health="starting"),
        _service("agent"),
    )
    settled = _running(
        _service("web", health="healthy"),
        _service("api", health="healthy"),
        _service("agent"),
    )
    inspector = _install_inspector(monkeypatch, _ScriptedInspector(recreated, settled))
    clock = _FakeClock()
    monkeypatch.setattr(monitor_resume_runtime, "_monotonic", clock.monotonic)
    monkeypatch.setattr(monitor_resume_runtime, "_sleep", clock.sleep)
    compose = _RecordingCompose()

    tolerated = await monitor_resume_runtime.restart_runtime_for_monitor_resume(
        compose,
        compose_project="awf_ws",
        compose_file=tmp_path / "compose.yml",
        workspace_id="ws",
        budget_seconds=300,
        dependency_services=("agent", "api", "web"),
        recreate_services=("api",),
    )

    assert tolerated is None
    assert inspector.calls == 2
    assert [
        (call.get("services"), call.get("force_recreate"), call["wait"]) for call in compose.calls
    ] == [
        (("api",), True, False),
        (("api", "web"), None, True),
        # Recreated once: the final recreate leaves the settled companion alone.
        (("agent",), True, True),
    ]


@pytest.mark.unit
async def test_every_recreated_companion_is_recreated_before_anything_is_waited_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRRT_kwDOSJAM6s6vY2fF: no dependency waits on a companion still holding stale config.

    A running companion whose env was rewritten used to be skipped by the wait and
    only recreated with the agent, so ``web``, which needs the new ``api``, stayed
    unhealthy until the wait timed out. The fresh ``api`` is then waited on itself.
    """
    recreated = _running(
        _service("web", health="unhealthy"),
        _service("api", health="starting"),
        _service("agent"),
    )
    settled = _running(
        _service("web", health="healthy"),
        _service("api", health="healthy"),
        _service("agent"),
    )
    compose = _RecordingCompose()
    inspected_after: list[int] = []

    class _Inspector(_ScriptedInspector):
        async def __call__(self, compose_project: str) -> RuntimeSnapshot:
            inspected_after.append(len(compose.calls))
            return await super().__call__(compose_project)

    inspector = _install_inspector(monkeypatch, _Inspector(recreated, recreated, settled))
    clock = _FakeClock()
    monkeypatch.setattr(monitor_resume_runtime, "_monotonic", clock.monotonic)
    monkeypatch.setattr(monitor_resume_runtime, "_sleep", clock.sleep)

    tolerated = await monitor_resume_runtime.restart_runtime_for_monitor_resume(
        compose,
        compose_project="awf_ws",
        compose_file=tmp_path / "compose.yml",
        workspace_id="ws",
        budget_seconds=300,
        dependency_services=("agent", "api", "web"),
        recreate_services=("api",),
    )

    assert tolerated is None
    # Every inspection of the wait follows the companion recreate.
    assert inspected_after == [1, 1, 1]
    assert inspector.calls == 3
    assert clock.sleeps == [5.0, 5.0]
    assert [
        (call.get("services"), call.get("force_recreate"), call.get("no_deps"), call["wait"])
        for call in compose.calls
    ] == [
        (("api",), True, True, False),
        (("api", "web"), None, None, True),
        (("agent",), True, True, True),
    ]


@pytest.mark.unit
async def test_failed_companion_recreate_is_never_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A companion left on stale env or secrets fails the restart before any wait."""
    inspector = _install_inspector(
        monkeypatch,
        _ScriptedInspector(_running(_service("api", health="healthy"), _service("agent"))),
    )
    compose = _RecordingCompose()

    async def _ensure(**kwargs: Any) -> None:
        compose.calls.append(kwargs)
        raise ComposeOperationError(operation="up", returncode=1, stdout="", stderr="api failed")

    monkeypatch.setattr(compose, "ensure_project_up", _ensure)

    async def _never_judged() -> bool:
        raise AssertionError("a failed companion recreate is never judged for tolerance")

    with pytest.raises(ComposeOperationError, match="api failed") as raised:
        await monitor_resume_runtime.restart_runtime_for_monitor_resume(
            compose,
            compose_project="awf_ws",
            compose_file=tmp_path / "compose.yml",
            workspace_id="ws",
            budget_seconds=300,
            dependency_services=("agent", "api"),
            runtime_usable=_never_judged,
            recreate_services=("api",),
        )

    assert not isinstance(raised.value, monitor_resume_runtime.AgentRecreateError)
    assert inspector.calls == 0
    assert [call.get("services") for call in compose.calls] == [("api",)]


@pytest.mark.unit
async def test_one_shot_dependencies_the_resume_ran_to_completion_are_not_started_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRRT_kwDOSJAM6s6vZ5eb: a migration job must not run twice in one resume.

    ``up --no-recreate`` starts every exited container, so a one-shot dependency
    started (or recreated) by the resume that already exited 0 is left out of the
    dependency ``up``; one that failed, or that the resume never started, is not.
    """
    stopped = _running(
        _service("postgres", health="healthy"),
        _service("cleanup"),
        _service("migrate", state="exited", status="Exited (0) 2 hours ago"),
        _service("seed", state="exited", status="Exited (0) 2 hours ago"),
        _service("worker", state="exited", status="Exited (1) 2 hours ago"),
        _service("agent"),
    )
    settled = _running(
        _service("postgres", health="healthy"),
        _service("migrate", state="exited", status="Exited (0) 1 second ago"),
        _service("seed", state="exited", status="Exited (0) 2 seconds ago"),
        _service("worker", state="exited", status="Exited (1) 1 second ago"),
        _service("cleanup", state="exited", status="Exited (0) 1 second ago"),
        _service("agent"),
    )
    _install_inspector(monkeypatch, _ScriptedInspector(stopped, settled))
    compose = _RecordingCompose()

    await monitor_resume_runtime.restart_runtime_for_monitor_resume(
        compose,
        compose_project="awf_ws",
        compose_file=tmp_path / "compose.yml",
        workspace_id="ws",
        budget_seconds=300,
        dependency_services=("agent", "cleanup", "migrate", "postgres", "seed", "worker"),
        recreate_services=("seed",),
    )

    assert [(call.get("services"), call.get("force_recreate")) for call in compose.calls] == [
        (("seed",), True),
        (("migrate",), None),
        (("worker",), None),
        (("cleanup", "postgres", "worker"), None),
        (("agent",), True),
    ]


@pytest.mark.unit
async def test_stopped_dependents_start_only_after_their_prerequisites_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRRT_kwDOSJAM6s6vdT89: ``--no-deps`` starts must still honor ``depends_on``.

    ``app`` sorts before ``postgres``; started first, a one-time init against a
    database that is not up yet could leave it running but unhealthy for good.
    Each stopped dependency now starts only once its prerequisites are healthy.
    """
    stopped = _running(
        _service("app", state="exited"),
        _service("postgres", state="exited"),
        _service("agent", state="exited"),
    )
    postgres_starting = _running(
        _service("app", state="exited"),
        _service("postgres", health="starting"),
        _service("agent", state="exited"),
    )
    postgres_healthy = _running(
        _service("app", state="exited"),
        _service("postgres", health="healthy"),
        _service("agent", state="exited"),
    )
    settled = _running(
        _service("app", health="healthy"),
        _service("postgres", health="healthy"),
        _service("agent", state="exited"),
    )
    inspector = _install_inspector(
        monkeypatch,
        _ScriptedInspector(stopped, postgres_starting, postgres_healthy, settled),
    )
    clock = _FakeClock()
    monkeypatch.setattr(monitor_resume_runtime, "_monotonic", clock.monotonic)
    monkeypatch.setattr(monitor_resume_runtime, "_sleep", clock.sleep)
    compose = _RecordingCompose()
    started_at: list[tuple[tuple[str, ...] | None, int]] = []

    async def _recording_up(**kwargs: Any) -> None:
        started_at.append((kwargs.get("services"), inspector.calls))
        compose.calls.append(kwargs)

    monkeypatch.setattr(compose, "ensure_project_up", _recording_up)

    await monitor_resume_runtime.restart_runtime_for_monitor_resume(
        compose,
        compose_project="awf_ws",
        compose_file=tmp_path / "compose.yml",
        workspace_id="ws",
        budget_seconds=300,
        dependency_services=("agent", "app", "postgres"),
        dependency_conditions={"app": {"postgres": "service_healthy", "agent": "service_started"}},
    )

    # postgres starts on the first snapshot; app only once postgres reports healthy.
    assert started_at == [
        (("postgres",), 1),
        (("app",), 3),
        (("app", "postgres"), 4),
        (("agent",), 4),
    ]
    assert compose.calls[1]["no_deps"] is True
    assert compose.calls[1]["wait"] is False


@pytest.mark.unit
async def test_dependents_wait_for_a_one_shot_and_skip_a_failed_prerequisite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed one-shot stays completed while a failed prerequisite blocks recovery.

    ``app`` waits while ``migrate`` runs; ``worker`` depends on ``broken``, which
    exits non-zero, so skipping Compose's dependency expansion must not start worker.
    """
    stopped = _running(
        _service("app", state="exited"),
        _service("broken", state="exited"),
        _service("migrate", state="exited"),
        _service("worker", state="exited"),
    )
    migrate_running = _running(
        _service("app", state="exited"),
        _service("broken", state="exited", status="Exited (1) 1 second ago"),
        _service("migrate"),
        _service("worker", state="exited"),
    )
    migrate_done = _running(
        _service("app", state="exited"),
        _service("broken", state="exited", status="Exited (1) 2 seconds ago"),
        _service("migrate", state="exited", status="Exited (0) 1 second ago"),
        _service("worker", state="exited"),
    )
    settled = _running(
        _service("app"),
        _service("broken", state="exited", status="Exited (1) 3 seconds ago"),
        _service("migrate", state="exited", status="Exited (0) 2 seconds ago"),
        _service("worker", state="exited"),
    )
    _install_inspector(
        monkeypatch, _ScriptedInspector(stopped, migrate_running, migrate_done, settled)
    )
    clock = _FakeClock()
    monkeypatch.setattr(monitor_resume_runtime, "_monotonic", clock.monotonic)
    monkeypatch.setattr(monitor_resume_runtime, "_sleep", clock.sleep)
    compose = _RecordingCompose()

    with pytest.raises(ComposeOperationError) as caught:
        await monitor_resume_runtime.restart_runtime_for_monitor_resume(
            compose,
            compose_project="awf_ws",
            compose_file=tmp_path / "compose.yml",
            workspace_id="ws",
            budget_seconds=300,
            dependency_services=("agent", "app", "broken", "migrate", "worker"),
            dependency_conditions={
                "app": {"migrate": "service_completed_successfully"},
                "worker": {"broken": "service_started"},
            },
        )

    assert caught.value.reason_code == "MONITOR_RESUME_DEPENDENCY_PREREQUISITE_FAILED"
    assert [call.get("services") for call in compose.calls] == [
        ("broken",),
        ("migrate",),
        ("app",),
    ]
    assert clock.sleeps == [5.0]


async def test_final_up_cannot_expand_a_completed_prerequisite(tmp_path, monkeypatch):
    completed = _service("migrate", state="exited", status="Exited (0) 1 second ago")
    _install_inspector(
        monkeypatch,
        _ScriptedInspector(
            _running(_service("migrate", state="exited"), _service("app", state="exited")),
            _running(completed, _service("app", state="exited")),
            _running(completed, _service("app")),
        ),
    )
    compose = _RecordingCompose()
    await monitor_resume_runtime.restart_runtime_for_monitor_resume(
        compose,
        compose_project="awf_ws",
        compose_file=tmp_path / "compose.yml",
        workspace_id="ws",
        budget_seconds=300,
        dependency_services=("agent", "migrate", "app"),
        dependency_conditions={"app": {"migrate": "service_completed_successfully"}},
    )
    assert [call["services"] for call in compose.calls] == [
        ("migrate",),
        ("app",),
        ("app",),
        ("agent",),
    ]
    final_dependencies = compose.calls[-2]
    assert final_dependencies["no_recreate"] is True
    assert final_dependencies.get("no_deps") is True


@pytest.mark.unit
async def test_unreadable_runtime_after_the_start_keeps_every_dependency_in_the_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a settled snapshot nothing is known to have completed, so nothing is skipped."""
    stopped = _running(
        _service("migrate", state="exited", status="Exited (0) 2 hours ago"),
        _service("agent"),
    )
    _install_inspector(monkeypatch, _ScriptedInspector(stopped, OSError("docker missing")))
    compose = _RecordingCompose()

    await _restart(compose, tmp_path, dependency_services=("agent", "migrate"))

    assert [call.get("services") for call in compose.calls] == [
        ("migrate",),
        ("migrate",),
        ("agent",),
    ]


@pytest.mark.unit
async def test_one_off_and_orphan_containers_are_neither_waited_on_nor_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only declared compose services gate the resume.

    A ``compose run`` container of a declared service and an orphan left by an
    older compose.yml never settle on their own, so waiting on them would
    burn the whole budget and fail a resume whose stack is fine.
    """
    inspector = _install_inspector(
        monkeypatch,
        _ScriptedInspector(
            _running(
                _service("postgres", health="healthy"),
                _service("postgres", health="unhealthy", one_off=True),
                _service("postgres", state="exited", one_off=True),
                _service("legacy-cache", health="starting"),
                _service("legacy-worker", state="exited"),
                _service("agent"),
            )
        ),
    )
    clock = _FakeClock()
    monkeypatch.setattr(monitor_resume_runtime, "_monotonic", clock.monotonic)
    monkeypatch.setattr(monitor_resume_runtime, "_sleep", clock.sleep)
    compose = _RecordingCompose()

    await _restart(compose, tmp_path, dependency_services=("agent", "postgres"))

    assert inspector.calls == 1
    assert clock.sleeps == []
    assert [call.get("services") for call in compose.calls] == [("postgres",), ("agent",)]
    assert compose.calls[0]["no_recreate"] is True


@pytest.mark.unit
async def test_one_off_containers_are_skipped_without_a_readable_compose_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspector = _install_inspector(
        monkeypatch,
        _ScriptedInspector(
            _running(_service("postgres", health="starting", one_off=True), _service("agent"))
        ),
    )
    started: list[tuple[str, ...]] = []

    async def _start(services: tuple[str, ...]) -> None:
        started.append(services)

    await monitor_resume_runtime.wait_for_recovering_dependencies(
        "awf_ws", workspace_id="ws", budget_seconds=300, start_stopped=_start
    )

    assert inspector.calls == 1
    assert started == []


@pytest.mark.unit
async def test_dependency_up_failure_skips_agent_recreate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_inspector(monkeypatch, _ScriptedInspector(_running(_service("agent"))))
    compose = _RecordingCompose()

    async def _ensure(**kwargs: Any) -> None:
        compose.calls.append(kwargs)
        raise ComposeOperationError(operation="up", returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(compose, "ensure_project_up", _ensure)

    with pytest.raises(ComposeOperationError):
        await _restart(compose, tmp_path)

    assert len(compose.calls) == 1
    assert compose.calls[0]["no_recreate"] is True


def _failing_dependency_up(compose: _RecordingCompose, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ensure(**kwargs: Any) -> None:
        compose.calls.append(kwargs)
        if kwargs.get("no_recreate"):
            raise ComposeOperationError(
                operation="up", returncode=1, stdout="", stderr="redis is unhealthy"
            )

    monkeypatch.setattr(compose, "ensure_project_up", _ensure)


@pytest.mark.unit
@pytest.mark.parametrize("dependency_services", [None, ("agent", "postgres")])
async def test_usable_runtime_after_dependency_up_failure_still_recreates_the_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dependency_services: tuple[str, ...] | None,
) -> None:
    """Auth overlays, env and #1090's setup replay all hinge on the agent recreate."""
    _install_inspector(
        monkeypatch,
        _ScriptedInspector(_running(_service("postgres", health="healthy"), _service("agent"))),
    )
    compose = _RecordingCompose()
    _failing_dependency_up(compose, monkeypatch)
    judged: list[int] = []

    async def _usable() -> bool:
        judged.append(len(compose.calls))
        return True

    tolerated = await _restart(
        compose, tmp_path, dependency_services=dependency_services, runtime_usable=_usable
    )

    assert tolerated is not None
    assert tolerated.stderr == "redis is unhealthy"
    # Usability is judged after the failed dependency up, before the agent recreate.
    assert judged == [1]
    assert len(compose.calls) == 2
    assert compose.calls[1]["force_recreate"] is True
    assert compose.calls[1]["no_deps"] is True
    assert compose.calls[1]["services"] == ("agent",)


@pytest.mark.unit
@pytest.mark.parametrize("dependency_up_fails", [False, True])
async def test_failed_agent_recreate_raises_a_distinct_error_never_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dependency_up_fails: bool
) -> None:
    """A usable old stack never makes a failed agent recreate a tolerated restart."""
    _install_inspector(
        monkeypatch,
        _ScriptedInspector(_running(_service("postgres", health="healthy"), _service("agent"))),
    )
    compose = _RecordingCompose()

    async def _ensure(**kwargs: Any) -> None:
        compose.calls.append(kwargs)
        if kwargs.get("force_recreate"):
            raise ComposeOperationError(
                operation="up",
                returncode=3,
                stdout="out",
                stderr="agent mount failed",
                reason_code="COMPOSE_UP_TIMEOUT",
            )
        if dependency_up_fails:
            raise ComposeOperationError(operation="up", returncode=1, stdout="", stderr="redis")

    monkeypatch.setattr(compose, "ensure_project_up", _ensure)

    async def _usable() -> bool:
        return True

    with pytest.raises(monitor_resume_runtime.AgentRecreateError) as raised:
        await _restart(compose, tmp_path, runtime_usable=_usable)

    error = raised.value
    assert (error.operation, error.returncode, error.stdout, error.stderr) == (
        "up",
        3,
        "out",
        "agent mount failed",
    )
    assert error.reason_code == "COMPOSE_UP_TIMEOUT"
    assert compose.calls[-1]["force_recreate"] is True


@pytest.mark.unit
async def test_unusable_runtime_after_dependency_up_failure_skips_the_agent_recreate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_inspector(monkeypatch, _ScriptedInspector(_running(_service("agent"))))
    compose = _RecordingCompose()
    _failing_dependency_up(compose, monkeypatch)

    async def _unusable() -> bool:
        return False

    with pytest.raises(ComposeOperationError, match="redis is unhealthy"):
        await _restart(compose, tmp_path, runtime_usable=_unusable)

    assert len(compose.calls) == 1


def _failing_dependency_start(compose: _RecordingCompose, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ensure(**kwargs: Any) -> None:
        compose.calls.append(kwargs)
        if kwargs.get("no_deps") and not kwargs.get("force_recreate"):
            raise ComposeOperationError(
                operation="up", returncode=1, stdout="", stderr="postgres failed to start"
            )

    monkeypatch.setattr(compose, "ensure_project_up", _ensure)


@pytest.mark.unit
async def test_usable_runtime_after_dependency_start_failure_still_recreates_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed start of a stopped dependency never leaves the stale agent running."""
    _install_inspector(
        monkeypatch,
        _ScriptedInspector(_running(_service("postgres", state="exited"), _service("agent"))),
    )
    compose = _RecordingCompose()
    _failing_dependency_start(compose, monkeypatch)
    judged: list[int] = []

    async def _usable() -> bool:
        judged.append(len(compose.calls))
        return True

    tolerated = await _restart(
        compose, tmp_path, dependency_services=("agent", "postgres"), runtime_usable=_usable
    )

    assert tolerated is not None
    assert tolerated.stderr == "postgres failed to start"
    assert judged == [1]
    assert compose.calls[0]["services"] == ("postgres",)
    assert compose.calls[-1]["force_recreate"] is True
    assert compose.calls[-1]["services"] == ("agent",)


@pytest.mark.unit
async def test_unusable_runtime_after_dependency_start_failure_skips_the_agent_recreate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_inspector(
        monkeypatch,
        _ScriptedInspector(_running(_service("postgres", state="exited"), _service("agent"))),
    )
    compose = _RecordingCompose()
    _failing_dependency_start(compose, monkeypatch)

    async def _unusable() -> bool:
        return False

    with pytest.raises(ComposeOperationError, match="postgres failed to start"):
        await _restart(
            compose, tmp_path, dependency_services=("agent", "postgres"), runtime_usable=_unusable
        )

    assert len(compose.calls) == 1


@pytest.mark.unit
async def test_dependency_wait_timeout_is_never_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timeout feeds the restart streak's unsettled-dependency escalation."""
    _install_inspector(
        monkeypatch,
        _ScriptedInspector(_running(_service("postgres", health="starting"), _service("agent"))),
    )
    clock = _FakeClock()
    monkeypatch.setattr(monitor_resume_runtime, "_monotonic", clock.monotonic)
    monkeypatch.setattr(monitor_resume_runtime, "_sleep", clock.sleep)
    compose = _RecordingCompose()

    async def _never_judged() -> bool:
        raise AssertionError("a dependency wait timeout is never judged for tolerance")

    with pytest.raises(monitor_resume_runtime.DependencyWaitTimeoutError):
        await _restart(compose, tmp_path, budget=5, runtime_usable=_never_judged)

    assert compose.calls == []


@pytest.mark.unit
async def test_clean_restart_tolerates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_inspector(monkeypatch, _ScriptedInspector(_running(_service("agent"))))

    async def _never_judged() -> bool:
        raise AssertionError("usability is only judged after a failed dependency up")

    assert await _restart(_RecordingCompose(), tmp_path, runtime_usable=_never_judged) is None


@pytest.mark.unit
async def test_inspect_resume_runtime_reads_the_compose_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _running(_service("agent"))
    seen: list[str] = []

    class _Inspector:
        async def inspect(self, compose_project: str) -> RuntimeSnapshot:
            seen.append(compose_project)
            return snapshot

    monkeypatch.setattr(monitor_resume_runtime, "RuntimeInspector", _Inspector)

    assert await _REAL_INSPECT("awf_ws") is snapshot
    assert seen == ["awf_ws"]


@pytest.mark.unit
async def test_inspect_resume_runtime_times_out_a_wedged_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _HungInspector:
        async def inspect(self, _compose_project: str) -> RuntimeSnapshot:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    monkeypatch.setattr(monitor_resume_runtime, "RuntimeInspector", _HungInspector)
    monkeypatch.setattr(monitor_resume_runtime, "DEPENDENCY_INSPECT_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(TimeoutError):
        await _REAL_INSPECT("awf_ws")
