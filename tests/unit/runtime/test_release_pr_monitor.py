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
from awf.runtime.validation import ValidationRunner


class _StubAdapter(AgentAdapter):
    runtime = AgentRuntime.claude_code

    def __init__(self) -> None:
        super().__init__(runner=None)  # type: ignore[arg-type]

    def get_provider(self, model: str | None) -> str:
        return "fake"

    @property
    def name(self) -> AgentRuntime:  # type: ignore[override]
        return AgentRuntime.claude_code

    def _cli_args(self, *, model: str | None) -> list[str]:
        return []

    async def run(  # type: ignore[override]
        self,
        *,
        compose_project: str,
        compose_file: Path,
        prompt: str,
        model: str | None = None,
        workspace_id: str | None = None,
    ) -> AgentRunResult:
        return AgentRunResult(returncode=0, stdout="", stderr="")


def _validation(cmd: FakeCommandRunner, tmp_path: Path) -> ValidationRunner:
    return ValidationRunner(runner=cmd, artifacts_dir=tmp_path / "artifacts")


@pytest.mark.unit
def test_release_monitor_has_auto_merge_disabled(tmp_path: Path) -> None:
    cmd = FakeCommandRunner()
    runner = build_release_pr_monitor(
        session_factory=None,  # type: ignore[arg-type] - not actually exercised
        runner=cmd,
        adapter=_StubAdapter(),
        gh=GitHubClient(cmd),
        validation=_validation(cmd, tmp_path),
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
        validation=_validation(cmd, tmp_path),
        worktrees_root=tmp_path,
    )
    assert runner._config.auto_merge is True


@pytest.mark.unit
def test_factories_allow_omitted_validation(tmp_path: Path) -> None:
    release_cmd = FakeCommandRunner()
    release_runner = build_release_pr_monitor(
        session_factory=None,  # type: ignore[arg-type]
        runner=release_cmd,
        adapter=_StubAdapter(),
        gh=GitHubClient(release_cmd),
        worktrees_root=tmp_path,
    )

    feature_cmd = FakeCommandRunner()
    feature_runner = build_feature_pr_monitor(
        session_factory=None,  # type: ignore[arg-type]
        runner=feature_cmd,
        adapter=_StubAdapter(),
        gh=GitHubClient(feature_cmd),
        worktrees_root=tmp_path,
    )

    assert release_runner._deps.validation is None
    assert feature_runner._deps.validation is None


@pytest.mark.unit
def test_feature_monitor_accepts_post_merge_target_reconciler(tmp_path: Path) -> None:
    async def _reconcile(*, repo_url: str, branch: str, workspace_id: str) -> object:
        return {"repo_url": repo_url, "branch": branch, "workspace_id": workspace_id}

    cmd = FakeCommandRunner()
    runner = build_feature_pr_monitor(
        session_factory=None,  # type: ignore[arg-type]
        runner=cmd,
        adapter=_StubAdapter(),
        gh=GitHubClient(cmd),
        validation=_validation(cmd, tmp_path),
        worktrees_root=tmp_path,
        post_merge_target_reconciler=_reconcile,
    )

    assert runner._deps.post_merge_target_reconciler is _reconcile


@pytest.mark.unit
def test_factories_plumb_configured_knobs(tmp_path: Path) -> None:
    cmd = FakeCommandRunner()
    runner = build_release_pr_monitor(
        session_factory=None,  # type: ignore[arg-type]
        runner=cmd,
        adapter=_StubAdapter(),
        gh=GitHubClient(cmd),
        validation=_validation(cmd, tmp_path),
        worktrees_root=tmp_path,
        poll_interval_seconds=15,
        settle_interval_seconds=7,
        initial_review_grace_period_seconds=123,
        pre_merge_settle_seconds=11,
        non_check_reviewer_settle_seconds=45,
        non_check_reviewer_logins=["custom-reviewer"],
        require_ci=False,
    )
    assert runner._config.poll_interval_seconds == 15
    assert runner._config.settle_interval_seconds == 7
    assert runner._config.initial_review_grace_period_seconds == 123
    assert runner._config.pre_merge_settle_seconds == 11
    assert runner._config.non_check_reviewer_settle_seconds == 45
    assert runner._config.non_check_reviewer_logins == ("custom-reviewer",)
    assert runner._config.require_ci is False


@pytest.mark.unit
@pytest.mark.parametrize("builder", [build_release_pr_monitor, build_feature_pr_monitor])
def test_factories_require_ci_defaults_true(builder, tmp_path: Path) -> None:
    cmd = FakeCommandRunner()
    runner = builder(
        session_factory=None,  # type: ignore[arg-type]
        runner=cmd,
        adapter=_StubAdapter(),
        gh=GitHubClient(cmd),
        validation=_validation(cmd, tmp_path),
        worktrees_root=tmp_path,
    )
    assert runner._config.require_ci is True


@pytest.mark.unit
@pytest.mark.parametrize("builder", [build_release_pr_monitor, build_feature_pr_monitor])
def test_factories_thread_require_ci_false(builder, tmp_path: Path) -> None:
    cmd = FakeCommandRunner()
    runner = builder(
        session_factory=None,  # type: ignore[arg-type]
        runner=cmd,
        adapter=_StubAdapter(),
        gh=GitHubClient(cmd),
        validation=_validation(cmd, tmp_path),
        worktrees_root=tmp_path,
        require_ci=False,
    )
    assert runner._config.require_ci is False
