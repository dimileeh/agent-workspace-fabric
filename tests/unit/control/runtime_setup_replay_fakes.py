"""Shared fakes for runtime setup replay tests (#787, #1006).

``FakeAgentContainer`` models the one thing a recreated runtime loses: state
that profile setup wrote inside the container (a tool under ``/tmp``). A compose
``up`` that recreates the container assigns a new identity and wipes ``/tmp``;
``FakeToolValidation`` installs the tool in ``setup`` and fails ``validate``
with 127 when the tool is gone, the exact shape of the reported failures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from awf.control.executor import runtime_setup_replay
from awf.control.executor.runtime_setup_replay import AgentContainerIdentity
from awf.runtime.validation import ValidationCommandResult, ValidationResult
from awf.runtime.validation_types import ValidateCommandProbeTarget, ValidateToolProbeResult

TOOL_PATH = "/tmp/awf-tools/bin/tool"
SETUP_COMMAND = f"install-tool --to {TOOL_PATH}"
VALIDATE_COMMAND = f"{TOOL_PATH} check"


class FakeAgentContainer:
    """In-memory agent container whose identity changes on every recreation."""

    def __init__(self, *, present: bool = True) -> None:
        self.generation = 0
        self.present = present
        self.tmp: set[str] = set()

    @property
    def container_id(self) -> str:
        return f"agent-{self.generation}"

    @property
    def started_at(self) -> str:
        return f"2026-10-22T00:00:{self.generation:02d}Z"

    def identity(self) -> AgentContainerIdentity | None:
        if not self.present:
            return None
        return AgentContainerIdentity(container_id=self.container_id, started_at=self.started_at)

    def recreate(self) -> None:
        self.generation += 1
        self.present = True
        self.tmp.clear()

    def release(self) -> None:
        """Model the runtime release on block: no agent container until the next ``up``."""
        self.present = False
        self.tmp.clear()


class FakeCompose:
    """Compose double whose ``up`` recreates the fake container (or leaves it alone)."""

    def __init__(self, container: FakeAgentContainer, *, recreate: bool = True) -> None:
        self.container = container
        self.recreate = recreate
        self.calls: list[dict[str, Any]] = []

    async def ensure_project_up(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if kwargs.get("no_recreate"):
            # ``--no-recreate`` only creates an absent container (#936).
            if not self.container.present:
                self.container.recreate()
            return
        if self.recreate or not self.container.present:
            self.container.recreate()


def _command(
    *, phase: str, command: str, returncode: int, stdout: str = ""
) -> ValidationCommandResult:
    return ValidationCommandResult(
        command=command,
        returncode=returncode,
        duration_seconds=0.0,
        stdout_path=Path("/nonexistent/stdout"),
        stderr_path=Path("/nonexistent/stderr"),
        phase=phase,
        captured_stdout=stdout,
        captured_stderr="tool: not found" if returncode == 127 else "",
    )


class FakeToolValidation:
    """Validation double: setup installs the tool; validate needs it (127 otherwise)."""

    def __init__(
        self,
        container: FakeAgentContainer,
        *,
        setup_returncode: int = 0,
        setup_stdout: str = "",
        setup_error: BaseException | None = None,
    ) -> None:
        self.container = container
        self.setup_returncode = setup_returncode
        self.setup_stdout = setup_stdout
        self.setup_error = setup_error
        self.phase_calls: list[tuple[str, ...]] = []
        self.probe_calls = 0
        self.trace: list[str] = []

    def setup_runs(self) -> int:
        return sum(1 for phases in self.phase_calls if "setup" in phases)

    async def run_profile_phases(
        self, *, phase_names: tuple[str, ...], **_kwargs: Any
    ) -> ValidationResult:
        self.phase_calls.append(tuple(phase_names))
        self.trace.append("phases:" + ",".join(phase_names))
        commands: list[ValidationCommandResult] = []
        for phase in phase_names:
            if phase == "setup":
                if self.setup_error is not None:
                    raise self.setup_error
                if self.setup_returncode == 0:
                    self.container.tmp.add(TOOL_PATH)
                commands.append(
                    _command(
                        phase=phase,
                        command=SETUP_COMMAND,
                        returncode=self.setup_returncode,
                        stdout=self.setup_stdout,
                    )
                )
            elif phase == "validate":
                returncode = 0 if TOOL_PATH in self.container.tmp else 127
                commands.append(
                    _command(phase=phase, command=VALIDATE_COMMAND, returncode=returncode)
                )
        return ValidationResult(commands=commands)

    async def probe_validate_command_tools(self, **_kwargs: Any) -> ValidateToolProbeResult:
        self.probe_calls += 1
        missing = (
            ()
            if TOOL_PATH in self.container.tmp
            else (ValidateCommandProbeTarget(tool=TOOL_PATH, command=VALIDATE_COMMAND),)
        )
        return ValidateToolProbeResult(missing=missing, probe_ran=True)

    async def run_profile_coverage(self, **_kwargs: Any) -> None:
        return None


def patch_identity(monkeypatch: pytest.MonkeyPatch, container: FakeAgentContainer) -> None:
    """Point the identity probe at ``container`` instead of the Docker CLI."""

    async def _capture(_compose_project: str | None, **_kwargs: Any) -> Any:
        return container.identity()

    monkeypatch.setattr(runtime_setup_replay, "capture_agent_container_identity", _capture)


async def allow_ownership_repair(**_kwargs: Any) -> bool:
    return True
