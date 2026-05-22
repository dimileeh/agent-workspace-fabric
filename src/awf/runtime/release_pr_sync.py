"""Release-PR sync — compute source→target divergence and find/create the PR.

Pure, unit-testable helpers backing the executor's ``sync_release_pr`` handoff.
No DB access and no workspace state here: callers pass an ``AsyncCommandRunner``
(for local git in the worktree), a ``GitHubClient`` (for ``gh``), and a parsed
``RepoRef``. The handoff owns persistence + monitor launch.

Flow:

1. ``git fetch origin`` then ``git rev-list --count origin/<target>..origin/<source>``
   to learn how many commits ``source`` is ahead of ``target``.
2. If zero, return a :class:`ReleasePrSyncNoOp` — the handoff completes the
   workspace cleanly without opening a PR or running the monitor.
3. Otherwise reuse an existing open ``source→target`` PR, or create one, then
   resolve its full adoption metadata and return a :class:`ReleasePrSyncResult`.

Generic AWF core behaviour — no hard-coded repositories or branch names.
"""

from __future__ import annotations

from dataclasses import dataclass

from awf.common.commands import AsyncCommandRunner
from awf.common.github_client import (
    GitHubClient,
    PullRequestAdoptionMetadata,
    RepoRef,
    fetch_pull_request_adoption_metadata,
    list_open_pull_requests_for_branch,
    parse_github_pull_request_url,
)
from awf.common.logging import get_logger

_log = get_logger(__name__)

NO_CHANGES_REASON_CODE = "NO_CHANGES_TO_SYNC"


class ReleasePrSyncError(Exception):
    """Structured failure while preparing a release-PR sync."""

    def __init__(
        self,
        *,
        reason_code: str,
        message: str,
        detail: dict[str, object] | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.message = message
        self.detail = detail
        super().__init__(message)


@dataclass(frozen=True)
class ReleasePrSyncNoOp:
    """No commits to sync — the workspace should complete without a PR."""

    source_branch: str
    target_branch: str
    reason_code: str = NO_CHANGES_REASON_CODE


@dataclass(frozen=True)
class ReleasePrSyncResult:
    """A release PR exists (reused or freshly created) and is ready to monitor."""

    metadata: PullRequestAdoptionMetadata
    created: bool
    commits_ahead: int
    source_branch: str
    target_branch: str


async def count_commits_ahead(
    *,
    runner: AsyncCommandRunner,
    cwd: str,
    source_branch: str,
    target_branch: str,
) -> int:
    """Return how many commits ``origin/<source>`` is ahead of ``origin/<target>``."""

    fetch = await runner.run(
        ["git", "fetch", "origin", target_branch, source_branch],
        cwd=cwd,
    )
    if fetch.returncode != 0:
        raise ReleasePrSyncError(
            reason_code="RELEASE_SYNC_FETCH_FAILED",
            message=(fetch.stderr or f"git fetch origin exited {fetch.returncode}").strip(),
            detail={"source_branch": source_branch, "target_branch": target_branch},
        )
    rev_list = await runner.run(
        [
            "git",
            "rev-list",
            "--count",
            f"origin/{target_branch}..origin/{source_branch}",
        ],
        cwd=cwd,
    )
    if rev_list.returncode != 0:
        raise ReleasePrSyncError(
            reason_code="RELEASE_SYNC_REV_LIST_FAILED",
            message=(rev_list.stderr or f"git rev-list exited {rev_list.returncode}").strip(),
            detail={"source_branch": source_branch, "target_branch": target_branch},
        )
    text = rev_list.stdout.strip()
    try:
        return int(text)
    except ValueError as exc:
        raise ReleasePrSyncError(
            reason_code="RELEASE_SYNC_REV_LIST_INVALID",
            message=f"git rev-list --count returned non-numeric output: {text!r}",
            detail={"source_branch": source_branch, "target_branch": target_branch},
        ) from exc


async def find_or_create_release_pr(
    *,
    runner: AsyncCommandRunner,
    gh: GitHubClient,
    repo: RepoRef,
    source_branch: str,
    target_branch: str,
    title: str,
    body: str,
) -> tuple[PullRequestAdoptionMetadata, bool]:
    """Reuse an open ``source→target`` PR if present, else create one.

    Returns the resolved adoption metadata plus a ``created`` flag.
    """

    existing = await list_open_pull_requests_for_branch(
        runner=runner,
        repo=repo,
        branch_name=source_branch,
        base_branch=target_branch,
    )
    if existing:
        pr_number = existing[0].number
        created = False
    else:
        pr_url = await gh.create_pull_request(
            repo=repo,
            base=target_branch,
            head=source_branch,
            title=title,
            body=body,
        )
        try:
            _repo, pr_number = parse_github_pull_request_url(pr_url)
        except ValueError as exc:
            raise ReleasePrSyncError(
                reason_code="RELEASE_SYNC_PR_URL_INVALID",
                message=f"gh pr create returned an unparseable PR URL: {pr_url!r}",
                detail={"source_branch": source_branch, "target_branch": target_branch},
            ) from exc
        created = True
    metadata = await fetch_pull_request_adoption_metadata(
        runner=runner,
        repo=repo,
        pr_number=pr_number,
    )
    return metadata, created


async def prepare_release_pr_sync(
    *,
    runner: AsyncCommandRunner,
    gh: GitHubClient,
    repo: RepoRef,
    cwd: str,
    source_branch: str,
    target_branch: str,
    title: str,
    body: str,
) -> ReleasePrSyncNoOp | ReleasePrSyncResult:
    """Decide what the handoff should do: no-op, or monitor a (reused/new) PR."""

    commits_ahead = await count_commits_ahead(
        runner=runner,
        cwd=cwd,
        source_branch=source_branch,
        target_branch=target_branch,
    )
    if commits_ahead <= 0:
        _log.info(
            "release_pr_sync.no_changes",
            repo=repo.slug(),
            source_branch=source_branch,
            target_branch=target_branch,
        )
        return ReleasePrSyncNoOp(source_branch=source_branch, target_branch=target_branch)

    metadata, created = await find_or_create_release_pr(
        runner=runner,
        gh=gh,
        repo=repo,
        source_branch=source_branch,
        target_branch=target_branch,
        title=title,
        body=body,
    )
    _log.info(
        "release_pr_sync.pr_ready",
        repo=repo.slug(),
        source_branch=source_branch,
        target_branch=target_branch,
        pr_number=metadata.number,
        created=created,
        commits_ahead=commits_ahead,
    )
    return ReleasePrSyncResult(
        metadata=metadata,
        created=created,
        commits_ahead=commits_ahead,
        source_branch=source_branch,
        target_branch=target_branch,
    )


def release_pr_title(*, source_branch: str, target_branch: str) -> str:
    return f"Release: merge {source_branch} into {target_branch}"


def release_pr_body(*, source_branch: str, target_branch: str) -> str:
    return (
        f"Automated AWF release PR syncing `{source_branch}` into `{target_branch}`.\n\n"
        "Opened by the `sync_release_pr` task kind and monitored with "
        "release/manual behavior (auto-merge disabled). Merging into the "
        "release target stays human-gated."
    )
