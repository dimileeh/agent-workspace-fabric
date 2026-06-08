"""Tests for GitHubClient CI-signal observation (no_checks_observed).

Split out of ``test_github_client_part_002`` to keep each part file under the
first-party line-count guardrail. Covers the authoritative ``no_checks_observed``
signal derived from the GraphQL ``statusCheckRollup`` (#469).
"""

from __future__ import annotations

import json

import pytest

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import (
    GitHubClient,
    GitHubClientError,
    RepoRef,
)
from awf.runtime.pr_monitor import CheckState


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


class TestFetchPrStatusCiSignal:
    @pytest.mark.unit
    async def test_no_checks_observed_when_rollup_absent(self) -> None:
        # GitHub returns no statusCheckRollup at all for a commit with no CI;
        # the authoritative signal is set True (#469).
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(status_check_rollup_present=False),
        )
        client = GitHubClient(fake)
        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )
        assert status.no_checks_observed is True
        assert status.check_state == CheckState.PENDING

    @pytest.mark.unit
    async def test_no_checks_observed_when_rollup_present_but_empty(self) -> None:
        # A present-but-empty rollup reports contexts.totalCount == 0.
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                check_state="PENDING",
                check_contexts=[],
                check_contexts_total_count=0,
            ),
        )
        client = GitHubClient(fake)
        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )
        assert status.no_checks_observed is True

    @pytest.mark.unit
    async def test_no_checks_observed_false_when_contexts_present(self) -> None:
        # ≥1 context (totalCount >= 1) ⇒ the signal stays off, never skipping CI.
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                check_state="PENDING",
                check_contexts=[
                    {
                        "__typename": "CheckRun",
                        "name": "build",
                        "status": "IN_PROGRESS",
                        "conclusion": None,
                    }
                ],
                check_contexts_total_count=1,
            ),
        )
        client = GitHubClient(fake)
        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )
        assert status.no_checks_observed is False

    @pytest.mark.unit
    async def test_fetch_failure_raises_not_no_checks(self) -> None:
        # A non-zero ``gh`` exit while fetching PR status MUST raise
        # GitHubClientError, never degrade to a mergeable no-checks PRStatus that
        # the require_ci opt-out could then merge blind (#469 safety regression).
        # ``_graphql`` raises before ``no_checks_observed`` is ever computed; this
        # mirrors the BitBucket
        # ``test_fetch_pr_status_statuses_fetch_failure_raises_not_no_checks``.
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="rate limited")
        client = GitHubClient(fake)
        with pytest.raises(GitHubClientError):
            await client.fetch_pr_status(
                repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
            )
