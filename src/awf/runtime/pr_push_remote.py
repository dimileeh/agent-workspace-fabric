"""Remote-selection helpers for updating existing pull requests."""

from __future__ import annotations

from collections.abc import Mapping

from awf.common.github_client import RepoRef
from awf.db.models import Workspace


def remote_push_url_for_workspace(ws: Workspace, *, base_repo: RepoRef) -> str | None:
    """Return the fork head-repo URL when an adopted feature PR needs one."""
    if ws.task_kind != "sync_feature_pr":
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
