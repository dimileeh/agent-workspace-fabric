"""Create-reconcile head matching helpers for the GitHub client.

Extracted from ``github_client.py`` to stay under the first-party file-size
guardrail. Call sites may import these from ``awf.common.github_client``
(compatibility re-export) or directly from this module.
"""

from __future__ import annotations

from typing import Protocol

from awf.common.github_client_ref import RepoRef


class _BranchOpenPrLike(Protocol):
    @property
    def number(self) -> int: ...  # pragma: no cover - Protocol declaration only.

    @property
    def url(self) -> str: ...  # pragma: no cover - Protocol declaration only.

    @property
    def head_ref(self) -> str: ...  # pragma: no cover - Protocol declaration only.

    @property
    def head_repo_slug(self) -> str: ...  # pragma: no cover - Protocol declaration only.

    @property
    def head_sha(self) -> str | None: ...  # pragma: no cover - Protocol declaration only.


def _branch_open_pr_metadata(pr: _BranchOpenPrLike) -> dict[str, object]:
    metadata: dict[str, object] = {
        "number": pr.number,
        "url": pr.url,
        "head_ref": pr.head_ref,
        "head_repo_slug": pr.head_repo_slug,
    }
    if pr.head_sha is not None:
        metadata["head_sha"] = pr.head_sha
    return metadata


def _create_pr_reconcile_head(
    *,
    repo: RepoRef,
    head: str,
    source_repo: RepoRef | None = None,
) -> tuple[str, str | None, str | None]:
    """Map create ``head`` to ``(list_branch, expected_slug, expected_owner)``.

    Cross-fork creates pass ``owner:branch``. ``gh pr list --head`` does not
    accept that qualified form, so reconcile lists by the plain branch.

    When ``source_repo`` is provided (the push/fork remote), match that slug
    exactly — including renamed forks whose name differs from ``repo.name``.
    Without ``source_repo``, match any open PR whose head-repo **owner** equals
    the qualified owner (still renamed-fork safe; avoids assuming
    ``owner/<base-repo-name>``). Same-repo creates keep the plain branch and
    expect the base repo slug.
    """
    stripped = head.strip()
    if ":" in stripped:
        owner, branch = stripped.split(":", 1)
        owner = owner.strip()
        branch = branch.strip()
        if owner and branch and "/" not in owner:
            if source_repo is not None:
                return branch, source_repo.slug().lower(), None
            return branch, None, owner.lower()
    if source_repo is not None:
        return stripped, source_repo.slug().lower(), None
    return stripped, repo.slug().lower(), None


def _reconcile_matches_head_repo(
    head_repo_slug: str,
    *,
    expected_slug: str | None,
    expected_owner: str | None,
) -> bool:
    """True when a listed open PR's head repo matches create-reconcile expectations."""
    slug = head_repo_slug.lower()
    if expected_slug is not None:
        return slug == expected_slug
    if expected_owner is not None:
        return slug.partition("/")[0] == expected_owner
    return False
