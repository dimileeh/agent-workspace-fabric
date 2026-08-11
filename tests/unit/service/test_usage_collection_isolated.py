"""Focused isolated ccusage collection regressions."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from stat import S_IMODE

import pytest

from awf.common.commands import FakeCommandRunner
from awf.db.enums import AgentRuntime
from awf.node.git_manager import AGENT_RUNTIME_GID, AGENT_RUNTIME_UID
from awf.service import usage_collection
from awf.service.usage_collection import CcusageCollector, _make_isolated_capture_dir
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
def test_isolated_capture_dir_assigns_private_runtime_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The root worker creates private captures for the non-root runtime."""
    ownership_calls: list[tuple[Path, int, int]] = []

    def _record_chown(path: Path, uid: int, gid: int) -> None:
        ownership_calls.append((path, uid, gid))

    monkeypatch.setattr(usage_collection.os, "chown", _record_chown)
    capture_dir = _make_isolated_capture_dir(tmp_path, workspace_id="ws_capture_owner")

    stat = capture_dir.stat()
    assert ownership_calls == [(capture_dir, AGENT_RUNTIME_UID, AGENT_RUNTIME_GID)]
    assert S_IMODE(stat.st_mode) == 0o700
    assert capture_dir.parent == tmp_path / "compose" / "ws_capture_owner" / "usage-captures"


@pytest.mark.unit
def test_isolated_capture_dir_removes_partial_dir_when_ownership_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed ownership handoff does not leak an unusable capture directory."""

    def _fail_chown(_path: Path, _uid: int, _gid: int) -> None:
        raise PermissionError("ownership denied")

    monkeypatch.setattr(usage_collection.os, "chown", _fail_chown)

    with pytest.raises(PermissionError, match="ownership denied"):
        _make_isolated_capture_dir(tmp_path, workspace_id="ws_capture_failure")

    assert list((tmp_path / "compose" / "ws_capture_failure" / "usage-captures").iterdir()) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("provider", "source"),
    [
        (AgentRuntime.claude_code, "claude"),
        (AgentRuntime.codex, "codex"),
        (AgentRuntime.gemini, "gemini"),
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
async def test_isolated_run_prepares_standalone_baseline_probe_before_agent_watchdog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clarification re-ask reads its disposable copied auth state, not ``agent``."""
    monkeypatch.setattr(usage_collection.os, "chown", lambda _path, _uid, _gid: None)
    runner = FakeCommandRunner()
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
    baseline_script = ctx.baseline_cli_args[2]
    assert ctx.baseline_cli_args[:2] == ["sh", "-lc"]
    assert "ccusage codex daily --json --offline" in baseline_script
    assert "timeout 3.5s ccusage codex daily --json --offline" in baseline_script
    assert "baseline.stdout" in baseline_script
    assert ctx.volume_binds[0][1] == "/tmp/awf-ccusage"
    capture_dir = ctx.volume_binds[0][0]
    assert capture_dir.parent == tmp_path / "compose" / "ws_isolated" / "usage-captures"
    for sample, total_tokens in (("baseline", 5), ("final", 8)):
        (capture_dir / f"{sample}.status").write_text("0\n", encoding="utf-8")
        (capture_dir / f"{sample}.stdout").write_text(
            json.dumps({"totals": {"totalTokens": total_tokens}}), encoding="utf-8"
        )
        (capture_dir / f"{sample}.stderr").write_text("", encoding="utf-8")

    await ctx.finalize(status="success")

    snapshot = read_latest_usage_snapshot("ws_isolated", work_dir=tmp_path)
    assert snapshot is not None
    assert snapshot.phase == "final"
    assert snapshot.status == "available"
    assert snapshot.total_tokens == 3
    assert not capture_dir.exists()
    await ctx.finalize(status="success")
