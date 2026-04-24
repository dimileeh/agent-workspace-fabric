"""Tests for the release / feature PR monitor factory helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.adapters.base import AgentAdapter, AgentRunResult
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClient
from awf.db.enums import AgentRuntime
from awf.runtime.release_pr_monitor import (
    build_feature_pr_monitor,
    build_release_pr_monitor,
)


class _StubAdapter(AgentAdapter):
    runtime = AgentRuntime.claude_code

    def __init__(self) -> None:
        super().__init__(runner=None)  # type: ignore[arg-type]

    @property
    def name(self) -> AgentRuntime:  # type: ignore[override]
        return AgentRuntime.claude_code

    def _cli_args(self, *, prompt: str, model: str | None) -> list[str]:  # type: ignore[override]
        return []

    async def run(  # type: ignore[override]
        self, *, compose_project: str, compose_file: Path, prompt: str, model: str | None = None
    ) -> AgentRunResult:
        return AgentRunResult(returncode=0, stdout="", stderr="")


@pytest.mark.unit
def test_release_monitor_has_auto_merge_disabled(tmp_path: Path) -> None:
    cmd = FakeCommandRunner()
    runner = build_release_pr_monitor(
        session_factory=None,  # type: ignore[arg-type] - not actually exercised
        runner=cmd,
        adapter=_StubAdapter(),
        gh=GitHubClient(cmd),
        worktrees_root=tmp_path,
    )
    assert runner._config.auto_merge is False


@pytest.mark.unit
def test_feature_monitor_has_auto_merge_enabled(tmp_path: Path) -> None:
    cmd = FakeCommandRunner()
    runner = build_feature_pr_monitor(
        session_factory=None,  # type: ignore[arg-type]
        runner=cmd,
        adapter=_StubAdapter(),
        gh=GitHubClient(cmd),
        worktrees_root=tmp_path,
    )
    assert runner._config.auto_merge is True


@pytest.mark.unit
def test_factories_plumb_configured_knobs(tmp_path: Path) -> None:
    cmd = FakeCommandRunner()
    runner = build_release_pr_monitor(
        session_factory=None,  # type: ignore[arg-type]
        runner=cmd,
        adapter=_StubAdapter(),
        gh=GitHubClient(cmd),
        worktrees_root=tmp_path,
        poll_interval_seconds=15,
        settle_interval_seconds=7,
    )
    assert runner._config.poll_interval_seconds == 15
    assert runner._config.settle_interval_seconds == 7
