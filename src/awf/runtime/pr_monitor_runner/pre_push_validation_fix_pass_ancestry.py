"""Merge-safety ancestry/tree helpers for pre-push validation fix passes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command


def _git_env_for_merge_safety_object_lookup() -> dict[str, str]:
    """Git env that ignores replace refs, grafts, and object-lookup overrides.

    Merge-safety ancestry and tree comparisons must see the real object graph.
    ``refs/replace/*``, ``GIT_REPLACE_REF_BASE``, and ``GIT_GRAFT_FILE`` /
    default ``$GIT_DIR/info/grafts`` can otherwise rewrite parentage or trees.
    Merely unsetting ``GIT_GRAFT_FILE`` falls back to ``info/grafts``, so force
    it to the OS null device. Always set ``GIT_NO_REPLACE_OBJECTS=1`` and strip
    replace-base / object-directory overrides.
    """
    git_env = git_env_without_object_lookup_overrides()
    git_env.pop("GIT_REPLACE_REF_BASE", None)
    git_env["GIT_GRAFT_FILE"] = os.devnull
    git_env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return git_env


async def _head_descends_from(
    self: Any,
    *,
    worktree_path: Path,
    ancestor: str,
    descendant: str,
) -> bool:
    """Return True when ``descendant`` is a descendant of ``ancestor``.

    Uses ``git merge-base --is-ancestor`` which exits 0 when the first ref is an
    ancestor of the second and non-zero otherwise. Callers only invoke this with
    distinct SHAs, so a 0 exit means the fix-pass agent advanced HEAD on top of
    the pre-fix commit rather than moving it sideways or backward.

    Replace refs and grafts can rewrite apparent parentage, so a lateral or
    older tip could otherwise satisfy FIXED / fix-pass ancestry. Use the shared
    merge-safety object-lookup env for this check.
    """
    result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
        ),
        env=_git_env_for_merge_safety_object_lookup(),
    )
    return bool(result.ok)


async def _commit_trees_differ(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
) -> bool:
    """Return True when ``left`` and ``right`` resolve to different trees.

    Forward ancestry alone accepts empty commits (``git commit --allow-empty``):
    the tip advances with an unchanged tree. FIXED evidence requires a content
    change, so compare ``^{tree}`` SHAs. Fail closed when either tree cannot be
    resolved.

    Tree resolution must use the same no-replace / no-graft env as ancestry:
    otherwise a real empty descendant paired with ``refs/replace/<empty>`` to a
    contentful commit can pass ancestry while ``rev-parse ^{tree}`` reports a
    forged content change.
    """
    git_env = _git_env_for_merge_safety_object_lookup()
    left_result = await self._deps.runner.run(
        git_worktree_command(worktree_path, "rev-parse", f"{left}^{{tree}}"),
        env=git_env,
    )
    left_tree = left_result.stdout.strip() if left_result.ok else ""
    if not left_tree:
        return False
    right_result = await self._deps.runner.run(
        git_worktree_command(worktree_path, "rev-parse", f"{right}^{{tree}}"),
        env=git_env,
    )
    right_tree = right_result.stdout.strip() if right_result.ok else ""
    if not right_tree:
        return False
    return left_tree.lower() != right_tree.lower()


def _normalize_evidence_item_path(path: str) -> str:
    """Normalize a review-item path for FIXED evidence path matching.

    Strip only exact leading ``./`` prefixes. ``str.lstrip("./")`` treats the
    argument as a character set and would collapse ``.github/...`` into
    ``github/...``, letting a distinct non-dot path satisfy a dotfile gate.
    """
    normalized = path.strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


async def _changed_paths_in_commit_range(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
) -> tuple[str, ...]:
    """Return paths changed between ``left`` and ``right`` (``--name-status -z``)."""
    from awf.runtime.pr_monitor_runner.path_parsing import _changed_paths_from_name_status_z
    from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError

    git_env = _git_env_for_merge_safety_object_lookup()
    result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "diff",
            "--name-status",
            "-z",
            left,
            right,
            "--",
        ),
        env=git_env,
    )
    if not result.ok:
        return ()
    raw = result.stdout_bytes
    if raw is not None:
        diff_text = raw.decode("utf-8", errors="surrogateescape")
    else:
        diff_text = result.stdout or ""
    try:
        return _changed_paths_from_name_status_z(diff_text)
    except ProtectedScopeDiffError:
        return ()


async def _commit_range_touches_path(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
    path: str,
) -> bool:
    """Return True when ``path`` appears in the ``left``..``right`` changed-path set.

    FIXED claims with a known review-item path must not treat an unrelated
    contentful advance (for example a README-only edit) as item evidence
    (PRRT_kwDOSJAM6s6Zzwl0). Rename/copy records count when either the old or
    new path matches. Fail closed on diff or parse errors.
    """
    normalized = _normalize_evidence_item_path(path)
    if not normalized:
        return False
    paths = await _changed_paths_in_commit_range(
        self,
        worktree_path=worktree_path,
        left=left,
        right=right,
    )
    return any(_normalize_evidence_item_path(changed) == normalized for changed in paths)


def _changed_path_in_item_scope(
    *,
    item_path: str,
    changed_path: str,
) -> bool:
    """Return True when ``changed_path`` is plausibly related to ``item_path``.

    Cross-file fixes in the same directory (or under the reviewed path) remain
    valid, but unrelated files such as README-only edits do not count as item
    evidence when the review anchor names a different path. Workspace
    ``owned_paths`` are coordination hints only and must not widen FIXED
    evidence beyond the review anchor or derived bundle scope
    (PRRT_kwDOSJAM6s6bbZlt).
    """
    from awf.db.repositories.base import _is_descendant, owned_paths_overlap

    normalized_item = _normalize_evidence_item_path(item_path)
    normalized_changed = _normalize_evidence_item_path(changed_path)
    if not normalized_item or not normalized_changed:
        return False
    if normalized_item == normalized_changed:
        return True
    if owned_paths_overlap(normalized_item, normalized_changed):
        return True
    item_parent = _normalize_evidence_item_path(str(Path(normalized_item).parent))
    changed_parent = _normalize_evidence_item_path(str(Path(normalized_changed).parent))
    if item_parent and item_parent == changed_parent:
        return True
    if _is_descendant(normalized_item, normalized_changed):
        return True
    return _is_descendant(normalized_changed, normalized_item)


async def _commit_range_in_item_scope(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
    item_path: str,
) -> bool:
    """Return True when the ``left``..``right`` delta touches the review scope."""
    normalized_item = _normalize_evidence_item_path(item_path)
    if not normalized_item:
        return True
    changed_paths = await _changed_paths_in_commit_range(
        self,
        worktree_path=worktree_path,
        left=left,
        right=right,
    )
    if not changed_paths:
        return False
    return any(
        _changed_path_in_item_scope(
            item_path=normalized_item,
            changed_path=changed,
        )
        for changed in changed_paths
    )
