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
from awf.runtime.pr_monitor import CheckState, CheckTiming


def _run_view_calls(fake: FakeCommandRunner) -> list[list[str]]:
    return [call.args for call in fake.calls if call.args[:3] == ["gh", "run", "view"]]


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
        # mirrors the Bitbucket
        # ``test_fetch_pr_status_statuses_fetch_failure_raises_not_no_checks``.
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="rate limited")
        client = GitHubClient(fake)
        with pytest.raises(GitHubClientError):
            await client.fetch_pr_status(
                repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
            )


class TestFetchFailingCheckLogsRollupFallback:
    @pytest.mark.unit
    async def test_rollup_details_url_fetches_missing_actions_run(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")
        fake.queue_result(
            returncode=0,
            stdout=(
                "python-coverage-shards (7)\tPull postgres\t/usr/bin/docker pull postgres:16\n"
                "context deadline exceeded\n"
                "Docker pull failed with exit code 1\n"
            ),
        )
        client = GitHubClient(fake)

        failures, runs_in_progress = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
            rollup_checks=(
                CheckTiming(
                    name="python-coverage-shards (7)",
                    conclusion="FAILURE",
                    details_url="https://github.com/o/r/actions/runs/123/job/456",
                    app_slug="github-actions",
                ),
            ),
        )

        assert len(failures) == 1
        assert failures[0].name == "python-coverage-shards (7)"
        assert failures[0].run_id == "123"
        assert "Docker pull failed" in failures[0].log_excerpt
        assert runs_in_progress is False
        assert _run_view_calls(fake) == [
            ["gh", "run", "view", "123", "--repo", "o/r", "--log-failed"]
        ]

    @pytest.mark.unit
    async def test_rollup_fallback_dedupes_run_list_results(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 123,
                        "name": "python-coverage-shards (7)",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        fake.queue_result(returncode=0, stdout="HTTP status server error (502 Bad Gateway)")
        client = GitHubClient(fake)

        failures, runs_in_progress = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
            rollup_checks=(
                CheckTiming(
                    name="python-coverage-shards (7)",
                    conclusion="FAILURE",
                    details_url="https://github.com/o/r/actions/runs/123/job/456",
                    app_slug="github-actions",
                ),
            ),
        )

        assert len(failures) == 1
        assert runs_in_progress is False
        assert len(_run_view_calls(fake)) == 1

    @pytest.mark.unit
    async def test_rollup_fallback_skips_in_progress_actions_run_without_fetching_log(
        self,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 123,
                        "name": "python-coverage-shards (7)",
                        "conclusion": "",
                        "status": "in_progress",
                    }
                ]
            ),
        )
        client = GitHubClient(fake)

        failures, runs_in_progress = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
            rollup_checks=(
                CheckTiming(
                    name="python-coverage-shards (7)",
                    conclusion="FAILURE",
                    details_url="https://github.com/o/r/actions/runs/123/job/456",
                    app_slug="github-actions",
                ),
            ),
        )

        assert failures == ()
        assert runs_in_progress is True
        assert _run_view_calls(fake) == []

    @pytest.mark.unit
    async def test_run_list_skips_failed_in_progress_run_without_fetching_log(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 124,
                        "name": "python-coverage-shards (8)",
                        "conclusion": "FAILURE",
                        "status": "in_progress",
                    }
                ]
            ),
        )
        client = GitHubClient(fake)

        failures, runs_in_progress = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
        )

        assert failures == ()
        assert runs_in_progress is True
        assert _run_view_calls(fake) == []

    @pytest.mark.unit
    async def test_completed_empty_log_uses_paginated_check_run_annotations_fallback(
        self,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 125,
                        "name": "lint-and-type",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        fake.queue_result(returncode=0, stdout="")
        fake.queue_result(
            returncode=0,
            stdout=json.dumps({"check_run_url": "https://api.github.com/repos/o/r/check-runs/456"}),
        )
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    [
                        {
                            "path": "src/awf/runtime/foo.py",
                            "start_line": 10,
                            "annotation_level": "failure",
                            "message": "F401 imported but unused",
                            "raw_details": "Error: Process completed with exit code 1.",
                        }
                    ],
                    [
                        {
                            "path": "src/awf/runtime/bar.py",
                            "start_line": 22,
                            "start_column": 7,
                            "annotation_level": "failure",
                            "message": "E501 line too long",
                        }
                    ],
                ]
            ),
        )
        client = GitHubClient(fake)

        failures, runs_in_progress = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
            rollup_checks=(
                CheckTiming(
                    name="lint-and-type",
                    conclusion="FAILURE",
                    details_url="https://github.com/o/r/actions/runs/125/job/789",
                    app_slug="github-actions",
                ),
            ),
        )

        assert runs_in_progress is False
        assert len(failures) == 1
        assert "src/awf/runtime/foo.py:10:1: F401 imported but unused" in failures[0].log_excerpt
        assert "src/awf/runtime/bar.py:22:7: E501 line too long" in failures[0].log_excerpt
        assert "Process completed with exit code 1" in failures[0].log_excerpt
        assert any("F401 imported" in item for item in failures[0].error_summaries)
        assert any(
            call.args
            == [
                "gh",
                "api",
                "repos/o/r/actions/jobs/789",
            ]
            for call in fake.calls
        )
        assert any(
            call.args
            == [
                "gh",
                "api",
                "repos/o/r/check-runs/456/annotations",
                "--paginate",
                "--slurp",
            ]
            for call in fake.calls
        )

    @pytest.mark.unit
    async def test_completed_empty_log_fetches_annotations_for_each_failed_shard(
        self,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 125,
                        "name": "python-coverage-shards",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        fake.queue_result(returncode=0, stdout="")
        fake.queue_result(
            returncode=0,
            stdout=json.dumps({"check_run_url": "https://api.github.com/repos/o/r/check-runs/456"}),
        )
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    [
                        {
                            "path": "tests/unit/test_alpha.py",
                            "start_line": 11,
                            "annotation_level": "failure",
                            "message": "alpha shard failed",
                        }
                    ]
                ]
            ),
        )
        fake.queue_result(
            returncode=0,
            stdout=json.dumps({"check_run_url": "https://api.github.com/repos/o/r/check-runs/457"}),
        )
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    [
                        {
                            "path": "tests/unit/test_beta.py",
                            "start_line": 22,
                            "annotation_level": "failure",
                            "message": "beta shard failed",
                        }
                    ]
                ]
            ),
        )
        client = GitHubClient(fake)

        failures, runs_in_progress = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
            rollup_checks=(
                CheckTiming(
                    name="python-coverage-shards (1)",
                    conclusion="FAILURE",
                    details_url="https://github.com/o/r/actions/runs/125/job/789",
                    app_slug="github-actions",
                ),
                CheckTiming(
                    name="python-coverage-shards (2)",
                    conclusion="FAILURE",
                    details_url="https://github.com/o/r/actions/runs/125/job/790",
                    app_slug="github-actions",
                ),
            ),
        )

        assert runs_in_progress is False
        assert len(failures) == 1
        assert "tests/unit/test_alpha.py:11:1: alpha shard failed" in failures[0].log_excerpt
        assert "tests/unit/test_beta.py:22:1: beta shard failed" in failures[0].log_excerpt
        assert [
            call.args[2]
            for call in fake.calls
            if call.args[:2] == ["gh", "api"] and call.args[2].endswith("/annotations")
        ] == [
            "repos/o/r/check-runs/456/annotations",
            "repos/o/r/check-runs/457/annotations",
        ]

    @pytest.mark.unit
    async def test_rollup_fallback_ignores_non_actions_evidence(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")
        client = GitHubClient(fake)

        failures, runs_in_progress = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
            rollup_checks=(
                CheckTiming(
                    name="status-context",
                    status="FAILURE",
                    details_url="https://github.com/o/r/actions/runs/123/job/456",
                ),
                CheckTiming(
                    name="third-party",
                    conclusion="FAILURE",
                    details_url="https://github.com/o/r/actions/runs/124/job/456",
                    app_slug="codecov",
                ),
                CheckTiming(
                    name="external",
                    conclusion="FAILURE",
                    details_url="https://ci.example.test/build/1",
                    app_slug="github-actions",
                ),
                CheckTiming(
                    name="green-actions",
                    conclusion="SUCCESS",
                    details_url="https://github.com/o/r/actions/runs/125/job/456",
                    app_slug="github-actions",
                ),
                CheckTiming(
                    name="malformed-actions",
                    conclusion="FAILURE",
                    details_url="https://github.com/o/r/actions/runs/126",
                    app_slug="github-actions",
                ),
                CheckTiming(
                    name="actions-without-details-url",
                    conclusion="FAILURE",
                    details_url=None,
                    app_slug="github-actions",
                ),
            ),
        )

        assert failures == ()
        assert runs_in_progress is False
        assert _run_view_calls(fake) == []

    @pytest.mark.unit
    async def test_rollup_fallback_preserves_warning_when_log_unavailable(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")
        fake.queue_result(returncode=1, stderr="gh network timeout with ghp_secret")
        client = GitHubClient(fake)

        failures, runs_in_progress = await client.fetch_failing_check_logs(
            repo=RepoRef(owner="o", name="r"),
            pr_number=1,
            head_sha="abc",
            rollup_checks=(
                CheckTiming(
                    name="python-coverage-shards (7)",
                    conclusion="FAILURE",
                    details_url="https://github.com/o/r/actions/runs/123/job/456",
                    app_slug="github-actions",
                ),
            ),
        )

        assert len(failures) == 1
        assert runs_in_progress is False
        assert failures[0].log_excerpt == ""
        assert failures[0].evidence_warnings == (
            "GitHub Actions log unavailable for failed check python-coverage-shards (7).",
        )
