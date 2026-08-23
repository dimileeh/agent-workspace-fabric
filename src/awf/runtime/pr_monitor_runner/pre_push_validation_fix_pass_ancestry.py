"""Merge-safety ancestry/tree helpers for pre-push validation fix passes."""

from __future__ import annotations

import os
import re
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


_UNIFIED_DIFF_HUNK_HEADER_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@",
    re.MULTILINE,
)


def _line_in_unified_diff_hunk_range(line: int, start: int, count: int) -> bool:
    """Return True when 1-based ``line`` falls inside a unified-diff hunk side."""
    if count <= 0:
        return False
    return start <= line < start + count


def _map_review_line_through_diff(line: int, diff_text: str) -> int:
    """Map a 1-based review anchor from the diff old file to the new file.

    GitHub inline anchors name a line in the cycle-start (pre-fix) blob. When an
    earlier fix-cycle item advances HEAD and inserts or deletes lines above a
    later item, FIXED evidence diffs ``item_start_head``..candidate and must
    compare against the anchor relocated into the per-item start blob
    (PRRT_kwDOSJAM6s6bdOXq).
    """
    if line < 1:
        return line
    mapped = line
    for match in _UNIFIED_DIFF_HUNK_HEADER_RE.finditer(diff_text):
        old_start = int(match.group(1))
        old_count = int(match.group(2)) if match.group(2) is not None else 1
        new_start = int(match.group(3))
        new_count = int(match.group(4)) if match.group(4) is not None else 1

        if line < old_start:
            break

        if old_count == 0:
            # Git insert-before form ``@@ -(line-1),0 +line,N @@`` keeps
            # ``old_start`` unmoved in cycle-start coordinates; only lines after
            # ``old_start`` shift (PRRT_kwDOSJAM6s6bdWnC). Top-of-file inserts
            # ``@@ -1,0 +1,N @@`` also shift anchors on ``old_start`` itself
            # (PRRT_kwDOSJAM6s6bdlxB).
            if line > old_start or (line == old_start and new_start == old_start):
                mapped += new_count
            continue

        old_end = old_start + old_count
        if line >= old_end:
            mapped += new_count - old_count
            continue

        offset_in_hunk = line - old_start
        if offset_in_hunk < new_count:
            return new_start + offset_in_hunk
        return new_start + max(new_count - 1, 0)

    return mapped


def _rename_map_from_name_status_z(diff_stdout: str) -> dict[str, str]:
    """Return old_path -> new_path rename edges from ``--name-status -z`` output."""
    if not diff_stdout or "\0" not in diff_stdout:
        return {}
    fields = diff_stdout.split("\0")
    if not fields or fields[-1] != "":
        return {}
    fields = fields[:-1]
    rename_map: dict[str, str] = {}
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if status.startswith("R"):
            if index + 1 > len(fields):
                break
            old_path = _normalize_evidence_item_path(fields[index])
            new_path = _normalize_evidence_item_path(fields[index + 1])
            index += 2
            if old_path and new_path:
                rename_map[old_path] = new_path
        elif status.startswith("C"):
            index += 2
        else:
            index += 1
    return rename_map


def _follow_rename_map(path: str, rename_map: dict[str, str]) -> str:
    """Follow rename edges until ``path`` reaches its target-head name."""
    mapped = path
    seen = {mapped}
    while mapped in rename_map:
        mapped = rename_map[mapped]
        if mapped in seen:
            break
        seen.add(mapped)
    return mapped


async def _rename_map_in_commit_range(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
) -> dict[str, str]:
    """Return rename old->new edges between ``left`` and ``right``."""
    from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError

    git_env = _git_env_for_merge_safety_object_lookup()
    result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "diff",
            "-M",
            "--name-status",
            "-z",
            left,
            right,
            "--",
        ),
        env=git_env,
    )
    if not result.ok:
        return {}
    raw = result.stdout_bytes
    if raw is not None:
        diff_text = raw.decode("utf-8", errors="surrogateescape")
    else:
        diff_text = result.stdout or ""
    try:
        # Reject malformed output the same way as changed-path parsing.
        from awf.runtime.pr_monitor_runner.path_parsing import _changed_paths_from_name_status_z

        _changed_paths_from_name_status_z(diff_text)
    except ProtectedScopeDiffError:
        return {}
    return _rename_map_from_name_status_z(diff_text)


async def _map_review_path_through_commits(
    self: Any,
    *,
    worktree_path: Path,
    anchor_head: str,
    target_head: str,
    path: str,
) -> str | None:
    """Relocate ``path`` from ``anchor_head`` coordinates into ``target_head``."""
    normalized = _normalize_evidence_item_path(path)
    if not normalized:
        return None
    if anchor_head.lower() == target_head.lower():
        return normalized
    rename_map = await _rename_map_in_commit_range(
        self,
        worktree_path=worktree_path,
        left=anchor_head,
        right=target_head,
    )
    return _follow_rename_map(normalized, rename_map)


def _rename_diff_preserves_line_numbers(rename_diff_text: str) -> bool:
    """Return True when a rename-aware diff has no content-changing hunks.

    Pathspec-filtered old/new diffs each look like whole-file delete/add with equal
    line counts even when a rename commit inserted above an anchor and deleted below
    it. Inspect the combined rename diff's actual hunks instead (PRRT_kwDOSJAM6s6bduAa).
    """
    return _UNIFIED_DIFF_HUNK_HEADER_RE.search(rename_diff_text) is None


