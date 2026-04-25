"""Runtime inspection edge cases."""

from __future__ import annotations

import pytest

from awf.runtime import inspection
from awf.runtime.inspection import RuntimeInspector


@pytest.mark.unit
async def test_runtime_inspector_returns_unavailable_when_docker_cli_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _missing_exec(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("docker")

    monkeypatch.setattr(inspection.asyncio, "create_subprocess_exec", _missing_exec)

    snapshot = await RuntimeInspector().inspect("awf_ws_missing_cli")

    assert snapshot.stack_state == "unavailable"
    assert snapshot.services == []
    assert snapshot.reason is not None
    assert "docker executable is not available" in snapshot.reason
