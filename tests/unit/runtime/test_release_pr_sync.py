"""Tests for release-PR sync core."""

from __future__ import annotations

import json

import pytest

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.runtime.release_pr_sync import (
    ReleasePrSyncError,
    ensure_release_pr_open,
)


_REPO = RepoRef(owner="dimileeh", name="aira-agent")


class TestNoDivergence:
    @pytest.mark.unit
    async def test_zero_ahead_returns_noop(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="0")  # gh api compare → ahead_by=0
        result = await ensure_release_pr_open(runner=fake, repo=_REPO)
        assert result.pr_number is None
        assert result.ahead_by == 0
        assert result.created is False
        assert "no release needed" in result.reason.lower()
        # Only ONE gh call — no PR list, no PR create.
        assert len(fake.calls) == 1


class TestExistingOpenPr:
    @pytest.mark.unit
    async def test_open_pr_reused_not_duplicated(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="7")  # ahead_by=7
        fake.queue_result(  # gh pr list
            returncode=0,
            stdout=json.dumps(
                [{"number": 99, "url": "https://github.com/dimileeh/aira-agent/pull/99"}]
            ),
        )
        result = await ensure_release_pr_open(runner=fake, repo=_REPO)
        assert result.pr_number == 99
        assert result.pr_url == "https://github.com/dimileeh/aira-agent/pull/99"
        assert result.ahead_by == 7
        assert result.created is False
        # No gh pr create call.
        assert not any(c.args[:3] == ["gh", "pr", "create"] for c in fake.calls)


class TestCreateNewPr:
    @pytest.mark.unit
    async def test_opens_pr_when_none_exists(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="3")  # ahead_by
        fake.queue_result(returncode=0, stdout="[]")  # gh pr list — none open
        # Commit list for body.
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {"sha": "abc1234", "message": "feat: thing A"},
                    {"sha": "def5678", "message": "fix: thing B"},
                    {"sha": "789abcd", "message": "chore: bump dep"},
                ]
            ),
        )
        # gh pr create stdout = the new PR URL.
        fake.queue_result(
            returncode=0, stdout="https://github.com/dimileeh/aira-agent/pull/555\n"
        )
        result = await ensure_release_pr_open(runner=fake, repo=_REPO)
        assert result.pr_number == 555
        assert result.pr_url == "https://github.com/dimileeh/aira-agent/pull/555"
        assert result.ahead_by == 3
        assert result.created is True

        # Inspect the PR create invocation.
        create_call = next(c for c in fake.calls if c.args[:3] == ["gh", "pr", "create"])
        assert "--repo" in create_call.args
        assert "dimileeh/aira-agent" in create_call.args
        assert "--base" in create_call.args
        assert "main" in create_call.args
        assert "--head" in create_call.args
        assert "development" in create_call.args
        # Title mentions the commit count.
        title_idx = create_call.args.index("--title") + 1
        assert "3 commits" in create_call.args[title_idx]
        # Body lists commits.
        body_idx = create_call.args.index("--body") + 1
        body = create_call.args[body_idx]
        assert "feat: thing A" in body
        assert "NOT auto-merge" in body

    @pytest.mark.unit
    async def test_singular_commit_in_title(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="1")  # ahead_by
        fake.queue_result(returncode=0, stdout="[]")  # no open PR
        fake.queue_result(
            returncode=0, stdout=json.dumps([{"sha": "abc1234", "message": "chore: x"}])
        )
        fake.queue_result(
            returncode=0, stdout="https://github.com/dimileeh/aira-agent/pull/1000"
        )
        result = await ensure_release_pr_open(runner=fake, repo=_REPO)
        create_call = next(c for c in fake.calls if c.args[:3] == ["gh", "pr", "create"])
        title_idx = create_call.args.index("--title") + 1
        assert "1 commit" in create_call.args[title_idx]
        assert "1 commits" not in create_call.args[title_idx]  # no plural 's'
        assert result.pr_number == 1000

    @pytest.mark.unit
    async def test_body_caps_at_50_commits(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="70")  # ahead_by
        fake.queue_result(returncode=0, stdout="[]")  # no open PR
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [{"sha": f"sha{i:04d}", "message": f"commit {i}"} for i in range(70)]
            ),
        )
        fake.queue_result(
            returncode=0, stdout="https://github.com/dimileeh/aira-agent/pull/999\n"
        )
        await ensure_release_pr_open(runner=fake, repo=_REPO)
        create_call = next(c for c in fake.calls if c.args[:3] == ["gh", "pr", "create"])
        body_idx = create_call.args.index("--body") + 1
        body = create_call.args[body_idx]
        assert "…and 20 more." in body
        # First + 49th commits included; 50th and beyond truncated.
        assert "commit 0" in body and "commit 49" in body
        assert "commit 50" not in body or "commit 50 " not in body

    @pytest.mark.unit
    async def test_body_best_effort_on_commit_list_failure(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="3")  # ahead_by
        fake.queue_result(returncode=0, stdout="[]")  # no open PR
        fake.queue_result(returncode=1, stderr="api error")  # commit list fails
        fake.queue_result(
            returncode=0, stdout="https://github.com/dimileeh/aira-agent/pull/2"
        )
        result = await ensure_release_pr_open(runner=fake, repo=_REPO)
        create_call = next(c for c in fake.calls if c.args[:3] == ["gh", "pr", "create"])
        body_idx = create_call.args.index("--body") + 1
        body = create_call.args[body_idx]
        assert "(commit list unavailable)" in body
        # Still opened the PR.
        assert result.pr_number == 2
        assert result.created is True


