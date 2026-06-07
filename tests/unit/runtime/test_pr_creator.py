"""PR creator tests with FakeCommandRunner (no real git or gh).

PR creation is forge-neutral (issue #451): ``push_and_open`` does a forge-neutral
``git push`` and then routes the PR-open step through an injected
:class:`~awf.common.forge.ForgeClient`. GitHub workspaces pass a real
:class:`~awf.common.github_client.GitHubClient` (exercising the full ``gh`` path);
BitBucket and error paths use a recording/​raising fake forge client.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.common.bitbucket_client import BitBucketClientError
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClient, RepoRef
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

    @pytest.mark.unit
    async def test_opens_pr_on_bitbucket_via_forge_client(self) -> None:
        # R-bb: a BitBucket workspace opens its PR via the forge client; the
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
        # R-err-bb: a BitBucketClientError maps to PullRequestError with the HTTP
        # status as returncode, redacted body as stderr, and head_sha preserved.
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        forge = _FakeForgeClient(
            error=BitBucketClientError(
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

    @pytest.mark.unit
    async def test_bitbucket_transport_failure_maps_status_none_to_zero(self) -> None:
        # R-err-bb variant: a transport error (status=None) maps to returncode 0.
        runner = FakeCommandRunner()
        _queue_pre_push_diagnostics(runner)
        runner.queue_result(returncode=0)  # git push

        forge = _FakeForgeClient(
            error=BitBucketClientError(
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
