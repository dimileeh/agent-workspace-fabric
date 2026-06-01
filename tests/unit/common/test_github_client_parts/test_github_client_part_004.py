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

from awf.common import github_client_parsing as github_client_module
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import (
    GitHubClient,
    GitHubClientError,
    RepoRef,
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
    async def test_create_pull_request_argv_and_url(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout="https://github.com/o/r/pull/321\n",
        )
        client = GitHubClient(fake)

        url = await client.create_pull_request(
            repo=RepoRef(owner="o", name="r"),
            base="main",
            head="development",
            title="Release: merge development into main",
            body="auto release sync",
        )

        args = fake.calls[0].args
        assert args[:3] == ["gh", "pr", "create"]
        assert "--repo" in args and "o/r" in args
        assert args[args.index("--base") + 1] == "main"
        assert args[args.index("--head") + 1] == "development"
        assert args[args.index("--title") + 1] == "Release: merge development into main"
        assert args[args.index("--body") + 1] == "auto release sync"
        assert url == "https://github.com/o/r/pull/321"

    @pytest.mark.unit
    async def test_create_pull_request_raises_on_error(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="no commits between main and development")
        client = GitHubClient(fake)
        with pytest.raises(GitHubClientError) as exc:
            await client.create_pull_request(
                repo=RepoRef(owner="o", name="r"),
                base="main",
                head="development",
                title="t",
                body="b",
            )
        assert "no commits between" in str(exc.value)

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

    @pytest.mark.unit
    async def test_fetch_repo_merge_methods_reads_repo_flags(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=(
                '{"allow_merge_commit":true,"allow_squash_merge":false,"allow_rebase_merge":true}'
            ),
        )
        client = GitHubClient(fake)

        methods = await client.fetch_repo_merge_methods(repo=RepoRef(owner="o", name="r"))

        assert methods == ("merge", "rebase")
        assert fake.calls[0].args == ["gh", "api", "repos/o/r"]

    @pytest.mark.unit
    async def test_fetch_branch_pull_request_allowed_merge_methods_empty_unconstrained(
        self,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")
        client = GitHubClient(fake)

        methods = await client.fetch_branch_pull_request_allowed_merge_methods(
            repo=RepoRef(owner="o", name="r"),
            branch="feature/dev",
        )

        assert methods is None
        assert fake.calls[0].args == ["gh", "api", "repos/o/r/rules/branches/feature%2Fdev"]

    @pytest.mark.unit
    async def test_fetch_branch_pull_request_allowed_merge_methods_ignores_non_pr_rules(
        self,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout='[{"type":"required_status_checks","parameters":{}}]',
        )
        client = GitHubClient(fake)

        methods = await client.fetch_branch_pull_request_allowed_merge_methods(
            repo=RepoRef(owner="o", name="r"),
            branch="main",
        )

        assert methods is None

    @pytest.mark.unit
    async def test_fetch_branch_pull_request_allowed_merge_methods_omitted_methods_unconstrained(
        self,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=('[{"type":"pull_request","parameters":{"required_approving_review_count":1}}]'),
        )
        client = GitHubClient(fake)

        methods = await client.fetch_branch_pull_request_allowed_merge_methods(
            repo=RepoRef(owner="o", name="r"),
            branch="main",
        )

        # GitHub omits allowed_merge_methods when the pull_request rule does
        # not constrain merge method choice, so the runner falls back to repo
        # merge flags instead of treating the rule as an empty method set.
        assert methods is None

    @pytest.mark.unit
    async def test_fetch_branch_pull_request_allowed_merge_methods_unknown_only_unconstrained(
        self,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=(
                '[{"type":"pull_request","parameters":'
                '{"allowed_merge_methods":["fast_forward","manual"]}}]'
            ),
        )
        client = GitHubClient(fake)

        methods = await client.fetch_branch_pull_request_allowed_merge_methods(
            repo=RepoRef(owner="o", name="r"),
            branch="main",
        )

        assert methods is None

    @pytest.mark.unit
    async def test_fetch_branch_pull_request_allowed_merge_methods_normalizes_values(
        self,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=(
                '[{"type":"pull_request","parameters":'
                '{"allowed_merge_methods":["merge","squash","rebase","invalid"]}}]'
            ),
        )
        client = GitHubClient(fake)

        methods = await client.fetch_branch_pull_request_allowed_merge_methods(
            repo=RepoRef(owner="o", name="r"),
            branch="main",
        )

        assert methods == ("merge", "squash", "rebase")

    @pytest.mark.unit
    async def test_fetch_branch_pull_request_allowed_merge_methods_intersects_multiple_rules(
        self,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=(
                "["
                '{"type":"pull_request","parameters":{"allowed_merge_methods":["merge","squash"]}},'
                '{"type":"pull_request","parameters":{"allowed_merge_methods":["merge","rebase"]}}'
                "]"
            ),
        )
        client = GitHubClient(fake)

        methods = await client.fetch_branch_pull_request_allowed_merge_methods(
            repo=RepoRef(owner="o", name="r"),
            branch="main",
        )

        assert methods == ("merge",)

    @pytest.mark.unit
    async def test_fetch_branch_pull_request_allowed_merge_methods_raises_on_gh_error(
        self,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="bad credentials with token secret")
        client = GitHubClient(fake)

        with pytest.raises(GitHubClientError) as exc:
            await client.fetch_branch_pull_request_allowed_merge_methods(
                repo=RepoRef(owner="o", name="r"),
                branch="main",
            )

        assert "bad credentials" in str(exc.value)

    @pytest.mark.unit
    async def test_fetch_branch_pull_request_allowed_merge_methods_redacts_gh_error_secret(
        self,
    ) -> None:
        secret = "ghp_branchRulesSecret123"
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr=f"HTTP 403 token {secret} denied")
        client = GitHubClient(fake)

        with pytest.raises(GitHubClientError) as exc:
            await client.fetch_branch_pull_request_allowed_merge_methods(
                repo=RepoRef(owner="o", name="r"),
                branch="main",
            )

        assert secret not in exc.value.stderr
        assert secret not in str(exc.value)
        assert "[redacted]" in exc.value.stderr

    @pytest.mark.unit
    async def test_fetch_branch_pull_request_allowed_merge_methods_raises_on_bad_json(
        self,
    ) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="{bad json")
        client = GitHubClient(fake)

        with pytest.raises(GitHubClientError) as exc:
            await client.fetch_branch_pull_request_allowed_merge_methods(
                repo=RepoRef(owner="o", name="r"),
                branch="main",
            )

        assert "json parse" in str(exc.value)

    @pytest.mark.unit
    async def test_fetch_branch_pull_request_allowed_merge_methods_redacts_bad_json_secret(
        self,
    ) -> None:
        secret = "ghp_branchRulesSecret123"
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=f"{{bad json {secret}")
        client = GitHubClient(fake)

        with pytest.raises(GitHubClientError) as exc:
            await client.fetch_branch_pull_request_allowed_merge_methods(
                repo=RepoRef(owner="o", name="r"),
                branch="main",
            )

        assert "json parse" in str(exc.value)
        assert secret not in exc.value.stderr
        assert secret not in str(exc.value)
        assert "[redacted]" in exc.value.stderr


