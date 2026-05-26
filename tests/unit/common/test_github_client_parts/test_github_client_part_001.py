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
    BranchOpenPullRequestResolver,
    PullRequestMetadataError,
    RepoRef,
    fetch_pull_request_adoption_metadata,
    list_open_pull_requests_for_branch,
    parse_github_pull_request_url,
)


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
    def test_rejects_non_github_pr_url(self) -> None:
        with pytest.raises(ValueError):
            parse_github_pull_request_url("https://gitlab.com/dimileeh/aira-web/pull/277")

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

    @pytest.mark.unit
    async def test_invalid_head_repository_name_with_owner_is_invalid(self) -> None:
        fake = FakeCommandRunner()
        payload = json.loads(_adoption_pr_payload(head_repo_slug="contributor/aira-web"))
        payload["headRepository"] = {"nameWithOwner": "not a repo"}
        fake.queue_result(returncode=0, stdout=json.dumps(payload))

        with pytest.raises(PullRequestMetadataError) as excinfo:
            await fetch_pull_request_adoption_metadata(
                runner=fake,
                repo=RepoRef(owner="dimileeh", name="aira-web"),
                pr_number=277,
            )

        assert excinfo.value.reason_code == "PR_METADATA_INVALID"
        assert excinfo.value.detail["field"] == "headRepository.nameWithOwner"

    @pytest.mark.unit
    async def test_cross_repository_pr_without_head_repository_identity_is_invalid(self) -> None:
        fake = FakeCommandRunner()
        payload = json.loads(_adoption_pr_payload(head_repo_slug="contributor/aira-web"))
        payload["headRepository"] = None
        fake.queue_result(returncode=0, stdout=json.dumps(payload))

        with pytest.raises(PullRequestMetadataError) as excinfo:
            await fetch_pull_request_adoption_metadata(
                runner=fake,
                repo=RepoRef(owner="dimileeh", name="aira-web"),
                pr_number=277,
            )

        assert excinfo.value.reason_code == "PR_METADATA_INVALID"
        assert excinfo.value.detail["field"] == "headRepository"

    @pytest.mark.unit
    async def test_non_cross_repository_pr_without_head_repository_uses_base_repo(self) -> None:
        fake = FakeCommandRunner()
        payload = json.loads(_adoption_pr_payload())
        payload["headRepository"] = None
        payload["isCrossRepository"] = False
        fake.queue_result(returncode=0, stdout=json.dumps(payload))

        metadata = await fetch_pull_request_adoption_metadata(
            runner=fake,
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=277,
        )

        assert metadata.head_repo_slug == "dimileeh/aira-web"


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
    async def test_blank_branch_returns_no_matches_without_gh_call(self) -> None:
        fake = FakeCommandRunner()

        matches = await list_open_pull_requests_for_branch(
            runner=fake,
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            branch_name="  ",
        )

        assert matches == []
        assert fake.calls == []

    @pytest.mark.unit
    async def test_gh_pr_list_failure_raises_lookup_failed(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="rate limit")

        with pytest.raises(PullRequestMetadataError) as excinfo:
            await list_open_pull_requests_for_branch(
                runner=fake,
                repo=RepoRef(owner="dimileeh", name="aira-web"),
                branch_name="feature/head",
                base_branch=" main ",
            )

        assert excinfo.value.reason_code == "OPEN_PR_LOOKUP_FAILED"
        assert excinfo.value.detail["base_branch"] == " main "

    @pytest.mark.unit
    async def test_gh_pr_list_invalid_json_raises_lookup_invalid(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="{not json")

        with pytest.raises(PullRequestMetadataError) as excinfo:
            await list_open_pull_requests_for_branch(
                runner=fake,
                repo=RepoRef(owner="dimileeh", name="aira-web"),
                branch_name="feature/head",
            )

        assert excinfo.value.reason_code == "OPEN_PR_LOOKUP_INVALID"

    @pytest.mark.unit
    async def test_gh_pr_list_non_list_json_raises_lookup_invalid(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=json.dumps({"items": []}))

        with pytest.raises(PullRequestMetadataError) as excinfo:
            await list_open_pull_requests_for_branch(
                runner=fake,
                repo=RepoRef(owner="dimileeh", name="aira-web"),
                branch_name="feature/head",
            )

        assert excinfo.value.reason_code == "OPEN_PR_LOOKUP_INVALID"

    @pytest.mark.unit
    async def test_non_object_pr_list_item_is_invalid(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=json.dumps(["not-object"]))

        with pytest.raises(PullRequestMetadataError) as excinfo:
            await list_open_pull_requests_for_branch(
                runner=fake,
                repo=RepoRef(owner="dimileeh", name="aira-web"),
                branch_name="feature/head",
            )

        assert excinfo.value.reason_code == "OPEN_PR_LOOKUP_INVALID"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "payload",
        [
            {
                "number": 0,
                "url": "https://github.com/dimileeh/aira-web/pull/277",
                "headRepository": {"nameWithOwner": "dimileeh/aira-web"},
            },
            {
                "number": 277,
                "url": " ",
                "headRepository": {"nameWithOwner": "dimileeh/aira-web"},
            },
        ],
    )
    async def test_invalid_pr_list_number_or_url_is_invalid(
        self, payload: dict[str, object]
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=json.dumps([payload]))

        with pytest.raises(PullRequestMetadataError) as excinfo:
            await list_open_pull_requests_for_branch(
                runner=fake,
                repo=RepoRef(owner="dimileeh", name="aira-web"),
                branch_name="feature/head",
            )

        assert excinfo.value.reason_code == "OPEN_PR_LOOKUP_INVALID"

    @pytest.mark.unit
    async def test_all_malformed_pr_list_items_raise_aggregated_parse_context(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "number": 277,
                        "url": "https://github.com/dimileeh/aira-web/pull/277",
                        "headRepository": None,
                    },
                    {
                        "number": 278,
                        "url": None,
                        "headRepository": {"nameWithOwner": "dimileeh/aira-web"},
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
        assert excinfo.value.detail is not None
        assert excinfo.value.detail["failure_count"] == 2
        assert excinfo.value.detail["failures"] == [
            {
                "item_index": 0,
                "reason_code": "OPEN_PR_LOOKUP_INVALID",
                "error": "gh pr list payload missing required headRepository identity.",
            },
            {
                "item_index": 1,
                "reason_code": "OPEN_PR_LOOKUP_INVALID",
                "error": "gh pr list payload missing required field: url",
            },
        ]
        aggregate_event = next(
            (item for item in captured if item.get("event") == "github.open_pr_batch_parse_failed"),
            None,
        )
        assert aggregate_event is not None
        assert aggregate_event.get("log_level") == "warning"
        assert aggregate_event.get("failure_count") == 2
        assert aggregate_event.get("failures") == excinfo.value.detail["failures"]

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "owner_payload",
        [
            {"headRepositoryOwner": {"login": "contributor"}},
            {"headRepositoryOwner": "contributor"},
            {"headRepository": {"owner": {"login": "contributor"}}},
        ],
    )
    async def test_head_repository_slug_falls_back_to_owner_and_name(
        self,
        owner_payload: dict[str, object],
    ) -> None:
        fake = FakeCommandRunner()
        payload = {
            "number": 277,
            "url": "https://github.com/dimileeh/aira-web/pull/277",
            "headRepository": {"name": "aira-web"},
        }
        if "headRepository" in owner_payload:
            payload["headRepository"] = {
                **payload["headRepository"],  # type: ignore[arg-type]
                **owner_payload["headRepository"],  # type: ignore[index]
            }
        else:
            payload.update(owner_payload)
        fake.queue_result(returncode=0, stdout=json.dumps([payload]))

        matches = await list_open_pull_requests_for_branch(
            runner=fake,
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            branch_name="feature/head",
        )

        assert matches[0].head_repo_slug == "contributor/aira-web"

    @pytest.mark.unit
    async def test_invalid_pr_list_head_repository_slug_is_invalid(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "number": 277,
                        "url": "https://github.com/dimileeh/aira-web/pull/277",
                        "headRepository": {"nameWithOwner": "not a repo"},
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
        assert excinfo.value.detail["field"] == "headRepository.nameWithOwner"

    @pytest.mark.unit
    async def test_mixed_malformed_and_parseable_items_returns_parseable_matches(self) -> None:
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

        with structlog.testing.capture_logs() as captured:
            matches = await list_open_pull_requests_for_branch(
                runner=fake,
                repo=RepoRef(owner="dimileeh", name="aira-web"),
                branch_name="feature/head",
            )

        assert len(matches) == 1
        assert matches[0].number == 278
        assert matches[0].url == "https://github.com/dimileeh/aira-web/pull/278"
        assert matches[0].head_ref == "feature/head"
        assert matches[0].head_repo_slug == "dimileeh/aira-web"
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
    async def test_valid_repo_url_delegates_to_branch_lookup(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")
        resolver = BranchOpenPullRequestResolver(fake)

        resolved = await resolver.resolve(
            repo_url="https://github.com/dimileeh/aira-web.git",
            branch_name="feature/head",
            base_branch="main",
        )

        assert resolved == []
        assert fake.calls[0].args[:3] == ["gh", "pr", "list"]

    @pytest.mark.unit
    async def test_invalid_repo_url_raises_lookup_invalid_and_warns(self) -> None:
        fake = FakeCommandRunner()
        resolver = BranchOpenPullRequestResolver(fake)
        repo_url = "https://x-access-token:secret-token@github.com/dimileeh"

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(PullRequestMetadataError) as excinfo,
        ):
            await resolver.resolve(
                repo_url=repo_url,
                branch_name="feature/head",
                base_branch="main",
            )

        assert excinfo.value.reason_code == "OPEN_PR_LOOKUP_INVALID"
        assert "secret-token" not in excinfo.value.message
        assert excinfo.value.detail == {
            "repo_url": "https://[redacted]@github.com/dimileeh",
            "branch_name": "feature/head",
            "base_branch": "main",
        }
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
