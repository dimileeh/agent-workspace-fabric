"""Comment-named target FIXED evidence (issue #1232).

The four earlier correction-attempt evidence gates (see
``comment_verdict_correction``) are all derived from the comment *anchor*.
Reviewers often anchor at a usage site and name the place to fix in the comment
*body* instead — a runbook line, the route helper's file, the tuple a filter
reads — so a genuine fix there escalated to ``needs_human`` three times on
2026-11-13.

This module adds the fifth gate. Over the item's own commit range it accepts:

* **(a)** a change to a repo-relative path the reviewer's comment names
  explicitly. With ``:line``, the range must touch that line under the existing
  line-anchored rule (``_commit_range_touches_path``); without one, any change
  to the file counts.
* **(b)** a change to the *definition* of an identifier the reviewer's comment
  names in single backticks (``name`` / ``name()``): a module-reachable head the
  callee check already resolves, or a module-level plain assignment. A
  definition the range *adds* — in an existing file or a new one — counts too.

Only the reviewer's text is read (``reviewer_comment_text``): AWF-authored
replies, which can carry the agent's own reason (#1018), and the agent's
verdict reason are never evidence. Absolute paths, ``..`` / ``.`` segments,
``~``, globs and URLs never count, and every unreadable Git read fails closed.
Kept in its own module because ``..._cross_file`` sits near the first-party line
budget, and because the rule reads as one unit.
"""

from __future__ import annotations

