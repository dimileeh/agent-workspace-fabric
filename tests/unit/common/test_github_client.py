"""Tests for the GitHubClient.

We don't need a fake GraphQL server — ``FakeCommandRunner`` returns the
canned stdout we want, and the client parses it. Assertions cover:

* request-argv shape (correct ``gh`` CLI / GraphQL variables)
* response parsing (field mapping, resolved/outdated filtering,
  check-state normalisation, mergeable normalisation)
* error propagation (non-zero exit, JSON parse failures, GraphQL errors)
"""

from __future__ import annotations

import json
import shlex

import pytest
import structlog

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import (
    BranchOpenPullRequestResolver,
    GitHubClient,
    GitHubClientError,
    PullRequestMetadataError,
    RepoRef,
    fetch_pull_request_adoption_metadata,
    list_open_pull_requests_for_branch,
    parse_github_pull_request_url,
)
from awf.runtime.pr_monitor import CheckState, MergeableState, MergeStateStatus

# ── RepoRef parsing ────────────────────────────────────────────────────────


class TestRepoRef:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "url, expected_slug",
        [
            ("dimileeh/aira-web", "dimileeh/aira-web"),
            ("git@github.com:dimileeh/aira-web.git", "dimileeh/aira-web"),
            ("git@github.com:dimileeh/aira-web", "dimileeh/aira-web"),
            ("ssh://git@github.com/dimileeh/aira-web.git", "dimileeh/aira-web"),
            ("https://github.com/dimileeh/aira-agent.git", "dimileeh/aira-agent"),
            (
                "https://x-access-token:credential-value@github.com/dimileeh/aira-agent.git",
                "dimileeh/aira-agent",
            ),
            ("https://github.com/dimileeh/aira-agent/", "dimileeh/aira-agent"),
            ("https://github.com/dimileeh/aira-agent", "dimileeh/aira-agent"),
        ],
    )
    def test_parses_common_github_url_shapes(self, url: str, expected_slug: str) -> None:
        ref = RepoRef.from_url(url)
        assert ref.slug() == expected_slug

    @pytest.mark.unit
    def test_rejects_non_github_urls(self) -> None:
        with pytest.raises(ValueError):
            RepoRef.from_url("git@gitlab.com:org/repo.git")

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "repo_url",
        [
            "https://github.com/dimileeh",
            "https://github.com/dimileeh/.git",
        ],
    )
    def test_rejects_incomplete_github_urls(self, repo_url: str) -> None:
        with pytest.raises(ValueError):
            RepoRef.from_url(repo_url)

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "reference_url, expected_url",
        [
            (
                "git@github.com:dimileeh/source.git",
                "git@github.com:contributor/aira-web.git",
            ),
            (
                "ssh://git@github.com/dimileeh/source.git",
                "git@github.com:contributor/aira-web.git",
            ),
            (
                "https://github.com/dimileeh/source.git",
                "https://github.com/contributor/aira-web.git",
            ),
            (
                "https://x-access-token:credential-value@github.com/dimileeh/source.git",
                "https://x-access-token:credential-value@github.com/contributor/aira-web.git",
            ),
            (
                "file:///tmp/source",
                "https://github.com/contributor/aira-web.git",
            ),
        ],
    )
    def test_clone_url_like_matches_github_transport(
        self,
        reference_url: str,
        expected_url: str,
    ) -> None:
        ref = RepoRef(owner="contributor", name="aira-web")

        assert ref.clone_url_like(reference_url) == expected_url


class TestPullRequestUrlParsing:
    @pytest.mark.unit
    def test_parses_canonical_pr_url(self) -> None:
        repo, number = parse_github_pull_request_url(
            "https://github.com/dimileeh/aira-web/pull/277"
        )

        assert repo.slug() == "dimileeh/aira-web"
        assert number == 277

    @pytest.mark.unit
    def test_rejects_non_pr_url(self) -> None:
        with pytest.raises(ValueError):
            parse_github_pull_request_url("https://github.com/dimileeh/aira-web/issues/277")

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "pr_url",
        [
            "https://github.com/dimileeh/aira-web/pull/not-a-number",
            "https://github.com/dimileeh/aira-web/pull/0",
        ],
    )
    def test_rejects_invalid_pr_numbers(self, pr_url: str) -> None:
        with pytest.raises(ValueError):
            parse_github_pull_request_url(pr_url)


def _adoption_pr_payload(
    *,
    number: int = 277,
    head_ref: str = "feature/head",
    head_repo_slug: str = "dimileeh/aira-web",
    base_ref: str = "development",
    head_sha: str = "h" * 40,
    base_sha: str = "b" * 40,
    state: str = "OPEN",
    is_draft: bool = False,
    author: str | None = "octocat",
    url: str = "https://github.com/dimileeh/aira-web/pull/277",
    title: str = "feature: ready",
) -> str:
    return json.dumps(
        {
            "number": number,
            "headRefName": head_ref,
            "headRepository": {
                "name": head_repo_slug.split("/", 1)[1],
                "nameWithOwner": head_repo_slug,
            },
            "isCrossRepository": head_repo_slug.lower() != "dimileeh/aira-web",
            "baseRefName": base_ref,
            "headRefOid": head_sha,
            "baseRefOid": base_sha,
            "state": state,
            "isDraft": is_draft,
            "author": {"login": author} if author is not None else None,
            "url": url,
            "title": title,
        }
    )


class TestFetchPullRequestAdoptionMetadata:
    @pytest.mark.unit
    async def test_returns_head_base_refs_and_shas_for_open_pr(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=_adoption_pr_payload())

        metadata = await fetch_pull_request_adoption_metadata(
            runner=fake,
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=277,
        )

        assert metadata.number == 277
        assert metadata.head_ref == "feature/head"
        assert metadata.head_repo_slug == "dimileeh/aira-web"
        assert metadata.base_ref == "development"
        assert metadata.head_sha == "h" * 40
        assert metadata.base_sha == "b" * 40
        assert metadata.url == "https://github.com/dimileeh/aira-web/pull/277"
        assert metadata.author == "octocat"
        assert metadata.closed is False
        assert metadata.merged is False

        args = fake.calls[0].args
        assert args[:3] == ["gh", "pr", "view"]
        fields = args[args.index("--json") + 1].split(",")
        assert "headRepository" in fields
        assert "isCrossRepository" in fields
        assert "headRefOid" in fields
        assert "baseRefOid" in fields
        assert "closed" not in fields
        assert "merged" not in fields

    @pytest.mark.unit
    async def test_returns_head_repository_identity_for_fork_pr(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_adoption_pr_payload(head_repo_slug="contributor/aira-web"),
        )

        metadata = await fetch_pull_request_adoption_metadata(
            runner=fake,
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=277,
        )

        assert metadata.head_ref == "feature/head"
        assert metadata.head_repo_slug == "contributor/aira-web"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "state, expected_closed, expected_merged",
        [
            ("CLOSED", True, False),
            ("MERGED", False, True),
        ],
    )
    async def test_terminal_prs_return_state_for_service_policy(
        self,
        state: str,
        expected_closed: bool,
        expected_merged: bool,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=_adoption_pr_payload(state=state))

        metadata = await fetch_pull_request_adoption_metadata(
            runner=fake,
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=277,
        )

        assert metadata.state == state
        assert metadata.closed is expected_closed
        assert metadata.merged is expected_merged

    @pytest.mark.unit
    async def test_missing_pr_raises_not_found_reason(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="GraphQL: Could not resolve to a PullRequest")

        with pytest.raises(PullRequestMetadataError) as excinfo:
            await fetch_pull_request_adoption_metadata(
                runner=fake,
                repo=RepoRef(owner="dimileeh", name="aira-web"),
                pr_number=404,
            )

        assert excinfo.value.reason_code == "PR_NOT_FOUND"

    @pytest.mark.unit
    async def test_metadata_fetch_failure_raises_fetch_failed_reason(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="rate limit exceeded")

        with pytest.raises(PullRequestMetadataError) as excinfo:
            await fetch_pull_request_adoption_metadata(
                runner=fake,
                repo=RepoRef(owner="dimileeh", name="aira-web"),
                pr_number=277,
            )

        assert excinfo.value.reason_code == "PR_METADATA_FETCH_FAILED"
        assert excinfo.value.detail["returncode"] == 1

    @pytest.mark.unit
    async def test_invalid_json_raises_invalid_metadata_reason(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="{not json")

        with pytest.raises(PullRequestMetadataError) as excinfo:
            await fetch_pull_request_adoption_metadata(
                runner=fake,
                repo=RepoRef(owner="dimileeh", name="aira-web"),
                pr_number=277,
            )

        assert excinfo.value.reason_code == "PR_METADATA_INVALID"

    @pytest.mark.unit
    async def test_blank_or_missing_required_payload_fields_are_invalid(self) -> None:
        invalid_payloads = [
            {"baseRefName": "development", "headRefName": "feature", "state": "OPEN"},
            {
                "number": 278,
                "baseRefName": "development",
                "headRefName": "feature",
                "state": "OPEN",
                "url": "https://github.com/dimileeh/aira-web/pull/278",
            },
            {
                "number": 277,
                "baseRefName": "development",
                "headRefName": " ",
                "state": "OPEN",
                "url": "https://github.com/dimileeh/aira-web/pull/277",
            },
            {
                "number": 277,
                "baseRefName": " ",
                "headRefName": "feature",
                "state": "OPEN",
                "url": "https://github.com/dimileeh/aira-web/pull/277",
            },
        ]

        for payload in invalid_payloads:
            fake = FakeCommandRunner()
            fake.queue_result(returncode=0, stdout=json.dumps(payload))

            with pytest.raises(PullRequestMetadataError) as excinfo:
                await fetch_pull_request_adoption_metadata(
                    runner=fake,
                    repo=RepoRef(owner="dimileeh", name="aira-web"),
                    pr_number=277,
                )

            assert excinfo.value.reason_code == "PR_METADATA_INVALID"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "sha_fields, expected_field",
        [
            ({"headRefOid": "  "}, "headRefOid"),
            ({"headRefOid": None}, "headRefOid"),
            ({"baseRefOid": "  "}, "baseRefOid"),
            ({"baseRefOid": None}, "baseRefOid"),
        ],
    )
    async def test_blank_or_non_string_required_shas_are_invalid(
        self,
        sha_fields: dict[str, object],
        expected_field: str,
    ) -> None:
        fake = FakeCommandRunner()
        payload = json.loads(_adoption_pr_payload())
        payload.update(sha_fields)
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(payload),
        )

        with pytest.raises(PullRequestMetadataError) as excinfo:
            await fetch_pull_request_adoption_metadata(
                runner=fake,
                repo=RepoRef(owner="dimileeh", name="aira-web"),
                pr_number=277,
            )

        assert excinfo.value.reason_code == "PR_METADATA_INVALID"
        assert expected_field in excinfo.value.message


