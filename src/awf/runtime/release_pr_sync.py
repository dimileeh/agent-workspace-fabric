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
from awf.common.forge import ForgeClient
from awf.common.github_client import (
    GitHubClientError,
    PullRequestAdoptionMetadata,
    RepoRef,
    fetch_pull_request_adoption_metadata,
    list_open_pull_requests_for_branch,
    parse_github_pull_request_url,
)
from awf.common.logging import get_logger

_log = get_logger(__name__)

NO_CHANGES_REASON_CODE = "NO_CHANGES_TO_SYNC"

# Distinct from ``FORGE_NOT_SUPPORTED``: the forge itself *is* supported (GitHub or
# BitBucket Cloud), but release-PR sync is GitHub-only — it shells ``gh pr list`` /
# ``gh pr view`` and parses github.com-only PR URLs. Mirrors
# ``OPEN_PR_RESOLVER_FORGE_NOT_SUPPORTED`` and ``PR_ADOPTION_METADATA_FETCH_GITHUB_ONLY``.
RELEASE_SYNC_FORGE_NOT_SUPPORTED_REASON_CODE = "RELEASE_SYNC_FORGE_NOT_SUPPORTED"


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


def _is_duplicate_pull_request_error(exc: GitHubClientError) -> bool:
    """True when ``gh pr create`` failed because the PR already exists.

    ``gh`` exits 1 with stderr like ``a pull request for branch "X" into
    branch "Y" already exists`` when a concurrent run or a human opened the
    source→target PR first. Only this signal is treated as a recoverable
    TOCTOU race; auth/network/branch-protection failures must propagate.
    """

    return exc.returncode == 1 and "already exists" in exc.stderr.lower()


async def _find_open_same_repo_pr_number(
    *,
    runner: AsyncCommandRunner,
    repo: RepoRef,
    source_branch: str,
    target_branch: str,
) -> int | None:
    """Number of an open same-repo ``source→target`` PR, or ``None`` if none.

    ``gh pr list --head`` matches by branch name alone, so a fork PR opened
    against this repo with an identically-named head branch can show up here.
    Only a PR whose head lives in the requested repo is eligible; fork
    collisions are ignored.
    """

    repo_slug = repo.slug()
    existing = await list_open_pull_requests_for_branch(
        runner=runner,
        repo=repo,
        branch_name=source_branch,
        base_branch=target_branch,
    )
    same_repo_existing = [pr for pr in existing if pr.head_repo_slug.lower() == repo_slug.lower()]
    return same_repo_existing[0].number if same_repo_existing else None


async def find_or_create_release_pr(
    *,
    runner: AsyncCommandRunner,
    gh: ForgeClient,
    repo: RepoRef,
    source_branch: str,
    target_branch: str,
    title: str,
    body: str,
) -> tuple[PullRequestAdoptionMetadata, bool]:
    """Reuse an open ``source→target`` PR if present, else create one.

    Returns the resolved adoption metadata plus a ``created`` flag.
    """

    if repo.forge != "github":
        # Release-PR sync is GitHub-only: the open-PR lookup shells ``gh pr
        # list``, adoption metadata shells ``gh pr view``, and the created-PR URL
        # is parsed with the github.com-only ``parse_github_pull_request_url`` —
        # only ``gh.create_pull_request`` is forge-neutral. BitBucket Cloud is a
        # *supported* forge (issue #345 Part 2), so it clears the executor forge
        # gate and reaches here; without this guard those GitHub-only steps would
        # mis-route to github.com for the same owner/repo slug (a different
        # repository) or reject the bitbucket.org create URL as
        # ``RELEASE_SYNC_PR_URL_INVALID``. Fail closed with an honest reason code
        # before any ``gh`` call — a forge-neutral release sync is deferred
        # follow-up work. Mirrors ``OPEN_PR_RESOLVER_FORGE_NOT_SUPPORTED`` and
        # ``PR_ADOPTION_METADATA_FETCH_GITHUB_ONLY``.
        raise ReleasePrSyncError(
            reason_code=RELEASE_SYNC_FORGE_NOT_SUPPORTED_REASON_CODE,
            message=(
                "release-PR sync is GitHub-only (shells `gh pr list` / `gh pr view` "
                f"and parses github.com PR URLs); forge {repo.forge!r} requires a "
                "forge-neutral release sync."
            ),
            detail={
                "repo_slug": repo.slug(),
                "forge": repo.forge,
                "source_branch": source_branch,
                "target_branch": target_branch,
            },
        )

    repo_slug = repo.slug()
    existing_number = await _find_open_same_repo_pr_number(
        runner=runner,
        repo=repo,
        source_branch=source_branch,
        target_branch=target_branch,
    )
    if existing_number is not None:
        pr_number = existing_number
        created = False
    else:
        try:
            pr_url = await gh.create_pull_request(
                repo=repo,
                base=target_branch,
                head=source_branch,
                title=title,
                body=body,
            )
        except GitHubClientError as exc:
            # TOCTOU: the list above can race a concurrent sync run or a human
            # opening the same source→target PR before this create. GitHub
            # rejects the duplicate with a recognisable "already exists" error,
            # so only then re-check and adopt the now-existing PR instead of
            # failing the workspace. Any other failure (auth, network, branch
            # protection) re-raises immediately with its original context
            # rather than being masked by a second, unrelated list call.
            if not _is_duplicate_pull_request_error(exc):
                raise
            raced_number = await _find_open_same_repo_pr_number(
                runner=runner,
                repo=repo,
                source_branch=source_branch,
                target_branch=target_branch,
            )
            if raced_number is None:
                raise
            pr_number = raced_number
            created = False
        else:
            try:
                parsed_repo, pr_number = parse_github_pull_request_url(pr_url)
            except ValueError as exc:
                raise ReleasePrSyncError(
                    reason_code="RELEASE_SYNC_PR_URL_INVALID",
                    message=f"gh pr create returned an unparseable PR URL: {pr_url!r}",
                    detail={"source_branch": source_branch, "target_branch": target_branch},
                ) from exc
            if parsed_repo.slug().lower() != repo_slug.lower():
                raise ReleasePrSyncError(
                    reason_code="RELEASE_SYNC_PR_REPO_MISMATCH",
                    message=(
                        f"gh pr create returned a PR URL for a different repository: {pr_url!r}"
                    ),
                    detail={
                        "expected_repo": repo_slug,
                        "parsed_repo": parsed_repo.slug(),
                        "source_branch": source_branch,
                        "target_branch": target_branch,
                    },
                )
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
    gh: ForgeClient,
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
