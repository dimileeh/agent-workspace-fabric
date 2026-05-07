"""PR creator tests with FakeCommandRunner (no real git or gh)."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.common.commands import FakeCommandRunner
from awf.runtime.pr_creator import PullRequestCreator, PullRequestError

_WORKTREE = Path("/fake/worktree")


def _queue_pre_push_diagnostics(runner: FakeCommandRunner) -> None:
    """Queue the 3 canned results the new pre-push diagnostic block
    consumes (``rev-parse HEAD``, ``rev-parse --abbrev-ref HEAD``,
    ``log origin/<base>..HEAD``). Values are deliberately realistic
    so the log line looks sane if a test inspects it."""
    runner.queue_result(returncode=0, stdout="abc123def4567890\n")  # rev-parse HEAD
    runner.queue_result(returncode=0, stdout="awf/ws_xyz\n")  # current branch
    runner.queue_result(returncode=0, stdout="abc123 some work\n")  # ahead-of-base


class TestPushAndOpen:
    @pytest.mark.unit
    async def test_pushes_branch_then_creates_pr(self) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
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
        # 3 diagnostic queries + 1 push + 1 gh create = 5 total calls.
        assert len(runner.calls) == 5
        # The push is at index 3 (after the 3 diagnostics).
        push_call = runner.calls[3]
        assert push_call.args[0] == "git"
        assert f"safe.directory={_WORKTREE}" in push_call.args
        assert "-C" in push_call.args
        assert "push" in push_call.args
        assert "-u" in push_call.args
        assert "origin" in push_call.args
        assert "awf/ws_xyz" in push_call.args

        gh_args = runner.calls[4].args
        assert gh_args[:3] == ["gh", "pr", "create"]
        assert "--base" in gh_args and "development" in gh_args
        assert "--head" in gh_args and "awf/ws_xyz" in gh_args
        assert "--title" in gh_args and "Add docstring" in gh_args
        assert "--body" in gh_args

    @pytest.mark.unit
    async def test_reuses_existing_pr_after_push_without_creating_duplicate(self) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        creator = PullRequestCreator(runner)
        result = await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="awf/ws_xyz",
            base_branch="development",
            title="Add docstring",
            body="One-line docstring on the module.",
            existing_pr_url="https://github.com/dimileeh/aira-agent/pull/42",
        )

        assert result.url == "https://github.com/dimileeh/aira-agent/pull/42"
        assert result.branch == "awf/ws_xyz"
        assert result.head_sha == "abc123def4567890"
        assert len(runner.calls) == 4
        assert all(call.args[:3] != ["gh", "pr", "create"] for call in runner.calls)

    @pytest.mark.unit
    async def test_updates_existing_pr_with_explicit_remote_head_ref(self) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        creator = PullRequestCreator(runner)
        result = await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="feature-sync/ws_local",
            base_branch="development",
            title="Update adopted PR",
            body="Existing PR update.",
            existing_pr_url="https://github.com/dimileeh/aira-agent/pull/42",
            remote_branch_name="awf/ws_original",
        )

        push_args = runner.calls[3].args
        assert result.url == "https://github.com/dimileeh/aira-agent/pull/42"
        assert result.branch == "awf/ws_original"
        assert push_args[:1] == ["git"]
        assert "push" in push_args
        assert "origin" in push_args
        assert "HEAD:refs/heads/awf/ws_original" in push_args
        assert "feature-sync/ws_local" not in push_args
        assert all(call.args[:3] != ["gh", "pr", "create"] for call in runner.calls)

    @pytest.mark.unit
    async def test_updates_existing_pr_with_qualified_remote_head_ref(self) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        creator = PullRequestCreator(runner)
        result = await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="feature-sync/ws_local",
            base_branch="development",
            title="Update adopted PR",
            body="Existing PR update.",
            existing_pr_url="https://github.com/dimileeh/aira-agent/pull/42",
            remote_branch_name="refs/heads/awf/ws_original",
        )

        push_args = runner.calls[3].args
        assert result.branch == "awf/ws_original"
        assert "HEAD:refs/heads/awf/ws_original" in push_args
        assert "HEAD:refs/heads/refs/heads/awf/ws_original" not in push_args

    @pytest.mark.unit
    async def test_updates_existing_pr_on_explicit_remote_url(self) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        creator = PullRequestCreator(runner)
        result = await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="feature-sync/ws_local",
            base_branch="development",
            title="Update adopted fork PR",
            body="Existing fork PR update.",
            existing_pr_url="https://github.com/base/aira-agent/pull/42",
            remote_branch_name="fix/fork-review",
            remote_url="git@github.com:contributor/aira-agent.git",
        )

        push_args = runner.calls[3].args
        push_index = push_args.index("push")
        assert result.url == "https://github.com/base/aira-agent/pull/42"
        assert result.branch == "fix/fork-review"
        assert push_args[push_index + 1] == "git@github.com:contributor/aira-agent.git"
        assert "HEAD:refs/heads/fix/fork-review" in push_args
        assert "origin" not in push_args[push_index + 1 :]
        assert all(call.args[:3] != ["gh", "pr", "create"] for call in runner.calls)

    @pytest.mark.unit
    async def test_explicit_remote_url_push_does_not_set_upstream(self) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        creator = PullRequestCreator(runner)
        await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="feature-sync/ws_local",
            base_branch="development",
            title="Update adopted fork PR",
            body="Existing fork PR update.",
            existing_pr_url="https://github.com/base/aira-agent/pull/42",
            remote_branch_name="fix/fork-review",
            remote_url="https://user:token@github.com/contributor/aira-agent.git",
        )

        push_args = runner.calls[3].args
        assert "-u" not in push_args
        assert "--set-upstream" not in push_args
        assert "https://user:token@github.com/contributor/aira-agent.git" in push_args
        assert "HEAD:refs/heads/fix/fork-review" in push_args

    @pytest.mark.unit
    async def test_extracts_pr_url_even_with_leading_noise(self) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
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
        _queue_pre_push_diagnostics(runner)
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
        # 3 diagnostic queries + 1 push = 4 calls; gh was never reached.
        assert len(runner.calls) == 4

    @pytest.mark.unit
    async def test_gh_failure_raises_with_stderr(self) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
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
    async def test_pre_push_diagnostics_run_before_push(self) -> None:
        """The three git diagnostic queries must fire BEFORE the push
        subprocess call — otherwise a push failure would short-circuit
        the block we actually need for debugging."""
        runner = FakeCommandRunner()
        runner.queue_result(returncode=0, stdout="deadbeef1234\n")  # rev-parse HEAD
        runner.queue_result(returncode=0, stdout="awf/ws_abc\n")  # abbrev-ref HEAD
        runner.queue_result(
            returncode=0, stdout="ab12345 feat: thing\ncd67890 chore: thing\n"
        )  # ahead-of-base
        runner.queue_result(returncode=0)  # git push
        runner.queue_result(
            returncode=0,
            stdout="https://github.com/x/y/pull/1\n",
        )  # gh pr create

        creator = PullRequestCreator(runner)
        await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="awf/ws_abc",
            base_branch="development",
            title="t",
            body="b",
        )

        # The first three calls are the diagnostics, in order.
        call0, call1, call2, call3, _ = runner.calls
        assert "rev-parse" in call0.args and "HEAD" in call0.args
        assert f"safe.directory={_WORKTREE}" in call0.args
        assert "--abbrev-ref" in call1.args
        assert "log" in call2.args and "origin/development..HEAD" in call2.args
        # The FOURTH call is the push.
        assert "push" in call3.args

    @pytest.mark.unit
    async def test_diagnostic_failure_does_not_block_push(self) -> None:
        """If a diagnostic query itself errors (weird worktree state,
        permissions), the normal push path still runs. Diagnostics are
        observability, not control flow."""
        runner = FakeCommandRunner()
        runner.queue_result(returncode=128, stderr="fatal: bad object")  # rev-parse fails
        runner.queue_result(returncode=128, stderr="fatal: bad object")  # abbrev-ref fails
        runner.queue_result(returncode=128, stderr="fatal: bad object")  # ahead-of-base fails
        runner.queue_result(returncode=0)  # git push still runs
        runner.queue_result(
            returncode=0,
            stdout="https://github.com/x/y/pull/1\n",
        )

        creator = PullRequestCreator(runner)
        result = await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="awf/ws_weird",
            base_branch="development",
            title="t",
            body="b",
        )
        assert result.url == "https://github.com/x/y/pull/1"

    @pytest.mark.unit
    async def test_missing_url_in_stdout_raises(self) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
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
