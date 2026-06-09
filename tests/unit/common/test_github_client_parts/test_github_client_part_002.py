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

import pytest
import structlog

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import (
    GitHubClient,
    GitHubClientError,
    RepoRef,
)
from awf.runtime.pr_monitor import CheckState, MergeableState


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


def _sample_pr_payload(
    *,
    head_sha: str = "abc123",
    created_at: str = "2026-05-06T10:00:00Z",
    updated_at: str = "2026-05-06T10:00:00Z",
    committed_date: str = "2026-05-06T10:00:00Z",
    closed: bool = False,
    merged: bool = False,
    merge_commit_sha: str = "mergecommit1234567890",
    mergeable: str = "MERGEABLE",
    merge_state_status: str = "CLEAN",
    check_state: str = "SUCCESS",
    check_contexts: list[dict] | None = None,
    check_contexts_has_next_page: bool = False,
    check_contexts_total_count: int | None = None,
    status_check_rollup_present: bool = True,
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
                        "createdAt": created_at,
                        "updatedAt": updated_at,
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
                                        "statusCheckRollup": (
                                            {
                                                "state": check_state,
                                                "contexts": {
                                                    "nodes": check_contexts or [],
                                                    "totalCount": check_contexts_total_count,
                                                    "pageInfo": {
                                                        "hasNextPage": check_contexts_has_next_page
                                                    },
                                                },
                                            }
                                            if status_check_rollup_present
                                            else None
                                        ),
                                        "committedDate": committed_date,
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


class TestFetchPrStatusPart001:
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
    async def test_review_activity_uses_updated_at_across_feedback_surfaces(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                created_at="2026-05-06T09:00:00Z",
                updated_at="2026-05-06T09:30:00Z",
                committed_date="2026-05-06T09:10:00Z",
                threads=[
                    {
                        "id": "T_activity",
                        "isResolved": False,
                        "isOutdated": False,
                        "path": "src/x.py",
                        "line": 7,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 9701,
                                    "bodyText": "Inline note.",
                                    "author": {"login": "greptile-apps[bot]"},
                                    "viewerDidAuthor": False,
                                    "createdAt": "2026-05-06T10:00:00Z",
                                    "updatedAt": "2026-05-06T10:05:00Z",
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    }
                ],
                reviews=[
                    {
                        "databaseId": 9702,
                        "body": "Review body.",
                        "state": "COMMENTED",
                        "author": {"login": "human-reviewer"},
                        "submittedAt": "2026-05-06T10:01:00Z",
                        "updatedAt": "2026-05-06T10:06:00Z",
                    }
                ],
                comments=[
                    {
                        "databaseId": 9703,
                        "body": "Top-level feedback.",
                        "isMinimized": False,
                        "viewerDidAuthor": False,
                        "author": {"login": "coderabbitai[bot]"},
                        "createdAt": "2026-05-06T10:02:00Z",
                        "updatedAt": "2026-05-06T10:07:00Z",
                    }
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert status.latest_external_review_activity_at is not None
        assert status.latest_external_review_activity_at.isoformat() == "2026-05-06T10:07:00+00:00"
        assert status.latest_external_review_activity_source == "issue_comment"
        assert status.quiet_period_anchor_at == status.latest_external_review_activity_at
        assert status.quiet_period_anchor_source == "issue_comment"
        assert status.unresolved_inline_threads[0].comments[0].updated_at is not None
        assert (
            status.unresolved_review_comments[-1].updated_at.isoformat()
            == "2026-05-06T10:07:00+00:00"
        )

    @pytest.mark.unit
    async def test_resolved_thread_comment_activity_counts_for_quiet_anchor(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                created_at="2026-05-06T09:00:00Z",
                updated_at="2026-05-06T09:30:00Z",
                committed_date="2026-05-06T09:10:00Z",
                threads=[
                    {
                        "id": "T_resolved",
                        "isResolved": True,
                        "isOutdated": False,
                        "path": "src/x.py",
                        "line": 7,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 9710,
                                    "bodyText": "Resolved but still recent.",
                                    "author": {"login": "human-reviewer"},
                                    "viewerDidAuthor": False,
                                    "createdAt": "2026-05-06T10:00:00Z",
                                    "updatedAt": "2026-05-06T10:03:00Z",
                                }
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

        assert status.unresolved_inline_threads == ()
        assert status.latest_external_review_activity_at is not None
        assert status.latest_external_review_activity_at.isoformat() == "2026-05-06T10:03:00+00:00"
        assert status.latest_external_review_activity_source == "review_thread_comment"

    @pytest.mark.unit
    async def test_external_review_activity_prevails_over_pr_updated_at_anchor(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                created_at="2026-05-06T09:00:00Z",
                updated_at="2026-05-06T10:10:00Z",
                committed_date="2026-05-06T09:20:00Z",
                threads=[
                    {
                        "id": "T_stale",
                        "isResolved": False,
                        "isOutdated": False,
                        "path": "src/x.py",
                        "line": 7,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 9740,
                                    "bodyText": "Needs final read.",
                                    "author": {"login": "human-reviewer"},
                                    "viewerDidAuthor": False,
                                    "createdAt": "2026-05-06T10:00:00Z",
                                    "updatedAt": "2026-05-06T10:03:00Z",
                                }
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

        assert status.latest_external_review_activity_at is not None
        assert status.latest_external_review_activity_at.isoformat() == "2026-05-06T10:03:00+00:00"
        assert status.latest_external_review_activity_source == "review_thread_comment"
        assert status.quiet_period_anchor_at is not None
        assert status.quiet_period_anchor_at.isoformat() == "2026-05-06T10:03:00+00:00"
        assert status.quiet_period_anchor_source == "review_thread_comment"

    @pytest.mark.unit
    async def test_viewer_authored_feedback_does_not_reset_quiet_anchor(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                created_at="2026-05-06T09:00:00Z",
                updated_at="2026-05-06T09:20:00Z",
                committed_date="2026-05-06T09:10:00Z",
                threads=[
                    {
                        "id": "T_viewer",
                        "isResolved": False,
                        "isOutdated": False,
                        "path": "src/x.py",
                        "line": 7,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 9720,
                                    "bodyText": "Monitor bookkeeping.",
                                    "author": {"login": "awf-bot"},
                                    "viewerDidAuthor": True,
                                    "createdAt": "2026-05-06T10:00:00Z",
                                    "updatedAt": "2026-05-06T10:30:00Z",
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    }
                ],
                reviews=[
                    {
                        "databaseId": 9721,
                        "body": "Self-authored review.",
                        "state": "COMMENTED",
                        "viewerDidAuthor": True,
                        "author": {"login": "awf-bot"},
                        "submittedAt": "2026-05-06T10:05:00Z",
                        "updatedAt": "2026-05-06T10:31:00Z",
                    }
                ],
                comments=[
                    {
                        "databaseId": 9722,
                        "body": "Self-authored issue comment.",
                        "isMinimized": False,
                        "viewerDidAuthor": True,
                        "author": {"login": "awf-bot"},
                        "createdAt": "2026-05-06T10:06:00Z",
                        "updatedAt": "2026-05-06T10:32:00Z",
                    }
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert status.latest_external_review_activity_at is None
        assert status.latest_external_review_activity_source is None
        assert status.quiet_period_anchor_at is not None
        assert status.quiet_period_anchor_source == "pull_request"
        assert status.quiet_period_anchor_at.isoformat() == "2026-05-06T09:20:00+00:00"

    @pytest.mark.unit
    async def test_review_activity_anchor_considers_head_commit_updates(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                created_at="2026-05-06T09:00:00Z",
                updated_at="2026-05-06T10:05:00Z",
                committed_date="2026-05-06T10:10:00Z",
                threads=[
                    {
                        "id": "T_anchor",
                        "isResolved": False,
                        "isOutdated": False,
                        "path": "src/x.py",
                        "line": 7,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 9730,
                                    "bodyText": "Needs recheck.",
                                    "author": {"login": "human-reviewer"},
                                    "viewerDidAuthor": False,
                                    "createdAt": "2026-05-06T09:15:00Z",
                                    "updatedAt": "2026-05-06T09:30:00Z",
                                }
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

        assert status.latest_external_review_activity_at is not None
        assert status.latest_external_review_activity_at.isoformat() == "2026-05-06T09:30:00+00:00"
        assert status.latest_external_review_activity_source == "review_thread_comment"
        assert status.quiet_period_anchor_at is not None
        assert status.quiet_period_anchor_at.isoformat() == "2026-05-06T10:10:00+00:00"
        assert status.quiet_period_anchor_source == "head_commit"

    @pytest.mark.unit
    async def test_stale_head_commit_uses_pr_updated_at_as_quiet_anchor(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                created_at="2026-05-06T09:00:00Z",
                updated_at="2026-05-06T09:45:00Z",
                committed_date="2026-05-06T09:10:00Z",
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert status.latest_external_review_activity_at is None
        assert status.quiet_period_anchor_at is not None
        assert status.quiet_period_anchor_source == "pull_request"
        assert status.quiet_period_anchor_at.isoformat() == "2026-05-06T09:45:00+00:00"

    @pytest.mark.unit
    async def test_no_review_feedback_uses_latest_pr_or_head_activity_anchor(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                created_at="2026-05-06T09:00:00Z",
                updated_at="2026-05-06T09:20:00Z",
                committed_date="2026-05-06T09:45:00Z",
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert status.latest_external_review_activity_at is None
        assert status.quiet_period_anchor_at is not None
        assert status.quiet_period_anchor_at.isoformat() == "2026-05-06T09:45:00+00:00"
        assert status.quiet_period_anchor_source == "head_commit"

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
async def test_aclose_is_a_noop_and_runs_no_commands() -> None:
    # GitHubClient wraps a stateless command runner, so aclose() owns nothing to
    # release. It exists to satisfy the ForgeClient.aclose contract (issue
    # :4640573294) so callers close any forge client uniformly without branching
    # on the concrete forge — for a BitbucketClient aclose() releases an httpx
    # connection pool.
    fake = FakeCommandRunner()
    client = GitHubClient(fake)

    await client.aclose()

    assert fake.calls == []


@pytest.mark.unit
async def test_async_context_manager_yields_self_and_closes_without_commands() -> None:
    fake = FakeCommandRunner()
    client = GitHubClient(fake)

    async with client as entered:
        assert entered is client

    assert fake.calls == []
