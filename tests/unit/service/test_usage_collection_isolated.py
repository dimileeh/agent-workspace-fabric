"""Focused isolated ccusage collection regressions."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from awf.common.commands import FakeCommandRunner
from awf.common.compose_exec import build_isolated_tracked_compose_run
from awf.db.enums import AgentRuntime
from awf.service.usage_collection import CcusageCollector
from awf.service.usage_store import read_latest_usage_snapshot

_COMPOSE_FILE = Path("/fake/compose.yml")


class FakeClock:
    """Deterministic clock: ``sleep`` blocks until the test calls ``tick``."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self._gate: asyncio.Queue[None] = asyncio.Queue()
        self._t = datetime(2026, 5, 22, tzinfo=UTC)

    def now(self) -> datetime:
        return self._t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await self._gate.get()
        self._t += timedelta(seconds=seconds)

    def tick(self) -> None:
        self._gate.put_nowait(None)


@pytest.mark.unit
async def test_isolated_capture_has_no_agent_writable_usage_mount(tmp_path: Path) -> None:
    """Usage evidence stays in the control plane, not the agent container."""
    collector = CcusageCollector(runner=FakeCommandRunner(), work_dir=tmp_path, clock=FakeClock())

    ctx = await collector.start_isolated(
        compose_project="proj",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_no_usage_mount",
        provider=AgentRuntime.codex,
        cli_args=["codex", "exec", "-"],
    )

    assert ctx.volume_binds == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("provider", "source"),
    [
        (AgentRuntime.claude_code, "claude"),
        (AgentRuntime.codex, "codex"),
        (AgentRuntime.opencode, "opencode"),
    ],
)
async def test_ccusage_argv_per_provider(
    tmp_path: Path, provider: AgentRuntime, source: str
) -> None:
    runner = FakeCommandRunner()
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="proj",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_argv",
        provider=provider,
    )
    await ctx.finalize(status="success")

    args = runner.calls[0].args
    assert args[:2] == ["docker", "compose"]
    assert "-p" in args and "proj" in args
    assert "agent" in args
    assert args[-7:] == [
        "ccusage",
        source,
        "daily",
        "--json",
        "--offline",
        "--config",
        "/opt/awf/ccusage-neutral.json",
    ]


@pytest.mark.unit
async def test_isolated_capture_uses_worker_command_results_for_usage_delta(tmp_path: Path) -> None:
    """The baseline and final output are retained outside the agent's mounts."""
    runner = FakeCommandRunner()
    runner.queue_result(returncode=0, stdout=json.dumps({"totals": {"totalTokens": 5}}))
    runner.queue_result(returncode=0, stdout=json.dumps({"totals": {"totalTokens": 8}}))
    collector = CcusageCollector(
        runner=runner,
        work_dir=tmp_path,
        clock=FakeClock(),
        command_timeout_seconds=3.5,
    )
    ctx = await collector.start_isolated(
        compose_project="proj",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_isolated",
        provider=AgentRuntime.codex,
        cli_args=["codex", "exec", "-"],
    )

    assert ctx.cli_args == ["codex", "exec", "-"]
    assert not hasattr(ctx, "agent_completion_marker")
    next_ctx = await collector.start_isolated(
        compose_project="proj",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_isolated_next",
        provider=AgentRuntime.codex,
        cli_args=["codex", "exec", "-"],
    )
    assert next_ctx.cli_args == ["codex", "exec", "-"]
    assert ctx.baseline_cli_args == [
        "timeout",
        "3.5s",
        "ccusage",
        "codex",
        "daily",
        "--json",
        "--offline",
        "--config",
        "/opt/awf/ccusage-neutral.json",
    ]
    baseline_invocation = build_isolated_tracked_compose_run(
        compose_project="proj",
        compose_file=_COMPOSE_FILE,
        cli_args=ctx.baseline_cli_args or [],
        source="usage",
        label="codex",
        worktree_host_path=Path("/worktrees/ws_isolated/reask"),
        extra_volume_binds=ctx.volume_binds,
    )
    assert "/tmp/awf-ccusage" not in baseline_invocation.args

    await ctx.capture_baseline_before_agent(invocation=baseline_invocation)
    await ctx.capture_final_before_cleanup(container_name="awf-reask-test")
    await ctx.finalize(status="success")

    snapshot = read_latest_usage_snapshot("ws_isolated", work_dir=tmp_path)
    assert snapshot is not None
    assert snapshot.phase == "final"
    assert snapshot.status == "available"
    assert snapshot.total_tokens == 3
    assert runner.calls[1].args[:4] == ["docker", "exec", "awf-reask-test", "timeout"]
