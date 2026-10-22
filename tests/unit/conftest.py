"""Shared autouse fixtures for unit tests."""

from __future__ import annotations

from typing import Any

import pytest

from awf.control.executor import runtime_setup_replay
from awf.control.executor.runtime_setup_replay import AgentContainerIdentity

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
