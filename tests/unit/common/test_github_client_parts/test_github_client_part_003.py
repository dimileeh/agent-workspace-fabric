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

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.forge_lifecycle import PullRequestLifecycle
from awf.common.github_client import (
    GitHubClient,
    GitHubClientError,
    RepoRef,
    _transient_graphql_payload_error,
)
from awf.runtime.pr_monitor import CheckState, MergeableState, MergeStateStatus


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
                                        "statusCheckRollup": {
                                            "state": check_state,
                                            "contexts": {
                                                "nodes": check_contexts or [],
                                                "pageInfo": {
                                                    "hasNextPage": check_contexts_has_next_page
                                                },
                                            },
                                        },
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


class _RecordedSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class TestFetchPrStatusPart002:
    @pytest.mark.unit
    async def test_pre_merge_recheck_fails_fast_on_paginated_page(self) -> None:
        # Regression (PR #729): retry=False must reach page 2+ of every
        # paginated connection with RetryPolicy.NEVER, not fall back to the
        # READ default. A transient blip on the review-threads second page must
        # raise on the first attempt (no transport backoff) so the merge
        # critical section fails fast instead of holding the merge lock.
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                threads_has_next_page=True,
                threads_end_cursor="cursor-1",
            ),
        )
        fake.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")
        sleep = _RecordedSleep()
        client = GitHubClient(fake, sleep=sleep)

        with pytest.raises(GitHubClientError, match="502"):
            await client.fetch_pr_status(
                repo=RepoRef(owner="o", name="r"),
                pr_number=1,
                base_behind_count=0,
                retry=False,
            )

        assert len(fake.calls) == 2  # page 1 ok, page 2 raises without retry
        assert sleep.calls == []  # no transport backoff in the merge critical section

    @pytest.mark.unit
    async def test_polling_default_retries_transient_on_paginated_page(self) -> None:
        # Contrast to the pre-merge recheck: ordinary polling (retry=True)
        # keeps allow-by-default retry on later pages, so a transient page-2
        # blip recovers in-cycle rather than surfacing to the monitor.
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                threads_has_next_page=True,
                threads_end_cursor="cursor-1",
            ),
        )
        fake.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [],
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                }
                            }
                        }
                    }
                }
            ),
        )
        sleep = _RecordedSleep()
        client = GitHubClient(fake, sleep=sleep)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            base_behind_count=0,
        )

        assert status.number == 42
        assert len(fake.calls) == 3  # page 2 retried after the transient blip
        assert len(sleep.calls) == 1  # one backoff before the retry
        for call in fake.calls:
            query_arg = next(arg for arg in call.args if arg.startswith("query="))
            assert "pullRequestReview" in query_arg

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
                            "https://github.com/dimileeh/agent-workspace-fabric/"
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
    async def test_keeps_review_body_in_inbox_when_live_inline_thread_bundles_it(self) -> None:
        """A bundled review body stays independently triageable alongside its inline thread."""
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                threads=[
                    {
                        "id": "T_same_review",
                        "isResolved": False,
                        "isOutdated": False,
                        "path": "src/awf/runtime/pr_monitor_runner/loop.py",
                        "line": 162,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 3836654862,
                                    "bodyText": "Clear the sticky workflow-scope marker.",
                                    "author": {"login": "reviewer-bot"},
                                    "pullRequestReview": {"databaseId": 5000732773},
                                }
                            ]
                        },
                    }
                ],
                reviews=[
                    {
                        "databaseId": 5000732773,
                        "body": "Automated review found one potential issue.",
                        "state": "COMMENTED",
                        "author": {"login": "reviewer-bot"},
                    }
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert [thread.thread_id for thread in status.unresolved_inline_threads] == [
            "T_same_review"
        ]
        bundled_thread = status.unresolved_inline_threads[0]
        assert bundled_thread.comments[0].review_id == "5000732773"
        assert bundled_thread.review_context is not None
        assert bundled_thread.review_context.comment_id == "5000732773"
        assert bundled_thread.review_context.body == "Automated review found one potential issue."
        assert [comment.comment_id for comment in status.unresolved_review_comments] == [
            "5000732773"
        ]
        query_arg = next(arg for arg in fake.calls[0].args if arg.startswith("query="))
        assert "pullRequestReview" in query_arg

    @pytest.mark.unit
    async def test_resolved_inline_review_does_not_hide_independent_review_body(self) -> None:
        """A resolved thread alone is not proof that its review body was handled."""
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                threads=[
                    {
                        "id": "T_resolved_same_review",
                        "isResolved": True,
                        "isOutdated": False,
                        "path": "src/resolved.py",
                        "line": 4,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 8101,
                                    "bodyText": "Resolved inline note.",
                                    "author": {"login": "reviewer"},
                                    "pullRequestReview": {"databaseId": 8100},
                                }
                            ]
                        },
                    }
                ],
                reviews=[
                    {
                        "databaseId": 8100,
                        "body": "Independent review-level follow-up remains actionable.",
                        "state": "COMMENTED",
                        "author": {"login": "reviewer"},
                    }
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert status.unresolved_inline_threads == ()
        assert status.outdated_unresolved_inline_threads == ()
        assert [comment.comment_id for comment in status.unresolved_review_comments] == ["8100"]

    @pytest.mark.unit
    async def test_outdated_inline_review_keeps_body_and_blocker(self) -> None:
        """Outdated hygiene cannot stand in for review-body triage or blocking state."""
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                threads=[
                    {
                        "id": "T_outdated_same_review",
                        "isResolved": False,
                        "isOutdated": True,
                        "path": "src/outdated.py",
                        "line": 8,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 8201,
                                    "bodyText": "Outdated but still unresolved inline note.",
                                    "author": {"login": "human-reviewer"},
                                    "pullRequestReview": {"databaseId": 8200},
                                }
                            ]
                        },
                    }
                ],
                reviews=[
                    {
                        "databaseId": 8200,
                        "body": "Please address the inline change request.",
                        "state": "CHANGES_REQUESTED",
                        "author": {"login": "human-reviewer"},
                    }
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert status.unresolved_inline_threads == ()
        assert [thread.thread_id for thread in status.outdated_unresolved_inline_threads] == [
            "T_outdated_same_review"
        ]
        assert [comment.comment_id for comment in status.unresolved_review_comments] == ["8200"]
        assert [review.comment_id for review in status.blocking_reviews] == ["8200"]

    @pytest.mark.unit
    async def test_review_body_is_attached_to_exactly_one_live_thread(self) -> None:
        """A multi-thread review is one body bundle, not one copy per inline item."""
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                threads=[
                    {
                        "id": "T_bundle_anchor",
                        "isResolved": False,
                        "isOutdated": False,
                        "path": "src/one.py",
                        "line": 3,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 8301,
                                    "bodyText": "First inline request.",
                                    "author": {"login": "reviewer"},
                                    "pullRequestReview": {"databaseId": 8300},
                                }
                            ]
                        },
                    },
                    {
                        "id": "T_bundle_second",
                        "isResolved": False,
                        "isOutdated": False,
                        "path": "src/two.py",
                        "line": 7,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 8302,
                                    "bodyText": "Second inline request.",
                                    "author": {"login": "reviewer"},
                                    "pullRequestReview": {"databaseId": 8300},
                                }
                            ]
                        },
                    },
                ],
                reviews=[
                    {
                        "databaseId": 8300,
                        "body": "Independent bundle-level request.",
                        "state": "COMMENTED",
                        "author": {"login": "reviewer"},
                    }
                ],
            ),
        )
        client = GitHubClient(fake)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )

        assert [thread.thread_id for thread in status.unresolved_inline_threads] == [
            "T_bundle_anchor",
            "T_bundle_second",
        ]
        contexts = [thread.review_context for thread in status.unresolved_inline_threads]
        assert [context.comment_id for context in contexts if context is not None] == ["8300"]
        assert [comment.comment_id for comment in status.unresolved_review_comments] == ["8300"]

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
    async def test_surfaces_outdated_unresolved_threads_for_resolution(self) -> None:
        """#473: an outdated-but-unresolved thread with external comments is kept
        out of the actionable feed yet surfaced in
        ``outdated_unresolved_inline_threads`` so the monitor can resolve the ones
        it addressed. A resolved-outdated thread appears in neither feed; a live
        thread appears only in the actionable feed."""
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                threads=[
                    {
                        "id": "T_resolved_outdated",
                        "isResolved": True,
                        "isOutdated": True,
                        "path": "a",
                        "line": 1,
                        "comments": {"nodes": [{"bodyText": "done", "author": {"login": "bot"}}]},
                    },
                    {
                        "id": "T_outdated",
                        "isResolved": False,
                        "isOutdated": True,
                        "path": "b",
                        "line": 2,
                        "comments": {
                            "nodes": [
                                {
                                    "bodyText": "fixed elsewhere",
                                    "author": {"login": "greptile"},
                                    "url": "https://github.example/review/9",
                                }
                            ]
                        },
                    },
                    {
                        "id": "T_outdated_only_viewer",
                        "isResolved": False,
                        "isOutdated": True,
                        "path": "c",
                        "line": 3,
                        "comments": {"nodes": [{"bodyText": "self note", "viewerDidAuthor": True}]},
                    },
                    {
                        "id": "T_live",
                        "isResolved": False,
                        "isOutdated": False,
                        "path": "d",
                        "line": 4,
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
        assert [t.thread_id for t in status.outdated_unresolved_inline_threads] == ["T_outdated"]
        outdated = status.outdated_unresolved_inline_threads[0]
        assert outdated.is_outdated is True
        assert outdated.is_resolved is False
        assert outdated.body_excerpt == "fixed elsewhere"
        assert outdated.url == "https://github.example/review/9"

    @pytest.mark.unit
    async def test_paginates_comment_history_for_resolved_and_outdated_threads(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                threads=[
                    {
                        "id": "T_resolved",
                        "isResolved": True,
                        "isOutdated": False,
                        "path": "src/resolved.py",
                        "line": 10,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 311,
                                    "bodyText": "Resolved comment history page one",
                                    "author": {"login": "reviewer-a"},
                                    "viewerDidAuthor": False,
                                    "createdAt": "2026-05-06T10:00:00Z",
                                    "url": "https://github.example/review/311",
                                }
                            ],
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-resolved"},
                        },
                    },
                    {
                        "id": "T_outdated",
                        "isResolved": False,
                        "isOutdated": True,
                        "path": "src/outdated.py",
                        "line": 20,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 321,
                                    "bodyText": "Outdated comment history page one",
                                    "author": {"login": "reviewer-b"},
                                    "viewerDidAuthor": False,
                                    "createdAt": "2026-05-06T10:01:00Z",
                                    "url": "https://github.example/review/321",
                                }
                            ],
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-outdated"},
                        },
                    },
                    {
                        "id": "T_live",
                        "isResolved": False,
                        "isOutdated": False,
                        "path": "src/live.py",
                        "line": 30,
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 331,
                                    "bodyText": "Live thread comment",
                                    "author": {"login": "reviewer-c"},
                                    "viewerDidAuthor": False,
                                    "createdAt": "2026-05-06T10:02:00Z",
                                    "url": "https://github.example/review/331",
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    },
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
                                        "databaseId": 312,
                                        "bodyText": "Resolved comment history page two",
                                        "author": {"login": "reviewer-a"},
                                        "viewerDidAuthor": False,
                                        "createdAt": "2026-05-06T10:10:00Z",
                                        "url": "https://github.example/review/312",
                                    }
                                ],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
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
                        "node": {
                            "comments": {
                                "nodes": [
                                    {
                                        "databaseId": 322,
                                        "bodyText": "Outdated comment history page two",
                                        "author": {"login": "reviewer-b"},
                                        "viewerDidAuthor": False,
                                        "createdAt": "2026-05-06T10:05:00Z",
                                        "url": "https://github.example/review/322",
                                    }
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

        assert [t.thread_id for t in status.unresolved_inline_threads] == ["T_live"]
        assert status.latest_external_review_activity_at is not None
        assert status.latest_external_review_activity_at.isoformat() == "2026-05-06T10:10:00+00:00"
        assert status.latest_external_review_activity_source == "review_thread_comment"
        assert any("threadId=T_resolved" in " ".join(call.args) for call in fake.calls[1:])
        assert any("threadId=T_outdated" in " ".join(call.args) for call in fake.calls[1:])
        for call in fake.calls[1:]:
            query_arg = next(arg for arg in call.args if arg.startswith("query="))
            assert "pullRequestReview" in query_arg
        assert len(fake.calls) == 3

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
    @pytest.mark.parametrize(
        ("closed", "merged", "expected"),
        [
            (False, False, PullRequestLifecycle.open),
            (True, False, PullRequestLifecycle.closed),
            (False, True, PullRequestLifecycle.merged),
            (True, True, PullRequestLifecycle.merged),
        ],
    )
    async def test_pull_request_lifecycle_lookup_fetches_only_lifecycle_state(
        self,
        closed: bool,
        merged: bool,
        expected: PullRequestLifecycle,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                {"data": {"repository": {"pullRequest": {"closed": closed, "merged": merged}}}}
            ),
        )
        client = GitHubClient(fake)

        assert (
            await client.fetch_pull_request_lifecycle(
                repo=RepoRef(owner="o", name="r"), pr_number=1
            )
            is expected
        )
        query_arg = next(arg for arg in fake.calls[0].args if arg.startswith("query="))
        assert "closed" in query_arg
        assert "merged" in query_arg
        assert "reviewThreads" not in query_arg
        assert "statusCheckRollup" not in query_arg

    @pytest.mark.unit
    async def test_pull_request_lifecycle_lookup_reports_missing_pr(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps({"data": {"repository": {"pullRequest": None}}}),
        )
        client = GitHubClient(fake)

        assert (
            await client.fetch_pull_request_lifecycle(
                repo=RepoRef(owner="o", name="r"), pr_number=404
            )
            is PullRequestLifecycle.missing
        )

    @pytest.mark.unit
    async def test_pull_request_snapshot_includes_live_head_ref(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "closed": False,
                                "merged": False,
                                "headRefName": "contributors/live-head",
                                "headRefOid": "a" * 40,
                                "baseRefOid": "b" * 40,
                            }
                        }
                    }
                }
            ),
        )
        client = GitHubClient(fake)

        snapshot = await client.fetch_pull_request_snapshot(
            repo=RepoRef(owner="o", name="r"), pr_number=1
        )

        assert snapshot.lifecycle is PullRequestLifecycle.open
        assert snapshot.head_ref == "contributors/live-head"
        assert snapshot.head_sha == "a" * 40
        assert snapshot.base_sha == "b" * 40
        query_arg = next(arg for arg in fake.calls[0].args if arg.startswith("query="))
        assert "headRefName" in query_arg
        assert "headRefOid" in query_arg
        assert "baseRefOid" in query_arg

    @pytest.mark.unit
    async def test_pull_request_lifecycle_lookup_retries_transient_response(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                {"data": {"repository": {"pullRequest": {"closed": False, "merged": False}}}}
            ),
        )
        sleep = _RecordedSleep()
        client = GitHubClient(fake, sleep=sleep)

        assert (
            await client.fetch_pull_request_lifecycle(
                repo=RepoRef(owner="o", name="r"), pr_number=1
            )
            is PullRequestLifecycle.open
        )
        assert len(fake.calls) == 2
        assert len(sleep.calls) == 1

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
            # retry=False (the pre-merge-recheck contract) surfaces the single
            # failure directly; with the polling default the transport would retry
            # this transient in-cycle rather than raise on the first attempt.
            await client.fetch_pr_status(
                repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0, retry=False
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
    async def test_transient_graphql_payload_error_retries_then_succeeds(self) -> None:
        # Regression (PR #729): a GraphQL HTTP-200 response carrying a transient
        # errors array ("something went wrong") makes gh exit 0, so the payload
        # error previously bypassed the transport retry. Under retry=True it must
        # now be retried in-cycle like a stderr-surfaced transient blip.
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                {"errors": [{"message": "something went wrong while executing your query"}]}
            ),
        )
        fake.queue_result(returncode=0, stdout=_sample_pr_payload())
        sleep = _RecordedSleep()
        client = GitHubClient(fake, sleep=sleep)

        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            base_behind_count=0,
        )

        assert status.number == 42
        assert len(fake.calls) == 2  # transient payload retried after backoff
        assert len(sleep.calls) == 1

    @pytest.mark.unit
    async def test_transient_graphql_payload_error_fails_fast_when_no_retry(self) -> None:
        # The pre-merge recheck passes retry=False: a transient payload error must
        # raise on the first attempt (no transport backoff) so the merge critical
        # section fails fast, mirroring the stderr-surfaced transient contract.
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps({"errors": [{"message": "something went wrong"}]}),
        )
        sleep = _RecordedSleep()
        client = GitHubClient(fake, sleep=sleep)

        with pytest.raises(GitHubClientError, match="something went wrong"):
            await client.fetch_pr_status(
                repo=RepoRef(owner="o", name="r"),
                pr_number=1,
                base_behind_count=0,
                retry=False,
            )

        assert len(fake.calls) == 1  # no in-cycle retry under retry=False
        assert sleep.calls == []

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