class TestDryRun:
    @pytest.mark.unit
    async def test_dry_run_skips_create_even_when_ahead(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="4")  # ahead_by
        fake.queue_result(returncode=0, stdout="[]")  # no open PR
        result = await ensure_release_pr_open(
            runner=fake, repo=_REPO, dry_run=True
        )
        assert result.pr_number is None
        assert result.ahead_by == 4
        assert result.created is False
        assert "dry-run" in result.reason.lower()
        # No gh pr create call.
        assert not any(c.args[:3] == ["gh", "pr", "create"] for c in fake.calls)


class TestErrorPaths:
    @pytest.mark.unit
    async def test_compare_api_failure_raises(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="404 not found")
        with pytest.raises(ReleasePrSyncError) as exc:
            await ensure_release_pr_open(runner=fake, repo=_REPO)
        assert "compare" in exc.value.operation.lower()
        assert "not found" in exc.value.stderr

    @pytest.mark.unit
    async def test_unparseable_ahead_count_raises(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="not-a-number")
        with pytest.raises(ReleasePrSyncError) as exc:
            await ensure_release_pr_open(runner=fake, repo=_REPO)
        assert "parse" in exc.value.operation.lower()

    @pytest.mark.unit
    async def test_empty_ahead_treated_as_zero(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="")  # empty stdout
        result = await ensure_release_pr_open(runner=fake, repo=_REPO)
        assert result.ahead_by == 0
        assert result.pr_number is None

    @pytest.mark.unit
    async def test_pr_list_failure_raises(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="2")
        fake.queue_result(returncode=1, stderr="permission denied")
        with pytest.raises(ReleasePrSyncError) as exc:
            await ensure_release_pr_open(runner=fake, repo=_REPO)
        assert "pr list" in exc.value.operation.lower()

    @pytest.mark.unit
    async def test_pr_create_failure_raises(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="2")
        fake.queue_result(returncode=0, stdout="[]")
        fake.queue_result(returncode=0, stdout="[]")  # commit list (empty ok)
        fake.queue_result(returncode=1, stderr="branch protection blocks")
        with pytest.raises(ReleasePrSyncError) as exc:
            await ensure_release_pr_open(runner=fake, repo=_REPO)
        assert "pr create" in exc.value.operation.lower()
        assert "branch protection" in exc.value.stderr

    @pytest.mark.unit
    async def test_pr_create_returns_unexpected_output_raises(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="2")
        fake.queue_result(returncode=0, stdout="[]")
        fake.queue_result(returncode=0, stdout="[]")
        fake.queue_result(returncode=0, stdout="not-a-pr-url")
        with pytest.raises(ReleasePrSyncError):
            await ensure_release_pr_open(runner=fake, repo=_REPO)


class TestCustomBranches:
    @pytest.mark.unit
    async def test_custom_source_and_target(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="5")  # ahead_by
        fake.queue_result(returncode=0, stdout="[]")
        fake.queue_result(returncode=0, stdout="[]")  # commits
        fake.queue_result(
            returncode=0, stdout="https://github.com/dimileeh/aira-agent/pull/42"
        )
        await ensure_release_pr_open(
            runner=fake,
            repo=_REPO,
            source_branch="staging",
            target_branch="production",
        )
        # Every gh call should use the custom branch names.
        for c in fake.calls:
            if "compare/" in " ".join(c.args):
                assert "compare/production...staging" in c.args[2]
            if c.args[:3] == ["gh", "pr", "list"]:
                assert "production" in c.args
                assert "staging" in c.args
            if c.args[:3] == ["gh", "pr", "create"]:
                assert "production" in c.args
                assert "staging" in c.args