class TestPrivateCoverageEdges:
    @pytest.mark.unit
    async def test_gh_json_and_run_gh_raise_on_strict_failures(self) -> None:
        fake = FakeCommandRunner()
        fake.queue_result(returncode=1, stderr="boom")
        fake.queue_result(returncode=1, stderr="strict boom")
        fake.queue_result(returncode=1, stderr="non-strict")
        client = GitHubClient(fake)

        with pytest.raises(GitHubClientError) as json_exc:
            await client._gh_json(["gh", "api", "repos/o/r"], operation="gh api")  # noqa: SLF001
        with pytest.raises(GitHubClientError) as run_exc:
            await client._run_gh(  # noqa: SLF001
                ["gh", "pr", "view"],
                operation="gh pr view",
                strict=True,
            )
        result = await client._run_gh(  # noqa: SLF001
            ["gh", "pr", "view"],
            operation="gh pr view",
            strict=False,
        )

        assert json_exc.value.operation == "gh api"
        assert run_exc.value.operation == "gh pr view"
        assert result.returncode == 1

    @pytest.mark.unit
    def test_private_nested_payload_and_review_helpers_cover_fallbacks(self) -> None:
        assert github_client_module._dig([{"name": "first"}], 1, "name") is None  # noqa: SLF001
        assert github_client_module._dig("not-dict", "name") is None  # noqa: SLF001
        assert (
            github_client_module._reviewer_effective_state_key(  # noqa: SLF001
                {"databaseId": 42},
                fetch_index=7,
            )
            == "review:42"
        )
        assert (
            github_client_module._reviewer_effective_state_key({}, fetch_index=7)  # noqa: SLF001
            == "review-fetch-index:7"
        )

        older = github_client_module._parse_fetched_review(  # noqa: SLF001
            {"state": "CHANGES_REQUESTED", "databaseId": 1},
            fetch_index=1,
        )
        newer = github_client_module._parse_fetched_review(  # noqa: SLF001
            {"state": "CHANGES_REQUESTED", "databaseId": 1},
            fetch_index=2,
        )

        assert github_client_module._review_is_later(newer, older)  # noqa: SLF001
        assert github_client_module._effective_blocking_reviews((older,)) == (  # noqa: SLF001
            github_client_module.replace(older.comment, blocks_merge=True),
        )
        assert github_client_module._connection_nodes(  # noqa: SLF001
            {"nodes": [{"id": "1"}, None, "bad", {"id": "2"}]}
        ) == [{"id": "1"}, {"id": "2"}]