class TestTransientGraphqlPayloadError:
    """Unit contract for the ``_graphql`` transport response-validator hook."""

    @staticmethod
    def _result(stdout: str) -> CommandResult:
        return CommandResult(returncode=0, stdout=stdout, stderr="")

    @pytest.mark.unit
    def test_transient_errors_payload_returns_text(self) -> None:
        text = _transient_graphql_payload_error(
            self._result(json.dumps({"errors": [{"message": "something went wrong"}]}))
        )
        assert text is not None and "something went wrong" in text

    @pytest.mark.unit
    def test_permanent_errors_payload_returns_none(self) -> None:
        assert (
            _transient_graphql_payload_error(
                self._result(json.dumps({"errors": [{"message": "could not resolve to a node"}]}))
            )
            is None
        )

    @pytest.mark.unit
    def test_non_dict_and_clean_payloads_return_none(self) -> None:
        # A non-object JSON body (defensive) and a clean payload both pass through.
        assert _transient_graphql_payload_error(self._result("[1, 2, 3]")) is None
        assert (
            _transient_graphql_payload_error(self._result(json.dumps({"data": {"ok": True}})))
            is None
        )

    @pytest.mark.unit
    def test_malformed_json_returns_none(self) -> None:
        # Malformed bodies are left to _graphql's own JSON decode + raise.
        assert _transient_graphql_payload_error(self._result("not json")) is None


