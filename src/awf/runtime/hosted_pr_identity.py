"""Shared hosted PR identity payload construction."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def hosted_pr_identity_for_workspace(
    workspace: Any,
    *,
    state: Any | None = None,
) -> dict[str, object]:
    policy = workspace.task_policy if isinstance(workspace.task_policy, Mapping) else {}
    adoption = policy.get("pr_adoption") if isinstance(policy, Mapping) else None
    adoption_map = adoption if isinstance(adoption, Mapping) else {}
    stored_head_ref = _nonblank_str(workspace.remote_push_branch) or _nonblank_str(
        adoption_map.get("head_ref")
    )
    head_ref = (
        _nonblank_str(getattr(state, "current_pr_head_ref", None))
        if getattr(state, "current_pr_head_ref_checked", False)
        else stored_head_ref
    )
    state_head_sha = _nonblank_str(getattr(state, "last_push_sha", None))
    return {
        "repo_url": workspace.repo_url,
        "pr_url": _nonblank_str(workspace.pr_url) or _nonblank_str(adoption_map.get("pr_url")),
        "pr_number": workspace.pr_number or _metadata_int(adoption_map, "pr_number"),
        "base_ref": _nonblank_str(adoption_map.get("base_ref")) or workspace.branch_base,
        "head_ref": head_ref,
        "head_repo_url": _nonblank_str(adoption_map.get("head_repo_url")) or workspace.repo_url,
        "head_repo_slug": _nonblank_str(adoption_map.get("head_repo_slug")),
        "owned_paths": list(workspace.owned_paths or []),
        "expected_head_sha": (
            state_head_sha
            or _nonblank_str(workspace.monitor_last_commit_sha)
            or _nonblank_str(adoption_map.get("head_sha"))
        ),
    }


def _nonblank_str(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _metadata_int(metadata: Mapping[object, object], key: str) -> int | None:
    value = metadata.get(key)
    return value if type(value) is int else None
