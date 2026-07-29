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

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from awf.common.commands import AsyncCommandRunner
from awf.common.forge import ForgeClient
from awf.common.github_client import (
    GitHubClient,
    GitHubClientError,
    PullRequestAdoptionMetadata,
    RepoRef,
    fetch_pull_request_adoption_metadata,
    list_open_pull_requests_for_branch,
    parse_github_pull_request_url,
)
from awf.common.github_retry import RetryPolicy
from awf.common.logging import get_logger

_log = get_logger(__name__)

NO_CHANGES_REASON_CODE = "NO_CHANGES_TO_SYNC"

# Distinct from ``FORGE_NOT_SUPPORTED``: the forge itself *is* supported (GitHub or
# Bitbucket Cloud), but release-PR sync is GitHub-only — it shells ``gh pr list`` /
# ``gh pr view`` and parses github.com-only PR URLs. Mirrors
# ``OPEN_PR_RESOLVER_FORGE_NOT_SUPPORTED`` and ``PR_ADOPTION_METADATA_FETCH_GITHUB_ONLY``.
RELEASE_SYNC_FORGE_NOT_SUPPORTED_REASON_CODE = "RELEASE_SYNC_FORGE_NOT_SUPPORTED"
# A reused PR body whose AWF merge-policy markers form anything other than a single,
# correctly ordered start→end pair (orphan marker, reversed order, or duplicate
# blocks). Reconciling such a layout by pairing the first start with the first end
# would splice across intervening human-authored content and delete it, so we fail
# closed and surface the tangle to a human instead.
RELEASE_SYNC_POLICY_MARKERS_MALFORMED_REASON_CODE = "RELEASE_SYNC_POLICY_MARKERS_MALFORMED"
_RELEASE_PR_CREATE_TRANSIENT_MAX_RETRIES = 3

SleepFn = Callable[[float], Awaitable[None]]


