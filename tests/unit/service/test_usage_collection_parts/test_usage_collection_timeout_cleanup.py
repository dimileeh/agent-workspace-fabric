"""Cancellation coverage for isolated ccusage timeout cleanup."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.db.enums import AgentRuntime
from awf.service import usage_collection
from awf.service.usage_collection import CcusageCollector, _is_missing_binary
from tests.unit.service.test_usage_collection import _COMPOSE_FILE, FakeClock


@pytest.mark.unit
async def test_timeout_cleanup_reraises_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = FakeCommandRunner()
    collector = CcusageCollector(runner=runner, work_dir=tmp_path, clock=FakeClock())
    ctx = await collector.start(
        compose_project="p",
        compose_file=_COMPOSE_FILE,
        workspace_id="ws_cleanup_cancel",
        provider=AgentRuntime.claude_code,
    )

    async def _raise_cleanup(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError

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

    with pytest.raises(asyncio.CancelledError):
        await ctx._cleanup_timed_out_invocation(invocation)

    await ctx.finalize(status="failed")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (CommandResult(returncode=127, stdout="", stderr=""), True),
        (CommandResult(returncode=1, stdout="", stderr="bash: ccusage: command not found"), True),
        (
            CommandResult(
                returncode=1,
                stdout="",
                stderr="Error: ENOENT: no such file or directory, open "
                "'/opt/awf/ccusage-neutral.json'",
            ),
            False,
        ),
        (CommandResult(returncode=1, stdout="", stderr="boom"), False),
        (CommandResult(returncode=1, stdout="", stderr="source not found"), False),
        (CommandResult(returncode=1, stdout="usage record not found", stderr=""), False),
    ],
)
def test_is_missing_binary(result: CommandResult, expected: bool) -> None:
    assert _is_missing_binary(result) is expected
