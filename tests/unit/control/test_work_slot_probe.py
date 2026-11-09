"""Local docker/compose work-slot process probe (slot-release, plan §6.2 E)."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.common.commands import FakeCommandRunner
from awf.control.worker.work_slot_probe import LocalWorkSlotProcessProbe
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot

pytestmark = pytest.mark.unit

_COMPOSE_FILE = Path("/tmp/awf/ws/compose.yml")


class _Inspector:
    def __init__(self, result: RuntimeSnapshot | BaseException) -> None:
        self._result = result

    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
        del compose_project_name
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


def _agent(state: str, *, one_off: bool = False) -> RuntimeService:
    return RuntimeService(
        name="agent", container_id="c1", image="img", state=state, one_off=one_off
    )


def _probe(
    inspector: _Inspector, runner: FakeCommandRunner | None = None
) -> LocalWorkSlotProcessProbe:
    return LocalWorkSlotProcessProbe(
        runner=runner or FakeCommandRunner(),
        runtime_inspector=inspector,
    )


async def _run(probe: LocalWorkSlotProcessProbe, **overrides: object) -> str:
    kwargs: dict[str, object] = {
        "workspace_id": "ws_1",
        "invocation_id": "wsl_abc",
        "compose_project": "awf_ws_1",
        "compose_file": _COMPOSE_FILE,
    }
    kwargs.update(overrides)
    return await probe.probe(**kwargs)  # type: ignore[arg-type]


async def test_missing_compose_stack_means_nothing_can_be_in_flight() -> None:
    probe = _probe(_Inspector(RuntimeError("must not inspect")))
    assert await _run(probe, compose_project=None) == "absent"
    assert await _run(probe, compose_file=None) == "absent"


@pytest.mark.parametrize(
    "result",
    [
        OSError("docker socket gone"),
        RuntimeError("docker exploded"),
        RuntimeSnapshot(stack_state="unavailable", reason="daemon down"),
    ],
)
async def test_unqueryable_runtime_is_unknown(result: RuntimeSnapshot | BaseException) -> None:
    assert await _run(_probe(_Inspector(result))) == "unknown"


def _unlabeled(state: str) -> RuntimeService:
    # ``docker inspect`` failed: the name falls back to the container name.
    return RuntimeService(
        name="awf_ws_1-agent-1",
        container_id="c1",
        image="img",
        state=state,
        service_label_available=False,
    )


@pytest.mark.parametrize(
    "services",
    [
        [],
        [_agent("exited")],
        [_agent("running", one_off=True)],
        [RuntimeService(name="postgres", container_id="p", image="pg", state="running")],
        [_unlabeled("exited")],
    ],
)
async def test_no_running_agent_container_is_absent(services: list[RuntimeService]) -> None:
    snapshot = RuntimeSnapshot(stack_state="running", services=services)
    assert await _run(_probe(_Inspector(snapshot))) == "absent"


async def test_running_container_without_service_label_is_unknown() -> None:
    snapshot = RuntimeSnapshot(stack_state="running", services=[_unlabeled("running")])
    assert await _run(_probe(_Inspector(snapshot))) == "unknown"


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [
        ("awf work-slot probe: present wsl_abc\n", 0, "present"),
        ("awf work-slot probe: absent wsl_abc\n", 0, "absent"),
        ("", 1, "unknown"),
    ],
)
async def test_running_agent_is_probed_for_the_tagged_process(
    stdout: str, returncode: int, expected: str
) -> None:
    runner = FakeCommandRunner()
    runner.queue_result(returncode=returncode, stdout=stdout)
    snapshot = RuntimeSnapshot(stack_state="running", services=[_agent("running")])

    assert await _run(_probe(_Inspector(snapshot), runner)) == expected
    assert runner.calls[0].args[-2:] == ["awf-work-slot-probe", "wsl_abc"]


async def test_exec_failure_is_unknown() -> None:
    class _ExplodingRunner(FakeCommandRunner):
        async def run(self, *_args: object, **_kwargs: object):  # type: ignore[no-untyped-def,override]
            raise OSError("docker compose missing")

    snapshot = RuntimeSnapshot(stack_state="running", services=[_agent("running")])
    assert await _run(_probe(_Inspector(snapshot), _ExplodingRunner())) == "unknown"
