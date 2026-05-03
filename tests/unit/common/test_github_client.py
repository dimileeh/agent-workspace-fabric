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
    PullRequestMetadataError,
    RepoRef,
    fetch_pull_request_adoption_metadata,
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
            ("https://github.com/dimileeh/aira-agent.git", "dimileeh/aira-agent"),
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


def _adoption_pr_payload(
    *,
    number: int = 277,
    head_ref: str = "feature/head",
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
        assert "headRefOid" in fields
        assert "baseRefOid" in fields
        assert "closed" not in fields
        assert "merged" not in fields

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "state, reason_code",
        [
            ("CLOSED", "PR_ALREADY_CLOSED"),
            ("MERGED", "PR_ALREADY_MERGED"),
        ],
    )
    async def test_terminal_prs_raise_structured_reason(
        self,
        state: str,
        reason_code: str,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=_adoption_pr_payload(state=state))

        with pytest.raises(PullRequestMetadataError) as excinfo:
            await fetch_pull_request_adoption_metadata(
                runner=fake,
                repo=RepoRef(owner="dimileeh", name="aira-web"),
                pr_number=277,
            )

        assert excinfo.value.reason_code == reason_code

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
    reviews: list[dict] | None = None,
    comments: list[dict] | None = None,
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
                        "reviewThreads": {"nodes": threads or []},
                        "reviews": {"nodes": reviews or []},
                        "comments": {"nodes": comments or []},
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
                                {"bodyText": "rename this", "author": {"login": "coderabbit"}}
                            ]
                        },
                    }
                ],
                reviews=[
                    {
                        "databaseId": 9001,
                        "body": "Summary with suggestions",
                        "state": "COMMENTED",
                        "author": {"login": "coderabbit"},
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
        assert t.author == "coderabbit"
        assert len(status.unresolved_review_comments) == 1
        c = status.unresolved_review_comments[0]
        assert c.comment_id == "9001"
        assert c.body_excerpt == "Summary with suggestions"
        assert c.blocks_merge is False

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
    async def test_ignores_non_actionable_review_disabled_issue_comment(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                comments=[
                    {
                        "databaseId": 77,
                        "body": (
                            "> [!IMPORTANT]\n"
                            "> ## Review skipped\n\n"
                            "Auto reviews are disabled on base/target branches "
                            "other than the configured development branch.\n\n"
                            "- [ ] Trigger review"
                        ),
                        "isMinimized": False,
                        "author": {"login": "coderabbitai"},
                    }
                ]
            ),
        )
        client = GitHubClient(fake)
        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )
        assert status.unresolved_review_comments == ()

    @pytest.mark.unit
    async def test_parses_actionable_trigger_review_issue_comment_as_blocking(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                comments=[
                    {
                        "databaseId": 77,
                        "body": (
                            "> [!IMPORTANT]\n"
                            "> ## Review skipped\n\n"
                            "Required review was skipped. Trigger review before merging.\n\n"
                            "- [ ] Trigger review"
                        ),
                        "isMinimized": False,
                        "author": {"login": "coderabbitai"},
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
        assert c.comment_id == "issue:77"
        assert c.author == "coderabbitai"
        assert "Review skipped" in c.body_excerpt
        assert c.blocks_merge is True

    @pytest.mark.unit
    async def test_ignores_non_blocking_bot_issue_comments(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                comments=[
                    {
                        "databaseId": 78,
                        "body": "Finishing touches and review summary.",
                        "isMinimized": False,
                        "author": {"login": "coderabbitai"},
                    }
                ]
            ),
        )
        client = GitHubClient(fake)
        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )
        assert status.unresolved_review_comments == ()

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
    async def test_ignores_non_actionable_bot_review_summaries(self, body: str) -> None:
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
        assert status.unresolved_review_comments == ()

    @pytest.mark.unit
    async def test_ignores_codex_review_envelope_when_inline_thread_is_actionable(
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
                            "- Comment \"@codex review\".\n\n"
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

        assert [t.thread_id for t in status.unresolved_inline_threads] == [
            "PRRT_kwDOSJAM6s5_H5DV"
        ]
        assert status.unresolved_review_comments == ()

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
    async def test_ignores_awf_status_issue_comments(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                comments=[
                    {
                        "databaseId": 780,
                        "body": (
                            "PR #18 needs human attention at commit `abc123`.\n\n"
                            "AWF did not auto-merge because a review bot reported "
                            "that review was skipped or left a trigger-review "
                            "checklist unresolved.\n\n"
                            "After the blocker is cleared or a new commit lands, "
                            "AWF will re-verify the PR before taking any merge action."
                        ),
                        "isMinimized": False,
                        "author": {"login": "dimileeh"},
                    }
                ]
            ),
        )
        client = GitHubClient(fake)
        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )
        assert status.unresolved_review_comments == ()

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "body",
        [
            "fixed in commit 831a9ff17936915968882306dd6ee32b47cc909f",
            "FALSE POSITIVE: the reviewer was looking at pre-refactor code.",
            (
                "FALSE POSITIVE on comment 4355744677: The hardcoded "
                "`agent_readiness` was already replaced by commit 579cf6d. "
                "The current code dynamically computes agent readiness."
            ),
            (
                "Review-level comment 4207199736 confirmed resolved: the weak "
                "`-> Any` return type was tightened to `-> Operation` in commit 2421e80."
            ),
            (
                "No further action required. The sole actionable item from review "
                "4207859399 was already fixed in commit 8bfd188d0f."
            ),
            "DEFER: needs maintainer input before changing API behavior.",
        ],
    )
    async def test_ignores_awf_resolution_issue_comments(self, body: str) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=_sample_pr_payload(
                comments=[
                    {
                        "databaseId": 781,
                        "body": body,
                        "isMinimized": False,
                        "author": {"login": "dimileeh"},
                    }
                ]
            ),
        )
        client = GitHubClient(fake)
        status = await client.fetch_pr_status(
            repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
        )
        assert status.unresolved_review_comments == ()

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
        assert any(a.startswith("query=") and "pageInfo { hasNextPage endCursor }" in a for a in args)
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
