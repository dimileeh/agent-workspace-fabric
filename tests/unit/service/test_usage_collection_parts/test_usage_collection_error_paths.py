"""Usage-collection error-path regression tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from awf.common.commands import FakeCommandRunner
from awf.common.compose_exec import build_isolated_tracked_compose_run
from awf.db.enums import AgentRuntime
from awf.service import usage_collection
from awf.service.usage_collection import CcusageCollector
from tests.unit.service.test_usage_collection import _COMPOSE_FILE, FakeClock


@pytest.mark.unit
async def test_safe_write_reading_logs_non_cancel_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    collector = CcusageCollector(runner=FakeCommandRunner(), work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_safe_write_error",
        provider=AgentRuntime.claude_code,
    )

    async def _raise_write(**_kwargs: object) -> None:
        raise RuntimeError("write failed")

    monkeypatch.setattr(ctx, "_write_reading", _raise_write)
    await ctx._safe_write_reading(
        usage=None,
        reason="unavailable",
        model=None,
        phase="live",
        run_status="running",
    )
    await ctx.finalize(status="failed")


@pytest.mark.unit
async def test_timeout_cleanup_logs_non_cancel_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_cleanup_error",
        provider=AgentRuntime.claude_code,
    )

    async def _raise_cleanup(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(usage_collection, "cleanup_compose_exec_invocation", _raise_cleanup)
    invocation = usage_collection.TrackedComposeExec(
        args=["docker", "compose", "exec"],
        invocation_id="inv",
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        service="agent",
        workdir="/workspace",
        source="usage",
        label="awf=usage",
        wrapper_script="wrapper",
        cleanup_script="cleanup",
    )
    await ctx._cleanup_timed_out_invocation(invocation)
    await ctx.finalize(status="failed")


@pytest.mark.unit
async def test_isolated_usage_setup_propagates_shutdown_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cancellation during the pre-agent snapshot read is never downgraded to unavailable."""
    collector = CcusageCollector(runner=FakeCommandRunner(), work_dir=tmp_path, clock=FakeClock())

    async def _cancel_thread(*_args: object, **_kwargs: object) -> object:
        raise asyncio.CancelledError

    monkeypatch.setattr(usage_collection.asyncio, "to_thread", _cancel_thread)

    with pytest.raises(asyncio.CancelledError):
        await collector.start_isolated(
            compose_project="p",
            compose_file=_COMPOSE_FILE,
            workspace_id="ws_isolated_setup_cancelled",
            provider=AgentRuntime.codex,
            cli_args=["codex", "exec"],
        )


@pytest.mark.unit
async def test_isolated_usage_setup_continues_after_snapshot_read_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A transient prior-snapshot failure cannot prevent isolated usage capture."""
    collector = CcusageCollector(runner=FakeCommandRunner(), work_dir=tmp_path, clock=FakeClock())

    async def _raise_read(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("snapshot unavailable")

    monkeypatch.setattr(usage_collection.asyncio, "to_thread", _raise_read)

    ctx = await collector.start_isolated(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_isolated_setup_error",
        provider=AgentRuntime.codex,
        cli_args=["codex", "exec"],
    )

    assert ctx.baseline_cli_args is not None


@pytest.mark.unit
async def test_isolated_baseline_capture_continues_after_probe_failure(tmp_path: Path) -> None:
    """A failed pre-agent probe records an unavailable baseline without aborting the run."""

    class _FailingProbeRunner:
        async def run_streaming(self, _args: list[str], **_kwargs: object) -> object:
            raise RuntimeError("ccusage probe unavailable")

    collector = CcusageCollector(runner=_FailingProbeRunner(), work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start_isolated(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_isolated_probe_error",
        provider=AgentRuntime.codex,
        cli_args=["codex", "exec"],
    )

    await ctx.capture_baseline_before_agent(
        invocation=build_isolated_tracked_compose_run(
            compose_project="p",
            compose_file=_COMPOSE_FILE,
            cli_args=ctx.baseline_cli_args or [],
            source="usage",
            label="codex",
            worktree_host_path=Path("/worktrees/ws_isolated_probe_error/reask"),
        )
    )

    assert ctx._capture_results == {}  # noqa: SLF001 - the probe result is intentionally absent