class TestCreatePullRequestUrlValidation:
    """``create_pull_request`` stdout-URL extraction and validation.

    Split out of ``test_github_client_part_004.py`` to keep each first-party test
    file under the maintainability line limit.
    """

    @pytest.mark.unit
    async def test_create_pull_request_extracts_url_from_noisy_stdout(self) -> None:
        # gh may prefix the URL with status noise on stdout; we extract the URL
        # rather than returning the whole line verbatim.
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=(
                "Creating pull request for development into main in o/r\n"
                "https://github.com/o/r/pull/777\n"
            ),
        )
        client = GitHubClient(fake)
        url = await client.create_pull_request(
            repo=RepoRef(owner="o", name="r"),
            base="main",
            head="development",
            title="t",
            body="b",
        )
        assert url == "https://github.com/o/r/pull/777"

    @pytest.mark.unit
    async def test_create_pull_request_raises_when_stdout_has_no_url(self) -> None:
        # gh exits 0 but prints only warning/status text — must raise rather than
        # persist a non-URL string that breaks downstream PR-number extraction.
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout="Warning: 1 uncommitted change\n",
        )
        client = GitHubClient(fake)
        with pytest.raises(GitHubClientError) as exc:
            await client.create_pull_request(
                repo=RepoRef(owner="o", name="r"),
                base="main",
                head="development",
                title="t",
                body="b",
            )
        assert exc.value.operation == "gh pr create (no URL in stdout)"
        assert "unexpected gh output" in str(exc.value)
