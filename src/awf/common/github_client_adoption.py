"""GitHub PR adoption payload parsing helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from awf.common.github_client import (
        BranchOpenPullRequest,
        PullRequestAdoptionMetadata,
        RepoRef,
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
