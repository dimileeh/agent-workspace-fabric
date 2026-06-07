"""Pull-request creator — pushes the feature branch and opens or reuses a PR.

Two responsibilities:

1. ``git -C <worktree> push -u origin <branch>`` — uploads the commits the
   coding CLI just made to the remote. When updating an adopted fork PR through
   an explicit push URL, AWF omits ``-u`` so credentialed URLs are not persisted
   in branch upstream config. The push is forge-neutral plain git.
2. Open the PR via the injected :class:`~awf.common.forge.ForgeClient` when one
   does not already exist. The forge client (GitHub or BitBucket) is resolved by
   the caller and passed in per-call, so ``PullRequestCreator`` stays
   forge-agnostic and stateless. The client returns the PR URL directly.

``PullRequestCreator`` shells plain ``git`` for the push but never shells the
forge CLI itself — the resolved ``ForgeClient`` owns the forge-specific PR-open
call (``gh pr create`` for GitHub, the REST API for BitBucket).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from awf.common.commands import AsyncCommandRunner
from awf.common.forge import ForgeClient
from awf.common.git_identity import git_safe_directory_config_args
from awf.common.github_client import GitHubClientError, RepoRef
from awf.common.logging import get_logger

_log = get_logger(__name__)

# Redact credentials embedded in URLs before logging. Git/gh can emit
# ``https://user:token@host`` in stderr under certain auth failures;
# those strings must never land in log storage. Matches both user-only
# (``https://user@host``) and user+password (``https://user:pwd@host``)
# forms and replaces the credential section with ``***``.
_URL_CREDENTIAL_PATTERN = re.compile(r"(https?://)[^/@\s]+(?::[^/@\s]+)?@")

# Bound the diagnostic log's list of commits-ahead-of-base. A feature
# branch with hundreds of commits (rare but possible for long-running
# workstreams) would otherwise emit an unbounded list that could
# exceed log-backend payload limits.
_MAX_DIAGNOSTIC_COMMITS = 50
_HEADS_REF_PREFIX = "refs/heads/"


def _redact_credentials(text: str) -> str:
    """Replace ``https://user[:pwd]@host`` patterns with ``https://***@host``
    so push/pr-create stderr can be safely logged."""
    return _URL_CREDENTIAL_PATTERN.sub(r"\1***@", text)


def _short_branch_name(branch_name: str) -> str:
    """Return the branch name without a leading ``refs/heads/`` prefix."""
    return branch_name.removeprefix(_HEADS_REF_PREFIX)


@dataclass(frozen=True)
class PullRequestResult:
    url: str
    branch: str
    head_sha: str | None = None


class PullRequestError(Exception):
    """Raised when the git push or the forge PR-open call fails."""

    def __init__(
        self,
        *,
        operation: str,
        returncode: int,
        stderr: str,
        head_sha: str | None = None,
    ) -> None:
        self.operation = operation
        self.returncode = returncode
        self.stderr = stderr
        self.head_sha = head_sha
        super().__init__(
            f"{operation} failed (exit={returncode}): {stderr.strip() or '<no output>'}"
        )


class PullRequestCreator:
    """Pushes a feature branch and opens a PR via an injected ``ForgeClient``."""

    def __init__(self, runner: AsyncCommandRunner) -> None:
        self._runner = runner

    async def push_and_open(
        self,
        *,
        worktree_path: Path,
        branch_name: str,
        base_branch: str,
        title: str,
        body: str,
        forge_client: ForgeClient,
        repo_url: str,
        existing_pr_url: str | None = None,
        remote_branch_name: str | None = None,
        remote_url: str | None = None,
    ) -> PullRequestResult:
        if existing_pr_url and remote_branch_name:
            push_target_branch = _short_branch_name(remote_branch_name)
            push_ref = f"HEAD:{_HEADS_REF_PREFIX}{push_target_branch}"
        else:
            push_target_branch = branch_name
            push_ref = branch_name
        push_remote = remote_url or "origin"
        # Step 0: capture the worktree's view of the branch state so we
        # can diagnose post-validation push failures. T39 (ws_eb8c2bd5)
        # hit ``gh pr create: No commits between development and
        # awf/ws_eb8c2bd5 ... Head ref must be a branch`` despite
        # validation having passed — we need to know whether (a) the
        # local branch had commits but push didn't move them, (b) the
        # local branch was empty relative to base (bad commit step), or
        # (c) HEAD was detached / on a different branch. These three
        # logs answer all three questions:
        head_sha = await self._log_pre_push_diagnostics(
            worktree_path=worktree_path,
            branch_name=push_target_branch,
            base_branch=base_branch,
        )

        # Step 1: push the branch.
        push = await self._runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                "push",
                *(["-u"] if remote_url is None else []),
                push_remote,
                push_ref,
            ],
        )
        # Log the verbatim push output BEFORE the ok check. If the push
        # silently said "Everything up-to-date" with returncode 0 (the
        # T39 signature), we want that recorded for triage.
        #
        # stderr/stdout passed through ``_redact_credentials`` first:
        # git can surface ``https://user:token@host`` in auth-failure
        # stderr (e.g. "fatal: unable to access '…@github.com/…'"),
        # and embedded credentials must not hit log storage.
        _log.info(
            "pr_creator.push_output",
            branch=push_target_branch,
            remote=_redact_credentials(push_remote),
            returncode=push.returncode,
            stdout=_redact_credentials(push.stdout.strip())[:500],
            stderr=_redact_credentials(push.stderr.strip())[:500],
        )
        if not push.ok:
            raise PullRequestError(
                operation="git push",
                returncode=push.returncode,
                stderr=push.stderr,
                head_sha=head_sha,
            )

        if existing_pr_url:
            _log.info("pr.reused", branch=push_target_branch, url=existing_pr_url)
            return PullRequestResult(
                url=existing_pr_url,
                branch=push_target_branch,
                head_sha=head_sha,
            )

        # Step 2: open the PR through the resolved forge client. The client
        # (GitHub or BitBucket) owns auth and the forge-specific call and returns
        # the PR URL directly — no github-shaped regex, so a BitBucket URL is
        # accepted verbatim (D7). ``BitBucketClientError`` is imported lazily so
        # the GitHub-only hot path never pays for the httpx import that the
        # BitBucket client drags in (mirrors ``forge.make_forge_client``).
        from awf.common.bitbucket_client import BitBucketClientError

        repo = RepoRef.from_url(remote_url or repo_url)
        try:
            url = await forge_client.create_pull_request(
                repo=repo,
                base=base_branch,
                head=branch_name,
                title=title,
                body=body,
            )
        except GitHubClientError as exc:
            # Preserve the structured returncode + already-redacted stderr +
            # operation (never ``str(exc)``, which could leak unredacted output);
            # the executor keys its audit branch off ``exc.operation``.
            raise PullRequestError(
                operation=exc.operation,
                returncode=exc.returncode,
                stderr=exc.stderr,
                head_sha=head_sha,
            ) from exc
        except BitBucketClientError as exc:
            # BitBucket uses HTTP status (``None`` for transport errors) and a
            # redacted ``body`` where GitHub uses returncode/stderr — map them
            # onto the same structured PullRequestError fields.
            raise PullRequestError(
                operation=exc.operation,
                returncode=exc.status if exc.status is not None else 0,
                stderr=exc.body,
                head_sha=head_sha,
            ) from exc

        if not url:
            raise PullRequestError(
                operation="create_pull_request (no URL)",
                returncode=0,
                stderr="forge returned an empty PR URL",
                head_sha=head_sha,
            )

        _log.info("pr.created", branch=branch_name, url=url)
        return PullRequestResult(url=url, branch=branch_name, head_sha=head_sha)

    async def _log_pre_push_diagnostics(
        self,
        *,
        worktree_path: Path,
        branch_name: str,
        base_branch: str,
    ) -> str | None:
        """Capture the local git state right before the push fires.

        Three queries, one structured log line:

          * Current HEAD SHA — tells us whether HEAD has a real commit.
          * Current branch (``--abbrev-ref HEAD``) — tells us whether we're
            on the branch we think we're about to push, or detached, or
            on some branch the agent accidentally switched to.
          * Commit list ahead of base — tells us whether the branch has
            anything to push at all. If this is empty on a workspace that
            validated green, something between the fix-cycle commits and
            the push reverted or lost them.

        We deliberately don't raise on any of these queries failing —
        they're diagnostic only. Normal push either succeeds (fine) or
        fails with a real error (triaged by the push step below).
        """
        head_sha = await self._runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                "rev-parse",
                "HEAD",
            ]
        )
        current_branch = await self._runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                "rev-parse",
                "--abbrev-ref",
                "HEAD",
            ]
        )
        ahead_of_base = await self._runner.run(
            [
                "git",
                *git_safe_directory_config_args(worktree_path),
                "-C",
                str(worktree_path),
                "log",
                f"origin/{base_branch}..HEAD",
                "--oneline",
                "--no-decorate",
            ]
        )
        # Include each diagnostic's exit code so the log reader can
        # tell "command failed; stdout empty" apart from "command
        # succeeded; state genuinely unknown". Without the rc, an
        # ``rev-parse HEAD`` failure produced the same log shape as a
        # legitimately-empty worktree, which cost triage time during
        # the T39 incident.
        commits = [line for line in ahead_of_base.stdout.splitlines() if line.strip()]
        truncated = len(commits) > _MAX_DIAGNOSTIC_COMMITS
        _log.info(
            "pr_creator.pre_push_state",
            worktree=str(worktree_path),
            push_target_branch=branch_name,
            current_branch=current_branch.stdout.strip() or "<unknown>",
            current_branch_rc=current_branch.returncode,
            head_sha=head_sha.stdout.strip() or "<unknown>",
            head_sha_rc=head_sha.returncode,
            # Bound the list to ``_MAX_DIAGNOSTIC_COMMITS`` so a branch
            # hundreds of commits ahead of base doesn't blow past
            # log-backend payload limits. ``commits_ahead_total`` lets
            # the reader know the full count even when truncated.
            commits_ahead_of_base=commits[:_MAX_DIAGNOSTIC_COMMITS],
            commits_ahead_total=len(commits),
            commits_ahead_truncated=truncated,
            commits_ahead_rc=ahead_of_base.returncode,
            base_branch=base_branch,
        )
        return head_sha.stdout.strip() or None
