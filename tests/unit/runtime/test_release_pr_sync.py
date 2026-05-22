"""Unit tests for ``awf.runtime.release_pr_sync``.

``FakeCommandRunner`` returns canned ``git`` / ``gh`` output; the helpers parse
it. Assertions cover ahead-count parsing, find-or-create branching, the no-op
result, and error propagation.
"""

from __future__ import annotations

import json

import pytest

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClient, RepoRef
from awf.runtime.release_pr_sync import (
    NO_CHANGES_REASON_CODE,
    ReleasePrSyncError,
    ReleasePrSyncNoOp,
    ReleasePrSyncResult,
    count_commits_ahead,
    find_or_create_release_pr,
    prepare_release_pr_sync,
    release_pr_body,
    release_pr_title,
)

_REPO = RepoRef(owner="o", name="r")


def _adoption_payload(*, number: int = 321) -> str:
    return json.dumps(
        {
            "number": number,
            "headRefName": "development",
            "headRepository": {"name": "r", "nameWithOwner": "o/r"},
            "isCrossRepository": False,
            "baseRefName": "main",
            "headRefOid": "h" * 40,
            "baseRefOid": "b" * 40,
            "state": "OPEN",
            "isDraft": False,
            "author": {"login": "octocat"},
            "url": f"https://github.com/o/r/pull/{number}",
            "title": "Release",
        }
    )


def _open_pr_list_payload(*, number: int = 321) -> str:
    return json.dumps(
        [
            {
                "number": number,
                "url": f"https://github.com/o/r/pull/{number}",
                "headRefName": "development",
                "headRefOid": "h" * 40,
                "headRepository": {"name": "r", "nameWithOwner": "o/r"},
                "headRepositoryOwner": {"login": "o"},
            }
        ]
    )


class TestCountCommitsAhead:
    @pytest.mark.unit
    async def test_parses_positive_count(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0)  # git fetch
        fake.queue_result(returncode=0, stdout="3\n")  # git rev-list --count

        count = await count_commits_ahead(
            runner=fake,
            cwd="/work",
            source_branch="development",
            target_branch="main",
        )

        assert count == 3
        fetch_args = fake.calls[0].args
        assert fetch_args[:3] == ["git", "fetch", "origin"]
        assert "main" in fetch_args and "development" in fetch_args
        assert fake.calls[0].cwd == "/work"
        rev_args = fake.calls[1].args
        assert rev_args[:3] == ["git", "rev-list", "--count"]
        assert rev_args[-1] == "origin/main..origin/development"

    @pytest.mark.unit
    async def test_parses_zero_count(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="0\n")

        count = await count_commits_ahead(
            runner=fake, cwd="/work", source_branch="development", target_branch="main"
        )

        assert count == 0

    @pytest.mark.unit
    async def test_fetch_failure_raises(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="network down")

        with pytest.raises(ReleasePrSyncError) as exc:
            await count_commits_ahead(
                runner=fake, cwd="/work", source_branch="development", target_branch="main"
            )

        assert exc.value.reason_code == "RELEASE_SYNC_FETCH_FAILED"
        assert "network down" in exc.value.message

    @pytest.mark.unit
    async def test_rev_list_failure_raises(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=1, stderr="bad revision")

        with pytest.raises(ReleasePrSyncError) as exc:
            await count_commits_ahead(
                runner=fake, cwd="/work", source_branch="development", target_branch="main"
            )

        assert exc.value.reason_code == "RELEASE_SYNC_REV_LIST_FAILED"

    @pytest.mark.unit
    async def test_rev_list_non_numeric_raises(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="not-a-number\n")

        with pytest.raises(ReleasePrSyncError) as exc:
            await count_commits_ahead(
                runner=fake, cwd="/work", source_branch="development", target_branch="main"
            )

        assert exc.value.reason_code == "RELEASE_SYNC_REV_LIST_INVALID"


