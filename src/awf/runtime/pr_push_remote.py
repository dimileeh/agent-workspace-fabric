"""Remote-selection helpers for updating existing pull requests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from awf.common.github_client import RepoRef
from awf.db.models import Workspace

_FORK_PUSH_TASK_KINDS = frozenset({"sync_feature_pr", "feature_branch_pr"})


def remote_push_url_for_workspace(ws: Workspace, *, base_repo: RepoRef) -> str | None:
    """Return the fork head-repo URL when an adopted feature PR needs one.

    Live ``sync_feature_pr`` adoptions store the fork under ``pr_adoption``.
    After a closed/merged adoption is replaced, identity is cleared and the
    task becomes ``feature_branch_pr``, but ``head_repo_slug`` /
    ``head_repo_url`` are retained so replacement and monitor pushes stay on
    the fork instead of ``origin``.
    """
    if ws.task_kind not in _FORK_PUSH_TASK_KINDS:
        return None
    policy = ws.task_policy if isinstance(ws.task_policy, dict) else {}
    adoption = policy.get("pr_adoption")
    if not isinstance(adoption, Mapping):
        return None
    head_repo_value = adoption.get("head_repo_slug") or adoption.get("head_repo_url")
    if not isinstance(head_repo_value, str) or not head_repo_value.strip():
        return None
    try:
        head_repo = RepoRef.from_url(head_repo_value)
    except ValueError:
        return None
    if head_repo.slug().lower() == base_repo.slug().lower():
        return None
    return head_repo.clone_url_like(ws.repo_url)


def retained_fork_pr_adoption(
    *,
    repo_url: str | None,
    adoption: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    """Return fork-only ``pr_adoption`` fields to keep after clearing PR identity.

    Drops PR number/URL/refs while preserving ``head_repo_slug`` /
    ``head_repo_url`` when the head repository differs from the workspace base.
    """
    if not isinstance(adoption, Mapping):
        return None
    if not isinstance(repo_url, str) or not repo_url.strip():
        return None
    head_repo_value = adoption.get("head_repo_slug") or adoption.get("head_repo_url")
    if not isinstance(head_repo_value, str) or not head_repo_value.strip():
        return None
    try:
        base_repo = RepoRef.from_url(repo_url)
        head_repo = RepoRef.from_url(head_repo_value)
    except ValueError:
        return None
    if head_repo.slug().lower() == base_repo.slug().lower():
        return None
    retained: dict[str, str] = {}
    head_slug = adoption.get("head_repo_slug")
    if isinstance(head_slug, str) and head_slug.strip():
        retained["head_repo_slug"] = head_slug.strip()
    head_url = adoption.get("head_repo_url")
    if isinstance(head_url, str) and head_url.strip():
        retained["head_repo_url"] = head_url.strip()
    return retained or None
