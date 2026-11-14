"""Comment-named target FIXED evidence (issue #1232).

The four earlier correction-attempt evidence gates (see
``comment_verdict_correction``) are all derived from the comment *anchor*.
Reviewers often anchor at a usage site and name the place to fix in the comment
*body* instead — a runbook line, the route helper's file, the tuple a filter
reads — so a genuine fix there escalated to ``needs_human`` three times on
2026-11-13.

This module adds the fifth gate. Over the item's own commit range it accepts:

* **(a)** a change to a repo-relative path the reviewer's comment names
  explicitly, in any language. With ``:line``, the range must touch that line
  under the existing line-anchored rule (``_commit_range_touches_path``);
  without one, any change to the file counts.
* **(b)** a change to the *definition* of an identifier the reviewer's comment
  names in single backticks (``name`` / ``name()``) in a **Python** source, read
  with the stdlib ``ast``: the effective module-level ``def``/``class`` or plain
  assignment of that name. A definition the range *adds* — in an existing file
  or a new one — counts too. JS/TS sources (``.js``/``.jsx``/``.ts``/``.tsx``/
  ``.mjs``/``.cjs``, ...) have no parser here, so an identifier-named target in
  them yields no evidence and the item fails closed, as before #1232.

Only the reviewer's text is read (``reviewer_comment_text``): AWF-authored
replies, which can carry the agent's own reason (#1018), and the agent's
verdict reason are never evidence. Absolute paths, ``..`` / ``.`` segments,
``~``, globs and URLs never count, and every unreadable Git read or unparsable
source fails closed. Kept in its own module because ``..._cross_file`` sits near
the first-party line budget, and because the rule reads as one unit.
"""

from __future__ import annotations

