"""GitHub PR adoption payload parsing helpers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from awf.common.audit import redact_audit_text
from awf.common.commands import AsyncCommandRunner
from awf.common.github_retry import RetryPolicy
from awf.common.github_transport import execute_gh_with_retry
from awf.common.logging import get_logger

if TYPE_CHECKING:
    from awf.common.github_client import (
        BranchOpenPullRequest,
        PullRequestAdoptionMetadata,
        RepoRef,
    )

_log = get_logger(__name__)

_PR_ADOPTION_VIEW_JSON_FIELDS = (
    "number,headRefName,headRepository,isCrossRepository,baseRefName,"
    "headRefOid,baseRefOid,state,isDraft,author,url,title"
)
_BRANCH_OPEN_PR_LIST_JSON_FIELDS = (
    "number,url,headRefName,headRefOid,headRepository,headRepositoryOwner"
)
_BRANCH_OPEN_PR_LIST_LIMIT = 1000


async def fetch_pull_request_adoption_metadata(
    *,
    runner: AsyncCommandRunner,
    repo: RepoRef,
    pr_number: int,
) -> PullRequestAdoptionMetadata:
    """Fetch one-shot metadata for adopting an existing GitHub PR."""
    from awf.common.github_client import (
        GITHUB_API_ERROR,
        GitHubClientError,
        PullRequestMetadataError,
    )

    # NEVER-retry: this one-shot read classifies its own failure into a stable
    # reason code (not-found vs fetch-failed). The transport retry would consume the
    # single failure and could return a stale/empty follow-up result, so we take the
    # raise on the first attempt and map it here.
    try:
        result = await execute_gh_with_retry(
            runner,
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo.slug(),
                "--json",
                _PR_ADOPTION_VIEW_JSON_FIELDS,
            ],
            operation="gh pr view adoption metadata",
            retry_policy=RetryPolicy.NEVER,
        )
    except GitHubClientError as exc:
        if _looks_like_missing_pr_error(exc.stderr):
            reason = "PR_NOT_FOUND"
        elif exc.reason_code != GITHUB_API_ERROR:
            # Preserve non-default transport provenance (e.g. COMMAND_TIMEOUT) so an
            # adoption/release-sync metadata lookup that times out keeps its specific
            # reason code instead of collapsing to the generic fetch-failed mapping.
            reason = exc.reason_code
        else:
            reason = "PR_METADATA_FETCH_FAILED"
        raise PullRequestMetadataError(
            reason_code=reason,
            message=(exc.stderr or f"gh pr view exited {exc.returncode}").strip(),
            detail={
                "repo_slug": repo.slug(),
                "pr_number": pr_number,
                "returncode": exc.returncode,
            },
        ) from exc

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PullRequestMetadataError(
            reason_code="PR_METADATA_INVALID",
            message=f"failed to parse gh pr view JSON: {exc}",
            detail={"repo_slug": repo.slug(), "pr_number": pr_number},
        ) from exc

    return _parse_pull_request_adoption_metadata(payload, repo=repo, pr_number=pr_number)


async def list_open_pull_requests_for_branch(
    *,
    runner: AsyncCommandRunner,
    repo: RepoRef,
    branch_name: str,
    base_branch: str | None = None,
    retry_policy: RetryPolicy = RetryPolicy.NEVER,
) -> list[BranchOpenPullRequest]:
    """List open GitHub PRs whose head branch matches ``branch_name``.

    Defaults to ``NEVER`` for the one-shot adoption lookup (classify a single
    failure). The create-PR reconcile path passes a retrying policy, because there
    the lookup is a mutation-adjacent recheck that should survive a transient blip.
    """
    from awf.common.github_client import GitHubClientError, PullRequestMetadataError

    stripped_branch = branch_name.strip()
    if not stripped_branch:
        return []
    command = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo.slug(),
        "--head",
        stripped_branch,
        "--state",
        "open",
        "--limit",
        str(_BRANCH_OPEN_PR_LIST_LIMIT),
    ]
    if base_branch is not None and base_branch.strip():
        command.extend(["--base", base_branch.strip()])
    command.extend(["--json", _BRANCH_OPEN_PR_LIST_JSON_FIELDS])
    # The caller picks the policy: NEVER for one-shot adoption (classify the single
    # failure), a retrying policy for the create-reconcile recheck.
    try:
        result = await execute_gh_with_retry(
            runner,
            command,
            operation="gh pr list",
            retry_policy=retry_policy,
        )
    except GitHubClientError as exc:
        raise PullRequestMetadataError(
            reason_code="OPEN_PR_LOOKUP_FAILED",
            message=(exc.stderr or f"gh pr list exited {exc.returncode}").strip(),
            detail={
                "repo_slug": repo.slug(),
                "branch_name": stripped_branch,
                "base_branch": base_branch,
                "returncode": exc.returncode,
            },
        ) from exc

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PullRequestMetadataError(
            reason_code="OPEN_PR_LOOKUP_INVALID",
            message=f"failed to parse gh pr list JSON: {exc}",
            detail={
                "repo_slug": repo.slug(),
                "branch_name": stripped_branch,
                "base_branch": base_branch,
            },
        ) from exc
    if not isinstance(payload, list):
        raise PullRequestMetadataError(
            reason_code="OPEN_PR_LOOKUP_INVALID",
            message="gh pr list returned non-list JSON.",
            detail={
                "repo_slug": repo.slug(),
                "branch_name": stripped_branch,
                "base_branch": base_branch,
            },
        )
    results: list[BranchOpenPullRequest] = []
    parse_failures: list[tuple[int, PullRequestMetadataError]] = []
    for index, item in enumerate(payload):
        try:
            results.append(
                _parse_branch_open_pull_request(item, repo=repo, branch_name=stripped_branch)
            )
        except PullRequestMetadataError as exc:
            parse_failures.append((index, exc))

    failure_summaries: list[dict[str, object]] = []
    for index, parse_error in parse_failures:
        error = redact_audit_text(parse_error.message)
        failure_summaries.append(
            {
                "item_index": index,
                "reason_code": parse_error.reason_code,
                "error": error,
            }
        )
        _log.warning(
            "github.open_pr_item_parse_failed",
            repo_slug=repo.slug(),
            branch_name=stripped_branch,
            item_index=index,
            reason_code=parse_error.reason_code,
            error=error,
        )
    if parse_failures and not results:
        failure_count = len(parse_failures)
        item_label = "item" if failure_count == 1 else "items"
        _log.warning(
            "github.open_pr_batch_parse_failed",
            repo_slug=repo.slug(),
            branch_name=stripped_branch,
            base_branch=base_branch,
            failure_count=failure_count,
            failures=failure_summaries,
        )
        if failure_count == 1:
            raise parse_failures[0][1]
        raise PullRequestMetadataError(
            reason_code="OPEN_PR_LOOKUP_INVALID",
            message=f"failed to parse {failure_count} gh pr list {item_label}.",
            detail={
                "repo_slug": repo.slug(),
                "branch_name": stripped_branch,
                "base_branch": base_branch,
                "failure_count": failure_count,
                "failures": failure_summaries,
            },
        ) from parse_failures[0][1]
    return results


def parse_github_pull_request_url(pr_url: str) -> tuple[RepoRef, int]:
    """Parse a canonical GitHub PR URL into ``(repo, number)``."""
    from urllib.parse import urlsplit

    from awf.common.github_client import RepoRef

    parsed = urlsplit(pr_url.strip())
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != "github.com":
        raise ValueError(f"Cannot parse GitHub pull request URL: {pr_url!r}")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 4 or parts[2] != "pull":
        raise ValueError(f"Cannot parse GitHub pull request URL: {pr_url!r}")
    try:
        number = int(parts[3])
    except ValueError as exc:
        raise ValueError(f"Cannot parse GitHub pull request URL: {pr_url!r}") from exc
    if number <= 0:
        raise ValueError(f"Cannot parse GitHub pull request URL: {pr_url!r}")
    return RepoRef(owner=parts[0], name=parts[1]), number


class BranchOpenPullRequestResolver:
    """Resolve open PRs for a branch using the GitHub CLI."""

    def __init__(self, runner: AsyncCommandRunner) -> None:
        """Store the command runner used for GH CLI queries."""
        self._runner = runner

    async def resolve(
        self,
        *,
        repo_url: str,
        branch_name: str,
        base_branch: str | None,
    ) -> list[BranchOpenPullRequest]:
        """Resolve open PRs for a branch, optionally scoped by base branch."""
        from awf.common.github_client import PullRequestMetadataError, RepoRef

        try:
            repo = RepoRef.from_url(repo_url)
        except ValueError as exc:
            redacted_repo_url = redact_audit_text(repo_url)
            redacted_error = redact_audit_text(str(exc))
            _log.warning(
                "github.open_pr_lookup_skipped_invalid_repo_url",
                repo_url=redacted_repo_url,
                branch_name=branch_name,
                base_branch=base_branch,
                error=redacted_error,
            )
            raise PullRequestMetadataError(
                reason_code="OPEN_PR_LOOKUP_INVALID",
                message=f"cannot parse repo_url for open PR lookup: {redacted_error}",
                detail={
                    "repo_url": redacted_repo_url,
                    "branch_name": branch_name,
                    "base_branch": base_branch,
                },
            ) from exc
        return await list_open_pull_requests_for_branch(
            runner=self._runner,
            # NEVER-retry: this resolver drives worker-recovery preserved-active
            # open-PR lookups (worker/recovery_preserved_queries), NOT the
            # create/release reconcile recheck. It is a plain recovery read whose
            # caller already classifies a failed lookup and re-polls next cycle, so
            # an in-transport retry here is redundant (and would needlessly burn the
            # stale-cycle deadline that NEVER-policy calls are exempt from). Only the
            # mutation-adjacent create/release reconcile lookups pass READ.
            retry_policy=RetryPolicy.NEVER,
            repo=repo,
            branch_name=branch_name,
            base_branch=base_branch,
        )


def _parse_pull_request_adoption_metadata(
    payload: dict[str, Any],
    *,
    repo: RepoRef,
    pr_number: int,
) -> PullRequestAdoptionMetadata:
    """Parse and validate payload from ``gh pr view`` adoption query."""
    from awf.common.github_client import (
        PullRequestAdoptionMetadata,
        PullRequestMetadataError,
    )

    try:
        number = int(payload["number"])
        head_ref = str(payload["headRefName"])
        base_ref = str(payload["baseRefName"])
        state = str(payload["state"])
        is_draft = bool(payload.get("isDraft", False))
        url = str(payload["url"])
        title = str(payload.get("title") or "")
    except (KeyError, TypeError, ValueError) as exc:
        raise PullRequestMetadataError(
            reason_code="PR_METADATA_INVALID",
            message=f"gh pr view payload missing required adoption field: {exc}",
            detail={"repo_slug": repo.slug(), "pr_number": pr_number},
        ) from exc

    if number != pr_number:
        raise PullRequestMetadataError(
            reason_code="PR_METADATA_INVALID",
            message=(f"gh pr view returned PR #{number}, expected #{pr_number} for {repo.slug()}"),
            detail={"repo_slug": repo.slug(), "pr_number": pr_number},
        )
    if not head_ref.strip():
        raise PullRequestMetadataError(
            reason_code="PR_METADATA_INVALID",
            message="PR has no headRefName; cannot check out the PR branch.",
            detail={"repo_slug": repo.slug(), "pr_number": pr_number},
        )
    if not base_ref.strip():
        raise PullRequestMetadataError(
            reason_code="PR_METADATA_INVALID",
            message="PR has no baseRefName; cannot record the merge target.",
            detail={"repo_slug": repo.slug(), "pr_number": pr_number},
        )

    closed = state == "CLOSED"
    merged = state == "MERGED"

    author_obj = payload.get("author")
    author = author_obj.get("login") if isinstance(author_obj, dict) else None
    head_repo_slug = _head_repo_slug_from_adoption_payload(
        payload,
        repo=repo,
        pr_number=pr_number,
    )
    head_sha = _required_nonempty_str(
        payload.get("headRefOid"),
        field_name="headRefOid",
        repo=repo,
        pr_number=pr_number,
        message="PR has no headRefOid; cannot adopt the PR monitor without a head commit.",
    )
    base_sha = _required_nonempty_str(
        payload.get("baseRefOid"),
        field_name="baseRefOid",
        repo=repo,
        pr_number=pr_number,
        message="PR has no baseRefOid; cannot adopt the PR monitor without a base commit.",
    )
    return PullRequestAdoptionMetadata(
        number=number,
        head_ref=head_ref,
        head_repo_slug=head_repo_slug,
        base_ref=base_ref,
        head_sha=head_sha,
        base_sha=base_sha,
        state=state,
        is_draft=is_draft,
        closed=closed,
        merged=merged,
        author=author,
        url=url,
        title=title,
    )


def _parse_branch_open_pull_request(
    payload: object,
    *,
    repo: RepoRef,
    branch_name: str,
) -> BranchOpenPullRequest:
    """Parse and validate one ``gh pr list`` branch-open payload item."""
    from awf.common.github_client import (
        BranchOpenPullRequest,
        PullRequestMetadataError,
    )

    if not isinstance(payload, dict):
        raise PullRequestMetadataError(
            reason_code="OPEN_PR_LOOKUP_INVALID",
            message="gh pr list item is not an object.",
            detail={"repo_slug": repo.slug(), "branch_name": branch_name},
        )
    try:
        number = int(payload["number"])
        raw_url = payload["url"]
        if not isinstance(raw_url, str):
            raise TypeError("url")
        url = raw_url.strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise PullRequestMetadataError(
            reason_code="OPEN_PR_LOOKUP_INVALID",
            message=f"gh pr list payload missing required field: {exc}",
            detail={"repo_slug": repo.slug(), "branch_name": branch_name},
        ) from exc
    if number <= 0 or not url:
        raise PullRequestMetadataError(
            reason_code="OPEN_PR_LOOKUP_INVALID",
            message="gh pr list payload has invalid number or URL.",
            detail={"repo_slug": repo.slug(), "branch_name": branch_name},
        )

    head_ref = _optional_nonempty_str(payload.get("headRefName")) or branch_name
    head_sha = _optional_nonempty_str(payload.get("headRefOid"))
    head_repo_slug = _head_repo_slug_from_branch_open_pr_payload(
        payload,
        repo=repo,
        branch_name=branch_name,
    )
    return BranchOpenPullRequest(
        url=url,
        number=number,
        head_ref=head_ref,
        head_repo_slug=head_repo_slug,
        head_sha=head_sha,
    )


def _head_repo_slug_from_branch_open_pr_payload(
    payload: dict[str, Any],
    *,
    repo: RepoRef,
    branch_name: str,
) -> str:
    """Extract branch-open PR head repository slug from payload."""
    from awf.common.github_client import PullRequestMetadataError

    head_repo = payload.get("headRepository")
    if isinstance(head_repo, dict):
        name_with_owner = _optional_nonempty_str(head_repo.get("nameWithOwner"))
        if name_with_owner is not None:
            return _parse_open_pr_head_repo_slug(
                name_with_owner,
                repo=repo,
                branch_name=branch_name,
                field_name="headRepository.nameWithOwner",
            )

        repo_name = _optional_nonempty_str(head_repo.get("name"))
        owner_login = _head_repo_owner_login_from_branch_payload(payload, head_repo)
        if repo_name is not None and owner_login is not None:
            return _parse_open_pr_head_repo_slug(
                f"{owner_login}/{repo_name}",
                repo=repo,
                branch_name=branch_name,
                field_name="headRepositoryOwner.login/headRepository.name",
            )

    raise PullRequestMetadataError(
        reason_code="OPEN_PR_LOOKUP_INVALID",
        message="gh pr list payload missing required headRepository identity.",
        detail={
            "repo_slug": repo.slug(),
            "branch_name": branch_name,
            "field": "headRepository",
        },
    )


def _head_repo_owner_login_from_branch_payload(
    payload: dict[str, Any],
    head_repo: dict[str, Any],
) -> str | None:
    """Resolve head repo owner login from payload fallback fields."""
    owner = payload.get("headRepositoryOwner")
    if isinstance(owner, dict):
        login = _optional_nonempty_str(owner.get("login"))
        if login is not None:
            return login
    elif isinstance(owner, str):
        login = _optional_nonempty_str(owner)
        if login is not None:
            return login

    nested_owner = head_repo.get("owner")
    if isinstance(nested_owner, dict):
        return _optional_nonempty_str(nested_owner.get("login"))
    return None


def _parse_open_pr_head_repo_slug(
    value: str,
    *,
    repo: RepoRef,
    branch_name: str,
    field_name: str,
) -> str:
    """Parse and normalize ``owner/name`` payload value."""
    from awf.common.github_client import PullRequestMetadataError, RepoRef

    try:
        return RepoRef.from_url(value).slug()
    except ValueError as exc:
        raise PullRequestMetadataError(
            reason_code="OPEN_PR_LOOKUP_INVALID",
            message=f"gh pr list payload has invalid {field_name}.",
            detail={
                "repo_slug": repo.slug(),
                "branch_name": branch_name,
                "field": field_name,
            },
        ) from exc


def _head_repo_slug_from_adoption_payload(
    payload: dict[str, Any],
    *,
    repo: RepoRef,
    pr_number: int,
) -> str:
    """Resolve PR head repository slug from adoption payload."""
    from awf.common.github_client import PullRequestMetadataError, RepoRef

    head_repo = payload.get("headRepository")
    if isinstance(head_repo, dict):
        name_with_owner = head_repo.get("nameWithOwner")
        if isinstance(name_with_owner, str) and name_with_owner.strip():
            try:
                return RepoRef.from_url(name_with_owner).slug()
            except ValueError as exc:
                raise PullRequestMetadataError(
                    reason_code="PR_METADATA_INVALID",
                    message="PR headRepository.nameWithOwner is not a valid GitHub repository.",
                    detail={
                        "repo_slug": repo.slug(),
                        "pr_number": pr_number,
                        "field": "headRepository.nameWithOwner",
                    },
                ) from exc

    if not bool(payload.get("isCrossRepository", False)):
        return repo.slug()

    raise PullRequestMetadataError(
        reason_code="PR_METADATA_INVALID",
        message="Fork PR has no headRepository identity; cannot update the PR head.",
        detail={"repo_slug": repo.slug(), "pr_number": pr_number, "field": "headRepository"},
    )


def _required_nonempty_str(
    value: object,
    *,
    field_name: str,
    repo: RepoRef,
    pr_number: int,
    message: str,
) -> str:
    """Return a required non-empty string or raise a metadata error."""
    from awf.common.github_client import PullRequestMetadataError

    if not isinstance(value, str):
        raise PullRequestMetadataError(
            reason_code="PR_METADATA_INVALID",
            message=f"{message} Missing field: {field_name}.",
            detail={"repo_slug": repo.slug(), "pr_number": pr_number, "field": field_name},
        )
    stripped = value.strip()
    if not stripped:
        raise PullRequestMetadataError(
            reason_code="PR_METADATA_INVALID",
            message=f"{message} Blank field: {field_name}.",
            detail={"repo_slug": repo.slug(), "pr_number": pr_number, "field": field_name},
        )
    return stripped


def _optional_nonempty_str(value: object) -> str | None:
    """Return trimmed string content or None when value is empty/missing."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _looks_like_missing_pr_error(stderr: str) -> bool:
    """Return True when GH stderr indicates a missing pull request."""
    lower = stderr.lower()
    return (
        "could not resolve to a pullrequest" in lower
        or "not found" in lower
        or "no pull requests found" in lower
    )
