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
  callee check already resolves, or a module-level plain assignment.

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
    _definition_head_scan_lines,
    _definition_span_end_line,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file import (
    _MAX_CALLEE_EVIDENCE_CANDIDATE_PATHS,
    _diff_adds_decorators_above_span,
    _importable_definition_spans_for_names,
    _path_diff_text_in_commit_range,
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


def _module_level_assignment_spans(
    file_text: str, names: frozenset[str], *, path: str
) -> list[tuple[int, int]]:
    """Spans of module-level plain bindings of ``names``, multi-line literals included."""
    lines = file_text.splitlines()
    spans: list[tuple[int, int]] = []
    for index, scan in enumerate(_definition_head_scan_lines(file_text, path=path)):
        match = _MODULE_LEVEL_ASSIGNMENT_RE.match(scan)
        if match is None or match.group(1) not in names:
            continue
        spans.append((index + 1, _definition_span_end_line(lines, index + 1, 0)))
    return spans


async def _commit_range_changes_comment_named_target(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
    comment_text: str,
) -> bool:
    """True when ``left``..``right`` changes a target the reviewer's text names.

    (a) A named path the range changed: any change without ``:line``, or one the
    line-anchored rule accepts for that line (the first
    ``_MAX_COMMENT_NAMED_PATHS`` such probes). (b) A named identifier whose
    definition span at ``left`` — a module-reachable head, decorators included,
    or a module-level plain binding — overlaps a hunk of the range in a changed
    file, diffed with its rename target so a pure move stays hunkless. Fails
    closed on any unreadable Git output.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _changed_paths_in_commit_range,
        _commit_range_touches_path,
        _diff_hunk_overlaps_line_span,
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
    line_probes = 0
    for path, line in named_paths:
        if path not in changed:
            continue
        if line is None:
            return True
        if line_probes >= _MAX_COMMENT_NAMED_PATHS:
            continue
        line_probes += 1
        if await _commit_range_touches_path(
            self, worktree_path=worktree_path, left=left, right=right, path=path, line=line
        ):
            return True
    if not names:
        return False
    rename_map, _name_status_z = await _rename_map_in_commit_range(
        self, worktree_path=worktree_path, left=left, right=right
    )
    for candidate in changed[:_MAX_CALLEE_EVIDENCE_CANDIDATE_PATHS]:
        file_text = await _path_text_at_ref(
            self, worktree_path=worktree_path, ref=left, path=candidate
        )
        if not file_text:
            continue
        spans = _importable_definition_spans_for_names(
            file_text, names, path=candidate
        ) + _module_level_assignment_spans(file_text, names, path=candidate)
        if not spans:
            continue
        diff_text = await _path_diff_text_in_commit_range(
            self,
            worktree_path=worktree_path,
            left=left,
            right=right,
            path=candidate,
            rename_path=rename_map.get(candidate),
        )
        if diff_text is None:
            continue
        if any(
            _diff_hunk_overlaps_line_span(diff_text, start, end, file_text=file_text)
            or _diff_adds_decorators_above_span(diff_text, start)
            for start, end in spans
        ):
            return True
    return False