import keyword
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
    _CALLEE_KEYWORD_BLOCKLIST,
    _JS_IDENT,
    _definition_binding_scope,
    _definition_head_scan_lines,
    _is_ignorable_span_gap_line,
    _iter_definition_spans,
    _leading_indent,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file import (
    _MAX_CALLEE_EVIDENCE_CANDIDATE_PATHS,
    _diff_adds_decorators_above_span,
    _importable_definition_spans_for_names,
    _path_diff_text_in_commit_range,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_rebinding import (
    _rebound_scope_names,
)

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import ReviewThread

# Upper bound on the ``:line`` probes one comment can trigger. Each runs the
# line-anchored rule's Git reads, and a review names one to three targets.
_MAX_COMMENT_NAMED_PATHS = 10

# ``scheme://...`` links are dropped before path parsing, so a GitHub permalink
# never contributes a path (its line fragment is a non-goal).
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S*")
# Tokens are split on whitespace and quoting/bracketing characters that wrap a
# path in prose or markdown links. ``[]{}*?`` stay inside a token so a glob is
# rejected whole rather than split into an innocent-looking fragment.
_PATH_TOKEN_SPLIT_RE = re.compile(r"[\s`'\"()<>]+")
_PATH_TRAILING_PUNCTUATION = ".,;:!?"
_NAMED_PATH_RE = re.compile(r"((?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]+)(?::(\d+)(?:-\d+)?)?")
_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_NAMED_IDENTIFIER_RE = re.compile(rf"({_JS_IDENT})(?:\(\))?")
_NON_DEFINITION_NAMES = (
    _CALLEE_KEYWORD_BLOCKLIST
    | frozenset(keyword.kwlist)
    | frozenset({"self", "cls", "this", "null", "undefined"})
)
# A module-level plain binding: ``name = ...``, ``name: T = ...``,
# ``const|let|var name = ...`` and ``export const name = ...``. Matched on masked
# scan lines at column 0, so text in strings/docstrings and class or function
# bodies never counts; ``==`` / ``+=`` are not bindings.
_MODULE_LEVEL_ASSIGNMENT_RE = re.compile(
    rf"^(?:export[ \t]+)?(?:(?:const|let|var)[ \t]+)?({_JS_IDENT})(?:[ \t]*:[^=\n]*)?[ \t]*=(?!=)"
)


def reviewer_comment_text(thread: ReviewThread) -> str:
    """The reviewer-authored text of ``thread`` — the only comment-named evidence source.

    Comments AWF posted (``viewer_did_author``) are excluded because they can
    carry the agent's own reason; ``review_context`` is a separate review body,
    not this thread's comment. ``body_excerpt`` is used only when GitHub
    supplied no comment history.
    """
    if thread.comments:
        return "\n".join(
            comment.body for comment in thread.comments if not comment.viewer_did_author
        )
    return thread.body_excerpt or ""


def _comment_named_paths(text: str) -> tuple[tuple[str, int | None], ...]:
    """Repo-relative ``(path, line)`` pairs named in ``text``, de-duplicated in order."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _normalize_evidence_item_path,
    )

    named: dict[tuple[str, int | None], None] = {}
    for token in _PATH_TOKEN_SPLIT_RE.split(_URL_RE.sub(" ", text)):
        match = _NAMED_PATH_RE.fullmatch(token.rstrip(_PATH_TRAILING_PUNCTUATION))
        if match is None:
            continue
        path = _normalize_evidence_item_path(match.group(1))
        if not path or any(segment in {"", ".", ".."} for segment in path.split("/")):
            continue
        line = int(match.group(2)) if match.group(2) is not None else None
        if line is not None and line < 1:
            continue
        named[(path, line)] = None
    return tuple(named)


def _comment_named_identifiers(text: str) -> frozenset[str]:
    """Identifiers ``text`` names in single backticks (``name`` or ``name()``)."""
    names: set[str] = set()
    for match in _INLINE_CODE_RE.finditer(_FENCED_BLOCK_RE.sub(" ", text)):
        named = _NAMED_IDENTIFIER_RE.fullmatch(match.group(1).strip())
        if named is not None and named.group(1) not in _NON_DEFINITION_NAMES:
            names.add(named.group(1))
    return frozenset(names)


def _assignment_span_end_line(scan_lines: list[str], start: int) -> int:
    """Inclusive end line of the column-0 binding whose statement opens at ``start``.

    Read over comment/string-masked lines, so a multi-line string's contents are
    blank gaps rather than dedented statements, and a line inside open brackets
    or after a ``\\`` continuation belongs to the statement whatever its indent
    (PRRT_kwDOSJAM6s63Lp9D). Past those, the next column-0 line ends the span on
    the line before it, as ``_definition_span_end_line`` does.
    """
    depth = 0
    continued = False
    for index in range(start - 1, len(scan_lines)):
        scan = scan_lines[index]
        if (
            index >= start
            and depth == 0
            and not continued
            and not _is_ignorable_span_gap_line(scan)
            and _leading_indent(scan) == 0
        ):
            return index
        for char in scan:
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth = max(depth - 1, 0)
        continued = scan.rstrip().endswith("\\")
    return len(scan_lines)


def _module_level_assignment_spans(
    file_text: str, names: frozenset[str], *, path: str
) -> list[tuple[int, int]]:
    """Spans of effective module-level plain bindings of ``names``, multi-line literals included.

    The module body runs its column-0 bindings in order, so only the last one of
    a name is the value importers reach; an earlier one is dead, and an edit
    confined to it changes nothing (PRRT_kwDOSJAM6s63LR_S). A name the module
    scope also binds another way — a ``def``/``class`` head after that last
    binding or under a guard, an ``import``, or any assignment, loop or ``with``
    target the ``_rebound_scope_names`` readers find — withholds it entirely,
    matching ``_importable_definition_spans_for_names``'s effective-binding rule.
    Only statement-opening lines count: a column-0 line inside open brackets or
    after a ``\\`` continuation (a keyword argument, say) binds nothing
    (PRRT_kwDOSJAM6s63L3DZ).
    """
    scan_lines = _definition_head_scan_lines(file_text, path=path)
    bindings: list[tuple[str, int]] = []
    depth = 0
    continued = False
    for index, scan in enumerate(scan_lines):
        match = None if depth or continued else _MODULE_LEVEL_ASSIGNMENT_RE.match(scan)
        if match is not None and match.group(1) in names:
            bindings.append((match.group(1), index + 1))
        for char in scan:
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth = max(depth - 1, 0)
        continued = scan.rstrip().endswith("\\")
    if not bindings:
        return []
    last = dict(bindings)
    starts = frozenset(start for _name, start in bindings)
    all_spans = _iter_definition_spans(file_text, path=path)
    shadowed = {
        name
        for name, start, _end, indent in all_spans
        if name in last
        and start not in starts
        and (indent > 0 or start > last[name])
        and _definition_binding_scope(file_text, all_spans, start=start, indent=indent)[0] == 0
    }
    rebound = _rebound_scope_names(
        file_text,
        all_spans,
        path=path,
        collected=[(name, start, 0, (0, 0), (start, start)) for name, start in bindings],
    )
    return [
        (start, _assignment_span_end_line(scan_lines, start))
        for name, start in last.items()
        if name not in shadowed and (0, name) not in rebound
    ]


def _without_trailing_gaps(
    file_text: str | None, spans: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """``spans`` ending at their last non-blank, non-comment line of ``file_text``.

    Definition spans keep the blank/comment gap before the next statement, but a
    hunk there changes no definition line, so it is dropped before the overlap
    check on either side (PRRT_kwDOSJAM6s63LtTS, PRRT_kwDOSJAM6s63L_5C). An
    insert in the gap that does extend a body shows up in the right-side span.
    """
    lines = (file_text or "").splitlines()
    trimmed: list[tuple[int, int]] = []
    for start, end in spans:
        while end > start and _is_ignorable_span_gap_line(lines[end - 1]):
            end -= 1
        trimmed.append((start, end))
    return trimmed


def _diff_new_hunk_overlaps_line_span(diff_text: str, start: int, end: int) -> bool:
    """True when a new-side hunk of ``diff_text`` overlaps the inclusive ``[start, end]``.

    ``start``/``end`` are lines of the *right* blob, so new-side hunk ranges are
    exact there: an overlap means the range added or rewrote a line of that span.
    Pure deletions (``new_count == 0``) have no new-side lines and never count.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _UNIFIED_DIFF_HUNK_HEADER_RE,
    )

    for match in _UNIFIED_DIFF_HUNK_HEADER_RE.finditer(diff_text):
        new_start = int(match.group(3))
        new_count = int(match.group(4)) if match.group(4) is not None else 1
        if new_count > 0 and new_start <= end and start < new_start + new_count:
            return True
    return False