class TestListOpenPullRequestsForBranch:
    @pytest.mark.unit
    async def test_requests_explicit_limit_above_default_page_size(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")

        await list_open_pull_requests_for_branch(
            runner=fake,
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            branch_name="feature/head",
        )

        args = fake.calls[0].args
        assert int(args[args.index("--limit") + 1]) > 30

    @pytest.mark.unit
    async def test_returns_head_repository_identity_for_branch_matches(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "number": 277,
                        "url": "https://github.com/dimileeh/aira-web/pull/277",
                        "headRefName": "feature/head",
                        "headRefOid": "h" * 40,
                        "headRepository": {
                            "name": "aira-web",
                            "nameWithOwner": "dimileeh/aira-web",
                        },
                        "headRepositoryOwner": {"login": "dimileeh"},
                    }
                ]
            ),
        )

        matches = await list_open_pull_requests_for_branch(
            runner=fake,
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            branch_name="feature/head",
            base_branch="development",
        )

        assert len(matches) == 1
        assert matches[0].head_repo_slug == "dimileeh/aira-web"
        json_fields = fake.calls[0].args[fake.calls[0].args.index("--json") + 1]
        assert "headRepository" in json_fields
        assert "headRepositoryOwner" in json_fields

    @pytest.mark.unit
    async def test_mixed_malformed_and_parseable_items_fail_closed(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "number": 277,
                        "url": "https://github.com/dimileeh/aira-web/pull/277",
                        "headRefName": "feature/head",
                        "headRefOid": "h" * 40,
                        "headRepository": None,
                    },
                    {
                        "number": 278,
                        "url": "https://github.com/dimileeh/aira-web/pull/278",
                        "headRefName": "feature/head",
                        "headRefOid": "g" * 40,
                        "headRepository": {
                            "name": "aira-web",
                            "nameWithOwner": "dimileeh/aira-web",
                        },
                    },
                ]
            ),
        )

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(PullRequestMetadataError) as excinfo,
        ):
            await list_open_pull_requests_for_branch(
                runner=fake,
                repo=RepoRef(owner="dimileeh", name="aira-web"),
                branch_name="feature/head",
            )

        assert excinfo.value.reason_code == "OPEN_PR_LOOKUP_INVALID"
        assert "mixed parseable and invalid items" in excinfo.value.message
        assert excinfo.value.detail == {
            "repo_slug": "dimileeh/aira-web",
            "branch_name": "feature/head",
            "base_branch": None,
            "parsed_count": 1,
            "parse_failure_count": 1,
            "parse_failure_indexes": [0],
        }
        event = next(
            (item for item in captured if item.get("event") == "github.open_pr_item_parse_failed"),
            None,
        )
        assert event is not None
        assert event.get("log_level") == "warning"
        assert event.get("repo_slug") == "dimileeh/aira-web"
        assert event.get("branch_name") == "feature/head"

    @pytest.mark.unit
    async def test_missing_head_repository_identity_is_invalid(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "number": 277,
                        "url": "https://github.com/dimileeh/aira-web/pull/277",
                        "headRefName": "feature/head",
                        "headRefOid": "h" * 40,
                    }
                ]
            ),
        )

        with pytest.raises(PullRequestMetadataError) as excinfo:
            await list_open_pull_requests_for_branch(
                runner=fake,
                repo=RepoRef(owner="dimileeh", name="aira-web"),
                branch_name="feature/head",
            )

        assert excinfo.value.reason_code == "OPEN_PR_LOOKUP_INVALID"
        assert "headRepository" in excinfo.value.message

    @pytest.mark.unit
    async def test_non_string_url_is_invalid(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "number": 277,
                        "url": None,
                        "headRefName": "feature/head",
                        "headRefOid": "h" * 40,
                    }
                ]
            ),
        )

        with pytest.raises(PullRequestMetadataError) as excinfo:
            await list_open_pull_requests_for_branch(
                runner=fake,
                repo=RepoRef(owner="dimileeh", name="aira-web"),
                branch_name="feature/head",
            )

        assert excinfo.value.reason_code == "OPEN_PR_LOOKUP_INVALID"
        assert "url" in excinfo.value.message


class TestBranchOpenPullRequestResolver:
    @pytest.mark.unit
    async def test_invalid_repo_url_returns_empty_list_and_warns(self) -> None:
        fake = FakeCommandRunner()
        resolver = BranchOpenPullRequestResolver(fake)
        repo_url = "https://x-access-token:secret-token@github.com/dimileeh"

        with structlog.testing.capture_logs() as captured:
            resolved = await resolver.resolve(
                repo_url=repo_url,
                branch_name="feature/head",
                base_branch="main",
            )

        assert resolved == []
        assert fake.calls == []
        event = next(
            (
                item
                for item in captured
                if item.get("event") == "github.open_pr_lookup_skipped_invalid_repo_url"
            ),
            None,
        )
        assert event is not None
        assert event.get("log_level") == "warning"
        assert event.get("repo_url") == "https://[redacted]@github.com/dimileeh"
        assert event.get("branch_name") == "feature/head"
        assert "github.com/dimileeh" in str(event.get("error"))
        assert "secret-token" not in str(event)


# ── fetch_pr_status ────────────────────────────────────────────────────────


def _sample_pr_payload(
    *,
    head_sha: str = "abc123",
    closed: bool = False,
    merged: bool = False,
    merge_commit_sha: str = "mergecommit1234567890",
    mergeable: str = "MERGEABLE",
    merge_state_status: str = "CLEAN",
    check_state: str = "SUCCESS",
    check_contexts: list[dict] | None = None,
    check_contexts_has_next_page: bool = False,
    threads: list[dict] | None = None,
    threads_has_next_page: bool = False,
    threads_end_cursor: str | None = None,
    reviews: list[dict] | None = None,
    reviews_has_next_page: bool = False,
    reviews_end_cursor: str | None = None,
    comments: list[dict] | None = None,
    comments_has_next_page: bool = False,
    comments_end_cursor: str | None = None,
    files: list[dict] | None = None,
    files_has_next_page: bool = False,
    files_end_cursor: str | None = None,
) -> str:
    """Build a canned ``gh api graphql`` stdout for one PR."""
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 42,
                        "headRefOid": head_sha,
                        "mergeable": mergeable,
                        "mergeStateStatus": merge_state_status,
                        "isDraft": False,
                        "closed": closed,
                        "merged": merged,
                        "mergeCommit": {"oid": merge_commit_sha} if merged else None,
                        "baseRef": {"name": "development", "target": {"oid": "base0"}},
                        "commits": {
                            "nodes": [
                                {
                                    "commit": {
                                        "statusCheckRollup": {
                                            "state": check_state,
                                            "contexts": {
                                                "nodes": check_contexts or [],
                                                "pageInfo": {
                                                    "hasNextPage": check_contexts_has_next_page
                                                },
                                            },
                                        }
                                    }
                                }
                            ]
                        },
                        "reviewThreads": {
                            "nodes": threads or [],
                            "pageInfo": {
                                "hasNextPage": threads_has_next_page,
                                "endCursor": threads_end_cursor,
                            },
                        },
                        "reviews": {
                            "nodes": reviews or [],
                            "pageInfo": {
                                "hasNextPage": reviews_has_next_page,
                                "endCursor": reviews_end_cursor,
                            },
                        },
                        "comments": {
                            "nodes": comments or [],
                            "pageInfo": {
                                "hasNextPage": comments_has_next_page,
                                "endCursor": comments_end_cursor,
                            },
                        },
                        "files": {
                            "nodes": files or [],
                            "pageInfo": {
                                "hasNextPage": files_has_next_page,
                                "endCursor": files_end_cursor,
                            },
                        },
                    }
                }
            }
        }
    )


