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
    name: str, *, state: str = "running", health: str | None = None, one_off: bool = False
) -> RuntimeService:
    return RuntimeService(
        name=name,
        container_id=f"{name}-id",
        image=None,
        state=state,
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
            _running(_service("postgres", health="healthy"), _service("agent", state="exited"))
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
    _install_inspector(monkeypatch, _ScriptedInspector(_running(_service("agent"))))
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
