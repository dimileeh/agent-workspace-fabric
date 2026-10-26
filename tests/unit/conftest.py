"""Shared autouse fixtures for unit tests."""

from __future__ import annotations

from typing import Any

import pytest

from awf.control.executor import (
    monitor_resume_restart_escalation,
    monitor_resume_runtime,
    runtime_setup_replay,
)
from awf.control.executor.runtime_setup_replay import AgentContainerIdentity
from awf.runtime.inspection import RuntimeSnapshot

_STABLE_AGENT_IDENTITY = AgentContainerIdentity(
    container_id="unit-test-agent", started_at="2026-01-01T00:00:00Z"
)


@pytest.fixture(autouse=True)
def _stable_agent_container_identity(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Report an unchanged agent container around every compose ``up``.

    The runtime setup replay (#787) reads the agent container identity through
    the Docker CLI. Unit tests use fake compose managers, so by default the
    identity never changes and setup is not replayed. Tests that exercise the
    replay patch the probe themselves; Docker-marked tests keep the real one.
    """
    if request.node.get_closest_marker("docker") is not None:
        return

    async def _capture(_compose_project: str | None, **_kwargs: Any) -> AgentContainerIdentity:
        return _STABLE_AGENT_IDENTITY

    monkeypatch.setattr(runtime_setup_replay, "capture_agent_container_identity", _capture)


@pytest.fixture(autouse=True)
def _settled_resume_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Report no recovering dependency to the monitor-resume dependency wait.

    The wait (#936) inspects the compose project through the Docker CLI before
    the resume's ``compose up``. Unit tests use fake compose managers and no
    Docker, so by default the runtime reads as unavailable and the wait returns
    at once without starting anything (an empty stack would mean every declared
    dependency is absent). Tests that exercise the wait patch the seam
    themselves; Docker-marked tests keep it.
    """
    if request.node.get_closest_marker("docker") is not None:
        return

    async def _inspect(_compose_project: str) -> RuntimeSnapshot:
        return RuntimeSnapshot(stack_state="unavailable", reason="no Docker in unit tests")

    monkeypatch.setattr(monitor_resume_runtime, "inspect_resume_runtime", _inspect)


@pytest.fixture(autouse=True)
def _unpaced_resume_restart_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the backoff between failed monitor-resume restarts (#936).

    Unit tests drive several failing resumes back to back; tests of the pacing
    itself patch the sleep with a recorder.
    """

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(monitor_resume_restart_escalation, "_sleep", _no_sleep)
