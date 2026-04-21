"""PR creator tests with FakeCommandRunner (no real git or gh)."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.common.commands import FakeCommandRunner
from awf.runtime.pr_creator import PullRequestCreator, PullRequestError

_WORKTREE = Path("/fake/worktree")


class TestPushAndOpen:
    @pytest.mark.unit
    async def test_pushes_branch_then_creates_pr(self) -> None:
        runner = FakeCommandRunner()
        runner.queue_result(returncode=0)  # git push
        runner.queue_result(
            returncode=0,
            stdout="https://github.com/dimileeh/aira-agent/pull/42\n",
        )  # gh pr create

        creator = PullRequestCreator(runner)
        result = await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="awf/ws_xyz",
            base_branch="development",
            title="Add docstring",
            body="One-line docstring on the module.",
        )

        assert result.url == "https://github.com/dimileeh/aira-agent/pull/42"
        assert result.branch == "awf/ws_xyz"
        assert len(runner.calls) == 2
        assert runner.calls[0].args[:2] == ["git", "-C"]
        assert "push" in runner.calls[0].args
        assert "-u" in runner.calls[0].args
        assert "origin" in runner.calls[0].args
        assert "awf/ws_xyz" in runner.calls[0].args

        gh_args = runner.calls[1].args
        assert gh_args[:3] == ["gh", "pr", "create"]
        assert "--base" in gh_args and "development" in gh_args
        assert "--head" in gh_args and "awf/ws_xyz" in gh_args
        assert "--title" in gh_args and "Add docstring" in gh_args
        assert "--body" in gh_args

    @pytest.mark.unit
    async def test_extracts_pr_url_even_with_leading_noise(self) -> None:
        runner = FakeCommandRunner()
        runner.queue_result(returncode=0)
        runner.queue_result(
            returncode=0,
            stdout=(
                "Creating pull request for awf/ws_abc into development in "
                "dimileeh/aira-agent\n\n"
                "https://github.com/dimileeh/aira-agent/pull/99\n"
            ),
        )

        creator = PullRequestCreator(runner)
        result = await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="awf/ws_abc",
            base_branch="development",
            title="t",
            body="b",
        )
        assert result.url == "https://github.com/dimileeh/aira-agent/pull/99"

    @pytest.mark.unit
    async def test_push_failure_raises_before_calling_gh(self) -> None:
        runner = FakeCommandRunner()
        runner.queue_result(returncode=128, stderr="remote: permission denied")

        creator = PullRequestCreator(runner)
        with pytest.raises(PullRequestError) as exc:
            await creator.push_and_open(
                worktree_path=_WORKTREE,
                branch_name="awf/ws_denied",
                base_branch="development",
                title="t",
                body="b",
            )
        assert exc.value.operation == "git push"
        assert exc.value.returncode == 128
        # gh was never called.
        assert len(runner.calls) == 1

    @pytest.mark.unit
    async def test_gh_failure_raises_with_stderr(self) -> None:
        runner = FakeCommandRunner()
        runner.queue_result(returncode=0)  # push succeeds
        runner.queue_result(returncode=1, stderr="gh: auth token expired")

        creator = PullRequestCreator(runner)
        with pytest.raises(PullRequestError) as exc:
            await creator.push_and_open(
                worktree_path=_WORKTREE,
                branch_name="awf/ws_x",
                base_branch="development",
                title="t",
                body="b",
            )
        assert exc.value.operation == "gh pr create"
        assert "gh: auth token expired" in exc.value.stderr

    @pytest.mark.unit
    async def test_missing_url_in_stdout_raises(self) -> None:
        runner = FakeCommandRunner()
        runner.queue_result(returncode=0)
        runner.queue_result(returncode=0, stdout="no url here at all\n")

        creator = PullRequestCreator(runner)
        with pytest.raises(PullRequestError) as exc:
            await creator.push_and_open(
                worktree_path=_WORKTREE,
                branch_name="awf/ws_x",
                base_branch="development",
                title="t",
                body="b",
            )
        assert "no URL" in exc.value.operation