class TestFetchPrStatus:
    @pytest.mark.unit
    async def test_happy_path_parses_all_gates(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                threads=[
                    {
                        "id": "T1",
                        "isResolved": False,
                        "isOutdated": False,
                        "path": "src/x.ts",
                        "line": 10,
                        "comments": {
                            "nodes": [
                                {"bodyText": "rename this", "author": {"login": "reviewer-bot"}}
                            ]
                        },
                    }
                ],
                reviews=[
                    {
                        "databaseId": 9001,
                        "body": "Summary with suggestions",
                        "state": "COMMENTED",
                        "author": {"login": "reviewer-bot"},
                    }
                ],
            ),
        )
        client = GitHubClient(fake)
        status = await client.fetch_pr_status(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            base_behind_count=3,
        )
        assert status.number == 42
        assert status.head_sha == "abc123"
        assert status.mergeable == MergeableState.MERGEABLE
        assert status.check_state == CheckState.SUCCESS
        assert status.base_behind_count == 3
        assert len(status.unresolved_inline_threads) == 1
        t = status.unresolved_inline_threads[0]
        assert t.thread_id == "T1"
        assert t.path == "src/x.ts"
        assert t.line == 10
        assert t.body_excerpt == "rename this"
        assert t.author == "reviewer-bot"
        assert len(status.unresolved_review_comments) == 1
        c = status.unresolved_review_comments[0]
        assert c.comment_id == "9001"
        assert c.body_excerpt == "Summary with suggestions"
        assert c.blocks_merge is False
        assert status.blocking_reviews == ()

    @pytest.mark.unit
    async def test_no_reviews_has_no_blocking_reviews(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=_sample_pr_payload())
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert status.unresolved_review_comments == ()
        assert status.blocking_reviews == ()

    @pytest.mark.unit
    async def test_commented_bot_reviews_are_advisory_not_blocking(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                reviews=[
                    {
                        "databaseId": 9101,
                        "body": "Greptile summary with suggestions.",
                        "state": "COMMENTED",
                        "author": {"login": "greptile-apps[bot]"},
                        "submittedAt": "2026-05-06T11:00:00Z",
                    },
                    {
                        "databaseId": 9102,
                        "body": "CodeRabbit advisory checklist.",
                        "state": "COMMENTED",
                        "author": {"login": "coderabbitai[bot]"},
                        "submittedAt": "2026-05-06T11:01:00Z",
                    },
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert [c.comment_id for c in status.unresolved_review_comments] == ["9101", "9102"]
        assert [c.blocks_merge for c in status.unresolved_review_comments] == [False, False]
        assert status.blocking_reviews == ()

    @pytest.mark.unit
    async def test_changes_requested_review_body_stays_triageable_when_blocking(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                reviews=[
                    {
                        "databaseId": 9201,
                        "body": "Please fix this before merge.",
                        "state": "CHANGES_REQUESTED",
                        "author": {"login": "human-reviewer"},
                        "submittedAt": "2026-05-06T11:00:00Z",
                    }
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert len(status.blocking_reviews) == 1
        blocker = status.blocking_reviews[0]
        assert blocker.comment_id == "9201"
        assert blocker.source_kind == "review"
        assert blocker.state == "CHANGES_REQUESTED"
        assert blocker.blocks_merge is True
        assert status.unresolved_review_comments[0].blocks_merge is False

    @pytest.mark.unit
    async def test_viewer_owned_changes_requested_review_is_not_blocking(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                reviews=[
                    {
                        "databaseId": 9202,
                        "body": "Self-authored change request should not block the monitor.",
                        "state": "CHANGES_REQUESTED",
                        "author": {"login": "token-owner"},
                        "viewerDidAuthor": True,
                        "submittedAt": "2026-05-06T11:00:00Z",
                    }
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert status.unresolved_review_comments == ()
        assert status.blocking_reviews == ()

    @pytest.mark.unit
    async def test_non_counting_changes_requested_review_is_advisory_not_blocking(
        self,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                reviews=[
                    {
                        "databaseId": 9251,
                        "body": "External contributor advisory change request.",
                        "state": "CHANGES_REQUESTED",
                        "author": {"login": "external-reviewer"},
                        "authorCanPushToRepository": False,
                        "submittedAt": "2026-05-06T11:00:00Z",
                    }
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        query_arg = next(arg for arg in fake.calls[0].args if arg.startswith("query="))
        assert "authorCanPushToRepository" in query_arg
        assert len(status.unresolved_review_comments) == 1
        review = status.unresolved_review_comments[0]
        assert review.comment_id == "9251"
        assert review.state == "CHANGES_REQUESTED"
        assert review.blocks_merge is False
        assert status.blocking_reviews == ()

    @pytest.mark.unit
    async def test_later_approval_from_same_reviewer_clears_blocking_review(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                reviews=[
                    {
                        "databaseId": 9301,
                        "body": "Please fix this before merge.",
                        "state": "CHANGES_REQUESTED",
                        "author": {"login": "human-reviewer"},
                        "submittedAt": "2026-05-06T11:00:00Z",
                    },
                    {
                        "databaseId": 9302,
                        "body": "Looks good now.",
                        "state": "APPROVED",
                        "author": {"login": "human-reviewer"},
                        "submittedAt": "2026-05-06T11:05:00Z",
                    },
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert [c.comment_id for c in status.unresolved_review_comments] == ["9301", "9302"]
        assert [c.blocks_merge for c in status.unresolved_review_comments] == [False, False]
        assert status.blocking_reviews == ()

    @pytest.mark.unit
    async def test_later_commented_review_does_not_clear_blocking_review(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                reviews=[
                    {
                        "databaseId": 9351,
                        "body": "Please fix this before merge.",
                        "state": "CHANGES_REQUESTED",
                        "author": {"login": "human-reviewer"},
                        "submittedAt": "2026-05-06T11:00:00Z",
                    },
                    {
                        "databaseId": 9352,
                        "body": "One more non-blocking note.",
                        "state": "COMMENTED",
                        "author": {"login": "human-reviewer"},
                        "submittedAt": "2026-05-06T11:05:00Z",
                    },
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert [c.comment_id for c in status.unresolved_review_comments] == ["9351", "9352"]
        assert [c.blocks_merge for c in status.unresolved_review_comments] == [False, False]
        assert len(status.blocking_reviews) == 1
        blocker = status.blocking_reviews[0]
        assert blocker.comment_id == "9351"
        assert blocker.blocks_merge is True

    @pytest.mark.unit
    async def test_later_dismissed_review_clears_blocking_review(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                reviews=[
                    {
                        "databaseId": 9361,
                        "body": "Please fix this before merge.",
                        "state": "CHANGES_REQUESTED",
                        "author": {"login": "human-reviewer"},
                        "submittedAt": "2026-05-06T11:00:00Z",
                    },
                    {
                        "databaseId": 9362,
                        "body": "Dismissed stale review record.",
                        "state": "DISMISSED",
                        "author": {"login": "human-reviewer"},
                        "submittedAt": "2026-05-06T11:05:00Z",
                    },
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert [c.comment_id for c in status.unresolved_review_comments] == ["9361", "9362"]
        assert [c.blocks_merge for c in status.unresolved_review_comments] == [False, False]
        assert status.blocking_reviews == ()

    @pytest.mark.unit
    async def test_empty_body_changes_requested_review_still_blocks(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                reviews=[
                    {
                        "databaseId": 9401,
                        "body": "   ",
                        "state": "CHANGES_REQUESTED",
                        "author": {"login": "human-reviewer"},
                        "submittedAt": "2026-05-06T11:00:00Z",
                    }
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert status.unresolved_review_comments == ()
        assert len(status.blocking_reviews) == 1
        blocker = status.blocking_reviews[0]
        assert blocker.comment_id == "9401"
        assert blocker.body_excerpt == ""
        assert blocker.blocks_merge is True

    @pytest.mark.unit
    async def test_top_level_issue_comments_are_advisory_not_blocking(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                comments=[
                    {
                        "databaseId": 9501,
                        "body": "CodeRabbit advisory top-level summary.",
                        "isMinimized": False,
                        "author": {"login": "coderabbitai[bot]"},
                        "createdAt": "2026-05-06T11:00:00Z",
                    }
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert [c.comment_id for c in status.unresolved_review_comments] == ["issue:9501"]
        assert status.unresolved_review_comments[0].blocks_merge is False
        assert status.blocking_reviews == ()

    @pytest.mark.unit
    async def test_preserves_full_unresolved_review_thread_comment_history(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                threads=[
                    {
                        "id": "T_multi",
                        "isResolved": False,
                        "isOutdated": False,
                        "path": "src/awf/runtime/pr_monitor_runner.py",
                        "line": 940,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 101,
                                    "bodyText": "Preserve the retry counter per action.",
                                    "author": {"login": "chatgpt-codex-connector[bot]"},
                                    "viewerDidAuthor": False,
                                    "createdAt": "2026-05-06T10:11:12Z",
                                    "url": "https://github.example/review/101",
                                },
                                {
                                    "databaseId": 102,
                                    "bodyText": "Still applies after the latest fix.",
                                    "author": {"login": "dimileeh"},
                                    "viewerDidAuthor": False,
                                    "createdAt": "2026-05-06T10:15:12Z",
                                    "url": "https://github.example/review/102",
                                },
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    }
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert len(status.unresolved_inline_threads) == 1
        thread = status.unresolved_inline_threads[0]
        assert thread.body_excerpt == "Preserve the retry counter per action."
        assert thread.author == "chatgpt-codex-connector[bot]"
        assert thread.url == "https://github.example/review/101"
        assert thread.is_outdated is False
        assert [(c.comment_id, c.author, c.body) for c in thread.comments] == [
            (
                "101",
                "chatgpt-codex-connector[bot]",
                "Preserve the retry counter per action.",
            ),
            ("102", "dimileeh", "Still applies after the latest fix."),
        ]
        assert thread.comments[0].created_at is not None
        assert thread.comments[0].created_at.isoformat() == "2026-05-06T10:11:12+00:00"
        assert thread.comments[1].url == "https://github.example/review/102"
        assert [c.viewer_did_author for c in thread.comments] == [False, False]

    @pytest.mark.unit
    async def test_paginates_review_thread_comment_history(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                threads=[
                    {
                        "id": "T_paginated",
                        "isResolved": False,
                        "isOutdated": False,
                        "path": "src/x.py",
                        "line": 7,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 201,
                                    "bodyText": "first page comment",
                                    "author": {"login": "reviewer-a"},
                                    "viewerDidAuthor": False,
                                    "createdAt": "2026-05-06T12:00:00Z",
                                    "url": "https://github.example/review/201",
                                }
                            ],
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                        },
                    }
                ],
            ),
        )
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "node": {
                            "comments": {
                                "nodes": [
                                    {
                                        "databaseId": 202,
                                        "bodyText": "second page comment",
                                        "author": {"login": "reviewer-b"},
                                        "viewerDidAuthor": False,
                                        "createdAt": "2026-05-06T12:01:00Z",
                                        "url": "https://github.example/review/202",
                                    },
                                    {
                                        "databaseId": 203,
                                        "bodyText": "second page self-authored bookkeeping",
                                        "author": {"login": "token-owner"},
                                        "viewerDidAuthor": True,
                                        "createdAt": "2026-05-06T12:02:00Z",
                                        "url": "https://github.example/review/203",
                                    },
                                ],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                }
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert [c.body for c in status.unresolved_inline_threads[0].comments] == [
            "first page comment",
            "second page comment",
        ]
        assert [c.comment_id for c in status.unresolved_inline_threads[0].comments] == [
            "201",
            "202",
        ]
        assert [c.viewer_did_author for c in status.unresolved_inline_threads[0].comments] == [
            False,
            False,
        ]
        assert len(fake.calls) == 2
        assert "threadId=T_paginated" in fake.calls[1].args
        assert "cursor=cursor-1" in fake.calls[1].args
        assert any(a.startswith("query=") and "viewerDidAuthor" in a for a in fake.calls[1].args)

    @pytest.mark.unit
    async def test_skips_sparse_review_thread_nodes_without_id(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                threads=[
                    {
                        "isResolved": False,
                        "isOutdated": False,
                        "path": "src/missing.py",
                        "line": 1,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 199,
                                    "bodyText": "malformed thread from GitHub",
                                    "author": {"login": "reviewer"},
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    },
                    {
                        "id": "T_valid",
                        "isResolved": False,
                        "isOutdated": False,
                        "path": "src/valid.py",
                        "line": 2,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 200,
                                    "bodyText": "valid thread",
                                    "author": {"login": "reviewer"},
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    },
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert [thread.thread_id for thread in status.unresolved_inline_threads] == ["T_valid"]
        assert status.unresolved_inline_threads[0].body_excerpt == "valid thread"

    @pytest.mark.unit
    async def test_paginates_review_and_issue_comment_connections(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                reviews=[
                    {
                        "databaseId": 301,
                        "body": "first review",
                        "state": "COMMENTED",
                        "author": {"login": "reviewer-a"},
                        "viewerDidAuthor": False,
                    }
                ],
                reviews_has_next_page=True,
                reviews_end_cursor="reviews-1",
                comments=[
                    {
                        "databaseId": 401,
                        "body": "first issue comment",
                        "isMinimized": False,
                        "author": {"login": "reviewer-c"},
                        "viewerDidAuthor": False,
                    }
                ],
                comments_has_next_page=True,
                comments_end_cursor="comments-1",
            ),
        )
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviews": {
                                    "nodes": [
                                        {
                                            "databaseId": 302,
                                            "body": "second review",
                                            "state": "COMMENTED",
                                            "author": {"login": "reviewer-b"},
                                            "viewerDidAuthor": False,
                                        },
                                        {
                                            "databaseId": 303,
                                            "body": "second page self-authored review",
                                            "state": "COMMENTED",
                                            "author": {"login": "token-owner"},
                                            "viewerDidAuthor": True,
                                        },
                                    ],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                }
                            }
                        }
                    }
                }
            ),
        )
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": 402,
                                            "body": "second issue comment",
                                            "isMinimized": False,
                                            "author": {"login": "reviewer-d"},
                                            "viewerDidAuthor": False,
                                        },
                                        {
                                            "databaseId": 403,
                                            "body": "second page self-authored issue comment",
                                            "isMinimized": False,
                                            "author": {"login": "token-owner"},
                                            "viewerDidAuthor": True,
                                        },
                                    ],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                }
                            }
                        }
                    }
                }
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert [c.comment_id for c in status.unresolved_review_comments] == [
            "301",
            "302",
            "issue:401",
            "issue:402",
        ]
        assert "cursor=reviews-1" in fake.calls[1].args
        assert "cursor=comments-1" in fake.calls[2].args
        assert any(a.startswith("query=") and "viewerDidAuthor" in a for a in fake.calls[1].args)
        assert any(a.startswith("query=") and "viewerDidAuthor" in a for a in fake.calls[2].args)

    @pytest.mark.unit
    async def test_preserves_review_and_issue_comment_metadata_without_bot_semantic_filtering(
        self,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                reviews=[
                    {
                        "databaseId": 7801,
                        "body": "I have no feedback to provide.",
                        "state": "COMMENTED",
                        "author": {"login": "gemini-code-assist[bot]"},
                        "submittedAt": "2026-05-06T11:00:00Z",
                        "url": "https://github.example/review/7801",
                    }
                ],
                comments=[
                    {
                        "databaseId": 7802,
                        "body": "Finishing touches and review summary.",
                        "isMinimized": False,
                        "author": {"login": "reviewer-bot"},
                        "createdAt": "2026-05-06T11:05:00Z",
                        "url": "https://github.example/comment/7802",
                    }
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert [c.comment_id for c in status.unresolved_review_comments] == [
            "7801",
            "issue:7802",
        ]
        review, issue = status.unresolved_review_comments
        assert review.body == "I have no feedback to provide."
        assert review.source_kind == "review"
        assert review.state == "COMMENTED"
        assert review.url == "https://github.example/review/7801"
        assert review.created_at is not None
        assert review.created_at.isoformat() == "2026-05-06T11:00:00+00:00"
        assert issue.body == "Finishing touches and review summary."
        assert issue.source_kind == "issue"
        assert issue.blocks_merge is False
        assert issue.url == "https://github.example/comment/7802"
        assert issue.created_at is not None
        assert issue.created_at.isoformat() == "2026-05-06T11:05:00+00:00"

    @pytest.mark.unit
    async def test_ignores_viewer_authored_pr_feedback_without_body_matching(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                threads=[
                    {
                        "id": "T1",
                        "isResolved": False,
                        "isOutdated": False,
                        "path": "src/x.py",
                        "line": 10,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 101,
                                    "bodyText": "Reviewer feedback that still matters.",
                                    "author": {"login": "reviewer"},
                                    "viewerDidAuthor": False,
                                },
                                {
                                    "databaseId": 102,
                                    "bodyText": "Any AWF bookkeeping text, no magic prefix needed.",
                                    "author": {"login": "token-owner"},
                                    "viewerDidAuthor": True,
                                },
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    },
                    {
                        "id": "T_self",
                        "isResolved": False,
                        "isOutdated": False,
                        "path": "src/y.py",
                        "line": 20,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 201,
                                    "bodyText": "Self-authored thread should not be fed back.",
                                    "author": {"login": "token-owner"},
                                    "viewerDidAuthor": True,
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    },
                ],
                reviews=[
                    {
                        "databaseId": 301,
                        "body": "Self-authored review body with arbitrary words.",
                        "state": "COMMENTED",
                        "author": {"login": "token-owner"},
                        "viewerDidAuthor": True,
                    }
                ],
                comments=[
                    {
                        "databaseId": 401,
                        "body": "Self-authored issue comment with arbitrary words.",
                        "isMinimized": False,
                        "author": {"login": "token-owner"},
                        "viewerDidAuthor": True,
                    }
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert [thread.thread_id for thread in status.unresolved_inline_threads] == ["T1"]
        thread = status.unresolved_inline_threads[0]
        assert [comment.comment_id for comment in thread.comments] == ["101"]
        assert thread.body_excerpt == "Reviewer feedback that still matters."
        assert status.unresolved_review_comments == ()

    @pytest.mark.unit
    async def test_preserves_non_viewer_comments_even_when_body_looks_like_awf_bookkeeping(
        self,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                comments=[
                    {
                        "databaseId": 402,
                        "body": "A reviewer comment that resembles bookkeeping still matters.",
                        "isMinimized": False,
                        "author": {"login": "reviewer"},
                        "viewerDidAuthor": False,
                    }
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert [comment.comment_id for comment in status.unresolved_review_comments] == [
            "issue:402"
        ]

    @pytest.mark.unit
    async def test_parses_check_timing_metadata_from_rollup_contexts(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                check_state="PENDING",
                check_contexts=[
                    {
                        "__typename": "CheckRun",
                        "name": "Greptile",
                        "status": "IN_PROGRESS",
                        "conclusion": None,
                        "startedAt": "2026-04-26T12:00:00Z",
                        "completedAt": None,
                        "detailsUrl": "https://checks.example/greptile",
                        "checkSuite": {
                            "app": {
                                "slug": "greptile-apps",
                                "name": "Greptile",
                            },
                            "creator": {"login": "octocat"},
                        },
                    },
                    {
                        "__typename": "StatusContext",
                        "context": "ci/build",
                        "state": "PENDING",
                        "targetUrl": "https://checks.example/build",
                        "creator": {"login": "github-actions[bot]"},
                    },
                ],
            ),
        )
        client = GitHubClient(fake)
        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert len(status.checks) == 2
        check_run = status.checks[0]
        assert check_run.name == "Greptile"
        assert check_run.status == "IN_PROGRESS"
        assert check_run.conclusion is None
        assert check_run.started_at is not None
        assert check_run.started_at.isoformat() == "2026-04-26T12:00:00+00:00"
        assert check_run.completed_at is None
        assert check_run.details_url == "https://checks.example/greptile"
        assert check_run.app_slug == "greptile-apps"
        assert check_run.app_name == "Greptile"
        assert check_run.creator_login == "octocat"

        status_context = status.checks[1]
        assert status_context.name == "ci/build"
        assert status_context.status == "PENDING"
        assert status_context.details_url == "https://checks.example/build"
        assert status_context.app_slug is None
        assert status_context.app_name is None
        assert status_context.creator_login == "github-actions[bot]"

    @pytest.mark.unit
    async def test_warns_when_check_contexts_are_truncated(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(check_contexts_has_next_page=True),
        )
        client = GitHubClient(fake)

        with structlog.testing.capture_logs() as captured:
            await client.fetch_pr_status(
                repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
            )

        assert {
            "event": "github.check_contexts_truncated",
            "repo": "o/r",
            "pr_number": 1,
            "fetched_contexts_limit": 100,
            "log_level": "warning",
        } in captured

    @pytest.mark.unit
    async def test_paginates_changed_paths_when_pr_files_have_more_pages(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                files=[{"path": "src/first.py"}],
                files_has_next_page=True,
                files_end_cursor="cursor-1",
            ),
        )
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "files": {
                                    "nodes": [{"path": "src/second.py"}],
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                }
                            }
                        }
                    }
                }
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert status.changed_paths == ("src/first.py", "src/second.py")
        assert len(fake.calls) == 2
        next_page_args = fake.calls[1].args
        assert any(
            a.startswith("query=") and "files(first: 100, after: $cursor)" in a
            for a in next_page_args
        )
        assert "cursor=cursor-1" in next_page_args

    @pytest.mark.unit
    async def test_pr_files_pagination_requires_end_cursor_when_more_pages_exist(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                files=[{"path": "src/first.py"}],
                files_has_next_page=True,
                files_end_cursor=None,
            ),
        )
        client = GitHubClient(fake)

        with pytest.raises(GitHubClientError, match="without an endCursor"):
            await client.fetch_pr_status(
                repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
            )

        assert len(fake.calls) == 1

    @pytest.mark.unit
    async def test_pr_files_pagination_requires_files_object_on_next_page(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                files=[{"path": "src/first.py"}],
                files_has_next_page=True,
                files_end_cursor="cursor-1",
            ),
        )
        fake.queue_result(
            returncode=0,
            stdout=json.dumps({"data": {"repository": {"pullRequest": {}}}}),
        )
        client = GitHubClient(fake)

        with pytest.raises(GitHubClientError, match="did not include files"):
            await client.fetch_pr_status(
                repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
            )

        assert len(fake.calls) == 2

    @pytest.mark.unit
    async def test_fetch_pr_status_ignores_malformed_optional_nodes(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                merge_state_status="NEW_GITHUB_STATE",
                check_contexts=[
                    None,  # type: ignore[list-item]
                    {"__typename": "StatusContext", "context": "   "},
                    {"__typename": "CheckRun", "name": ""},
                    {
                        "__typename": "CheckRun",
                        "name": "build",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                        "startedAt": "not-a-date",
                        "completedAt": "2026-04-26T12:34:56",
                        "detailsUrl": "https://checks.example/build",
                    },
                ],
                comments=[
                    {
                        "databaseId": 10,
                        "body": "minimized",
                        "isMinimized": True,
                        "author": {"login": "octocat"},
                    },
                    {
                        "databaseId": 11,
                        "body": "   ",
                        "isMinimized": False,
                        "author": {"login": "octocat"},
                    },
                ],
                files=[
                    None,  # type: ignore[list-item]
                    {"path": "   "},
                    {"path": "src/ok.py"},
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert status.merge_state_status == MergeStateStatus.UNKNOWN
        assert status.unresolved_review_comments == ()
        assert status.changed_paths == ("src/ok.py",)
        assert len(status.checks) == 1
        check = status.checks[0]
        assert check.name == "build"
        assert check.started_at is None
        assert check.completed_at is not None
        assert check.completed_at.isoformat() == "2026-04-26T12:34:56+00:00"

    @pytest.mark.unit
    async def test_routes_bot_issue_summary_to_agent_feedback(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                comments=[
                    {
                        "databaseId": 78,
                        "body": "Finishing touches and review summary.",
                        "isMinimized": False,
                        "author": {"login": "reviewer-bot"},
                    }
                ]
            ),
        )
        client = GitHubClient(fake)
        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )
        assert [c.comment_id for c in status.unresolved_review_comments] == ["issue:78"]
        assert "Finishing touches" in status.unresolved_review_comments[0].body_excerpt

    @pytest.mark.unit
    async def test_parses_actionable_codex_issue_comment_as_review_comment(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                comments=[
                    {
                        "databaseId": 4390521275,
                        "body": (
                            "\n### 💡 Codex Review\n\n"
                            "https://github.com/dimileeh/aira-agent-workspace-fabric/"
                            "blob/49c0c400de80f2b7ffb4f67bb6a76868f4d0e6ae/"
                            "src/awf/runtime/pr_monitor_runner.py#L940-L941\n"
                            "**P2 Preserve action-specific base-fetch retry counts**\n\n"
                            "When `sync_base` or the pre-merge recheck keeps hitting "
                            "a transient `BaseFetchError`, clear only the successful "
                            "context's counter."
                        ),
                        "isMinimized": False,
                        "author": {"login": "chatgpt-codex-connector[bot]"},
                    }
                ]
            ),
        )
        client = GitHubClient(fake)
        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert len(status.unresolved_review_comments) == 1
        comment = status.unresolved_review_comments[0]
        assert comment.comment_id == "issue:4390521275"
        assert comment.author == "chatgpt-codex-connector[bot]"
        assert "Preserve action-specific base-fetch retry counts" in comment.body_excerpt
        assert comment.blocks_merge is False

    @pytest.mark.unit
    async def test_codex_review_envelope_does_not_hide_actionable_review_thread(
        self,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                threads=[
                    {
                        "id": "PRRT_kwDOSJAM6s5_-ehR",
                        "isResolved": False,
                        "isOutdated": False,
                        "path": "src/awf/runtime/merge_eligibility.py",
                        "line": 164,
                        "comments": {
                            "nodes": [
                                {
                                    "bodyText": (
                                        "**P2 Keep tier-1 deferred coverage stale**\n\n"
                                        "The candidate can be marked fresh before "
                                        "the final coverage recovery runs."
                                    ),
                                    "author": {"login": "chatgpt-codex-connector[bot]"},
                                }
                            ]
                        },
                    }
                ],
                reviews=[
                    {
                        "databaseId": 4236551690,
                        "body": (
                            "\n### 💡 Codex Review\n\n"
                            "Here are some automated review suggestions for this pull request.\n\n"
                            "**Reviewed commit:** `7b94ebd4b6`\n\n"
                            "<details> <summary>ℹ️ About Codex in GitHub</summary>\n"
                            "<br/>\n\n"
                            "Codex has been enabled to automatically review pull requests "
                            "in this repo."
                            "</details>"
                        ),
                        "state": "COMMENTED",
                        "author": {"login": "chatgpt-codex-connector[bot]"},
                    }
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert [t.thread_id for t in status.unresolved_inline_threads] == ["PRRT_kwDOSJAM6s5_-ehR"]
        assert "Keep tier-1 deferred coverage stale" in (
            status.unresolved_inline_threads[0].body_excerpt
        )
        assert [c.comment_id for c in status.unresolved_review_comments] == ["4236551690"]
        assert "Codex Review" in status.unresolved_review_comments[0].body_excerpt

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "body",
        [
            "I have no feedback to provide.",
            (
                "## Code Review\n\n"
                "This pull request introduces profile linting support. "
                "The feedback focuses on improving the robustness and granularity "
                "of the linter."
            ),
        ],
    )
    async def test_routes_bot_review_summaries_to_agent_feedback(self, body: str) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                reviews=[
                    {
                        "databaseId": 7801,
                        "body": body,
                        "state": "COMMENTED",
                        "author": {"login": "gemini-code-assist[bot]"},
                    }
                ]
            ),
        )
        client = GitHubClient(fake)
        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )
        assert [c.comment_id for c in status.unresolved_review_comments] == ["7801"]
        assert status.unresolved_review_comments[0].body == body

    @pytest.mark.unit
    async def test_codex_review_envelope_is_preserved_alongside_actionable_thread(
        self,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                threads=[
                    {
                        "id": "PRRT_kwDOSJAM6s5_H5DV",
                        "isResolved": False,
                        "isOutdated": False,
                        "path": "src/awf/db/repositories.py",
                        "line": 3187,
                        "comments": {
                            "nodes": [
                                {
                                    "bodyText": (
                                        "Remove OFFSET from SKIP LOCKED scheduler queries"
                                    ),
                                    "author": {"login": "chatgpt-codex-connector[bot]"},
                                }
                            ]
                        },
                    }
                ],
                reviews=[
                    {
                        "databaseId": 4215124378,
                        "body": (
                            "\n### 💡 Codex Review\n\n"
                            "Here are some automated review suggestions for this pull request.\n\n"
                            "**Reviewed commit:** `062c9ceab4`\n\n"
                            "<details> <summary>ℹ️ About Codex in GitHub</summary>\n"
                            "<br/>\n\n"
                            "Codex has been enabled to automatically review pull requests "
                            "in this repo. Reviews are triggered when you\n"
                            "- Open a pull request for review\n"
                            "- Mark a draft as ready\n"
                            '- Comment "@codex review".\n\n'
                            "If Codex has suggestions, it will comment; otherwise it will "
                            "react with 👍.\n"
                            "</details>"
                        ),
                        "state": "COMMENTED",
                        "author": {"login": "chatgpt-codex-connector[bot]"},
                    }
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert [t.thread_id for t in status.unresolved_inline_threads] == ["PRRT_kwDOSJAM6s5_H5DV"]
        assert [c.comment_id for c in status.unresolved_review_comments] == ["4215124378"]

    @pytest.mark.unit
    async def test_keeps_actionable_bot_review_body(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                reviews=[
                    {
                        "databaseId": 7802,
                        "body": "Please document why this branch is safe.",
                        "state": "COMMENTED",
                        "author": {"login": "gemini-code-assist[bot]"},
                    }
                ]
            ),
        )
        client = GitHubClient(fake)
        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )
        assert [c.comment_id for c in status.unresolved_review_comments] == ["7802"]

    @pytest.mark.unit
    async def test_parses_human_issue_comment_as_review_comment(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                comments=[
                    {
                        "databaseId": 79,
                        "body": "Please wait for the product owner to check this.",
                        "isMinimized": False,
                        "author": {"login": "octocat"},
                    }
                ]
            ),
        )
        client = GitHubClient(fake)
        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )
        assert len(status.unresolved_review_comments) == 1
        c = status.unresolved_review_comments[0]
        assert c.comment_id == "issue:79"
        assert c.author == "octocat"
        assert c.blocks_merge is False

    @pytest.mark.unit
    async def test_filters_out_resolved_and_outdated_threads(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                threads=[
                    {
                        "id": "T_resolved",
                        "isResolved": True,
                        "isOutdated": False,
                        "path": "a",
                        "line": 1,
                        "comments": {"nodes": []},
                    },
                    {
                        "id": "T_outdated",
                        "isResolved": False,
                        "isOutdated": True,
                        "path": "b",
                        "line": 2,
                        "comments": {"nodes": []},
                    },
                    {
                        "id": "T_live",
                        "isResolved": False,
                        "isOutdated": False,
                        "path": "c",
                        "line": 3,
                        "comments": {
                            "nodes": [{"bodyText": "fix me", "author": {"login": "alice"}}]
                        },
                    },
                ]
            ),
        )
        client = GitHubClient(fake)
        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )
        assert [t.thread_id for t in status.unresolved_inline_threads] == ["T_live"]

    @pytest.mark.unit
    async def test_skips_review_with_empty_body(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                reviews=[
                    {"databaseId": 1, "body": "", "state": "APPROVED", "author": None},
                    {"databaseId": 2, "body": "   ", "state": "COMMENTED", "author": None},
                    {
                        "databaseId": 3,
                        "body": "real feedback",
                        "state": "COMMENTED",
                        "author": None,
                    },
                ]
            ),
        )
        client = GitHubClient(fake)
        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )
        assert [c.comment_id for c in status.unresolved_review_comments] == ["3"]

    @pytest.mark.unit
    async def test_truncates_long_bodies(self) -> None:
        huge_body = "x" * 5000
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                threads=[
                    {
                        "id": "T1",
                        "isResolved": False,
                        "isOutdated": False,
                        "path": "a",
                        "line": 1,
                        "comments": {"nodes": [{"bodyText": huge_body, "author": {"login": "x"}}]},
                    }
                ],
                reviews=[
                    {"databaseId": 1, "body": huge_body, "state": "COMMENTED", "author": None}
                ],
            ),
        )
        client = GitHubClient(fake)
        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )
        assert len(status.unresolved_inline_threads[0].body_excerpt) == 400
        assert len(status.unresolved_review_comments[0].body_excerpt) == 400

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "gql, expected",
        [
            ("SUCCESS", CheckState.SUCCESS),
            ("FAILURE", CheckState.FAILURE),
            ("ERROR", CheckState.FAILURE),
            ("PENDING", CheckState.PENDING),
            ("EXPECTED", CheckState.PENDING),
            ("", CheckState.PENDING),  # default when rollup missing
            ("WAT", CheckState.NEUTRAL),
        ],
    )
    async def test_check_state_normalisation(self, gql: str, expected: CheckState) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=_sample_pr_payload(check_state=gql))
        client = GitHubClient(fake)
        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )
        assert status.check_state == expected

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "gql, expected",
        [
            ("MERGEABLE", MergeableState.MERGEABLE),
            ("CONFLICTING", MergeableState.CONFLICTING),
            ("UNKNOWN", MergeableState.UNKNOWN),
            ("", MergeableState.UNKNOWN),
        ],
    )
    async def test_mergeable_normalisation(self, gql: str, expected: MergeableState) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=_sample_pr_payload(mergeable=gql))
        client = GitHubClient(fake)
        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )
        assert status.mergeable == expected

    @pytest.mark.unit
    async def test_closed_and_merged_flags_propagate(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=_sample_pr_payload(closed=True, merged=True))
        client = GitHubClient(fake)
        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )
        assert status.closed is True
        assert status.merged is True
        assert status.merge_commit_sha == "mergecommit1234567890"

    @pytest.mark.unit
    async def test_graphql_argv_shape(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=_sample_pr_payload())
        client = GitHubClient(fake)
        await client.fetch_pr_status(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=123,
            base_behind_count=0,
        )
        args = fake.calls[0].args
        assert args[0:3] == ["gh", "api", "graphql"]
        # Query passed via -f query=…, numeric number passed via -F
        assert any(a.startswith("query=") and "pullRequest" in a for a in args)
        assert any(
            a.startswith("query=") and "pageInfo { hasNextPage endCursor }" in a for a in args
        )
        assert any(a.startswith("query=") and "viewerDidAuthor" in a for a in args)
        assert "number=123" in args and "-F" in args
        assert "owner=dimileeh" in args and "repo=aira-web" in args

    @pytest.mark.unit
    async def test_non_zero_exit_raises(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="rate limited")
        client = GitHubClient(fake)
        with pytest.raises(GitHubClientError) as exc:
            await client.fetch_pr_status(
                repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
            )
        assert "rate limited" in str(exc.value)

    @pytest.mark.unit
    async def test_pr_not_found_raises(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps({"data": {"repository": {"pullRequest": None}}}),
        )
        client = GitHubClient(fake)
        with pytest.raises(GitHubClientError) as exc:
            await client.fetch_pr_status(
                repo=RepoRef(owner="o", name="r"),
                pr_number=1,
                base_behind_count=0,
            )
        assert "not found" in str(exc.value)

    @pytest.mark.unit
    async def test_graphql_errors_raise(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps({"errors": [{"message": "Field 'foo' doesn't exist"}]}),
        )
        client = GitHubClient(fake)
        with pytest.raises(GitHubClientError) as exc:
            await client.fetch_pr_status(
                repo=RepoRef(owner="o", name="r"),
                pr_number=1,
                base_behind_count=0,
            )
        assert "doesn't exist" in str(exc.value)

    @pytest.mark.unit
    async def test_bad_json_raises(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="not json")
        client = GitHubClient(fake)
        with pytest.raises(GitHubClientError):
            await client.fetch_pr_status(
                repo=RepoRef(owner="o", name="r"),
                pr_number=1,
                base_behind_count=0,
            )


