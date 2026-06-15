"""Pull-request creator — pushes the feature branch and opens or reuses a PR.

Two responsibilities:

1. ``git -C <worktree> push -u origin <branch>`` — uploads the commits the
   coding CLI just made to the remote. When updating an adopted fork PR through
   an explicit push URL, AWF omits ``-u`` so credentialed URLs are not persisted
   in branch upstream config. The push is forge-neutral plain git.
2. Open the PR via the injected :class:`~awf.common.forge.ForgeClient` when one
   does not already exist. The forge client (GitHub or Bitbucket) is resolved by
   the caller and passed in per-call, so ``PullRequestCreator`` stays
   forge-agnostic and stateless. The client returns the PR URL directly.

``PullRequestCreator`` shells plain ``git`` for the push but never shells the
forge CLI itself — the resolved ``ForgeClient`` owns the forge-specific PR-open
call (``gh pr create`` for GitHub, the REST API for Bitbucket).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from awf.common.commands import AsyncCommandRunner
from awf.common.forge import ForgeClient
from awf.common.git_identity import git_safe_directory_config_args
from awf.common.github_client import (
    BranchOpenPullRequest,
    GitHubClientError,
    PullRequestMetadataError,
    RepoRef,
    list_open_pull_requests_for_branch,
)
from awf.common.github_transient import is_transient_github_error_text
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
_PR_CREATE_TRANSIENT_MAX_RETRIES = 3
_PR_CREATE_TRANSIENT_INITIAL_BACKOFF_SECONDS = 5.0
_PR_CREATE_TRANSIENT_MAX_BACKOFF_SECONDS = 30.0

_Sleep = Callable[[float], Awaitable[None]]


def _redact_credentials(text: str) -> str:
    """Replace ``https://user[:pwd]@host`` patterns with ``https://***@host``
    so push/pr-create stderr can be safely logged."""
    return _URL_CREDENTIAL_PATTERN.sub(r"\1***@", text)


def _short_branch_name(branch_name: str) -> str:
    """Return the branch name without a leading ``refs/heads/`` prefix."""
    return branch_name.removeprefix(_HEADS_REF_PREFIX)


def _is_duplicate_pull_request_error(exc: GitHubClientError) -> bool:
    return exc.returncode == 1 and "already exists" in exc.stderr.lower()


def _short_error(text: str) -> str:
    cleaned = text.strip() or "<no output>"
    return cleaned[:1000]


def _github_pr_create_failure_detail(
    exc: GitHubClientError,
    *,
    attempt: int,
    transient: bool,
    duplicate: bool,
) -> dict[str, object]:
    return {
        "attempt": attempt,
        "operation": exc.operation,
        "returncode": exc.returncode,
        "error_message": _short_error(exc.stderr),
        "transient": transient,
        "duplicate": duplicate,
        "will_retry": False,
    }


def _branch_open_pr_metadata(pr: BranchOpenPullRequest) -> dict[str, object]:
    metadata: dict[str, object] = {
        "number": pr.number,
        "url": pr.url,
        "head_ref": pr.head_ref,
        "head_repo_slug": pr.head_repo_slug,
    }
    if pr.head_sha is not None:
        metadata["head_sha"] = pr.head_sha
    return metadata


def _github_pr_create_details(
    *,
    strategy: str,
    attempts: int,
    max_retries: int,
    failures: list[dict[str, object]],
    lookups: list[dict[str, object]],
    matched_pr: dict[str, object] | None = None,
) -> dict[str, object]:
    details: dict[str, object] = {
        "strategy": strategy,
        "attempts": attempts,
        "retry_count": max(attempts - 1, 0),
        "max_retries": max_retries,
        "failures": list(failures),
        "reconcile_lookups": list(lookups),
    }
    if matched_pr is not None:
        details["matched_pr"] = matched_pr
    return details


def _pull_request_error_from_github(
    exc: GitHubClientError,
    *,
    head_sha: str | None,
    details: dict[str, object] | None = None,
) -> PullRequestError:
    return PullRequestError(
        operation=exc.operation,
        returncode=exc.returncode,
        stderr=exc.stderr,
        head_sha=head_sha,
        details=details,
    )


@dataclass(frozen=True)
class PullRequestResult:
    url: str
    branch: str
    head_sha: str | None = None
    open_metadata: dict[str, object] | None = None


class PullRequestError(Exception):
    """Raised when the git push or the forge PR-open call fails."""

    def __init__(
        self,
        *,
        operation: str,
        returncode: int,
        stderr: str,
        head_sha: str | None = None,
        reason_code: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        self.operation = operation
        self.returncode = returncode
        self.stderr = stderr
        self.head_sha = head_sha
        self.details = details
        # Forge clients that carry an actionable reason code (e.g.
        # ``BitbucketClientError`` with auth / rate-limit / transport codes)
        # propagate it here so the executor records the specific doctor
        # guidance on the failed workspace instead of a generic
        # ``PR_CREATE_FAILED``. ``GitHubClientError`` has no reason code, and
        # the push / no-client / no-URL raises leave this ``None``.
        self.reason_code = reason_code
        super().__init__(
            f"{operation} failed (exit={returncode}): {stderr.strip() or '<no output>'}"
        )


class PullRequestCreator:
    """Pushes a feature branch and opens a PR via an injected ``ForgeClient``."""

    def __init__(
        self,
        runner: AsyncCommandRunner,
        *,
        pr_create_transient_max_retries: int = _PR_CREATE_TRANSIENT_MAX_RETRIES,
        pr_create_transient_initial_backoff_seconds: float = (
            _PR_CREATE_TRANSIENT_INITIAL_BACKOFF_SECONDS
        ),
        pr_create_transient_max_backoff_seconds: float = _PR_CREATE_TRANSIENT_MAX_BACKOFF_SECONDS,
        sleep: _Sleep = asyncio.sleep,
    ) -> None:
        self._runner = runner
        self._pr_create_transient_max_retries: int = max(pr_create_transient_max_retries, 0)
        self._pr_create_transient_initial_backoff_seconds: float = max(
            pr_create_transient_initial_backoff_seconds,
            0.0,
        )
        self._pr_create_transient_max_backoff_seconds: float = max(
            pr_create_transient_max_backoff_seconds,
            0.0,
        )
        self._sleep: _Sleep = sleep

    async def push_and_open(
        self,
        *,
        worktree_path: Path,
        branch_name: str,
        base_branch: str,
        title: str,
        body: str,
        forge_client: ForgeClient | None = None,
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
        # (GitHub or Bitbucket) owns auth and the forge-specific call and returns
        # the PR URL directly — no github-shaped regex, so a Bitbucket URL is
        # accepted verbatim (D7). ``BitbucketClientError`` is imported lazily so
        # the GitHub-only hot path never pays for the httpx import that the
        # Bitbucket client drags in (mirrors ``forge.make_forge_client``).
        from awf.common.bitbucket_client import BitbucketClientError

        if forge_client is None:  # pragma: no cover - open path always supplies one
            # The reuse path (``existing_pr_url`` set) returns above without ever
            # touching the forge client, so callers may omit it there. Opening a
            # new PR requires one; treat a missing client as a forge failure
            # rather than an ``AttributeError`` so it flows through the same
            # structured error handling.
            raise PullRequestError(
                operation="create_pull_request (no forge client)",
                returncode=0,
                stderr="forge client required to open a new PR",
                head_sha=head_sha,
            )

        repo = RepoRef.from_url(remote_url or repo_url)
        try:
            if repo.forge == "github":
                return await self._create_github_pull_request_with_redundancy(
                    forge_client=forge_client,
                    repo=repo,
                    base_branch=base_branch,
                    branch_name=branch_name,
                    title=title,
                    body=body,
                    head_sha=head_sha,
                )
            url = await forge_client.create_pull_request(
                repo=repo,
                base=base_branch,
                head=branch_name,
                title=title,
                body=body,
            )
        except PullRequestError:
            raise
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
        except BitbucketClientError as exc:
            # Bitbucket uses HTTP status (``None`` for transport errors) and a
            # redacted ``body`` where GitHub uses returncode/stderr — map them
            # onto the same structured PullRequestError fields. Preserve
            # ``exc.reason_code`` (auth / rate-limit / transport) so the
            # executor records the specific doctor guidance instead of a
            # generic ``PR_CREATE_FAILED`` (mirrors the other Bitbucket
            # handoff paths that flow ``reason_code`` end-to-end).
            raise PullRequestError(
                operation=exc.operation,
                returncode=exc.status if exc.status is not None else 0,
                stderr=exc.body,
                head_sha=head_sha,
                reason_code=exc.reason_code,
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

    async def _create_github_pull_request_with_redundancy(
        self,
        *,
        forge_client: ForgeClient,
        repo: RepoRef,
        base_branch: str,
        branch_name: str,
        title: str,
        body: str,
        head_sha: str | None,
    ) -> PullRequestResult:
        failures: list[dict[str, object]] = []
        lookups: list[dict[str, object]] = []
        attempt = 0

        while True:
            attempt += 1
            try:
                url = await forge_client.create_pull_request(
                    repo=repo,
                    base=base_branch,
                    head=branch_name,
                    title=title,
                    body=body,
                )
            except GitHubClientError as exc:
                duplicate = _is_duplicate_pull_request_error(exc)
                transient = is_transient_github_error_text(
                    operation=exc.operation,
                    stderr=exc.stderr,
                )
                failure = _github_pr_create_failure_detail(
                    exc,
                    attempt=attempt,
                    transient=transient,
                    duplicate=duplicate,
                )
                failures.append(failure)
                if not transient and not duplicate:
                    raise _pull_request_error_from_github(
                        exc,
                        head_sha=head_sha,
                        details=_github_pr_create_details(
                            strategy="failed",
                            attempts=attempt,
                            max_retries=self._pr_create_transient_max_retries,
                            failures=failures,
                            lookups=lookups,
                        ),
                    ) from exc

                existing_pr, lookup_detail = await self._lookup_same_repo_open_pr(
                    repo=repo,
                    branch_name=branch_name,
                    base_branch=base_branch,
                )
                lookups.append(lookup_detail)
                if existing_pr is not None:
                    metadata = _github_pr_create_details(
                        strategy=(
                            "reconciled_after_duplicate"
                            if duplicate
                            else "reconciled_after_transient"
                        ),
                        attempts=attempt,
                        max_retries=self._pr_create_transient_max_retries,
                        failures=failures,
                        lookups=lookups,
                        matched_pr=_branch_open_pr_metadata(existing_pr),
                    )
                    _log.info(
                        "pr_creator.github_pr_create_reconciled",
                        repo=repo.slug(),
                        branch=branch_name,
                        base_branch=base_branch,
                        attempt=attempt,
                        pr_url=existing_pr.url,
                        pr_number=existing_pr.number,
                        strategy=metadata["strategy"],
                    )
                    return PullRequestResult(
                        url=existing_pr.url,
                        branch=existing_pr.head_ref or branch_name,
                        head_sha=existing_pr.head_sha or head_sha,
                        open_metadata=metadata,
                    )

                lookup_failed = lookup_detail.get("status") == "failed"
                if duplicate and lookup_failed and attempt <= self._pr_create_transient_max_retries:
                    wait_seconds = self._pr_create_retry_wait_seconds(attempt)
                    failure["will_retry"] = True
                    failure["wait_seconds"] = wait_seconds
                    _log.warning(
                        "pr_creator.github_pr_create_duplicate_lookup_retrying",
                        repo=repo.slug(),
                        branch=branch_name,
                        base_branch=base_branch,
                        attempt=attempt,
                        max_retries=self._pr_create_transient_max_retries,
                        wait_seconds=wait_seconds,
                        reason_code=lookup_detail.get("reason_code"),
                        error=failure["error_message"],
                    )
                    if wait_seconds > 0:
                        await self._sleep(wait_seconds)
                    continue

                if duplicate:
                    raise _pull_request_error_from_github(
                        exc,
                        head_sha=head_sha,
                        details=_github_pr_create_details(
                            strategy=(
                                "duplicate_lookup_failed"
                                if lookup_failed
                                else "duplicate_lookup_missed"
                            ),
                            attempts=attempt,
                            max_retries=self._pr_create_transient_max_retries,
                            failures=failures,
                            lookups=lookups,
                        ),
                    ) from exc

                if attempt > self._pr_create_transient_max_retries:
                    details = _github_pr_create_details(
                        strategy="transient_retry_exhausted",
                        attempts=attempt,
                        max_retries=self._pr_create_transient_max_retries,
                        failures=failures,
                        lookups=lookups,
                    )
                    _log.warning(
                        "pr_creator.github_pr_create_retry_exhausted",
                        repo=repo.slug(),
                        branch=branch_name,
                        base_branch=base_branch,
                        attempt=attempt,
                        max_retries=self._pr_create_transient_max_retries,
                        error=failure["error_message"],
                    )
                    raise _pull_request_error_from_github(
                        exc,
                        head_sha=head_sha,
                        details=details,
                    ) from exc

                wait_seconds = self._pr_create_retry_wait_seconds(attempt)
                failure["will_retry"] = True
                failure["wait_seconds"] = wait_seconds
                _log.warning(
                    "pr_creator.github_pr_create_retrying",
                    repo=repo.slug(),
                    branch=branch_name,
                    base_branch=base_branch,
                    attempt=attempt,
                    max_retries=self._pr_create_transient_max_retries,
                    wait_seconds=wait_seconds,
                    error=failure["error_message"],
                )
                if wait_seconds > 0:
                    await self._sleep(wait_seconds)
                continue

            if not url:
                raise PullRequestError(
                    operation="create_pull_request (no URL)",
                    returncode=0,
                    stderr="forge returned an empty PR URL",
                    head_sha=head_sha,
                    details=_github_pr_create_details(
                        strategy="failed",
                        attempts=attempt,
                        max_retries=self._pr_create_transient_max_retries,
                        failures=failures,
                        lookups=lookups,
                    ),
                )
            open_metadata = (
                _github_pr_create_details(
                    strategy="created_after_retry",
                    attempts=attempt,
                    max_retries=self._pr_create_transient_max_retries,
                    failures=failures,
                    lookups=lookups,
                )
                if failures
                else None
            )
            _log.info("pr.created", branch=branch_name, url=url, attempts=attempt)
            return PullRequestResult(
                url=url,
                branch=branch_name,
                head_sha=head_sha,
                open_metadata=open_metadata,
            )

    async def _lookup_same_repo_open_pr(
        self,
        *,
        repo: RepoRef,
        branch_name: str,
        base_branch: str,
    ) -> tuple[BranchOpenPullRequest | None, dict[str, object]]:
        try:
            candidates = await list_open_pull_requests_for_branch(
                runner=self._runner,
                repo=repo,
                branch_name=branch_name,
                base_branch=base_branch,
            )
        except PullRequestMetadataError as exc:
            lookup_failure_detail: dict[str, object] = {
                "status": "failed",
                "reason_code": exc.reason_code,
                "message": exc.message[:500],
            }
            _log.warning(
                "pr_creator.github_pr_create_reconcile_lookup_failed",
                repo=repo.slug(),
                branch=branch_name,
                base_branch=base_branch,
                reason_code=exc.reason_code,
                error=exc.message[:500],
            )
            return None, lookup_failure_detail

        repo_slug = repo.slug().lower()
        same_repo = [
            candidate for candidate in candidates if candidate.head_repo_slug.lower() == repo_slug
        ]
        lookup_detail: dict[str, object] = {
            "status": "found" if same_repo else "not_found",
            "candidate_count": len(candidates),
            "same_repo_count": len(same_repo),
            "fork_collision_count": len(candidates) - len(same_repo),
        }
        if not same_repo:
            return None, lookup_detail
        match = same_repo[0]
        lookup_detail["matched_pr"] = _branch_open_pr_metadata(match)
        return match, lookup_detail

    def _pr_create_retry_wait_seconds(self, attempt: int) -> float:
        wait = self._pr_create_transient_initial_backoff_seconds * (2 ** (attempt - 1))
        return float(min(wait, self._pr_create_transient_max_backoff_seconds))

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
