"""Merge-safety ancestry/tree helpers for pre-push validation fix passes."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, cast

from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
    _callee_refs_from_file_line as _callee_refs_from_file_line,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
    _enclosing_definition_identity as _enclosing_definition_identity,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
    _is_block_closer_line as _is_block_closer_line,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
    _is_ignorable_span_gap_line as _is_ignorable_span_gap_line,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
    _leading_indent as _leading_indent,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
    _resolve_callee_definition_span as _resolve_callee_definition_span,
)


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


# Bare ``-M`` keeps git's default 50% similarity gate, so low-similarity renames
# surface as separate A/D records and old-path-only diffs look like whole-file
# deletions that satisfy any review anchor (PRRT_kwDOSJAM6s6beOKJ).
_GIT_DIFF_FIND_RENAMES = "-M01"


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
            _GIT_DIFF_FIND_RENAMES,
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
            if index + 1 >= len(fields):
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


def _test_prefixed_stem_targets_deleted(deleted_path: str, added_path: str) -> bool:
    """Return True when ``added_path`` follows a ``test_<stem>`` rename convention."""
    deleted_stem = Path(deleted_path).stem
    added_stem = Path(added_path).stem
    if added_stem.startswith("test_"):
        test_target = added_stem.removeprefix("test_")
        if test_target == deleted_stem or test_target.startswith(f"{deleted_stem}_"):
            return True
    if added_stem.endswith("_test"):
        test_target = added_stem.removesuffix("_test")
        if test_target == deleted_stem:
            return True
    return False


def _colocated_addition_unrelated_to_deletion(deleted_path: str, added_path: str) -> bool:
    """Return True when a same-directory add is unrelated to deleting ``deleted_path``.

    Colocated regression tests must not make D+A pairs look like below-threshold
    renames (PRRT_kwDOSJAM6s6bfLFk). Same-directory ``conftest.py`` additions are
    not exempt by filename alone; callers compare candidate content instead
    (PRRT_kwDOSJAM6s6bfPjA).
    """
    return _test_prefixed_stem_targets_deleted(deleted_path, added_path)


_TRIVIAL_CONTENT_OVERLAP_LINE_RE = re.compile(
    r"^(?:"
    r"#.*"
    r"|import .+"
    r"|from .+ import .+"
    r"|pass"
    r"|return(?:\s+None)?"
    r"|[)\]},]+"
    r")$"
)


def _is_trivial_content_overlap_line(line: str) -> bool:
    """Return True when a shared line is too generic to prove rename-like overlap."""
    stripped = line.strip()
    if not stripped or len(stripped) <= 3:
        return True
    return _TRIVIAL_CONTENT_OVERLAP_LINE_RE.match(stripped) is not None


def _paths_have_meaningful_line_level_content_overlap(
    left_lines: set[str],
    right_lines: set[str],
) -> bool:
    """Return True when two path blobs share substantive line-level content."""
    left_substantive = {
        line.strip()
        for line in left_lines
        if line.strip() and not _is_trivial_content_overlap_line(line)
    }
    right_substantive = {
        line.strip()
        for line in right_lines
        if line.strip() and not _is_trivial_content_overlap_line(line)
    }
    if not left_substantive or not right_substantive:
        return False
    shared = left_substantive & right_substantive
    if not shared:
        return False
    if len(shared) >= 2:
        return True
    smaller = min(len(left_substantive), len(right_substantive))
    return len(shared) / smaller >= 0.5


async def _path_line_at_ref(
    self: Any,
    *,
    worktree_path: Path,
    ref: str,
    path: str,
    line: int,
) -> str | None:
    """Return the 1-based ``line`` from ``path`` at ``ref``, or None when missing."""
    if line < 1:
        return None
    git_env = _git_env_for_merge_safety_object_lookup()
    result = await self._deps.runner.run(
        git_worktree_command(worktree_path, "show", f"{ref}:{path}"),
        env=git_env,
    )
    if not result.ok:
        return None
    lines = result.stdout.splitlines()
    if line > len(lines):
        return None
    return str(lines[line - 1])


async def _paths_share_review_anchor_line(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
    left_path: str,
    right_path: str,
    line: int,
) -> bool:
    """Return True when ``right_path`` retains the review anchor from ``left_path``."""
    anchor_line = await _path_line_at_ref(
        self,
        worktree_path=worktree_path,
        ref=left,
        path=left_path,
        line=line,
    )
    if anchor_line is None:
        return False
    anchor_stripped = anchor_line.strip()
    if not anchor_stripped or _is_trivial_content_overlap_line(anchor_stripped):
        return False
    git_env = _git_env_for_merge_safety_object_lookup()
    right_result = await self._deps.runner.run(
        git_worktree_command(worktree_path, "show", f"{right}:{right_path}"),
        env=git_env,
    )
    if not right_result.ok:
        return False
    return any(
        candidate.strip() == anchor_stripped for candidate in right_result.stdout.splitlines()
    )


async def _paths_share_line_level_content(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
    left_path: str,
    right_path: str,
) -> bool:
    """Return True when ``right_path`` meaningfully overlaps ``left_path`` at the two refs."""
    git_env = _git_env_for_merge_safety_object_lookup()
    left_result = await self._deps.runner.run(
        git_worktree_command(worktree_path, "show", f"{left}:{left_path}"),
        env=git_env,
    )
    if not left_result.ok:
        return False
    right_result = await self._deps.runner.run(
        git_worktree_command(worktree_path, "show", f"{right}:{right_path}"),
        env=git_env,
    )
    if not right_result.ok:
        return False
    left_lines = {line.strip() for line in left_result.stdout.splitlines() if line.strip()}
    right_lines = {line.strip() for line in right_result.stdout.splitlines() if line.strip()}
    return _paths_have_meaningful_line_level_content_overlap(left_lines, right_lines)


def _added_paths_from_name_status_z(name_status_z: str) -> tuple[str, ...]:
    """Return paths with ``A`` status from ``--name-status -z`` output."""
    if not name_status_z or "\0" not in name_status_z:
        return ()
    fields = name_status_z.split("\0")
    if not fields or fields[-1] != "":
        return ()
    fields = fields[:-1]
    added_paths: list[str] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if status.startswith("R") or status.startswith("C"):
            index += 2
        elif status.startswith("A"):
            if index < len(fields):
                added_path = _normalize_evidence_item_path(fields[index])
                if added_path:
                    added_paths.append(added_path)
            index += 1
        else:
            index += 1
    return tuple(added_paths)


def _plausible_rename_partners_for_deletion(
    name_status_z: str,
    deleted_path: str,
) -> tuple[str, ...]:
    """Return added paths that could be below-threshold renames of ``deleted_path``."""
    if not name_status_z or "\0" not in name_status_z:
        return ()
    fields = name_status_z.split("\0")
    if not fields or fields[-1] != "":
        return ()
    fields = fields[:-1]
    normalized = _normalize_evidence_item_path(deleted_path)
    if not normalized:
        return ()
    partners: list[str] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if status.startswith("R") or status.startswith("C"):
            index += 2
        elif status.startswith("A"):
            if index < len(fields):
                added_path = _normalize_evidence_item_path(fields[index])
                if added_path and _plausible_rename_replacement(normalized, added_path):
                    partners.append(added_path)
            index += 1
        else:
            index += 1
    return tuple(partners)


async def _same_dir_unrelated_conftest_addition(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
    deleted_path: str,
    name_status_z: str,
    line: int | None = None,
) -> bool:
    """Return True when unrelated same-dir conftest is the only plausible D+A partner.

    When a reviewed helper such as ``fixtures.py`` is rewritten as ``conftest.py``,
    Git can report separate D/A records. Filename-only exemptions then let whole-file
    deletion hunks satisfy old-path anchors while the reviewed line survives in
    ``conftest.py`` (PRRT_kwDOSJAM6s6bfPjA). Unrelated same-dir ``conftest.py``
    additions must not bypass that guard when another plausible rename partner exists
    (PRRT_kwDOSJAM6s6bfThO). Filename heuristics can omit below-threshold renames such as
    ``tests/test_<stem>.py``; any other added path retaining deleted content must also
    block exemption (PRRT_kwDOSJAM6s6bfUzh). When ``line`` is set, compare that anchor
    directly before granting the exemption (PRRT_kwDOSJAM6s6bfmuj).
    """
    deleted_norm = _normalize_evidence_item_path(deleted_path)
    if not deleted_norm:
        return False
    deleted_parent = _normalize_evidence_item_path(str(Path(deleted_norm).parent))
    partners = _plausible_rename_partners_for_deletion(name_status_z, deleted_path)
    if not partners:
        return False
    unrelated_conftest_partners: set[str] = set()
    for partner in partners:
        partner_norm = _normalize_evidence_item_path(partner)
        partner_parent = _normalize_evidence_item_path(str(Path(partner_norm).parent))
        if Path(partner_norm).name != "conftest.py" or partner_parent != deleted_parent:
            return False
        if await _paths_share_line_level_content(
            self,
            worktree_path=worktree_path,
            left=left,
            right=right,
            left_path=deleted_path,
            right_path=partner,
        ):
            return False
        if line is not None and await _paths_share_review_anchor_line(
            self,
            worktree_path=worktree_path,
            left=left,
            right=right,
            left_path=deleted_path,
            right_path=partner,
            line=line,
        ):
            return False
        unrelated_conftest_partners.add(partner_norm)
    for added_path in _added_paths_from_name_status_z(name_status_z):
        added_norm = _normalize_evidence_item_path(added_path)
        if added_norm in unrelated_conftest_partners:
            continue
        if await _paths_share_line_level_content(
            self,
            worktree_path=worktree_path,
            left=left,
            right=right,
            left_path=deleted_path,
            right_path=added_path,
        ):
            return False
        if line is not None and await _paths_share_review_anchor_line(
            self,
            worktree_path=worktree_path,
            left=left,
            right=right,
            left_path=deleted_path,
            right_path=added_path,
            line=line,
        ):
            return False
    return True


async def _unrelated_test_prefix_rename_addition(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
    deleted_path: str,
    name_status_z: str,
    line: int | None = None,
) -> bool:
    """Return True when unrelated ``tests/test_<stem>`` is the only plausible D+A partner.

    Conventional test-path rewrites such as ``src/foo.py`` -> ``tests/test_foo.py`` can
    appear as separate D/A records while the reviewed line survives in the new file.
    Filename-only heuristics must not treat those as unrelated regression tests when
    content overlaps, but unrelated ``tests/test_<stem>`` adds must still allow anchored
    deletions (PRRT_kwDOSJAM6s6bfUzl). When ``line`` is set, compare that anchor
    directly before granting the exemption (PRRT_kwDOSJAM6s6bfhwX).
    """
    deleted_norm = _normalize_evidence_item_path(deleted_path)
    if not deleted_norm:
        return False
    partners = _plausible_rename_partners_for_deletion(name_status_z, deleted_path)
    if not partners:
        return False
    unrelated_test_partners: set[str] = set()
    for partner in partners:
        partner_norm = _normalize_evidence_item_path(partner)
        partner_parts = Path(partner_norm).parts
        if not (
            partner_parts
            and partner_parts[0] == "tests"
            and _test_prefixed_stem_targets_deleted(deleted_norm, partner_norm)
        ):
            return False
        if await _paths_share_line_level_content(
            self,
            worktree_path=worktree_path,
            left=left,
            right=right,
            left_path=deleted_path,
            right_path=partner,
        ):
            return False
        if line is not None and await _paths_share_review_anchor_line(
            self,
            worktree_path=worktree_path,
            left=left,
            right=right,
            left_path=deleted_path,
            right_path=partner,
            line=line,
        ):
            return False
        unrelated_test_partners.add(partner_norm)
    for added_path in _added_paths_from_name_status_z(name_status_z):
        added_norm = _normalize_evidence_item_path(added_path)
        if added_norm in unrelated_test_partners:
            continue
        if await _paths_share_line_level_content(
            self,
            worktree_path=worktree_path,
            left=left,
            right=right,
            left_path=deleted_path,
            right_path=added_path,
        ):
            return False
        if line is not None and await _paths_share_review_anchor_line(
            self,
            worktree_path=worktree_path,
            left=left,
            right=right,
            left_path=deleted_path,
            right_path=added_path,
            line=line,
        ):
            return False
    return True


def _plausible_rename_replacement(deleted_path: str, added_path: str) -> bool:
    """Return True when ``added_path`` could be a below-threshold rename of ``deleted_path``."""
    deleted_norm = _normalize_evidence_item_path(deleted_path)
    added_norm = _normalize_evidence_item_path(added_path)
    if not deleted_norm or not added_norm:
        return False
    deleted_parent = _normalize_evidence_item_path(str(Path(deleted_norm).parent))
    added_parent = _normalize_evidence_item_path(str(Path(added_norm).parent))
    # Root-level D+A pairs are plausible below-threshold renames (PRRT_kwDOSJAM6s6bfHED).
    if deleted_parent == added_parent == ".":
        return True
    # Delete + unrelated test additions must not block anchored deletions (PRRT_kwDOSJAM6s6be20X).
    # Same-basename moves into ``tests/`` remain plausible below-threshold renames
    # (PRRT_kwDOSJAM6s6bfEkW); compare basenames instead of exempting every test add.
    deleted_parts = Path(deleted_norm).parts
    added_parts = Path(added_norm).parts
    if (
        added_parts
        and added_parts[0] == "tests"
        and (not deleted_parts or deleted_parts[0] != "tests")
        and Path(deleted_norm).name != Path(added_norm).name
    ):
        return _test_prefixed_stem_targets_deleted(deleted_norm, added_norm)
    if deleted_parent == added_parent:
        return not _colocated_addition_unrelated_to_deletion(deleted_norm, added_norm)
    # Cross-directory D+A is a plausible below-threshold rename (PRRT_kwDOSJAM6s6be6p8,
    # PRRT_kwDOSJAM6s6bfBxP).
    return True


def _path_deletion_addition_without_rename(name_status_z: str, path: str) -> bool:
    """Return True when ``path`` was deleted alongside a plausible rename add.

    Below-threshold renames can still appear as separate D/A records even with
    ``-M01``. Treat that pattern as non-evidence for line-anchored FIXED claims
    so unrelated bulk rewrites on the added path cannot satisfy old-path anchors.
    Unrelated D+A commits (for example deleting an obsolete module while adding a
    regression test elsewhere) must not trigger this guard.
    """
    if not name_status_z or "\0" not in name_status_z:
        return False
    fields = name_status_z.split("\0")
    if not fields or fields[-1] != "":
        return False
    fields = fields[:-1]
    renamed_old_paths: set[str] = set()
    deleted_paths: set[str] = set()
    added_paths: set[str] = set()
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if status.startswith("R"):
            if index + 1 > len(fields):
                break
            old_path = _normalize_evidence_item_path(fields[index])
            index += 2
            if old_path:
                renamed_old_paths.add(old_path)
        elif status.startswith("C"):
            index += 2
        elif status.startswith("D"):
            if index < len(fields):
                deleted_paths.add(_normalize_evidence_item_path(fields[index]))
            index += 1
        elif status.startswith("A"):
            if index < len(fields):
                added_paths.add(_normalize_evidence_item_path(fields[index]))
            index += 1
        else:
            index += 1
    normalized = _normalize_evidence_item_path(path)
    if not normalized or normalized not in deleted_paths:
        return False
    if normalized in renamed_old_paths:
        return False
    return any(_plausible_rename_replacement(normalized, added_path) for added_path in added_paths)


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


def _merge_rename_edge(rename_map: dict[str, str], old_path: str, new_path: str) -> None:
    """Record ``old_path`` -> ``new_path`` and extend any existing rename chains."""
    old_norm = _normalize_evidence_item_path(old_path)
    new_norm = _normalize_evidence_item_path(new_path)
    if not old_norm or not new_norm:
        return
    for key, mapped in list(rename_map.items()):
        if mapped == old_norm:
            rename_map[key] = new_norm
    rename_map[old_norm] = new_norm


def _add_missing_per_commit_rename_edges(
    rename_map: dict[str, str],
    per_commit_map: dict[str, str],
) -> None:
    """Add per-commit rename edges without overwriting range-level aggregates."""
    for old_path, new_path in per_commit_map.items():
        old_norm = _normalize_evidence_item_path(old_path)
        if old_norm and old_norm not in rename_map:
            _merge_rename_edge(rename_map, old_path, new_path)


async def _name_status_z_between(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
) -> str:
    """Return raw ``--name-status -z`` output between two refs."""
    git_env = _git_env_for_merge_safety_object_lookup()
    result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "diff",
            _GIT_DIFF_FIND_RENAMES,
            "--name-status",
            "-z",
            left,
            right,
            "--",
        ),
        env=git_env,
    )
    if not result.ok:
        return ""
    raw = result.stdout_bytes
    if raw is not None:
        return str(raw.decode("utf-8", errors="surrogateescape"))
    return str(result.stdout or "")


async def _per_commit_rename_map_in_range(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
) -> dict[str, str]:
    """Accumulate rename edges from each commit in ``left``..``right``."""
    from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError

    if left.lower() == right.lower():
        return {}
    git_env = _git_env_for_merge_safety_object_lookup()
    rev_result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "rev-list",
            "--first-parent",
            "--reverse",
            f"{left}..{right}",
        ),
        env=git_env,
    )
    if not rev_result.ok:
        return {}
    rename_map: dict[str, str] = {}
    for commit in rev_result.stdout.splitlines():
        commit_sha = commit.strip()
        if not commit_sha:
            continue
        parent_result = await self._deps.runner.run(
            git_worktree_command(worktree_path, "rev-parse", f"{commit_sha}^"),
            env=git_env,
        )
        if not parent_result.ok:
            continue
        parent_sha = parent_result.stdout.strip()
        if not parent_sha:
            continue
        name_status_z = await _name_status_z_between(
            self,
            worktree_path=worktree_path,
            left=parent_sha,
            right=commit_sha,
        )
        if not name_status_z:
            continue
        try:
            from awf.runtime.pr_monitor_runner.path_parsing import _changed_paths_from_name_status_z

            _changed_paths_from_name_status_z(name_status_z)
        except ProtectedScopeDiffError:
            continue
        for old_path, new_path in _rename_map_from_name_status_z(name_status_z).items():
            _merge_rename_edge(rename_map, old_path, new_path)
    return rename_map


async def _rename_map_in_commit_range(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
) -> tuple[dict[str, str], str]:
    """Return rename old->new edges and raw ``--name-status -z`` between refs."""
    from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError

    cache_key = (str(worktree_path), left, right)
    cache_value = getattr(self, "_rename_map_in_commit_range_cache", None)
    cache: dict[tuple[str, str, str], tuple[dict[str, str], str]]
    if isinstance(cache_value, dict):
        cache = cast(dict[tuple[str, str, str], tuple[dict[str, str], str]], cache_value)
    else:
        cache = {}
        self._rename_map_in_commit_range_cache = cache
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    diff_text = await _name_status_z_between(
        self,
        worktree_path=worktree_path,
        left=left,
        right=right,
    )
    if not diff_text:
        result: tuple[dict[str, str], str] = ({}, "")
        cache[cache_key] = result
        return result
    try:
        # Reject malformed output the same way as changed-path parsing.
        from awf.runtime.pr_monitor_runner.path_parsing import _changed_paths_from_name_status_z

        _changed_paths_from_name_status_z(diff_text)
    except ProtectedScopeDiffError:
        result = ({}, "")
        cache[cache_key] = result
        return result
    rename_map = _rename_map_from_name_status_z(diff_text)
    per_commit_map = await _per_commit_rename_map_in_range(
        self,
        worktree_path=worktree_path,
        left=left,
        right=right,
    )
    _add_missing_per_commit_rename_edges(rename_map, per_commit_map)
    result = (rename_map, diff_text)
    cache[cache_key] = result
    return result


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
    rename_map, _ = await _rename_map_in_commit_range(
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
            _GIT_DIFF_FIND_RENAMES,
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
    rename_map, name_status_z = await _rename_map_in_commit_range(
        self,
        worktree_path=worktree_path,
        left=anchor_head,
        right=target_head,
    )
    renamed_to = rename_map.get(normalized)
    if (
        renamed_to is None
        and _path_deletion_addition_without_rename(name_status_z, normalized)
        and not await _same_dir_unrelated_conftest_addition(
            self,
            worktree_path=worktree_path,
            left=anchor_head,
            right=target_head,
            deleted_path=normalized,
            name_status_z=name_status_z,
            line=line,
        )
        and not await _unrelated_test_prefix_rename_addition(
            self,
            worktree_path=worktree_path,
            left=anchor_head,
            right=target_head,
            deleted_path=normalized,
            name_status_z=name_status_z,
            line=line,
        )
    ):
        return None
    if renamed_to is not None:
        rename_result = await self._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "diff",
                _GIT_DIFF_FIND_RENAMES,
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


# How far above a review anchor a same-file hunk may land and still count as
# related FIXED evidence (guards / setup inserted a few lines before the call).
# Kept tight so distant same-file edits remain rejected without a call-site link.
_RELATED_LINE_PROXIMITY_BEFORE = 12


def _diff_hunk_overlaps_line_span(
    diff_text: str,
    start: int,
    end: int,
    *,
    file_text: str | None = None,
) -> bool:
    """Return True when any old-side hunk overlaps the inclusive ``[start, end]`` span.

    Pure inserts (``old_count == 0``) are placed *after* ``old_start``. Inserts
    clearly inside the body count directly; inserts at/after the last body line
    (including ``old_start == end`` and trailing span gaps) count only when added
    lines continue the body by indentation — never after a brace closer
    (PRRT_kwDOSJAM6s6dUMC7).
    """
    if start < 1 or end < start:
        return False
    for old_start, old_count, added_lines in _iter_unified_diff_old_hunks(diff_text):
        if old_count == 0:
            if not (start <= old_start <= end):
                continue
            if _pure_insert_overlaps_definition_span(
                file_text,
                start=start,
                end=end,
                old_start=old_start,
                added_lines=added_lines,
            ):
                return True
            continue
        hunk_end = old_start + old_count - 1
        if old_start <= end and hunk_end >= start:
            return True
    return False


def _pure_insert_overlaps_definition_span(
    file_text: str | None,
    *,
    start: int,
    end: int,
    old_start: int,
    added_lines: list[str],
) -> bool:
    """Whether a pure insert after ``old_start`` overlaps definition ``[start, end]``.

    Without ``file_text``, only inserts after a non-final span line
    (``old_start < end``) count — boundary ``old_start == end`` fails closed.
    With ``file_text``, inserts after the last non-gap body line need deeper
    indent on an added line; inserts at/after a same-indent brace closer never
    count.
    """
    if file_text is None:
        return old_start < end
    lines = file_text.splitlines()
    if start < 1 or end < 1 or start > len(lines) or end > len(lines):
        return False
    def_indent = _leading_indent(lines[start - 1])
    last_content: int | None = None
    last_is_closer = False
    for idx in range(end - 1, start - 2, -1):
        raw = lines[idx]
        if _is_ignorable_span_gap_line(raw):
            continue
        last_content = idx + 1
        last_is_closer = _leading_indent(raw) == def_indent and _is_block_closer_line(raw)
        break
    if last_content is None:
        return False
    if last_is_closer:
        return start <= old_start < last_content
    if start <= old_start < last_content:
        return True
    if not (last_content <= old_start <= end):
        return False
    for added in added_lines:
        if _is_ignorable_span_gap_line(added):
            continue
        if _leading_indent(added) > def_indent:
            return True
    return False


def _diff_hunk_near_anchor_related(
    diff_text: str,
    line: int,
    *,
    file_text: str | None = None,
) -> bool:
    """Return True when a pure insert lands just before ``line`` in the same scope.

    Covers guards / setup **inserted** a few lines above the GitHub old-side
    anchor. Exact overlap and insert-at ``line`` / ``line-1`` stay in
    ``_diff_hunk_touches_line``. Same-file **modifications** outside that exact
    span must use call-site→definition linking (or remain rejected) so unrelated
    edits a few lines above the anchor do not count as FIXED evidence.

    Near-anchor inserts also require the same enclosing def/class identity as the
    review line (or both module-level) so an unrelated insert in a neighboring
    function within the proximity window cannot satisfy FIXED evidence.
    """
    if line < 1 or not file_text:
        return False
    window_start = max(1, line - _RELATED_LINE_PROXIMITY_BEFORE)
    anchor_id = _enclosing_definition_identity(file_text, line)
    for match in _UNIFIED_DIFF_HUNK_HEADER_RE.finditer(diff_text):
        old_start = int(match.group(1))
        old_count = int(match.group(2)) if match.group(2) is not None else 1
        if old_count != 0:
            continue
        # Pure insertion: git ``@@ -N,0 …`` inserts after old line N.
        # Exact matcher already accepts N == line or N == line - 1.
        if not (window_start <= old_start < line):
            continue
        insert_id = _enclosing_definition_identity(file_text, old_start)
        if anchor_id is None and insert_id is None:
            return True
        if anchor_id is not None and anchor_id == insert_id:
            return True
    return False


def _diff_hunk_related_line_evidence(
    diff_text: str,
    line: int,
    *,
    file_text: str | None = None,
) -> bool:
    """Exact old-side overlap or near-anchor related hunk before ``line``."""
    if line < 1:
        return False
    return _diff_hunk_touches_line(diff_text, line) or _diff_hunk_near_anchor_related(
        diff_text, line, file_text=file_text
    )


def _iter_unified_diff_old_hunks(
    diff_text: str,
) -> list[tuple[int, int, list[str]]]:
    """Return ``(old_start, old_count, added_lines)`` for each unified hunk.

    ``added_lines`` are the ``+`` body lines (without the leading ``+``), used to
    decide whether a pure insert after a definition's last line continues the
    body by indentation.
    """
    matches = list(_UNIFIED_DIFF_HUNK_HEADER_RE.finditer(diff_text))
    results: list[tuple[int, int, list[str]]] = []
    for i, match in enumerate(matches):
        old_start = int(match.group(1))
        old_count = int(match.group(2)) if match.group(2) is not None else 1
        body_start = match.end()
        if body_start < len(diff_text) and diff_text[body_start] == "\n":
            body_start += 1
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(diff_text)
        added_lines = [
            line[1:]
            for line in diff_text[body_start:body_end].splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        results.append((old_start, old_count, added_lines))
    return results


async def _path_text_at_ref(
    self: Any,
    *,
    worktree_path: Path,
    ref: str,
    path: str,
) -> str | None:
    """Return the full text of ``path`` at ``ref``, or None when missing."""
    git_env = _git_env_for_merge_safety_object_lookup()
    result = await self._deps.runner.run(
        git_worktree_command(worktree_path, "show", f"{ref}:{path}"),
        env=git_env,
    )
    if not result.ok:
        return None
    return str(result.stdout)


async def _diff_changes_referenced_definition(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    path: str,
    line: int,
    diff_text: str,
    file_text: str | None = None,
) -> bool:
    """Return True when the diff changes the in-scope definition named by the review line."""
    if line < 1:
        return False
    if file_text is None:
        file_text = await _path_text_at_ref(
            self,
            worktree_path=worktree_path,
            ref=left,
            path=path,
        )
    if not file_text:
        return False
    # Use file lexical context so multiline string/docstring decoys are not callees.
    refs = _callee_refs_from_file_line(file_text, line, path=path)
    if not refs:
        return False
    for qualifier, name in refs:
        span = _resolve_callee_definition_span(
            file_text,
            call_line=line,
            qualifier=qualifier,
            name=name,
            path=path,
        )
        if span is None:
            continue
        start, end = span
        if _diff_hunk_overlaps_line_span(diff_text, start, end, file_text=file_text):
            return True
    return False


async def _diff_provides_related_line_evidence(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    path: str,
    line: int,
    diff_text: str,
) -> bool:
    """Exact, near-anchor (same scope), or call-site→definition FIXED line evidence."""
    if line < 1:
        return False
    if _diff_hunk_touches_line(diff_text, line):
        return True
    file_text = await _path_text_at_ref(
        self,
        worktree_path=worktree_path,
        ref=left,
        path=path,
    )
    if _diff_hunk_near_anchor_related(diff_text, line, file_text=file_text):
        return True
    return await _diff_changes_referenced_definition(
        self,
        worktree_path=worktree_path,
        left=left,
        path=path,
        line=line,
        diff_text=diff_text,
        file_text=file_text,
    )


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

    When ``line`` is set, the delta must also include line-related FIXED evidence
    for that review anchor: exact old-side hunk overlap, a related near-anchor
    pure insert a few lines before the review line in the same enclosing
    definition, or a same-path change to the in-scope definition named by the
    review call site. FIXED claims with a known review-item path must not treat
    an unrelated contentful advance (for example a README-only edit or an
    unrelated edit elsewhere in the same file) as item evidence
    (PRRT_kwDOSJAM6s6Zzwl0, issue:5381831025). Rename/copy records count when
    either the old or new path matches. Fail closed on diff or parse errors.
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
            _GIT_DIFF_FIND_RENAMES,
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
    rename_map, name_status_z = await _rename_map_in_commit_range(
        self,
        worktree_path=worktree_path,
        left=left,
        right=right,
    )
    renamed_to = rename_map.get(normalized)
    if (
        renamed_to is None
        and _path_deletion_addition_without_rename(name_status_z, normalized)
        and not await _same_dir_unrelated_conftest_addition(
            self,
            worktree_path=worktree_path,
            left=left,
            right=right,
            deleted_path=normalized,
            name_status_z=name_status_z,
            line=line,
        )
        and not await _unrelated_test_prefix_rename_addition(
            self,
            worktree_path=worktree_path,
            left=left,
            right=right,
            deleted_path=normalized,
            name_status_z=name_status_z,
            line=line,
        )
    ):
        return False
    if renamed_to is not None:
        rename_result = await self._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "diff",
                _GIT_DIFF_FIND_RENAMES,
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
        # Rename diffs keep exact old-side overlap only. Near-anchor inserts and
        # call-site→definition linking are same-path related-repair signals and
        # must not re-open fail-closed rename / rewrite evidence.
        return _diff_hunk_touches_line(rename_diff_text, line)
    return await _diff_provides_related_line_evidence(
        self,
        worktree_path=worktree_path,
        left=left,
        path=normalized,
        line=line,
        diff_text=diff_text,
    )


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