# ── fetch_failing_check_logs ───────────────────────────────────────────────


class TestFetchFailingCheckLogs:
    @pytest.mark.unit
    async def test_extracts_single_pytest_failure_evidence_and_focused_command(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 42,
                        "name": "coverage-gate",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        fake.queue_result(
            returncode=0,
            stdout=(
                "coverage\tRun tests\tuv run --python 3.12 --extra dev pytest -n 8 "
                "--dist=loadscope --cov=awf --cov-fail-under=99\n"
                "FAILED tests/unit/docs/test_catalog_coverage.py::test_catalog_coverage "
                "- AssertionError: Missing reason catalog entries: ARTIFACT_BLOCKED, "
                "ARTIFACT_OVERSIZED\n"
                "E   AssertionError: Missing reason catalog entries: ARTIFACT_BLOCKED, "
                "ARTIFACT_OVERSIZED\n"
            ),
        )
        client = GitHubClient(fake)

        failures = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
        )

        assert len(failures) == 1
        failure = failures[0]
        assert failure.name == "coverage-gate"
        assert failure.test_node_ids == (
            "tests/unit/docs/test_catalog_coverage.py::test_catalog_coverage",
        )
        assert failure.suggested_repro_commands == (
            "uv run --python 3.12 --extra dev pytest "
            "tests/unit/docs/test_catalog_coverage.py::test_catalog_coverage -q",
        )
        assert any("Missing reason catalog entries" in item for item in failure.error_summaries)
        assert any("ARTIFACT_BLOCKED" in item for item in failure.assertion_snippets)

    @pytest.mark.unit
    async def test_extracts_full_nested_pytest_node_path(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 421,
                        "name": "coverage-gate",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        fake.queue_result(
            returncode=0,
            stdout=(
                "coverage\tRun tests\tpython -m pytest pkg/tests\n"
                "FAILED pkg/tests/test_api.py::test_x - AssertionError: boom\n"
            ),
        )
        client = GitHubClient(fake)

        failures = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
        )

        failure = failures[0]
        assert failure.test_node_ids == ("pkg/tests/test_api.py::test_x",)
        assert failure.suggested_repro_commands == (
            "python -m pytest pkg/tests/test_api.py::test_x -q",
        )

    @pytest.mark.unit
    async def test_extracts_multiple_pytest_failures_without_untrusted_fallback_command(
        self,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 43,
                        "name": "any-provider-check",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        fake.queue_result(
            returncode=0,
            stdout=(
                "FAILED tests/unit/a/test_one.py::test_alpha - AssertionError: alpha\n"
                "FAILED tests/unit/b/test_two.py::TestTwo::test_beta - AssertionError: beta\n"
                "FAILED tests/unit/c/test_three.py::test_gamma - AssertionError: gamma\n"
                "FAILED tests/unit/d/test_four.py::test_delta - AssertionError: delta\n"
                "FAILED tests/unit/e/test_five.py::test_epsilon - AssertionError: epsilon\n"
                "FAILED tests/unit/f/test_six.py::test_zeta - AssertionError: zeta\n"
            ),
        )
        client = GitHubClient(fake)

        failures = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
        )

        failure = failures[0]
        assert failure.test_node_ids[:2] == (
            "tests/unit/a/test_one.py::test_alpha",
            "tests/unit/b/test_two.py::TestTwo::test_beta",
        )
        assert len(failure.test_node_ids) == 6
        selected = failure.test_node_ids[:5]
        quoted = " ".join(shlex.quote(node_id) for node_id in selected)
        assert failure.suggested_repro_commands == (
            f"uv run --python 3.12 --extra dev pytest {quoted} -q",
        )

    @pytest.mark.unit
    async def test_extracts_multiple_pytest_failures_with_supplied_fallback_command(
        self,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 4300,
                        "name": "any-provider-check",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        fake.queue_result(
            returncode=0,
            stdout=(
                "FAILED tests/unit/a/test_one.py::test_alpha - AssertionError: alpha\n"
                "FAILED tests/unit/b/test_two.py::TestTwo::test_beta - AssertionError: beta\n"
            ),
        )
        client = GitHubClient(fake)

        failures = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
            pytest_fallback_commands=(
                "ruff check .",
                "uv run --python 3.12 --extra dev pytest --cov=awf",
            ),
        )

        failure = failures[0]
        assert failure.test_node_ids == (
            "tests/unit/a/test_one.py::test_alpha",
            "tests/unit/b/test_two.py::TestTwo::test_beta",
        )
        assert failure.suggested_repro_commands == (
            "uv run --python 3.12 --extra dev pytest "
            "tests/unit/a/test_one.py::test_alpha "
            "tests/unit/b/test_two.py::TestTwo::test_beta -q",
        )

    @pytest.mark.unit
    async def test_builds_focused_command_from_detected_pytest_command(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 430,
                        "name": "python-tests",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        fake.queue_result(
            returncode=0,
            stdout=(
                "tests\tRun tests\tpython -m pytest -n 8 tests/unit\n"
                "FAILED tests/unit/runtime/test_prompt.py::test_one - AssertionError: boom\n"
            ),
        )
        client = GitHubClient(fake)

        failures = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
        )

        failure = failures[0]
        assert failure.suggested_repro_commands == (
            "python -m pytest tests/unit/runtime/test_prompt.py::test_one -q",
        )

    @pytest.mark.unit
    async def test_does_not_promote_untrusted_printed_pytest_commands(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 4301,
                        "name": "python-tests",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        fake.queue_result(
            returncode=0,
            stdout=(
                "pytest tests/unit/runtime/test_prompt.py::test_one; echo owned\n"
                "FAILED tests/unit/runtime/test_prompt.py::test_one - AssertionError: boom\n"
            ),
        )
        client = GitHubClient(fake)

        failures = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
        )

        failure = failures[0]
        assert failure.failing_commands == ()
        assert failure.suggested_repro_commands == (
            "uv run --python 3.12 --extra dev pytest "
            "tests/unit/runtime/test_prompt.py::test_one -q",
        )

    @pytest.mark.unit
    async def test_quotes_parametrized_pytest_node_ids_in_focused_command(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 431,
                        "name": "any-provider-check",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        fake.queue_result(
            returncode=0,
            stdout=(
                "tests\tRun tests\tpython -m pytest tests/unit/runtime/test_prompt.py\n"
                "FAILED tests/unit/runtime/test_prompt.py::test_handles[bad value; "
                "echo owned] - AssertionError: boom\n"
            ),
        )
        client = GitHubClient(fake)

        failures = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
        )

        failure = failures[0]
        node_id = "tests/unit/runtime/test_prompt.py::test_handles[bad value; echo owned]"
        assert failure.test_node_ids == (node_id,)
        assert failure.suggested_repro_commands == (f"python -m pytest {shlex.quote(node_id)} -q",)

    @pytest.mark.unit
    async def test_quotes_parametrized_pytest_node_ids_from_non_failed_lines(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 432,
                        "name": "any-provider-check",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        fake.queue_result(
            returncode=0,
            stdout=(
                "tests\tRun tests\tpython -m pytest tests/unit/runtime/test_prompt.py\n"
                "ERROR tests/unit/runtime/test_prompt.py::test_handles[bad value; "
                "echo owned] - RuntimeError: boom\n"
            ),
        )
        client = GitHubClient(fake)

        failures = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
        )

        failure = failures[0]
        node_id = "tests/unit/runtime/test_prompt.py::test_handles[bad value; echo owned]"
        assert failure.test_node_ids == (node_id,)
        assert failure.suggested_repro_commands == (f"python -m pytest {shlex.quote(node_id)} -q",)

    @pytest.mark.unit
    async def test_preserves_pytest_param_ids_containing_failure_delimiter_text(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 433,
                        "name": "any-provider-check",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        fake.queue_result(
            returncode=0,
            stdout=(
                "tests\tRun tests\tpython -m pytest tests/unit/runtime/test_prompt.py\n"
                "FAILED tests/unit/runtime/test_prompt.py::test_handles[a - b] "
                "- AssertionError: boom\n"
            ),
        )
        client = GitHubClient(fake)

        failures = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
        )

        failure = failures[0]
        node_id = "tests/unit/runtime/test_prompt.py::test_handles[a - b]"
        assert failure.test_node_ids == (node_id,)
        assert failure.suggested_repro_commands == (f"python -m pytest {shlex.quote(node_id)} -q",)

    @pytest.mark.unit
    async def test_preserves_significant_whitespace_in_pytest_param_ids(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 434,
                        "name": "any-provider-check",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        fake.queue_result(
            returncode=0,
            stdout=(
                "tests\tRun tests\tpython -m pytest tests/unit/runtime/test_prompt.py\n"
                "FAILED tests/unit/runtime/test_prompt.py::test_handles[bad  value] "
                "- AssertionError: boom\n"
            ),
        )
        client = GitHubClient(fake)

        failures = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
        )

        failure = failures[0]
        node_id = "tests/unit/runtime/test_prompt.py::test_handles[bad  value]"
        assert failure.test_node_ids == (node_id,)
        assert failure.suggested_repro_commands == (f"python -m pytest {shlex.quote(node_id)} -q",)

    @pytest.mark.unit
    async def test_extracts_long_pytest_param_id_before_truncating_display_lines(self) -> None:
        param_id = "case-" + ("x" * 520)
        node_id = f"tests/unit/runtime/test_prompt.py::test_handles[{param_id}]"
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 435,
                        "name": "python-full-coverage",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        fake.queue_result(
            returncode=0,
            stdout=(
                "tests\tRun tests\tpython -m pytest tests/unit/runtime/test_prompt.py\n"
                f"FAILED {node_id} - AssertionError: boom\n"
            ),
        )
        client = GitHubClient(fake)

        failures = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
        )

        failure = failures[0]
        assert failure.test_node_ids == (node_id,)
        assert failure.suggested_repro_commands == (f"python -m pytest {shlex.quote(node_id)} -q",)
        assert all(len(item) <= 500 for item in failure.error_summaries)

    @pytest.mark.unit
    async def test_extracts_non_test_command_failure_evidence(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 44,
                        "name": "lint-and-type",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        fake.queue_result(
            returncode=0,
            stdout=(
                "lint\tRun ruff\tuv run --python 3.12 --extra dev ruff check src/awf tests\n"
                "src/awf/runtime/foo.py:10:1: F401 imported but unused\n"
                "Error: Process completed with exit code 1.\n"
            ),
        )
        client = GitHubClient(fake)

        failures = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
        )

        failure = failures[0]
        assert failure.failing_commands == (
            "uv run --python 3.12 --extra dev ruff check src/awf tests",
        )
        assert failure.suggested_repro_commands == ()
        assert any("F401 imported but unused" in item for item in failure.error_summaries)

    @pytest.mark.unit
    async def test_redacts_secrets_before_log_and_evidence_are_stored(self) -> None:
        secret = "sk-proj-ci-failure-secret"
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 45,
                        "name": "external-ci",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        fake.queue_result(
            returncode=0,
            stdout=(
                f"pytest\tRun\tuv run pytest tests/unit/test_secret.py::test_no_token TOKEN={secret}\n"
                f"FAILED tests/unit/test_secret.py::test_no_token - AssertionError: {secret}\n"
                f"E   Authorization: Bearer {secret}\n"
            ),
        )
        client = GitHubClient(fake)

        failures = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
        )

        serialized = repr(failures[0])
        assert secret not in failures[0].log_excerpt
        assert secret not in serialized
        assert "<redacted>" in serialized

    @pytest.mark.unit
    async def test_missing_log_records_evidence_warning(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 46,
                        "name": "logs-purged",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        fake.queue_result(returncode=1, stderr="log not found")
        client = GitHubClient(fake)

        failures = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"), pr_number=1, head_sha="abc"
        )

        assert failures[0].log_excerpt == ""
        assert failures[0].evidence_warnings == (
            "GitHub Actions log unavailable for failed check logs-purged.",
        )

    @pytest.mark.unit
    async def test_collects_failures_and_truncates_log(self) -> None:
        fake = FakeCommandRunner()
        # 1) gh run list
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 1,
                        "name": "lint",
                        "conclusion": "SUCCESS",
                        "status": "completed",
                    },
                    {
                        "databaseId": 2,
                        "name": "playwright",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    },
                    {
                        "databaseId": 3,
                        "name": "unit-tests",
                        "conclusion": "TIMED_OUT",
                        "status": "completed",
                    },
                ]
            ),
        )
        # 2) gh run view 2 --log-failed (for playwright)
        fake.queue_result(returncode=0, stdout="line1\n" + ("x" * 5000) + "\nlast line")
        # 3) gh run view 3 --log-failed (for unit-tests)
        fake.queue_result(returncode=0, stdout="timeout log")
        client = GitHubClient(fake)
        failures = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
            log_tail_chars=200,
        )
        assert len(failures) == 2
        names = {f.name for f in failures}
        assert names == {"playwright", "unit-tests"}
        assert {f.run_id for f in failures} == {"2", "3"}
        for f in failures:
            if f.name == "playwright":
                # Truncated to ~200 chars + prefix marker.
                assert len(f.log_excerpt) <= 300
                assert "truncated" in f.log_excerpt
            if f.name == "unit-tests":
                assert f.log_excerpt == "timeout log"

    @pytest.mark.unit
    async def test_handles_missing_log(self) -> None:
        """If the log fetch fails (purged / permission), log_excerpt is empty
        — we don't abort the monitor."""
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 2,
                        "name": "playwright",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        fake.queue_result(returncode=1, stderr="log not found")
        client = GitHubClient(fake)
        failures = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"), pr_number=1, head_sha="abc"
        )
        assert len(failures) == 1
        assert failures[0].log_excerpt == ""

    @pytest.mark.unit
    async def test_missing_run_database_id_stays_nullable(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": None,
                        "name": "lint-and-type",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        client = GitHubClient(fake)

        failures = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"), pr_number=1, head_sha="abc"
        )

        assert len(failures) == 1
        assert failures[0].run_id is None
        assert failures[0].log_excerpt == ""
        assert len(fake.calls) == 1

    @pytest.mark.unit
    async def test_missing_run_database_id_and_name_uses_unknown_fallback(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": None,
                        "name": None,
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        client = GitHubClient(fake)

        failures = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"), pr_number=1, head_sha="abc"
        )

        assert len(failures) == 1
        assert failures[0].name == "run/unknown"
        assert failures[0].run_id is None
        assert failures[0].log_excerpt == ""
        assert len(fake.calls) == 1

    @pytest.mark.unit
    async def test_ignores_non_failure_runs(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {"databaseId": 1, "name": "a", "conclusion": "SUCCESS", "status": "completed"},
                    {"databaseId": 2, "name": "b", "conclusion": "SKIPPED", "status": "completed"},
                    {"databaseId": 3, "name": "c", "conclusion": None, "status": "in_progress"},
                ]
            ),
        )
        client = GitHubClient(fake)
        failures = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"), pr_number=1, head_sha="abc"
        )
        assert failures == ()

    @pytest.mark.unit
    async def test_empty_run_list_stdout_returns_no_failures_without_fetching_logs(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="")
        client = GitHubClient(fake)

        failures = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"), pr_number=1, head_sha="abc"
        )

        assert failures == ()
        assert len(fake.calls) == 1

    @pytest.mark.unit
    async def test_reruns_failed_jobs_for_workflow_run(self) -> None:
        fake = FakeCommandRunner()
        client = GitHubClient(fake)

        await client.rerun_failed_workflow_jobs(
            repo=RepoRef(owner="o", name="r"),
            run_id="25655330295",
        )

        assert fake.calls[0].args == [
            "gh",
            "run",
            "rerun",
            "25655330295",
            "--repo",
            "o/r",
            "--failed",
        ]

    @pytest.mark.unit
    async def test_rerun_failed_jobs_raises_client_error_on_gh_failure(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="HTTP 403")
        client = GitHubClient(fake)

        with pytest.raises(GitHubClientError) as exc_info:
            await client.rerun_failed_workflow_jobs(
                repo=RepoRef(owner="o", name="r"),
                run_id="25655330295",
            )

        assert exc_info.value.operation == "rerun_failed_workflow_jobs"
        assert exc_info.value.stderr == "HTTP 403"