class TestFindOrCreateReleasePr:
    @pytest.mark.unit
    async def test_reuses_existing_open_pr_without_create(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=_open_pr_list_payload(number=99))  # gh pr list
        fake.queue_result(returncode=0, stdout=_adoption_payload(number=99))  # gh pr view
        gh = GitHubClient(fake)

        metadata, created = await find_or_create_release_pr(
            runner=fake,
            gh=gh,
            repo=_REPO,
            source_branch="development",
            target_branch="main",
            title="t",
            body="b",
        )

        assert created is False
        assert metadata.number == 99
        # Only list + view ran; no `gh pr create`.
        assert [c.args[:3] for c in fake.calls] == [
            ["gh", "pr", "list"],
            ["gh", "pr", "view"],
        ]

    @pytest.mark.unit
    async def test_creates_pr_when_none_exists(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")  # gh pr list -> none
        fake.queue_result(returncode=0, stdout="https://github.com/o/r/pull/321\n")  # gh pr create
        fake.queue_result(returncode=0, stdout=_adoption_payload(number=321))  # gh pr view
        gh = GitHubClient(fake)

        metadata, created = await find_or_create_release_pr(
            runner=fake,
            gh=gh,
            repo=_REPO,
            source_branch="development",
            target_branch="main",
            title="Release: merge development into main",
            body="b",
        )

        assert created is True
        assert metadata.number == 321
        create_args = fake.calls[1].args
        assert create_args[:3] == ["gh", "pr", "create"]
        assert create_args[create_args.index("--base") + 1] == "main"
        assert create_args[create_args.index("--head") + 1] == "development"

    @pytest.mark.unit
    async def test_unparseable_create_url_raises(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")
        fake.queue_result(returncode=0, stdout="not-a-url\n")
        gh = GitHubClient(fake)

        with pytest.raises(ReleasePrSyncError) as exc:
            await find_or_create_release_pr(
                runner=fake,
                gh=gh,
                repo=_REPO,
                source_branch="development",
                target_branch="main",
                title="t",
                body="b",
            )

        assert exc.value.reason_code == "RELEASE_SYNC_PR_URL_INVALID"


class TestPrepareReleasePrSync:
    @pytest.mark.unit
    async def test_no_commits_returns_noop(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0)  # fetch
        fake.queue_result(returncode=0, stdout="0\n")  # rev-list
        gh = GitHubClient(fake)

        outcome = await prepare_release_pr_sync(
            runner=fake,
            gh=gh,
            repo=_REPO,
            cwd="/work",
            source_branch="development",
            target_branch="main",
            title="t",
            body="b",
        )

        assert isinstance(outcome, ReleasePrSyncNoOp)
        assert outcome.reason_code == NO_CHANGES_REASON_CODE
        assert outcome.source_branch == "development"
        assert outcome.target_branch == "main"
        # No PR list/create/view calls beyond the two git calls.
        assert len(fake.calls) == 2

    @pytest.mark.unit
    async def test_ahead_creates_pr_and_returns_result(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0)  # fetch
        fake.queue_result(returncode=0, stdout="4\n")  # rev-list
        fake.queue_result(returncode=0, stdout="[]")  # gh pr list
        fake.queue_result(returncode=0, stdout="https://github.com/o/r/pull/321\n")  # create
        fake.queue_result(returncode=0, stdout=_adoption_payload(number=321))  # view
        gh = GitHubClient(fake)

        outcome = await prepare_release_pr_sync(
            runner=fake,
            gh=gh,
            repo=_REPO,
            cwd="/work",
            source_branch="development",
            target_branch="main",
            title="t",
            body="b",
        )

        assert isinstance(outcome, ReleasePrSyncResult)
        assert outcome.created is True
        assert outcome.commits_ahead == 4
        assert outcome.metadata.number == 321
        assert outcome.metadata.base_ref == "main"

    @pytest.mark.unit
    async def test_ahead_reuses_existing_pr(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0)  # fetch
        fake.queue_result(returncode=0, stdout="2\n")  # rev-list
        fake.queue_result(returncode=0, stdout=_open_pr_list_payload(number=88))  # gh pr list
        fake.queue_result(returncode=0, stdout=_adoption_payload(number=88))  # view
        gh = GitHubClient(fake)

        outcome = await prepare_release_pr_sync(
            runner=fake,
            gh=gh,
            repo=_REPO,
            cwd="/work",
            source_branch="development",
            target_branch="main",
            title="t",
            body="b",
        )

        assert isinstance(outcome, ReleasePrSyncResult)
        assert outcome.created is False
        assert outcome.metadata.number == 88
        assert all(c.args[:3] != ["gh", "pr", "create"] for c in fake.calls)


class TestReleasePrText:
    @pytest.mark.unit
    def test_title_and_body_mention_branches(self) -> None:
        title = release_pr_title(source_branch="development", target_branch="main")
        body = release_pr_body(source_branch="development", target_branch="main")
        assert "development" in title and "main" in title
        assert "development" in body and "main" in body
        assert "auto-merge disabled" in body
