"""Unit tests for ``awf.runtime.release_pr_sync``.

``FakeCommandRunner`` returns canned ``git`` / ``gh`` output; the helpers parse
it. Assertions cover ahead-count parsing, find-or-create branching, the no-op
result, and error propagation.
"""

from __future__ import annotations

import json

import pytest

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClient, GitHubClientError, RepoRef
from awf.runtime.release_pr_sync import (
    _RELEASE_PR_CREATE_TRANSIENT_MAX_RETRIES,
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


def _fork_open_pr_list_payload(*, number: int = 555) -> str:
    return json.dumps(
        [
            {
                "number": number,
                "url": f"https://github.com/o/r/pull/{number}",
                "headRefName": "development",
                "headRefOid": "f" * 40,
                "headRepository": {"name": "r", "nameWithOwner": "fork/r"},
                "headRepositoryOwner": {"login": "fork"},
            }
        ]
    )


def _fork_and_same_repo_pr_list_payload(
    *, fork_number: int = 555, same_repo_number: int = 99
) -> str:
    # The fork PR is listed first so the test proves selection filters by repo
    # identity rather than list order.
    return json.dumps(
        [
            {
                "number": fork_number,
                "url": f"https://github.com/o/r/pull/{fork_number}",
                "headRefName": "development",
                "headRefOid": "f" * 40,
                "headRepository": {"name": "r", "nameWithOwner": "fork/r"},
                "headRepositoryOwner": {"login": "fork"},
            },
            {
                "number": same_repo_number,
                "url": f"https://github.com/o/r/pull/{same_repo_number}",
                "headRefName": "development",
                "headRefOid": "h" * 40,
                "headRepository": {"name": "r", "nameWithOwner": "o/r"},
                "headRepositoryOwner": {"login": "o"},
            },
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
        # ``gh`` exits 0 but prints no PR URL: GitHubClient.create_pull_request
        # raises GitHubClientError(returncode=0). find_or_create_release_pr must
        # translate that into the release-sync reason code rather than leaking the
        # raw gh error, keeping the RELEASE_SYNC_PR_URL_INVALID contract.
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

    @pytest.mark.unit
    async def test_create_url_with_invalid_pr_number_raises(self) -> None:
        # A github-shaped URL clears GitHubClient's URL guard but still fails
        # parse_github_pull_request_url (e.g. PR number 0). That ValueError must
        # surface as RELEASE_SYNC_PR_URL_INVALID from the post-create parse path.
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")
        fake.queue_result(returncode=0, stdout="https://github.com/o/r/pull/0\n")
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

    @pytest.mark.unit
    async def test_non_github_forge_fails_closed_before_any_gh_call(self) -> None:
        # Bitbucket Cloud is a supported forge (issue #345 Part 2), so it clears
        # the executor forge gate and reaches here — but the open-PR lookup
        # (``gh pr list``), adoption metadata (``gh pr view``), and github.com-only
        # URL parse are all GitHub-only. Fail closed with an honest reason code
        # before any ``gh`` call instead of mis-routing to github.com for the same
        # slug or rejecting the bitbucket.org create URL.
        fake = FakeCommandRunner()
        bitbucket_repo = RepoRef(owner="o", name="r", forge="bitbucket")
        gh = GitHubClient(fake)

        with pytest.raises(ReleasePrSyncError) as exc:
            await find_or_create_release_pr(
                runner=fake,
                gh=gh,
                repo=bitbucket_repo,
                source_branch="development",
                target_branch="main",
                title="t",
                body="b",
            )

        assert exc.value.reason_code == "RELEASE_SYNC_FORGE_NOT_SUPPORTED"
        assert exc.value.detail == {
            "repo_slug": "o/r",
            "forge": "bitbucket",
            "source_branch": "development",
            "target_branch": "main",
        }
        # No git/gh subprocess was invoked — the guard fires first.
        assert fake.calls == []

    @pytest.mark.unit
    async def test_ignores_fork_pr_and_creates_in_repo(self) -> None:
        # gh pr list can return a fork PR sharing the head branch name; it must
        # not be adopted — a same-repo PR is created instead.
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=_fork_open_pr_list_payload(number=555))  # list
        fake.queue_result(returncode=0, stdout="https://github.com/o/r/pull/321\n")  # create
        fake.queue_result(returncode=0, stdout=_adoption_payload(number=321))  # view
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

        assert created is True
        assert metadata.number == 321
        assert [c.args[:3] for c in fake.calls] == [
            ["gh", "pr", "list"],
            ["gh", "pr", "create"],
            ["gh", "pr", "view"],
        ]

    @pytest.mark.unit
    async def test_prefers_same_repo_pr_over_fork_collision(self) -> None:
        # When `gh pr list` returns both a fork PR and a same-repo PR sharing
        # the head branch, the same-repo PR must be adopted (no create).
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_fork_and_same_repo_pr_list_payload(fork_number=555, same_repo_number=99),
        )  # gh pr list
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
        # Only list + view of the same-repo PR ran; the fork PR is never viewed
        # and no `gh pr create` is issued.
        assert [c.args[:3] for c in fake.calls] == [
            ["gh", "pr", "list"],
            ["gh", "pr", "view"],
        ]
        assert fake.calls[1].args[:4] == ["gh", "pr", "view", "99"]

    @pytest.mark.unit
    async def test_created_url_for_other_repo_raises(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")  # gh pr list -> none
        fake.queue_result(returncode=0, stdout="https://github.com/other/repo/pull/7\n")  # create
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

        assert exc.value.reason_code == "RELEASE_SYNC_PR_REPO_MISMATCH"
        assert exc.value.detail is not None
        assert exc.value.detail["expected_repo"] == "o/r"
        assert exc.value.detail["parsed_repo"] == "other/repo"
        # No gh pr view should run once the repo mismatch is detected.
        assert all(c.args[:3] != ["gh", "pr", "view"] for c in fake.calls)

    @pytest.mark.unit
    async def test_adopts_pr_when_create_loses_race(self) -> None:
        # The initial list finds no PR, but a concurrent sync run or a human
        # opens one before `gh pr create` runs, so create fails on the
        # duplicate. The re-check must adopt the now-existing same-repo PR
        # instead of failing the workspace.
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")  # gh pr list -> none yet
        fake.queue_result(
            returncode=1,
            stderr='a pull request for branch "development" into branch "main" already exists',
        )  # gh pr create -> duplicate rejected
        fake.queue_result(
            returncode=0, stdout=_open_pr_list_payload(number=77)
        )  # gh pr list (re-check)
        fake.queue_result(returncode=0, stdout=_adoption_payload(number=77))  # gh pr view
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
        assert metadata.number == 77
        assert [c.args[:3] for c in fake.calls] == [
            ["gh", "pr", "list"],
            ["gh", "pr", "create"],
            ["gh", "pr", "list"],
            ["gh", "pr", "view"],
        ]

    @pytest.mark.unit
    async def test_transient_create_failure_retries_then_succeeds(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")  # gh pr list -> none
        fake.queue_result(returncode=1, stderr="HTTP 500 from GitHub")  # gh pr create -> fails
        fake.queue_result(returncode=0, stdout="[]")  # gh pr list recheck -> none
        fake.queue_result(returncode=0, stdout="https://github.com/o/r/pull/321\n")
        fake.queue_result(returncode=0, stdout=_adoption_payload(number=321))  # gh pr view
        gh = GitHubClient(fake)
        slept: list[float] = []

        async def _record_sleep(seconds: float) -> None:
            slept.append(seconds)

        metadata, created = await find_or_create_release_pr(
            runner=fake,
            gh=gh,
            repo=_REPO,
            source_branch="development",
            target_branch="main",
            title="t",
            body="b",
            sleep=_record_sleep,
        )

        assert created is True
        assert metadata.number == 321
        assert slept == [5.0]
        assert [c.args[:3] for c in fake.calls] == [
            ["gh", "pr", "list"],
            ["gh", "pr", "create"],
            ["gh", "pr", "list"],
            ["gh", "pr", "create"],
            ["gh", "pr", "view"],
        ]

    @pytest.mark.unit
    async def test_malformed_graphql_resubmit_create_failure_retries_then_succeeds(
        self,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")  # gh pr list -> none
        fake.queue_result(
            returncode=1,
            stderr=(
                "pull request create failed: HTTP 400: We received a malformed request "
                "from your client. Sorry about that. Please try resubmitting your "
                "request and contact us if the problem persists. "
                "(https://api.github.com/graphql)"
            ),
        )
        fake.queue_result(returncode=0, stdout="[]")  # gh pr list recheck -> none
        fake.queue_result(returncode=0, stdout="https://github.com/o/r/pull/321\n")
        fake.queue_result(returncode=0, stdout=_adoption_payload(number=321))  # gh pr view
        gh = GitHubClient(fake)

        async def _no_sleep(_seconds: float) -> None:
            return None

        metadata, created = await find_or_create_release_pr(
            runner=fake,
            gh=gh,
            repo=_REPO,
            source_branch="development",
            target_branch="main",
            title="t",
            body="b",
            sleep=_no_sleep,
        )

        assert created is True
        assert metadata.number == 321
        assert len([c for c in fake.calls if c.args[:3] == ["gh", "pr", "create"]]) == 2

    @pytest.mark.unit
    async def test_transient_create_failure_reconciles_existing_pr(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")  # gh pr list -> none
        fake.queue_result(returncode=1, stderr="HTTP 503 from GitHub")  # gh pr create -> fails
        fake.queue_result(returncode=0, stdout=_open_pr_list_payload(number=77))
        fake.queue_result(returncode=0, stdout=_adoption_payload(number=77))  # gh pr view
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
        assert metadata.number == 77
        assert [c.args[:3] for c in fake.calls] == [
            ["gh", "pr", "list"],
            ["gh", "pr", "create"],
            ["gh", "pr", "list"],
            ["gh", "pr", "view"],
        ]

    @pytest.mark.unit
    async def test_transient_create_failure_recheck_list_error_retries_then_succeeds(
        self,
    ) -> None:
        # The post-create reconcile ``gh pr list`` itself fails. That call raises
        # ``PullRequestMetadataError`` (not ``GitHubClientError``); the lookup must
        # treat it as a failed reconcile so the bounded transient-retry loop still
        # runs instead of letting the metadata error escape.
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")  # gh pr list -> none
        fake.queue_result(returncode=1, stderr="HTTP 500 from GitHub")  # gh pr create -> fails
        fake.queue_result(
            returncode=1, stderr="HTTP 502 from GitHub"
        )  # gh pr list recheck -> errors
        fake.queue_result(returncode=0, stdout="https://github.com/o/r/pull/321\n")
        fake.queue_result(returncode=0, stdout=_adoption_payload(number=321))  # gh pr view
        gh = GitHubClient(fake)
        slept: list[float] = []

        async def _record_sleep(seconds: float) -> None:
            slept.append(seconds)

        metadata, created = await find_or_create_release_pr(
            runner=fake,
            gh=gh,
            repo=_REPO,
            source_branch="development",
            target_branch="main",
            title="t",
            body="b",
            sleep=_record_sleep,
        )

        assert created is True
        assert metadata.number == 321
        assert slept == [5.0]
        assert [c.args[:3] for c in fake.calls] == [
            ["gh", "pr", "list"],
            ["gh", "pr", "create"],
            ["gh", "pr", "list"],
            ["gh", "pr", "create"],
            ["gh", "pr", "view"],
        ]

    @pytest.mark.unit
    async def test_duplicate_create_failure_recheck_list_error_retries_lookup(self) -> None:
        # A duplicate (non-transient) create failure whose reconcile ``gh pr list``
        # errors must still drive the duplicate-lookup retry: the
        # ``PullRequestMetadataError`` is caught as a failed lookup rather than
        # escaping and bypassing the retry path.
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")  # gh pr list -> none
        fake.queue_result(
            returncode=1,
            stderr='a pull request for branch "development" into branch "main" already exists',
        )  # gh pr create -> duplicate rejected
        fake.queue_result(
            returncode=1, stderr="HTTP 500 from GitHub"
        )  # gh pr list recheck -> errors
        fake.queue_result(
            returncode=1,
            stderr='a pull request for branch "development" into branch "main" already exists',
        )  # gh pr create (retry) -> still duplicate
        fake.queue_result(returncode=0, stdout=_open_pr_list_payload(number=88))  # recheck -> found
        fake.queue_result(returncode=0, stdout=_adoption_payload(number=88))  # gh pr view
        gh = GitHubClient(fake)

        async def _no_sleep(_seconds: float) -> None:
            return None

        metadata, created = await find_or_create_release_pr(
            runner=fake,
            gh=gh,
            repo=_REPO,
            source_branch="development",
            target_branch="main",
            title="t",
            body="b",
            sleep=_no_sleep,
        )

        assert created is False
        assert metadata.number == 88
        # First recheck errors (caught, not escaped) -> the duplicate-lookup retry
        # re-runs create, whose recheck then adopts the raced PR.
        assert [c.args[:3] for c in fake.calls] == [
            ["gh", "pr", "list"],
            ["gh", "pr", "create"],
            ["gh", "pr", "list"],
            ["gh", "pr", "create"],
            ["gh", "pr", "list"],
            ["gh", "pr", "view"],
        ]

    @pytest.mark.unit
    async def test_transient_create_failure_retry_exhausted_reraises(self) -> None:
        # Four consecutive transient create failures whose reconcile lookups find
        # nothing must exhaust the bounded retry loop
        # (``attempt > _RELEASE_PR_CREATE_TRANSIENT_MAX_RETRIES``) and re-raise the
        # original gh error rather than looping forever. Mirrors the feature-PR
        # exhaustion guard in ``test_pr_creator.py``.
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")  # gh pr list -> none
        for _ in range(_RELEASE_PR_CREATE_TRANSIENT_MAX_RETRIES + 1):
            fake.queue_result(returncode=1, stderr="HTTP 500 from GitHub")  # gh pr create
            fake.queue_result(returncode=0, stdout="[]")  # reconcile gh pr list -> none
        gh = GitHubClient(fake)
        slept: list[float] = []

        async def _record_sleep(seconds: float) -> None:
            slept.append(seconds)

        with pytest.raises(GitHubClientError) as exc:
            await find_or_create_release_pr(
                runner=fake,
                gh=gh,
                repo=_REPO,
                source_branch="development",
                target_branch="main",
                title="t",
                body="b",
                sleep=_record_sleep,
            )

        assert exc.value.operation == "gh pr create"
        # One initial list + four (create, reconcile-list) attempts; no `gh pr view`.
        assert [c.args[:3] for c in fake.calls] == [
            ["gh", "pr", "list"],
            ["gh", "pr", "create"],
            ["gh", "pr", "list"],
            ["gh", "pr", "create"],
            ["gh", "pr", "list"],
            ["gh", "pr", "create"],
            ["gh", "pr", "list"],
            ["gh", "pr", "create"],
            ["gh", "pr", "list"],
        ]
        # Backoff before the 2nd/3rd/4th attempts only — the exhausting attempt
        # re-raises before sleeping.
        assert slept == [5.0, 10.0, 20.0]

    @pytest.mark.unit
    async def test_deterministic_create_failure_reraises_without_recheck(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")  # gh pr list -> none
        fake.queue_result(returncode=1, stderr="Bad credentials")  # gh pr create -> fails
        gh = GitHubClient(fake)

        with pytest.raises(GitHubClientError) as exc:
            await find_or_create_release_pr(
                runner=fake,
                gh=gh,
                repo=_REPO,
                source_branch="development",
                target_branch="main",
                title="t",
                body="b",
            )

        assert exc.value.operation == "gh pr create"
        # Only the initial list + the failed create ran: no retry, re-check, or view.
        assert [c.args[:3] for c in fake.calls] == [
            ["gh", "pr", "list"],
            ["gh", "pr", "create"],
        ]

    @pytest.mark.unit
    async def test_duplicate_signal_recheck_finds_nothing_reraises(self) -> None:
        # The create fails with a duplicate-PR signal, so the re-check runs, but
        # no same-repo PR materialises (e.g. the raced PR was closed). The
        # original gh error must surface rather than being swallowed.
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")  # gh pr list -> none
        fake.queue_result(
            returncode=1,
            stderr='a pull request for branch "development" into branch "main" already exists',
        )  # gh pr create -> duplicate rejected
        fake.queue_result(returncode=0, stdout="[]")  # gh pr list (re-check) -> still none
        gh = GitHubClient(fake)

        with pytest.raises(GitHubClientError) as exc:
            await find_or_create_release_pr(
                runner=fake,
                gh=gh,
                repo=_REPO,
                source_branch="development",
                target_branch="main",
                title="t",
                body="b",
            )

        assert exc.value.operation == "gh pr create"
        # The duplicate signal triggers a re-check list, but no `gh pr view`.
        assert [c.args[:3] for c in fake.calls] == [
            ["gh", "pr", "list"],
            ["gh", "pr", "create"],
            ["gh", "pr", "list"],
        ]

    @pytest.mark.unit
    async def test_race_recheck_ignores_fork_collision(self) -> None:
        # If create fails and the re-check only finds a fork PR sharing the head
        # branch name, that fork must not be adopted; the original error
        # re-raises rather than monitoring a fork PR.
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")  # gh pr list -> none
        fake.queue_result(returncode=1, stderr="already exists")  # gh pr create -> fails
        fake.queue_result(
            returncode=0, stdout=_fork_open_pr_list_payload(number=555)
        )  # gh pr list (re-check) -> only a fork PR
        gh = GitHubClient(fake)

        with pytest.raises(GitHubClientError):
            await find_or_create_release_pr(
                runner=fake,
                gh=gh,
                repo=_REPO,
                source_branch="development",
                target_branch="main",
                title="t",
                body="b",
            )

        assert all(c.args[:3] != ["gh", "pr", "view"] for c in fake.calls)


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
