"""Agent runs hand the idle watchdog a worktree activity probe (issue #932).

``AgentAdapter.run()`` already receives the workspace worktree; it must thread it
into the streaming runner so a print-mode CLI that is silently editing files is
not declared idle. Monitor repair runs go through the same adapter, so the local
(compose) monitor path must pass ``worktree_path`` too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from awf.adapters.base import AgentAdapter
from awf.common.commands import CommandResult
from awf.db.enums import AgentRuntime

_COMPOSE_PROJECT = "awf_ws_probe"


class _RecordingStreamingRunner:
    """Capture the kwargs the adapter hands to ``run_streaming``."""

    def __init__(self) -> None:
        self.streaming_kwargs: list[dict[str, Any]] = []

    async def run(self, args: list[str], **_kwargs: Any) -> CommandResult:
        del args
        return CommandResult(returncode=0, stdout="", stderr="")

    async def run_streaming(self, args: list[str], **kwargs: Any) -> CommandResult:
        del args
        self.streaming_kwargs.append(dict(kwargs))
        return CommandResult(returncode=0, stdout="ok\n", stderr="")


class _ProbeAdapter(AgentAdapter):
    name = AgentRuntime.codex

    def get_provider(self, model: str | None) -> str:
        del model
        return "openai"

    def _cli_args(self, *, model: str | None = None) -> list[str]:
        del model
        return ["codex", "exec"]


@pytest.mark.unit
async def test_worktree_path_produces_an_activity_probe(tmp_path: Path) -> None:
    runner = _RecordingStreamingRunner()
    adapter = _ProbeAdapter(runner=runner)
    worktree = tmp_path / "ws_probe"
    worktree.mkdir()

    await adapter.run(
        compose_project=_COMPOSE_PROJECT,
        compose_file=tmp_path / "compose.yml",
        prompt="do the work",
        workspace_id="ws_probe",
        worktree_path=worktree,
    )

    assert len(runner.streaming_kwargs) == 1
    probe = runner.streaming_kwargs[0]["activity_probe"]
    assert probe is not None
    assert await probe() in (True, False)


@pytest.mark.unit
async def test_no_worktree_path_leaves_the_watchdog_output_only(tmp_path: Path) -> None:
    runner = _RecordingStreamingRunner()
    adapter = _ProbeAdapter(runner=runner)

    await adapter.run(
        compose_project=_COMPOSE_PROJECT,
        compose_file=tmp_path / "compose.yml",
        prompt="do the work",
        workspace_id="ws_probe",
    )

    assert len(runner.streaming_kwargs) == 1
    assert runner.streaming_kwargs[0].get("activity_probe") is None


@pytest.mark.unit
async def test_missing_worktree_path_produces_no_probe(tmp_path: Path) -> None:
    runner = _RecordingStreamingRunner()
    adapter = _ProbeAdapter(runner=runner)

    await adapter.run(
        compose_project=_COMPOSE_PROJECT,
        compose_file=tmp_path / "compose.yml",
        prompt="do the work",
        workspace_id="ws_probe",
        worktree_path=tmp_path / "never_provisioned",
    )

    assert runner.streaming_kwargs[0].get("activity_probe") is None


@pytest.mark.unit
async def test_local_monitor_run_kwargs_include_the_worktree_path(tmp_path: Path) -> None:
    """The monitor's local (compose) repair run must get the probe as well."""
    from awf.runtime.pr_monitor_runner import agent_service_recovery

    captured: dict[str, Any] = {}

    class _Adapter:
        is_hosted = False

        async def run(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            from awf.adapters.base import AgentRunResult

            return AgentRunResult(returncode=0, stdout="ok", stderr="")

    class _Runner:
        _worktrees_root = tmp_path
        _workspace_profile = None

        def __init__(self) -> None:
            from types import SimpleNamespace

            self._deps = SimpleNamespace(adapter=_Adapter())

    runner = _Runner()
    await agent_service_recovery._run_monitor_agent_with_service_recovery_locked(
        runner,
        workspace_id="ws_probe",
        compose_project=_COMPOSE_PROJECT,
        compose_file=tmp_path / "compose.yml",
        prompt="repair the review comment",
        log_source="recovery",
    )

    assert captured["worktree_path"] == tmp_path / "ws_probe"