async def _map_review_line_through_commits(
    self: Any,
    *,
    worktree_path: Path,
    anchor_head: str,
    target_head: str,
    path: str,
    line: int,
) -> int | None:
    """Relocate ``line`` from ``anchor_head`` coordinates into ``target_head``."""
    if line < 1 or anchor_head.lower() == target_head.lower():
        return line
    normalized = _normalize_evidence_item_path(path)
    if not normalized:
        return None
    git_env = _git_env_for_merge_safety_object_lookup()
    result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "diff",
            "-U0",
            anchor_head,
            target_head,
            "--",
            normalized,
        ),
        env=git_env,
    )
    if not result.ok:
        return None
    raw = result.stdout_bytes
    if raw is not None:
        diff_text = raw.decode("utf-8", errors="surrogateescape")
    else:
        diff_text = result.stdout or ""
    rename_map = await _rename_map_in_commit_range(
        self,
        worktree_path=worktree_path,
        left=anchor_head,
        right=target_head,
    )
    renamed_to = rename_map.get(normalized)
    if renamed_to is not None:
        rename_result = await self._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "diff",
                "-U0",
                anchor_head,
                target_head,
                "--",
                normalized,
                renamed_to,
            ),
            env=git_env,
        )
        if rename_result.ok:
            rename_raw = rename_result.stdout_bytes
            if rename_raw is not None:
                rename_diff_text = rename_raw.decode("utf-8", errors="surrogateescape")
            else:
                rename_diff_text = rename_result.stdout or ""
            if _rename_diff_preserves_line_numbers(rename_diff_text):
                return line
            return _map_review_line_through_diff(line, rename_diff_text)
    return _map_review_line_through_diff(line, diff_text)


def _diff_hunk_touches_line(diff_text: str, line: int) -> bool:
    """Return True when any ``-U0`` hunk in ``diff_text`` overlaps ``line``.

    GitHub inline review anchors use 1-based line numbers from the pre-fix
    (left/old) blob. Only the old-side hunk range is consulted; matching the
    new-side range falsely accepts unrelated earlier insertions whose shifted
    span merely covers the anchor number.
    """
    if line < 1:
        return False
    for match in _UNIFIED_DIFF_HUNK_HEADER_RE.finditer(diff_text):
        old_start = int(match.group(1))
        old_count = int(match.group(2)) if match.group(2) is not None else 1
        if old_count > 0:
            if _line_in_unified_diff_hunk_range(line, old_start, old_count):
                return True
        elif old_start == line or old_start == line - 1:
            # Pure insertion at or immediately before the review anchor line in
            # the pre-fix blob (git emits ``@@ -(line-1),0 +line,N @@`` for the
            # latter case; PRRT_kwDOSJAM6s6bdKiS).
            return True
    return False


async def _commit_range_touches_path(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
    path: str,
    line: int | None = None,
) -> bool:
    """Return True when ``path`` appears in the ``left``..``right`` changed-path set.

    When ``line`` is set, the delta must also include a hunk that overlaps that
    review anchor line in the anchored file. FIXED claims with a known review-
    item path must not treat an unrelated contentful advance (for example a
    README-only edit or an unrelated edit elsewhere in the same file) as item
    evidence (PRRT_kwDOSJAM6s6Zzwl0, issue:5381831025). Rename/copy records
    count when either the old or new path matches. Fail closed on diff or parse
    errors.
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
    if not any(_normalize_evidence_item_path(changed) == normalized for changed in paths):
        return False
    if line is None:
        return True
    git_env = _git_env_for_merge_safety_object_lookup()
    result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "diff",
            "-U0",
            left,
            right,
            "--",
            normalized,
        ),
        env=git_env,
    )
    if not result.ok:
        return False
    raw = result.stdout_bytes
    if raw is not None:
        diff_text = raw.decode("utf-8", errors="surrogateescape")
    else:
        diff_text = result.stdout or ""
    rename_map = await _rename_map_in_commit_range(
        self,
        worktree_path=worktree_path,
        left=left,
        right=right,
    )
    renamed_to = rename_map.get(normalized)
    if renamed_to is not None:
        rename_result = await self._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "diff",
                "-U0",
                left,
                right,
                "--",
                normalized,
                renamed_to,
            ),
            env=git_env,
        )
        if not rename_result.ok:
            return False
        rename_raw = rename_result.stdout_bytes
        if rename_raw is not None:
            rename_diff_text = rename_raw.decode("utf-8", errors="surrogateescape")
        else:
            rename_diff_text = rename_result.stdout or ""
        if _rename_diff_preserves_line_numbers(rename_diff_text):
            return False
        return _diff_hunk_touches_line(rename_diff_text, line)
    return _diff_hunk_touches_line(diff_text, line)


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
    from awf.db.repositories.base import _is_descendant

    normalized_item = _normalize_evidence_item_path(item_path)
    normalized_changed = _normalize_evidence_item_path(changed_path)
    if not normalized_item or not normalized_changed:
        return False
    if normalized_item == normalized_changed:
        return True
    item_parent = _normalize_evidence_item_path(str(Path(normalized_item).parent))
    changed_parent = _normalize_evidence_item_path(str(Path(normalized_changed).parent))
    # Root-level files share parent "." but are not directory siblings
    # (PRRT_kwDOSJAM6s6bbkfx).
    if item_parent and item_parent != "." and item_parent == changed_parent:
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
