"""Private GitHubClient helper edge tests.

Split out of ``test_github_client_part_004`` to keep each part file under the
first-party line-count guardrail.
"""

from __future__ import annotations

import pytest

from awf.common import github_client_parsing as github_client_module
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClient, GitHubClientError


class TestPrivateCoverageEdges:
    """Coverage edge tests for private GitHub client helper paths."""

    @pytest.mark.unit
    async def test_gh_json_and_run_gh_raise_on_strict_failures(self) -> None:
        """Strict helper failures raise while non-strict command failures return."""
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
        """Nested payload helpers preserve fallback behavior for review parsing."""
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
