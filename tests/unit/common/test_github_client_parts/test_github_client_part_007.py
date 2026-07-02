"""Mutation and merge-method GitHubClient tests.

Split out of ``test_github_client_part_004`` to keep each part file under the
first-party line-count guardrail.
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


class TestMutations:
    """Mutation and merge-method GitHub client command tests."""

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
        fake.queue_result(returncode=0)  # merge (no pre-check: the monitor recheck owns state)
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
        fake.queue_result(returncode=0)  # merge
        fake.queue_result(returncode=0, stdout="MERGESHA123\n")  # sha fetch
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
        fake.queue_result(returncode=1, stderr="branch protection blocked merge")  # merge fails
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
        fake.queue_result(
            returncode=1, stderr="not found"
        )  # sha fetch fails (permanent, not retried)
        client = GitHubClient(fake, sleep=lambda _: None)
        sha = await client.merge_pr(repo=RepoRef(owner="o", name="r"), pr_number=42)
        assert sha == ""

    @pytest.mark.unit
    async def test_fetch_repo_merge_methods_reads_repo_flags(self) -> None:
        """Repository merge method discovery follows the GitHub repo flags."""
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
    async def test_fetch_repo_merge_methods_rejects_missing_repo_flags(self) -> None:
        """Missing repo merge flags are an API anomaly, not a genuine empty policy."""
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout='{"name":"r"}')
        client = GitHubClient(fake)

        with pytest.raises(GitHubClientError, match="omitted merge method flags") as exc:
            await client.fetch_repo_merge_methods(repo=RepoRef(owner="o", name="r"))

        assert "allow_merge_commit" in exc.value.stderr
        assert "allow_squash_merge" in exc.value.stderr
        assert "allow_rebase_merge" in exc.value.stderr
        assert exc.value.returncode == 1
        assert "temporarily unavailable" in exc.value.stderr

    @pytest.mark.unit
    async def test_fetch_repo_merge_methods_rejects_partial_repo_flags(self) -> None:
        """Partially omitted merge flags are anomalous API payloads."""
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout='{"allow_merge_commit":true,"allow_squash_merge":true}',
        )
        client = GitHubClient(fake)

        with pytest.raises(GitHubClientError, match="omitted merge method flags") as exc:
            await client.fetch_repo_merge_methods(repo=RepoRef(owner="o", name="r"))

        assert "allow_rebase_merge" in exc.value.stderr
        assert "allow_merge_commit" not in exc.value.stderr
        assert "allow_squash_merge" not in exc.value.stderr
        assert exc.value.returncode == 1
        assert "try again" in exc.value.stderr

    @pytest.mark.unit
    async def test_fetch_repo_merge_methods_all_false_is_empty_policy(self) -> None:
        """Explicit false repo merge flags still represent a real empty repository policy."""
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=(
                '{"allow_merge_commit":false,"allow_squash_merge":false,"allow_rebase_merge":false}'
            ),
        )
        client = GitHubClient(fake)

        methods = await client.fetch_repo_merge_methods(repo=RepoRef(owner="o", name="r"))

        assert methods == ()

    @pytest.mark.unit
    async def test_fetch_branch_pull_request_allowed_merge_methods_empty_unconstrained(
        self,
    ) -> None:
        """An empty branch-rules response leaves merge method choice unconstrained."""
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout="[]")
        client = GitHubClient(fake)

        methods = await client.fetch_branch_pull_request_allowed_merge_methods(
            repo=RepoRef(owner="o", name="r"),
            branch="feature/dev",
        )

        assert methods is None
        assert fake.calls[0].args == [
            "gh",
            "api",
            "repos/o/r/rules/branches/feature%2Fdev",
            "--paginate",
            "--slurp",
        ]

    @pytest.mark.unit
    async def test_fetch_branch_pull_request_allowed_merge_methods_raises_on_empty_slurp_stdout(
        self,
    ) -> None:
        """Empty stdout from a slurped branch-rules response is an API anomaly."""
        fake = FakeCommandRunner()
        fake.queue_result(returncode=0, stdout=" \n")
        client = GitHubClient(fake)

        with pytest.raises(GitHubClientError, match="empty response") as exc:
            await client.fetch_branch_pull_request_allowed_merge_methods(
                repo=RepoRef(owner="o", name="r"),
                branch="feature/dev",
            )

        assert "--paginate" in fake.calls[0].args
        assert "--slurp" in fake.calls[0].args
        assert "branch rules" in exc.value.operation
        assert "try again" in exc.value.stderr

    @pytest.mark.unit
    async def test_fetch_branch_pull_request_allowed_merge_methods_reads_later_pages(
        self,
    ) -> None:
        """Paginated branch rules include later-page merge-method constraints."""
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    [{"type": "required_status_checks", "parameters": {}}],
                    [
                        {
                            "type": "pull_request",
                            "parameters": {"allowed_merge_methods": ["rebase"]},
                        }
                    ],
                ]
            ),
        )
        client = GitHubClient(fake)

        methods = await client.fetch_branch_pull_request_allowed_merge_methods(
            repo=RepoRef(owner="o", name="r"),
            branch="main",
        )

        assert methods == ("rebase",)
        assert "--paginate" in fake.calls[0].args
        assert "--slurp" in fake.calls[0].args

    @pytest.mark.unit
    async def test_fetch_branch_pull_request_allowed_merge_methods_ignores_non_pr_rules(
        self,
    ) -> None:
        """Non-pull-request branch rules do not constrain merge methods."""
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
        """Pull-request rules without allowed methods remain unconstrained."""
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
    async def test_fetch_branch_pull_request_allowed_merge_methods_empty_list_is_empty_policy(
        self,
    ) -> None:
        """An explicit empty allowed_merge_methods list allows no merge methods."""
        fake = FakeCommandRunner()
        fake.queue_result(
            returncode=0,
            stdout='[{"type":"pull_request","parameters":{"allowed_merge_methods":[]}}]',
        )
        client = GitHubClient(fake)

        methods = await client.fetch_branch_pull_request_allowed_merge_methods(
            repo=RepoRef(owner="o", name="r"),
            branch="main",
        )

        assert methods == ()

    @pytest.mark.unit
    async def test_fetch_branch_pull_request_allowed_merge_methods_unknown_only_unconstrained(
        self,
    ) -> None:
        """Unknown-only merge method values do not constrain AWF methods."""
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
        """Known merge method values are normalized and unknown values ignored."""
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
        """Multiple pull-request rules intersect their recognized method sets."""
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
        """Branch rules API failures are surfaced as GitHub client errors."""
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
        """Branch rules API failures redact token-like stderr before surfacing."""
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
        """Malformed branch rules JSON is wrapped as a GitHub client error."""
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
        """Malformed branch rules JSON redacts token-like output in errors."""
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