async def _commit_range_changes_comment_named_target(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
    comment_text: str,
    anchor_head: str | None = None,
) -> bool:
    """True when ``left``..``right`` changes a target the reviewer's text names.

    (a) A named path the range changed: any change without ``:line``, or one the
    line-anchored rule accepts for that line (the first
    ``_MAX_COMMENT_NAMED_PATHS`` such probes). Named paths and lines are in the
    coordinates of ``anchor_head``, the PR head the review was written against,
    so they are first relocated into ``left`` through the commits between them —
    an earlier item in the batch may have renamed the file or shifted the line
    (PRRT_kwDOSJAM6s63LTKz); one that no longer maps is skipped. (b) A named identifier whose
    definition span at ``left`` — a module-reachable head, decorators included,
    or a module-level plain binding — overlaps an old-side hunk of the range in
    a changed file, or whose span at ``right`` overlaps a new-side hunk, so a
    definition the range adds counts in an existing or a new file
    (PRRT_kwDOSJAM6s63LTK6). Each file is diffed paired with its rename source or
    target so a pure move stays hunkless. Fails closed on any unreadable Git
    output.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _changed_paths_in_commit_range,
        _commit_range_touches_path,
        _diff_hunk_overlaps_line_span,
        _map_review_line_through_commits,
        _map_review_path_through_commits,
        _normalize_evidence_item_path,
        _path_text_at_ref,
        _rename_map_in_commit_range,
    )

    named_paths = _comment_named_paths(comment_text)
    names = _comment_named_identifiers(comment_text)
    if not (named_paths or names):
        return False
    changed = [
        normalized
        for normalized in (
            _normalize_evidence_item_path(path)
            for path in await _changed_paths_in_commit_range(
                self, worktree_path=worktree_path, left=left, right=right
            )
        )
        if normalized
    ]
    if not changed:
        return False
    anchor = (anchor_head or "").strip()
    remap = bool(anchor) and anchor.lower() != left.lower()
    line_probes = 0
    for named_path, named_line in named_paths:
        path: str | None = named_path
        if remap:
            path = await _map_review_path_through_commits(
                self,
                worktree_path=worktree_path,
                anchor_head=anchor,
                target_head=left,
                path=named_path,
            )
        if path is None or path not in changed:
            continue
        if named_line is None:
            return True
        if line_probes >= _MAX_COMMENT_NAMED_PATHS:
            continue
        line_probes += 1
        line: int | None = named_line
        if remap:
            line = await _map_review_line_through_commits(
                self,
                worktree_path=worktree_path,
                anchor_head=anchor,
                target_head=left,
                path=named_path,
                line=named_line,
            )
        if line is None or line < 1:
            continue
        if await _commit_range_touches_path(
            self, worktree_path=worktree_path, left=left, right=right, path=path, line=line
        ):
            return True
    if not names:
        return False
    rename_map, _name_status_z = await _rename_map_in_commit_range(
        self, worktree_path=worktree_path, left=left, right=right
    )
    renamed_from = {target: source for source, target in rename_map.items()}
    for candidate in changed[:_MAX_CALLEE_EVIDENCE_CANDIDATE_PATHS]:
        file_text = await _path_text_at_ref(
            self, worktree_path=worktree_path, ref=left, path=candidate
        )
        spans = _without_trailing_gaps(
            file_text, _named_definition_spans(file_text, names, path=candidate)
        )
        # A rename source's right-side text is read under its target, itself a candidate.
        right_text = (
            None
            if candidate in rename_map
            else await _path_text_at_ref(
                self, worktree_path=worktree_path, ref=right, path=candidate
            )
        )
        right_spans = _without_trailing_gaps(
            right_text, _named_definition_spans(right_text, names, path=candidate)
        )
        if not (spans or right_spans):
            continue
        source = renamed_from.get(candidate)
        diff_text = await _path_diff_text_in_commit_range(
            self,
            worktree_path=worktree_path,
            left=left,
            right=right,
            path=source or candidate,
            rename_path=candidate if source else rename_map.get(candidate),
        )
        if diff_text is None:
            continue
        if any(
            _diff_hunk_overlaps_line_span(diff_text, start, end, file_text=file_text)
            or _diff_adds_decorators_above_span(diff_text, start)
            for start, end in spans
        ) or any(
            _diff_new_hunk_overlaps_line_span(diff_text, start, end) for start, end in right_spans
        ):
            return True
    return False


def _named_definition_spans(
    file_text: str | None, names: frozenset[str], *, path: str
) -> list[tuple[int, int]]:
    """Module-scope definition and binding spans of ``names`` in ``file_text``.

    A backticked name carries no receiver, so it is held to the bare-name
    module-scope rule: a same-named method of some class in a changed file is
    not the definition the reviewer named (PRRT_kwDOSJAM6s63LVep).
    """
    if not file_text:
        return []
    return _importable_definition_spans_for_names(
        file_text, frozenset(), path=path, bare_names=names
    ) + _module_level_assignment_spans(file_text, names, path=path)