import ast
import keyword
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
    _CALLEE_KEYWORD_BLOCKLIST,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file import (
    _MAX_CALLEE_EVIDENCE_CANDIDATE_PATHS,
    _PYTHON_CALL_SITE_SUFFIXES,
    _diff_adds_decorators_above_span,
    _path_diff_text_in_commit_range,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

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
# A Python identifier; a JS-only spelling such as ``$helper`` names nothing here.
_NAMED_IDENTIFIER_RE = re.compile(r"((?!\d)\w+)(?:\(\))?")
_NON_DEFINITION_NAMES = (
    _CALLEE_KEYWORD_BLOCKLIST
    | frozenset(keyword.kwlist)
    | frozenset({"self", "cls", "this", "null", "undefined"})
)
_PYTHON_FUNCTION_OR_CLASS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


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


def _module_scope_nodes(tree: ast.Module) -> Iterator[ast.AST]:
    """Every node of ``tree`` evaluated in module scope, in source order.

    Function, class and lambda bodies and comprehension targets bind in scopes
    of their own, so only their module-scope parts — decorators, defaults,
    bases, a comprehension's iterables and conditions — are walked.
    """
    stack: list[ast.AST] = list(reversed(tree.body))
    while stack:
        node = stack.pop()
        yield node
        children: list[ast.AST]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            children = [
                *getattr(node, "decorator_list", ()),
                *node.args.defaults,
                *(default for default in node.args.kw_defaults if default is not None),
            ]
        elif isinstance(node, ast.ClassDef):
            children = [*node.decorator_list, *node.bases, *node.keywords]
        elif isinstance(node, ast.comprehension):
            children = [node.iter, *node.ifs]
        else:
            children = list(ast.iter_child_nodes(node))
        stack.extend(reversed(children))


def _bound_names(node: ast.AST) -> tuple[str, ...]:
    """The names ``node`` binds (or deletes) in the scope it runs in."""
    if isinstance(node, ast.Name):
        return () if isinstance(node.ctx, ast.Load) else (node.id,)
    if isinstance(node, _PYTHON_FUNCTION_OR_CLASS):
        return (node.name,)
    if isinstance(node, ast.alias):
        return ((node.asname or node.name).split(".")[0],)
    if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
        return (node.name,) if node.name else ()
    if isinstance(node, ast.MatchMapping):
        return (node.rest,) if node.rest else ()
    return ()


def _named_definition_spans(
    file_text: str | None, names: frozenset[str]
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Inclusive line spans of the effective module-level definitions of ``names``.

    Returns the ``def``/``class`` spans (decorators included) and the plain
    assignment spans separately, read from the Python ``ast`` of ``file_text``.
    A backticked name carries no receiver, so only a top-level statement binding
    it counts — a same-named method, closure or class attribute is not the
    definition the reviewer named (PRRT_kwDOSJAM6s63LVep). The module body runs
    in order, so only the last such statement is the value importers reach; an
    earlier one is dead (PRRT_kwDOSJAM6s63LR_S). A name the module scope also
    binds any other way — under a guard, by import, as a loop/``with``/unpacking
    target, through ``global`` in a function — has no single definition and is
    withheld, as is everything after a ``from ... import *``. Unparsable text
    fails closed.
    """
    if not file_text or not names:
        return [], []
    try:
        tree = ast.parse(file_text)
    except (SyntaxError, ValueError, RecursionError):
        return [], []
    definitions: dict[ast.AST, ast.stmt] = {}
    for statement in tree.body:
        if isinstance(statement, _PYTHON_FUNCTION_OR_CLASS):
            definitions[statement] = statement
        elif isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    definitions[target] = statement
        elif (
            isinstance(statement, ast.AnnAssign)
            and statement.value is not None
            and isinstance(statement.target, ast.Name)
        ):
            definitions[statement.target] = statement
    withheld = {
        name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names
    }
    last: dict[str, ast.stmt] = {}
    for node in _module_scope_nodes(tree):
        if isinstance(node, ast.alias) and node.name == "*":
            return [], []
        for name in _bound_names(node):
            if name not in names:
                continue
            defining = definitions.get(node)
            if defining is None:
                withheld.add(name)
            else:
                last[name] = defining
    heads: list[tuple[int, int]] = []
    bindings: list[tuple[int, int]] = []
    for name, statement in last.items():
        if name in withheld:
            continue
        end = statement.end_lineno or statement.lineno
        if isinstance(statement, _PYTHON_FUNCTION_OR_CLASS):
            start = min([statement.lineno, *(item.lineno for item in statement.decorator_list)])
            heads.append((start, end))
        else:
            bindings.append((statement.lineno, end))
    return heads, bindings


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
    # Definition discovery reads Python sources only: a JS/TS identifier-named
    # target has no parser here and fails closed, and a docs or text snippet
    # spelling the name is not its definition (PRRT_kwDOSJAM6s63O-t2).
    sources = [path for path in changed if Path(path).suffix.lower() in _PYTHON_CALL_SITE_SUFFIXES]
    for candidate in sources[:_MAX_CALLEE_EVIDENCE_CANDIDATE_PATHS]:
        file_text = await _path_text_at_ref(
            self, worktree_path=worktree_path, ref=left, path=candidate
        )
        spans, bindings = _named_definition_spans(file_text, names)
        # A rename source's right-side text is read under its target, itself a candidate.
        right_text = (
            None
            if candidate in rename_map
            else await _path_text_at_ref(
                self, worktree_path=worktree_path, ref=right, path=candidate
            )
        )
        right_heads, right_bindings = _named_definition_spans(right_text, names)
        right_spans = right_heads + right_bindings
        if not (spans or bindings or right_spans):
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
        if (
            any(
                _diff_hunk_overlaps_line_span(diff_text, start, end, file_text=file_text)
                or _diff_adds_decorators_above_span(diff_text, start)
                for start, end in spans
            )
            or any(
                # A statement does not extend by indentation, so a left-side insert
                # counts only inside the binding; one that does continue it overlaps
                # its right-side span (PRRT_kwDOSJAM6s63P5ZX).
                _diff_hunk_overlaps_line_span(diff_text, start, end)
                for start, end in bindings
            )
            or any(
                _diff_new_hunk_overlaps_line_span(diff_text, start, end)
                for start, end in right_spans
            )
        ):
            return True
    return False