# ── resolve_thread / post_comment / merge_pr ───────────────────────────────


class TestMutations:
    @pytest.mark.unit
    async def test_resolve_thread_posts_expected_mutation(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                {"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
            ),
        )
        client = GitHubClient(fake)
        await client.resolve_thread(thread_id="T1")
        args = fake.calls[0].args
        assert args[0:3] == ["gh", "api", "graphql"]
        assert any(a.startswith("query=") and "resolveReviewThread" in a for a in args)
        assert "threadId=T1" in args

    @pytest.mark.unit
    async def test_resolve_thread_raises_on_error(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="thread gone")
        client = GitHubClient(fake)
        with pytest.raises(GitHubClientError):
            await client.resolve_thread(thread_id="T1")

    @pytest.mark.unit
    async def test_post_comment_argv(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0)
        client = GitHubClient(fake)
        await client.post_comment(
            repo=RepoRef(owner="o", name="r"), pr_number=99, body="ready to merge"
        )
        args = fake.calls[0].args
        assert args[:3] == ["gh", "pr", "comment"]
        assert "99" in args
        assert "--body" in args and "ready to merge" in args
        assert "--repo" in args and "o/r" in args

    @pytest.mark.unit
    async def test_post_comment_raises_on_error(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="forbidden")
        client = GitHubClient(fake)
        with pytest.raises(GitHubClientError):
            await client.post_comment(repo=RepoRef(owner="o", name="r"), pr_number=1, body="x")

    @pytest.mark.unit
    async def test_merge_pr_squash_delete_branch_default(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0)  # merge
        fake.queue_result(returncode=0, stdout="MERGESHA123\n")  # sha fetch
        client = GitHubClient(fake)
        sha = await client.merge_pr(repo=RepoRef(owner="o", name="r"), pr_number=42)
        merge_args = fake.calls[0].args
        assert merge_args[:3] == ["gh", "pr", "merge"]
        assert "--squash" in merge_args
        assert "--delete-branch" in merge_args
        assert sha == "MERGESHA123"

    @pytest.mark.unit
    async def test_merge_pr_honors_method_and_omits_delete_branch_flag(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0)
        fake.queue_result(returncode=0, stdout="MERGESHA123\n")
        client = GitHubClient(fake)

        sha = await client.merge_pr(
            repo=RepoRef(owner="o", name="r"),
            pr_number=42,
            method="merge",
            delete_branch=False,
        )

        merge_args = fake.calls[0].args
        assert "--merge" in merge_args
        assert "--squash" not in merge_args
        assert "--delete-branch" not in merge_args
        assert sha == "MERGESHA123"

    @pytest.mark.unit
    async def test_merge_pr_error_raises(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="branch protection blocked merge")
        client = GitHubClient(fake)
        with pytest.raises(GitHubClientError) as exc:
            await client.merge_pr(repo=RepoRef(owner="o", name="r"), pr_number=1)
        assert "branch protection" in str(exc.value)

    @pytest.mark.unit
    async def test_merge_pr_sha_fetch_best_effort(self) -> None:
        """If the post-merge SHA fetch fails, merge is still successful.
        The SHA is optional metadata, not a functional gate."""
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0)  # merge ok
        fake.queue_result(returncode=1, stderr="some api hiccup")  # sha fetch fails
        client = GitHubClient(fake)
        sha = await client.merge_pr(repo=RepoRef(owner="o", name="r"), pr_number=42)
        assert sha == ""