def ensure_release_sync_forge_supported(
    forge: str,
    *,
    repo_slug: str,
    source_branch: str,
    target_branch: str,
) -> None:
    """Fail closed before any ``gh`` call when release-PR sync hits a non-GitHub forge.

    Release-PR sync is GitHub-only: the open-PR lookup shells ``gh pr list``,
    adoption metadata shells ``gh pr view``, and the created-PR URL is parsed with
    the github.com-only ``parse_github_pull_request_url`` — only
    ``gh.create_pull_request`` is forge-neutral. Bitbucket Cloud is a *supported*
    forge (issue #345 Part 2), so it clears the executor forge gate; without this
    guard those GitHub-only steps would mis-route to github.com for the same
    owner/repo slug (a different repository) or reject the bitbucket.org create URL
    as ``RELEASE_SYNC_PR_URL_INVALID``. Callers invoke this both inside
    :func:`find_or_create_release_pr` and *before constructing the forge client* in
    the executor handoff, so a missing-credential ``BitbucketClient.from_env()``
    cannot mask this honest reason code with ``BITBUCKET_AUTH_NOT_CONFIGURED``.
    Mirrors ``OPEN_PR_RESOLVER_FORGE_NOT_SUPPORTED`` and
    ``PR_ADOPTION_METADATA_FETCH_GITHUB_ONLY``.
    """
    if forge == "github":
        return
    raise ReleasePrSyncError(
        reason_code=RELEASE_SYNC_FORGE_NOT_SUPPORTED_REASON_CODE,
        message=(
            "release-PR sync is GitHub-only (shells `gh pr list` / `gh pr view` "
            f"and parses github.com PR URLs); forge {forge!r} requires a "
            "forge-neutral release sync."
        ),
        detail={
            "repo_slug": repo_slug,
            "forge": forge,
            "source_branch": source_branch,
            "target_branch": target_branch,
        },
    )


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

    The single pre-create lookup must surface a lookup failure cleanly, so the
    read is never retried here.
    """

    repo_slug = repo.slug()
    existing = await list_open_pull_requests_for_branch(
        runner=runner,
        repo=repo,
        branch_name=source_branch,
        base_branch=target_branch,
        retry_policy=RetryPolicy.NEVER,
    )
    same_repo_existing = [pr for pr in existing if pr.head_repo_slug.lower() == repo_slug.lower()]
    return same_repo_existing[0].number if same_repo_existing else None


async def _create_release_pr_with_redundancy(
    *,
    runner: AsyncCommandRunner,
    gh: ForgeClient,
    repo: RepoRef,
    source_branch: str,
    target_branch: str,
    title: str,
    body: str,
    sleep: SleepFn,
) -> tuple[int, bool]:
    del runner, sleep
    # Release-PR sync is GitHub-only — ``ensure_release_sync_forge_supported``
    # fails closed upstream — so ``gh`` is always a ``GitHubClient`` here. Narrow
    # it so the release path can wire its own transient-retry budget through
    # ``transient_max_attempts`` (retries + the initial attempt), mirroring
    # ``PullRequestCreator``. Without this, create silently falls back to the
    # transport default of five attempts instead of
    # ``_RELEASE_PR_CREATE_TRANSIENT_MAX_RETRIES``.
    assert isinstance(gh, GitHubClient)
    try:
        pr_url = await gh.create_pull_request(
            repo=repo,
            base=target_branch,
            head=source_branch,
            title=title,
            body=body,
            transient_max_attempts=_RELEASE_PR_CREATE_TRANSIENT_MAX_RETRIES + 1,
        )
    except GitHubClientError as exc:
        if exc.returncode == 0:
            raise ReleasePrSyncError(
                reason_code="RELEASE_SYNC_PR_URL_INVALID",
                message=f"gh pr create returned no parseable PR URL: {exc.stderr!r}",
                detail={"source_branch": source_branch, "target_branch": target_branch},
            ) from exc
        raise

    try:
        parsed_repo, pr_number = parse_github_pull_request_url(pr_url)
    except ValueError as exc:
        raise ReleasePrSyncError(
            reason_code="RELEASE_SYNC_PR_URL_INVALID",
            message=f"gh pr create returned an unparseable PR URL: {pr_url!r}",
            detail={"source_branch": source_branch, "target_branch": target_branch},
        ) from exc
    if parsed_repo.slug().lower() != repo.slug().lower():
        raise ReleasePrSyncError(
            reason_code="RELEASE_SYNC_PR_REPO_MISMATCH",
            message=f"gh pr create returned a PR URL for a different repository: {pr_url!r}",
            detail={
                "expected_repo": repo.slug(),
                "parsed_repo": parsed_repo.slug(),
                "source_branch": source_branch,
                "target_branch": target_branch,
            },
        )
    if isinstance(gh, GitHubClient):
        outcome = gh.last_pr_create_outcome
        if outcome is not None and outcome.strategy.startswith("reconciled_"):
            _log.info(
                "release_pr_sync.create_reconciled",
                repo=repo.slug(),
                source_branch=source_branch,
                target_branch=target_branch,
                attempt=outcome.attempts,
                pr_number=pr_number,
                failures=outcome.failures,
                reconcile_lookups=outcome.reconcile_lookups,
            )
            return pr_number, False
        if outcome is not None and outcome.failures:
            _log.info(
                "release_pr_sync.create_succeeded_after_retry",
                repo=repo.slug(),
                source_branch=source_branch,
                target_branch=target_branch,
                attempt=outcome.attempts,
                pr_number=pr_number,
                failures=outcome.failures,
                reconcile_lookups=outcome.reconcile_lookups,
            )
    return pr_number, True


async def find_or_create_release_pr(
    *,
    runner: AsyncCommandRunner,
    gh: ForgeClient,
    repo: RepoRef,
    source_branch: str,
    target_branch: str,
    title: str,
    body: str,
    sleep: SleepFn | None = None,
) -> tuple[PullRequestAdoptionMetadata, bool]:
    """Reuse an open ``source→target`` PR if present, else create one.

    Returns the resolved adoption metadata plus a ``created`` flag.
    """

    # Defense-in-depth: the executor handoff already gates the concrete client
    # forge before constructing the forge client, but this keeps the GitHub-only
    # contract enforced for every caller (and catches a github-client /
    # non-github-repo mismatch) before any ``gh`` call.
    ensure_release_sync_forge_supported(
        repo.forge,
        repo_slug=repo.slug(),
        source_branch=source_branch,
        target_branch=target_branch,
    )

    existing_number = await _find_open_same_repo_pr_number(
        runner=runner,
        repo=repo,
        source_branch=source_branch,
        target_branch=target_branch,
    )
    if existing_number is not None:
        pr_number = existing_number
        created = False
        # A reused open PR skips ``_create_release_pr_with_redundancy`` — the only
        # creator that applies ``body`` — so its description can still advertise a
        # stale merge policy (e.g. the "human-gated" manual text from an earlier
        # ``auto_merge=false`` open) that contradicts the now-resolved
        # ``auto_merge`` the attached monitor enforces. But the reused PR may be a
        # manually authored release PR carrying human release notes and checklists,
        # so overwriting the whole body would silently destroy that context on every
        # sync. Fetch the current body and reconcile *only* AWF's marker-delimited
        # merge-policy section, preserving everything else; skip the edit when the
        # body already matches so a no-op sync makes no write. Release-PR sync is
        # GitHub-only (``ensure_release_sync_forge_supported`` above), so ``gh`` is a
        # ``GitHubClient`` whose read/edit map to reason-coded failures.
        assert isinstance(gh, GitHubClient)
        existing_body = await gh.fetch_pull_request_body(repo=repo, pr_number=pr_number)
        reconciled_body = reconcile_release_pr_body(
            existing_body=existing_body, generated_body=body
        )
        if reconciled_body != existing_body:
            await gh.update_pull_request_body(repo=repo, pr_number=pr_number, body=reconciled_body)
    else:
        pr_number, created = await _create_release_pr_with_redundancy(
            runner=runner,
            gh=gh,
            repo=repo,
            source_branch=source_branch,
            target_branch=target_branch,
            title=title,
            body=body,
            sleep=sleep or asyncio.sleep,
        )
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


# HTML-comment fences (invisible in rendered Markdown) that delimit the single
# AWF-managed merge-policy paragraph inside a release PR body. Reconciling a
# reused PR rewrites *only* the text between these markers, so human-authored
# release notes and checklists elsewhere in the description survive every sync.
_MANAGED_POLICY_START = "<!-- AWF:release-merge-policy:start -->"
_MANAGED_POLICY_END = "<!-- AWF:release-merge-policy:end -->"


def _release_merge_policy_text(*, auto_merge: bool) -> str:
    """The bare AWF-managed merge-policy paragraph (no marker fences)."""

    if auto_merge:
        # Mirror the monitor selection in ``worker._pr_monitor_factory``: a resolved
        # ``auto_merge=True`` release-sync workspace runs the feature monitor and
        # squash-merges into the release target on green, so the body must not claim
        # the merge stays human-gated.
        return (
            "Opened by the `sync_release_pr` task kind with auto-merge enabled: "
            "AWF's monitor squash-merges into the release target once checks are "
            "green and review comments are addressed."
        )
    return (
        "Opened by the `sync_release_pr` task kind and monitored with "
        "release/manual behavior (auto-merge disabled). Merging into the "
        "release target stays human-gated."
    )


def _release_merge_policy_section(*, auto_merge: bool) -> str:
    """The marker-delimited AWF-managed merge-policy block for a release PR."""

    text = _release_merge_policy_text(auto_merge=auto_merge)
    return f"{_MANAGED_POLICY_START}\n{text}\n{_MANAGED_POLICY_END}"


_FENCE_OPEN_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})")


def _fence_closes(line: str, *, fence: str) -> bool:
    """Whether ``line`` closes a code fence opened with ``fence``."""

    return re.match(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$", line) is not None


def _advance_html_comment_state(line: str, *, in_comment: bool) -> bool:
    """Whether an HTML comment is still open after consuming ``line``."""

    index = 0
    while True:
        if in_comment:
            close = line.find("-->", index)
            if close == -1:
                return True
            in_comment = False
            index = close + len("-->")
        else:
            opening = line.find("<!--", index)
            if opening == -1:
                return False
            in_comment = True
            index = opening + len("<!--")


def _strip_legacy_policy_paragraph(body: str) -> str:
    """Remove a pre-marker AWF-generated merge-policy paragraph from ``body``.

    A release PR opened before the managed markers existed carries the old
    *unfenced* generated paragraph — either the "human-gated" manual text or the
    "auto-merge enabled" text — with no fences for
    :func:`_extract_managed_policy_section` to find. Left in place, the marker-only
    reconciliation would append the fresh section beside the stale paragraph,
    yielding a PR that simultaneously claims manual and automatic merge. Delete the
    exact generated paragraph text (both variants) while preserving surrounding
    human-authored content.

    Only a *standalone* occurrence — the paragraph alone on its own line, outside
    any container — is AWF-authored output. A human release note may quote the same
    sentences blockquoted, inside a list item, or embedded mid-line with commentary
    around it; it may also quote the old body verbatim in a fenced code block or
    park it in an HTML comment, where the paragraph does sit alone at column zero.
    An unrestricted replace would silently delete that human text (and leave a
    dangling bullet or a gutted code block), so the match is anchored to whole
    lines *and* skipped inside fenced code blocks and HTML comments.
    """

    legacy_paragraphs = {
        _release_merge_policy_text(auto_merge=auto_merge) for auto_merge in (False, True)
    }
    kept: list[str] = []
    fence: str | None = None
    in_comment = False
    for line in body.split("\n"):
        if in_comment:
            in_comment = _advance_html_comment_state(line, in_comment=True)
        elif fence is not None:
            if _fence_closes(line, fence=fence):
                fence = None
        elif (opened := _FENCE_OPEN_RE.match(line)) is not None:
            fence = opened.group("fence")
        elif line.rstrip(" \t") in legacy_paragraphs:
            # Blank the line rather than dropping it, so the surrounding paragraph
            # spacing a human wrote around the stale text is left untouched.
            kept.append("")
            continue
        else:
            in_comment = _advance_html_comment_state(line, in_comment=False)
        kept.append(line)
    return "\n".join(kept)


def _managed_marker_offsets(body: str) -> tuple[list[int], list[int]]:
    """Offsets of the live start/end managed markers in ``body``.

    Only markers at the top level are AWF's own output. A human may paste a whole
    managed block into a fenced example ("AWF renders this block:") or park one in
    an HTML comment; those markers are illustrative text. Counting them would let
    reconciliation rewrite the bytes inside the human's fence — leaving the real
    description with no policy statement — or read a lone fenced example beside the
    live block as a duplicate layout and wedge every later sync. Skip the same
    containers :func:`_strip_legacy_policy_paragraph` skips.
    """

    starts: list[int] = []
    ends: list[int] = []
    offset = 0
    fence: str | None = None
    in_comment = False
    for line in body.split("\n"):
        if in_comment:
            in_comment = _advance_html_comment_state(line, in_comment=True)
        elif fence is not None:
            if _fence_closes(line, fence=fence):
                fence = None
        elif (opened := _FENCE_OPEN_RE.match(line)) is not None:
            fence = opened.group("fence")
        else:
            for marker, found in ((_MANAGED_POLICY_START, starts), (_MANAGED_POLICY_END, ends)):
                index = line.find(marker)
                while index != -1:
                    found.append(offset + index)
                    index = line.find(marker, index + len(marker))
            in_comment = _advance_html_comment_state(line, in_comment=False)
        offset += len(line) + 1
    return starts, ends


def _extract_managed_policy_section(body: str) -> str | None:
    """Return the marker-delimited managed section of ``body``, or ``None``."""

    start = body.find(_MANAGED_POLICY_START)
    end = body.find(_MANAGED_POLICY_END)
    if start == -1 or end == -1 or end < start:
        return None
    return body[start : end + len(_MANAGED_POLICY_END)]


def reconcile_release_pr_body(*, existing_body: str, generated_body: str) -> str:
    """Splice AWF's managed merge-policy section into a reused PR's description.

    ``generated_body`` is the freshly rendered :func:`release_pr_body` (intro +
    managed policy section). For a *reused* PR — which may be a manually authored
    release PR carrying human release notes, checklists, and other context — we
    must not overwrite the whole description. Preserve ``existing_body`` verbatim
    and only replace the marker-delimited AWF policy block in place (inserting it
    at the end when absent), so the reused PR advertises the merge policy the
    attached monitor now enforces without destroying human-authored content.

    Markers are located with :func:`_managed_marker_offsets`, so a pair a human
    quoted inside a fenced example or an HTML comment is never mistaken for the
    live managed section.

    When the markers are absent, ``existing_body`` may still carry a pre-marker
    AWF-generated policy paragraph (a PR opened before the fences existed); strip
    that stale paragraph before appending the fresh section so the reused PR never
    claims both manual and automatic merge.
    """

    section = _extract_managed_policy_section(generated_body)
    if section is None:
        # Defensive: a body without managed markers can't be reconciled section-wise.
        # Never seen in practice (``release_pr_body`` always emits them); fall back to
        # the generated body rather than silently dropping the policy statement.
        return generated_body
    starts, ends = _managed_marker_offsets(existing_body)
    if starts or ends:
        # A managed block is (partially) present. Only an unambiguous, correctly
        # ordered single pair is safe to splice in place: pairing the first start
        # with the first end across an orphan marker or a duplicate block would
        # delete every byte in between — including human-authored release notes.
        # Fail closed on any malformed or duplicate layout so a human untangles the
        # markers rather than AWF silently destroying content on the next sync.
        if len(starts) == 1 and len(ends) == 1 and starts[0] < ends[0]:
            start, end = starts[0], ends[0]
            return existing_body[:start] + section + existing_body[end + len(_MANAGED_POLICY_END) :]
        raise ReleasePrSyncError(
            reason_code=RELEASE_SYNC_POLICY_MARKERS_MALFORMED_REASON_CODE,
            message=(
                "Reused release PR body has a malformed AWF merge-policy marker layout; "
                "refusing to reconcile to avoid deleting human-authored content."
            ),
            detail={
                "start_marker_count": len(starts),
                "end_marker_count": len(ends),
            },
        )
    stripped = _strip_legacy_policy_paragraph(existing_body).rstrip()
    if not stripped:
        return section
    return f"{stripped}\n\n{section}"


def release_pr_body(*, source_branch: str, target_branch: str, auto_merge: bool = False) -> str:
    intro = f"Automated AWF release PR syncing `{source_branch}` into `{target_branch}`.\n\n"
    return intro + _release_merge_policy_section(auto_merge=auto_merge)
