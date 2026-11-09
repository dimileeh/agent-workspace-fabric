"""``AgentAdapter.run`` holds the caller-injected work slot (slot-release).

The hold wraps the whole agent execution (local compose exec and hosted
delegation) and the local ``awf-exec`` carries the
``AWF_WORK_SLOT_INVOCATION_ID`` marker the restart probe looks for. Without an
injected slot the argv is byte-identical to before.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import awf.adapters.registry  # noqa: F401 — populate registry
from awf.adapters.base import AgentRunError
from awf.adapters.codex import CodexAdapter
from awf.adapters.runtime_executor import AgentRuntimeExecRequest, AgentRuntimeExecResult
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.work_slot import WORK_SLOT_INVOCATION_ENV

pytestmark = pytest.mark.unit

_PROMPT = "Add a one-line docstring."
_COMPOSE_PROJECT = "awf_ws_slot"
_COMPOSE_FILE = Path("/fake/path/compose.yml")


@dataclass
class _Handle:
    invocation_id: str


@dataclass
class _RecordingWorkSlot:
    invocation_id: str = "wsl_agent_run"
    events: list[str] = field(default_factory=list)
    active: bool = False

    @asynccontextmanager
    async def hold(self, phase: str) -> AsyncIterator[_Handle]:
        self.events.append(f"enter:{phase}")
        self.active = True
        try:
            yield _Handle(self.invocation_id)
        except BaseException as exc:
            self.events.append(f"error:{type(exc).__name__}")
            raise
        finally:
            self.active = False
            self.events.append(f"exit:{phase}")

    async def release_idle(self) -> None:  # pragma: no cover - not used by adapters
        self.events.append("release_idle")


class _SlotCheckingRunner(FakeCommandRunner):
    def __init__(self, slot: _RecordingWorkSlot) -> None:
        super().__init__()
        self._slot = slot
        self.active_during_run: list[bool] = []

    async def run(self, args: list[str], **kwargs: object) -> CommandResult:  # type: ignore[override]
        self.active_during_run.append(self._slot.active)
        return await super().run(args, **kwargs)  # type: ignore[arg-type]


async def test_local_run_holds_the_slot_and_tags_awf_exec() -> None:
    slot = _RecordingWorkSlot()
    runner = _SlotCheckingRunner(slot)
    adapter = CodexAdapter(runner=runner, default_model="gpt-5")

    await adapter.run(
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        prompt=_PROMPT,
        workspace_id="ws_slot",
        work_slot=slot,
    )

    assert slot.events == ["enter:agent", "exit:agent"]
    assert runner.active_during_run and all(runner.active_during_run)
    args = runner.calls[0].args
    marker = f"{WORK_SLOT_INVOCATION_ENV}=wsl_agent_run"
    assert marker in args
    assert args[args.index(marker) - 1] == "-e"


async def test_without_a_work_slot_the_agent_argv_is_unchanged() -> None:
    plain = FakeCommandRunner()
    explicit_none = FakeCommandRunner()
    await CodexAdapter(runner=plain, default_model="gpt-5").run(
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        prompt=_PROMPT,
    )
    await CodexAdapter(runner=explicit_none, default_model="gpt-5").run(
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        prompt=_PROMPT,
        work_slot=None,
    )

    plain_args = plain.calls[0].args
    none_args = explicit_none.calls[0].args
    # Only the per-call random awf-exec invocation id differs between the runs.
    awf_exec_index = plain_args.index("awf-exec")
    assert plain_args[:awf_exec_index] == none_args[:awf_exec_index]
    assert plain_args[awf_exec_index + 2 :] == none_args[awf_exec_index + 2 :]
    assert not any(WORK_SLOT_INVOCATION_ENV in arg for arg in plain_args)


async def test_agent_failure_propagates_through_the_hold() -> None:
    slot = _RecordingWorkSlot()
    runner = FakeCommandRunner()
    runner.queue_result(returncode=1, stdout="", stderr="provider exploded")
    adapter = CodexAdapter(runner=runner, default_model="gpt-5")

    with pytest.raises(AgentRunError):
        await adapter.run(
            compose_project=_COMPOSE_PROJECT,
            compose_file=_COMPOSE_FILE,
            prompt=_PROMPT,
            work_slot=slot,
        )

    assert slot.events == ["enter:agent", "error:AgentRunError", "exit:agent"]


class _HostedExecutor:
    def __init__(self, slot: _RecordingWorkSlot) -> None:
        self._slot = slot
        self.active_during_execute: list[bool] = []

    async def execute(self, request: AgentRuntimeExecRequest) -> AgentRuntimeExecResult:
        del request
        self.active_during_execute.append(self._slot.active)
        return AgentRuntimeExecResult(returncode=0, stdout="ok", stderr="")


async def test_hosted_run_holds_the_slot_without_a_process_marker() -> None:
    slot = _RecordingWorkSlot()
    executor = _HostedExecutor(slot)
    adapter = CodexAdapter(
        runner=FakeCommandRunner(),
        default_model="gpt-5",
        runtime_executor=executor,  # type: ignore[arg-type]
    )

    await adapter.run(
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        prompt=_PROMPT,
        workspace_id="ws_hosted",
        work_slot=slot,
    )

    assert executor.active_during_execute == [True]
    assert slot.events == ["enter:agent", "exit:agent"]
