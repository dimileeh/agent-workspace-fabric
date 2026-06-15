"""PR creator tests with FakeCommandRunner (no real git or gh).

PR creation is forge-neutral (issue #451): ``push_and_open`` does a forge-neutral
``git push`` and then routes the PR-open step through an injected
:class:`~awf.common.forge.ForgeClient`. GitHub workspaces pass a real
:class:`~awf.common.github_client.GitHubClient` (exercising the full ``gh`` path);
Bitbucket and error paths use a recording/​raising fake forge client.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf.common.bitbucket_client import (
    BITBUCKET_API_ERROR,
    BITBUCKET_AUTH_NOT_CONFIGURED,
    BitbucketClientError,
)
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClient, GitHubClientError, RepoRef
from awf.runtime.pr_creator import PullRequestCreator, PullRequestError

_WORKTREE = Path("/fake/worktree")
_GH_REPO_URL = "https://github.com/dimileeh/aira-agent.git"


def _queue_pre_push_diagnostics(runner: FakeCommandRunner) -> None:
    """Queue the 3 canned results the new pre-push diagnostic block
    consumes (``rev-parse HEAD``, ``rev-parse --abbrev-ref HEAD``,
    ``log origin/<base>..HEAD``). Values are deliberately realistic
    so the log line looks sane if a test inspects it."""
    runner.queue_result(returncode=0, stdout="abc123def4567890\n")  # rev-parse HEAD
    runner.queue_result(returncode=0, stdout="awf/ws_xyz\n")  # current branch
    runner.queue_result(returncode=0, stdout="abc123 some work\n")  # ahead-of-base


def _open_pr_list_payload(
    *,
    number: int = 42,
    repo_slug: str = "dimileeh/aira-agent",
    branch: str = "awf/ws_x",
    head_sha: str = "f" * 40,
) -> str:
    owner, repo = repo_slug.split("/", 1)
    return json.dumps(
        [
            {
                "number": number,
                "url": f"https://github.com/{repo_slug}/pull/{number}",
                "headRefName": branch,
                "headRefOid": head_sha,
                "headRepository": {"name": repo, "nameWithOwner": repo_slug},
                "headRepositoryOwner": {"login": owner},
            }
        ]
    )


class _FakeForgeClient:
    """Records ``create_pull_request`` kwargs; returns a URL or raises an error."""

    def __init__(
        self,
        *,
        url: str = "https://bitbucket.org/workspace/repo/pull-requests/7",
        error: Exception | None = None,
    ) -> None:
        self._url = url
        self._error = error
        self.calls: list[dict[str, object]] = []

    async def create_pull_request(
        self,
        *,
        repo: RepoRef,
        base: str,
        head: str,
        title: str,
        body: str,
    ) -> str:
        self.calls.append({"repo": repo, "base": base, "head": head, "title": title, "body": body})
        if self._error is not None:
            raise self._error
        return self._url


class _SequencedForgeClient:
    """Returns or raises one queued PR-create outcome per call."""

    def __init__(self, outcomes: list[str | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    async def create_pull_request(
        self,
        *,
        repo: RepoRef,
        base: str,
        head: str,
        title: str,
        body: str,
    ) -> str:
        self.calls.append({"repo": repo, "base": base, "head": head, "title": title, "body": body})
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _RaisingForgeClient:
    """Fails the test if ``create_pull_request`` is ever called (reuse-path proof)."""

    async def create_pull_request(self, **_kwargs: object) -> str:
        raise AssertionError("create_pull_request must not be called on the reuse path")


class TestPushAndOpen:
    @pytest.mark.unit
    async def test_pushes_branch_then_creates_pr_on_github(self) -> None:
        # R-gh (CRITICAL regression): a GitHub workspace opens its PR via the
        # injected GitHubClient with the same base/head/title/body and returns
        # the same URL the old `gh pr create` produced.
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push
        runner.queue_result(
            returncode=0,
            stdout="https://github.com/dimileeh/aira-agent/pull/42\n",
        )  # gh pr create

        creator = PullRequestCreator(runner)
        result = await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="awf/ws_xyz",
            base_branch="development",
            title="Add docstring",
            body="One-line docstring on the module.",
            forge_client=GitHubClient(runner),
            repo_url=_GH_REPO_URL,
        )

        assert result.url == "https://github.com/dimileeh/aira-agent/pull/42"
        assert result.branch == "awf/ws_xyz"
        # 3 diagnostic queries + 1 push + 1 gh create = 5 total calls.
        assert len(runner.calls) == 5
        # The push is at index 3 (after the 3 diagnostics).
        push_call = runner.calls[3]
        assert push_call.args[0] == "git"
        assert f"safe.directory={_WORKTREE}" in push_call.args
        assert "-C" in push_call.args
        assert "push" in push_call.args
        assert "-u" in push_call.args
        assert "origin" in push_call.args
        assert "awf/ws_xyz" in push_call.args

        gh_args = runner.calls[4].args
        assert gh_args[:3] == ["gh", "pr", "create"]
        # GitHubClient targets the repo explicitly (--repo) rather than via cwd —
        # behavior-equivalent against the just-pushed head on origin.
        assert "--repo" in gh_args and "dimileeh/aira-agent" in gh_args
        assert "--base" in gh_args and "development" in gh_args
        assert "--head" in gh_args and "awf/ws_xyz" in gh_args
        assert "--title" in gh_args and "Add docstring" in gh_args
        assert "--body" in gh_args

    @pytest.mark.unit
    async def test_github_failure_raises_pull_request_error(self) -> None:
        # R-err-gh (CRITICAL regression): a GitHubClientError from create maps to
        # PullRequestError with the head_sha and redacted stderr preserved.
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # push succeeds
        runner.queue_result(returncode=1, stderr="gh: auth token expired")

        creator = PullRequestCreator(runner)
        with pytest.raises(PullRequestError) as exc:
            await creator.push_and_open(
                worktree_path=_WORKTREE,
                branch_name="awf/ws_x",
                base_branch="development",
                title="t",
                body="b",
                forge_client=GitHubClient(runner),
                repo_url=_GH_REPO_URL,
            )
        assert exc.value.operation == "gh pr create"
        assert exc.value.returncode == 1
        assert exc.value.head_sha == "abc123def4567890"
        assert "gh: auth token expired" in exc.value.stderr
        # GitHubClientError carries no reason_code, so the wrapper leaves it None.
        assert exc.value.reason_code is None

    @pytest.mark.unit
    async def test_github_transient_pr_create_failure_retries_then_succeeds(self) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # push succeeds
        runner.queue_result(
            returncode=1,
            stderr='Post "https://api.github.com/graphql": dial tcp: i/o timeout',
        )
        runner.queue_result(returncode=0, stdout="[]")  # reconcile lookup: none
        runner.queue_result(
            returncode=0,
            stdout="https://github.com/dimileeh/aira-agent/pull/42\n",
        )

        creator = PullRequestCreator(
            runner,
            pr_create_transient_max_retries=1,
            pr_create_transient_initial_backoff_seconds=0,
        )
        result = await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="awf/ws_x",
            base_branch="development",
            title="t",
            body="b",
            forge_client=GitHubClient(runner),
            repo_url=_GH_REPO_URL,
        )

        assert result.url == "https://github.com/dimileeh/aira-agent/pull/42"
        assert result.open_metadata is not None
        assert result.open_metadata["strategy"] == "created_after_retry"
        assert result.open_metadata["attempts"] == 2
        create_calls = [call for call in runner.calls if call.args[:3] == ["gh", "pr", "create"]]
        list_calls = [call for call in runner.calls if call.args[:3] == ["gh", "pr", "list"]]
        assert len(create_calls) == 2
        assert len(list_calls) == 1

    @pytest.mark.unit
    async def test_github_transient_pr_create_failure_then_empty_url_preserves_retry_details(
        self,
    ) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # push succeeds
        runner.queue_result(returncode=0, stdout="[]")  # reconcile lookup: none
        forge = _SequencedForgeClient(
            [
                GitHubClientError(
                    operation="gh pr create",
                    returncode=1,
                    stderr='Post "https://api.github.com/graphql": dial tcp: i/o timeout',
                ),
                "",
            ]
        )

        creator = PullRequestCreator(
            runner,
            pr_create_transient_max_retries=1,
            pr_create_transient_initial_backoff_seconds=0,
        )
        with pytest.raises(PullRequestError) as exc:
            await creator.push_and_open(
                worktree_path=_WORKTREE,
                branch_name="awf/ws_x",
                base_branch="development",
                title="t",
                body="b",
                forge_client=forge,
                repo_url=_GH_REPO_URL,
            )

        assert "no URL" in exc.value.operation
        assert exc.value.details is not None
        assert exc.value.details["strategy"] == "failed"
        assert exc.value.details["attempts"] == 2
        assert exc.value.details["retry_count"] == 1
        failures = exc.value.details["failures"]
        assert isinstance(failures, list)
        assert failures[0]["will_retry"] is True
        lookups = exc.value.details["reconcile_lookups"]
        assert isinstance(lookups, list)
        assert lookups[0]["status"] == "not_found"

    @pytest.mark.unit
    async def test_github_transient_pr_create_reconciles_existing_same_repo_pr(self) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # push succeeds
        runner.queue_result(
            returncode=1,
            stderr='Post "https://api.github.com/graphql": dial tcp: i/o timeout',
        )
        runner.queue_result(
            returncode=0,
            stdout=_open_pr_list_payload(number=77, branch="awf/ws_x", head_sha="a" * 40),
        )

        creator = PullRequestCreator(
            runner,
            pr_create_transient_initial_backoff_seconds=0,
        )
        result = await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="awf/ws_x",
            base_branch="development",
            title="t",
            body="b",
            forge_client=GitHubClient(runner),
            repo_url=_GH_REPO_URL,
        )

        assert result.url == "https://github.com/dimileeh/aira-agent/pull/77"
        assert result.head_sha == "a" * 40
        assert result.open_metadata is not None
        assert result.open_metadata["strategy"] == "reconciled_after_transient"
        assert result.open_metadata["matched_pr"] == {
            "number": 77,
            "url": "https://github.com/dimileeh/aira-agent/pull/77",
            "head_ref": "awf/ws_x",
            "head_repo_slug": "dimileeh/aira-agent",
            "head_sha": "a" * 40,
        }
        create_calls = [call for call in runner.calls if call.args[:3] == ["gh", "pr", "create"]]
        assert len(create_calls) == 1

    @pytest.mark.unit
    async def test_github_duplicate_pr_create_error_reconciles_existing_pr(self) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # push succeeds
        runner.queue_result(
            returncode=1,
            stderr='a pull request for branch "awf/ws_x" into branch "development" already exists',
        )
        runner.queue_result(
            returncode=0,
            stdout=_open_pr_list_payload(number=88, branch="awf/ws_x"),
        )

        creator = PullRequestCreator(runner)
        result = await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="awf/ws_x",
            base_branch="development",
            title="t",
            body="b",
            forge_client=GitHubClient(runner),
            repo_url=_GH_REPO_URL,
        )

        assert result.url == "https://github.com/dimileeh/aira-agent/pull/88"
        assert result.open_metadata is not None
        assert result.open_metadata["strategy"] == "reconciled_after_duplicate"
        assert len([call for call in runner.calls if call.args[:3] == ["gh", "pr", "create"]]) == 1

    @pytest.mark.unit
    async def test_github_duplicate_pr_create_retries_failed_lookup(self) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        duplicate_error = (
            'a pull request for branch "awf/ws_x" into branch "development" already exists'
        )
        runner.queue_result(returncode=0)  # push succeeds
        runner.queue_result(returncode=1, stderr=duplicate_error)
        runner.queue_result(returncode=1, stderr="gh api timeout")
        runner.queue_result(returncode=1, stderr=duplicate_error)
        runner.queue_result(
            returncode=0,
            stdout=_open_pr_list_payload(number=89, branch="awf/ws_x"),
        )

        creator = PullRequestCreator(
            runner,
            pr_create_transient_initial_backoff_seconds=0,
        )
        result = await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="awf/ws_x",
            base_branch="development",
            title="t",
            body="b",
            forge_client=GitHubClient(runner),
            repo_url=_GH_REPO_URL,
        )

        assert result.url == "https://github.com/dimileeh/aira-agent/pull/89"
        assert result.open_metadata is not None
        assert result.open_metadata["strategy"] == "reconciled_after_duplicate"
        lookups = result.open_metadata["reconcile_lookups"]
        assert isinstance(lookups, list)
        assert lookups[0]["status"] == "failed"
        assert lookups[0]["reason_code"] == "OPEN_PR_LOOKUP_FAILED"
        assert lookups[1]["status"] == "found"
        failures = result.open_metadata["failures"]
        assert isinstance(failures, list)
        assert failures[0]["will_retry"] is True
        create_calls = [call for call in runner.calls if call.args[:3] == ["gh", "pr", "create"]]
        list_calls = [call for call in runner.calls if call.args[:3] == ["gh", "pr", "list"]]
        assert len(create_calls) == 2
        assert len(list_calls) == 2

    @pytest.mark.unit
    async def test_github_duplicate_pr_create_reports_exhausted_lookup_failure(self) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # push succeeds
        runner.queue_result(
            returncode=1,
            stderr='a pull request for branch "awf/ws_x" into branch "development" already exists',
        )
        runner.queue_result(returncode=1, stderr="gh api timeout")

        creator = PullRequestCreator(
            runner,
            pr_create_transient_max_retries=0,
            pr_create_transient_initial_backoff_seconds=0,
        )
        with pytest.raises(PullRequestError) as exc:
            await creator.push_and_open(
                worktree_path=_WORKTREE,
                branch_name="awf/ws_x",
                base_branch="development",
                title="t",
                body="b",
                forge_client=GitHubClient(runner),
                repo_url=_GH_REPO_URL,
            )

        assert exc.value.details is not None
        assert exc.value.details["strategy"] == "duplicate_lookup_failed"
        lookups = exc.value.details["reconcile_lookups"]
        assert isinstance(lookups, list)
        assert lookups[0]["status"] == "failed"
        assert lookups[0]["reason_code"] == "OPEN_PR_LOOKUP_FAILED"
        failures = exc.value.details["failures"]
        assert isinstance(failures, list)
        assert failures[0]["will_retry"] is False

    @pytest.mark.unit
    async def test_github_transient_pr_create_ignores_fork_pr_collision(self) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # push succeeds
        runner.queue_result(
            returncode=1,
            stderr='Post "https://api.github.com/graphql": dial tcp: i/o timeout',
        )
        runner.queue_result(
            returncode=0,
            stdout=_open_pr_list_payload(
                number=99,
                repo_slug="fork/aira-agent",
                branch="awf/ws_x",
            ),
        )

        creator = PullRequestCreator(
            runner,
            pr_create_transient_max_retries=0,
            pr_create_transient_initial_backoff_seconds=0,
        )
        with pytest.raises(PullRequestError) as exc:
            await creator.push_and_open(
                worktree_path=_WORKTREE,
                branch_name="awf/ws_x",
                base_branch="development",
                title="t",
                body="b",
                forge_client=GitHubClient(runner),
                repo_url=_GH_REPO_URL,
            )

        assert exc.value.details is not None
        assert exc.value.details["strategy"] == "transient_retry_exhausted"
        lookups = exc.value.details["reconcile_lookups"]
        assert isinstance(lookups, list)
        assert lookups[0]["fork_collision_count"] == 1
        assert lookups[0]["same_repo_count"] == 0

    @pytest.mark.unit
    async def test_github_deterministic_pr_create_failure_does_not_retry_or_lookup(self) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # push succeeds
        runner.queue_result(returncode=1, stderr="Bad credentials")

        creator = PullRequestCreator(
            runner,
            pr_create_transient_initial_backoff_seconds=0,
        )
        with pytest.raises(PullRequestError) as exc:
            await creator.push_and_open(
                worktree_path=_WORKTREE,
                branch_name="awf/ws_x",
                base_branch="development",
                title="t",
                body="b",
                forge_client=GitHubClient(runner),
                repo_url=_GH_REPO_URL,
            )

        assert exc.value.details is not None
        assert exc.value.details["strategy"] == "failed"
        assert not any(call.args[:3] == ["gh", "pr", "list"] for call in runner.calls)
        assert len([call for call in runner.calls if call.args[:3] == ["gh", "pr", "create"]]) == 1

    @pytest.mark.unit
    async def test_opens_pr_on_bitbucket_via_forge_client(self) -> None:
        # R-bb: a Bitbucket workspace opens its PR via the forge client; the
        # returned URL is used verbatim (no github-shaped regex parse) and no
        # `gh pr create` command hits the runner.
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        forge = _FakeForgeClient(url="https://bitbucket.org/workspace/repo/pull-requests/7")
        creator = PullRequestCreator(runner)
        result = await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="awf/ws_xyz",
            base_branch="development",
            title="Add docstring",
            body="One-line docstring.",
            forge_client=forge,
            repo_url="git@bitbucket.org:workspace/repo.git",
        )

        assert result.url == "https://bitbucket.org/workspace/repo/pull-requests/7"
        assert result.branch == "awf/ws_xyz"
        assert len(forge.calls) == 1
        call = forge.calls[0]
        assert call["base"] == "development"
        assert call["head"] == "awf/ws_xyz"
        assert call["title"] == "Add docstring"
        assert call["body"] == "One-line docstring."
        # Only the 3 diagnostics + push hit the runner; no `gh pr create`.
        assert len(runner.calls) == 4
        assert all(call.args[:3] != ["gh", "pr", "create"] for call in runner.calls)

    @pytest.mark.unit
    async def test_bitbucket_failure_raises_pull_request_error(self) -> None:
        # R-err-bb: a BitbucketClientError maps to PullRequestError with the HTTP
        # status as returncode, redacted body as stderr, and head_sha preserved.
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        forge = _FakeForgeClient(
            error=BitbucketClientError(
                operation="bitbucket create_pull_request",
                status=403,
                body="forbidden",
            )
        )
        creator = PullRequestCreator(runner)
        with pytest.raises(PullRequestError) as exc:
            await creator.push_and_open(
                worktree_path=_WORKTREE,
                branch_name="awf/ws_x",
                base_branch="development",
                title="t",
                body="b",
                forge_client=forge,
                repo_url="git@bitbucket.org:workspace/repo.git",
            )
        assert exc.value.operation == "bitbucket create_pull_request"
        assert exc.value.returncode == 403
        assert exc.value.head_sha == "abc123def4567890"
        assert "forbidden" in exc.value.stderr
        # The BitbucketClientError's default reason_code flows through verbatim.
        assert exc.value.reason_code == BITBUCKET_API_ERROR

    @pytest.mark.unit
    async def test_bitbucket_failure_preserves_actionable_reason_code(self) -> None:
        # PRRT_kwDOSJAM6s6HqvLL: a BitbucketClientError carrying an actionable
        # reason_code (auth / rate-limit / transport) must propagate it onto the
        # PullRequestError so the executor records the specific doctor guidance
        # instead of a generic PR_CREATE_FAILED.
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        forge = _FakeForgeClient(
            error=BitbucketClientError(
                operation="bitbucket create_pull_request",
                status=401,
                body="BITBUCKET_API_TOKEN is required.",
                reason_code=BITBUCKET_AUTH_NOT_CONFIGURED,
            )
        )
        creator = PullRequestCreator(runner)
        with pytest.raises(PullRequestError) as exc:
            await creator.push_and_open(
                worktree_path=_WORKTREE,
                branch_name="awf/ws_x",
                base_branch="development",
                title="t",
                body="b",
                forge_client=forge,
                repo_url="git@bitbucket.org:workspace/repo.git",
            )
        assert exc.value.reason_code == BITBUCKET_AUTH_NOT_CONFIGURED

    @pytest.mark.unit
    async def test_bitbucket_transport_failure_maps_status_none_to_zero(self) -> None:
        # R-err-bb variant: a transport error (status=None) maps to returncode 0.
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        forge = _FakeForgeClient(
            error=BitbucketClientError(
                operation="bitbucket create_pull_request",
                status=None,
                body="connection reset",
            )
        )
        creator = PullRequestCreator(runner)
        with pytest.raises(PullRequestError) as exc:
            await creator.push_and_open(
                worktree_path=_WORKTREE,
                branch_name="awf/ws_x",
                base_branch="development",
                title="t",
                body="b",
                forge_client=forge,
                repo_url="git@bitbucket.org:workspace/repo.git",
            )
        assert exc.value.returncode == 0
        assert "connection reset" in exc.value.stderr

    @pytest.mark.unit
    async def test_repo_ref_built_from_repo_url_for_bitbucket(self) -> None:
        # R-repo: the RepoRef passed to create_pull_request is parsed from repo_url.
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        forge = _FakeForgeClient(url="https://bitbucket.org/workspace/repo/pull-requests/7")
        creator = PullRequestCreator(runner)
        await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="awf/ws_xyz",
            base_branch="development",
            title="t",
            body="b",
            forge_client=forge,
            repo_url="git@bitbucket.org:workspace/repo.git",
        )
        repo = forge.calls[0]["repo"]
        assert repo == RepoRef(owner="workspace", name="repo", forge="bitbucket")

    @pytest.mark.unit
    async def test_repo_ref_built_from_repo_url_for_github(self) -> None:
        # R-repo (github): same wiring detects github from the repo URL host.
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        forge = _FakeForgeClient(url="https://github.com/dimileeh/aira-agent/pull/9")
        creator = PullRequestCreator(runner)
        await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="awf/ws_xyz",
            base_branch="development",
            title="t",
            body="b",
            forge_client=forge,
            repo_url=_GH_REPO_URL,
        )
        repo = forge.calls[0]["repo"]
        assert repo == RepoRef(owner="dimileeh", name="aira-agent", forge="github")

    @pytest.mark.unit
    async def test_remote_url_wins_over_repo_url_for_repo_ref(self) -> None:
        # The locked expression is RepoRef.from_url(remote_url or repo_url): an
        # explicit fork push URL targets the fork's RepoRef, not the base repo_url.
        # (No PR is opened on the reuse path, so this asserts via a non-reuse call
        # by leaving existing_pr_url unset while still passing remote_url.)
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        forge = _FakeForgeClient(url="https://github.com/contributor/aira-agent/pull/3")
        creator = PullRequestCreator(runner)
        await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="awf/ws_xyz",
            base_branch="development",
            title="t",
            body="b",
            forge_client=forge,
            repo_url=_GH_REPO_URL,
            remote_url="git@github.com:contributor/aira-agent.git",
        )
        repo = forge.calls[0]["repo"]
        assert repo == RepoRef(owner="contributor", name="aira-agent", forge="github")

    @pytest.mark.unit
    async def test_reuses_existing_pr_after_push_without_creating_duplicate(self) -> None:
        # Reuse path: returns the existing URL, opens no PR, and works even with a
        # forge client that would fail if consulted.
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        creator = PullRequestCreator(runner)
        result = await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="awf/ws_xyz",
            base_branch="development",
            title="Add docstring",
            body="One-line docstring on the module.",
            forge_client=_RaisingForgeClient(),
            repo_url=_GH_REPO_URL,
            existing_pr_url="https://github.com/dimileeh/aira-agent/pull/42",
        )

        assert result.url == "https://github.com/dimileeh/aira-agent/pull/42"
        assert result.branch == "awf/ws_xyz"
        assert result.head_sha == "abc123def4567890"
        assert len(runner.calls) == 4
        assert all(call.args[:3] != ["gh", "pr", "create"] for call in runner.calls)

    @pytest.mark.unit
    async def test_reuses_existing_pr_without_a_forge_client(self) -> None:
        # Reuse needs no forge client at all: the caller may omit it (passing
        # ``None``) because the reuse path returns after the git push and never
        # touches the client. This lets the executor skip resolving a Bitbucket
        # client (and its env-dependent ``from_env()``) on a reuse push.
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        creator = PullRequestCreator(runner)
        result = await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="awf/ws_xyz",
            base_branch="development",
            title="Add docstring",
            body="One-line docstring on the module.",
            forge_client=None,
            repo_url=_GH_REPO_URL,
            existing_pr_url="https://github.com/dimileeh/aira-agent/pull/42",
        )

        assert result.url == "https://github.com/dimileeh/aira-agent/pull/42"
        assert result.branch == "awf/ws_xyz"
        assert all(call.args[:3] != ["gh", "pr", "create"] for call in runner.calls)

    @pytest.mark.unit
    async def test_updates_existing_pr_with_explicit_remote_head_ref(self) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        creator = PullRequestCreator(runner)
        result = await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="feature-sync/ws_local",
            base_branch="development",
            title="Update adopted PR",
            body="Existing PR update.",
            forge_client=_RaisingForgeClient(),
            repo_url=_GH_REPO_URL,
            existing_pr_url="https://github.com/dimileeh/aira-agent/pull/42",
            remote_branch_name="awf/ws_original",
        )

        push_args = runner.calls[3].args
        assert result.url == "https://github.com/dimileeh/aira-agent/pull/42"
        assert result.branch == "awf/ws_original"
        assert push_args[:1] == ["git"]
        assert "push" in push_args
        assert "origin" in push_args
        assert "HEAD:refs/heads/awf/ws_original" in push_args
        assert "feature-sync/ws_local" not in push_args
        assert all(call.args[:3] != ["gh", "pr", "create"] for call in runner.calls)

    @pytest.mark.unit
    async def test_updates_existing_pr_with_qualified_remote_head_ref(self) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        creator = PullRequestCreator(runner)
        result = await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="feature-sync/ws_local",
            base_branch="development",
            title="Update adopted PR",
            body="Existing PR update.",
            forge_client=_RaisingForgeClient(),
            repo_url=_GH_REPO_URL,
            existing_pr_url="https://github.com/dimileeh/aira-agent/pull/42",
            remote_branch_name="refs/heads/awf/ws_original",
        )

        push_args = runner.calls[3].args
        assert result.branch == "awf/ws_original"
        assert "HEAD:refs/heads/awf/ws_original" in push_args
        assert "HEAD:refs/heads/refs/heads/awf/ws_original" not in push_args

    @pytest.mark.unit
    async def test_updates_existing_pr_on_explicit_remote_url(self) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        creator = PullRequestCreator(runner)
        result = await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="feature-sync/ws_local",
            base_branch="development",
            title="Update adopted fork PR",
            body="Existing fork PR update.",
            forge_client=_RaisingForgeClient(),
            repo_url=_GH_REPO_URL,
            existing_pr_url="https://github.com/base/aira-agent/pull/42",
            remote_branch_name="fix/fork-review",
            remote_url="git@github.com:contributor/aira-agent.git",
        )

        push_args = runner.calls[3].args
        push_index = push_args.index("push")
        assert result.url == "https://github.com/base/aira-agent/pull/42"
        assert result.branch == "fix/fork-review"
        assert push_args[push_index + 1] == "git@github.com:contributor/aira-agent.git"
        assert "HEAD:refs/heads/fix/fork-review" in push_args
        assert "origin" not in push_args[push_index + 1 :]
        assert all(call.args[:3] != ["gh", "pr", "create"] for call in runner.calls)

    @pytest.mark.unit
    async def test_explicit_remote_url_push_does_not_set_upstream(self) -> None:
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        creator = PullRequestCreator(runner)
        await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="feature-sync/ws_local",
            base_branch="development",
            title="Update adopted fork PR",
            body="Existing fork PR update.",
            forge_client=_RaisingForgeClient(),
            repo_url=_GH_REPO_URL,
            existing_pr_url="https://github.com/base/aira-agent/pull/42",
            remote_branch_name="fix/fork-review",
            remote_url="https://user:token@github.com/contributor/aira-agent.git",
        )

        push_args = runner.calls[3].args
        assert "-u" not in push_args
        assert "--set-upstream" not in push_args
        assert "https://user:token@github.com/contributor/aira-agent.git" in push_args
        assert "HEAD:refs/heads/fix/fork-review" in push_args

    @pytest.mark.unit
    async def test_push_failure_raises_before_consulting_forge(self) -> None:
        # git push failure: forge client is never consulted.
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=128, stderr="remote: permission denied")

        creator = PullRequestCreator(runner)
        with pytest.raises(PullRequestError) as exc:
            await creator.push_and_open(
                worktree_path=_WORKTREE,
                branch_name="awf/ws_denied",
                base_branch="development",
                title="t",
                body="b",
                forge_client=_RaisingForgeClient(),
                repo_url=_GH_REPO_URL,
            )
        assert exc.value.operation == "git push"
        assert exc.value.returncode == 128
        # A push failure has no forge reason_code to carry.
        assert exc.value.reason_code is None
        # 3 diagnostic queries + 1 push = 4 calls; the forge was never reached.
        assert len(runner.calls) == 4

    @pytest.mark.unit
    async def test_pre_push_diagnostics_run_before_push(self) -> None:
        """The three git diagnostic queries must fire BEFORE the push
        subprocess call — otherwise a push failure would short-circuit
        the block we actually need for debugging."""
        runner = FakeCommandRunner()
        runner.queue_result(returncode=0, stdout="deadbeef1234\n")  # rev-parse HEAD
        runner.queue_result(returncode=0, stdout="awf/ws_abc\n")  # abbrev-ref HEAD
        runner.queue_result(
            returncode=0, stdout="ab12345 feat: thing\ncd67890 chore: thing\n"
        )  # ahead-of-base
        runner.queue_result(returncode=0)  # git push

        forge = _FakeForgeClient(url="https://github.com/x/y/pull/1")
        creator = PullRequestCreator(runner)
        await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="awf/ws_abc",
            base_branch="development",
            title="t",
            body="b",
            forge_client=forge,
            repo_url=_GH_REPO_URL,
        )

        # The first three calls are the diagnostics, in order.
        call0, call1, call2, call3 = runner.calls
        assert "rev-parse" in call0.args and "HEAD" in call0.args
        assert f"safe.directory={_WORKTREE}" in call0.args
        assert "--abbrev-ref" in call1.args
        assert "log" in call2.args and "origin/development..HEAD" in call2.args
        # The FOURTH call is the push.
        assert "push" in call3.args

    @pytest.mark.unit
    async def test_diagnostic_failure_does_not_block_push(self) -> None:
        """If a diagnostic query itself errors (weird worktree state,
        permissions), the normal push path still runs. Diagnostics are
        observability, not control flow."""
        runner = FakeCommandRunner()
        runner.queue_result(returncode=128, stderr="fatal: bad object")  # rev-parse fails
        runner.queue_result(returncode=128, stderr="fatal: bad object")  # abbrev-ref fails
        runner.queue_result(returncode=128, stderr="fatal: bad object")  # ahead-of-base fails
        runner.queue_result(returncode=0)  # git push still runs

        forge = _FakeForgeClient(url="https://github.com/x/y/pull/1")
        creator = PullRequestCreator(runner)
        result = await creator.push_and_open(
            worktree_path=_WORKTREE,
            branch_name="awf/ws_weird",
            base_branch="development",
            title="t",
            body="b",
            forge_client=forge,
            repo_url=_GH_REPO_URL,
        )
        assert result.url == "https://github.com/x/y/pull/1"

    @pytest.mark.unit
    async def test_empty_url_from_forge_raises(self) -> None:
        # No-URL guard (D7): an empty PR URL → PullRequestError with "no URL".
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        forge = _FakeForgeClient(url="")
        creator = PullRequestCreator(runner)
        with pytest.raises(PullRequestError) as exc:
            await creator.push_and_open(
                worktree_path=_WORKTREE,
                branch_name="awf/ws_x",
                base_branch="development",
                title="t",
                body="b",
                forge_client=forge,
                repo_url=_GH_REPO_URL,
            )
        assert "no URL" in exc.value.operation
        assert exc.value.head_sha == "abc123def4567890"
