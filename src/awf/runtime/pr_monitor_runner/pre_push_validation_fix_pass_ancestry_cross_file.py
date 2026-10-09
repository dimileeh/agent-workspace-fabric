"""Cross-file call-site→definition FIXED evidence (issue #1019).

Attempt 0 already accepts call-site→definition evidence, but only *within* the
reviewed file: ``_diff_provides_related_line_evidence`` resolves the callee
referenced at the anchored line against that same file's definition spans. A
review anchored where a behaviour is *observed* and fixed where it is
*implemented* therefore carries no evidence AWF can see once the two live in
different packages, and the correction attempt parks a correct fix as
``needs_human`` (aira-agent PRs #1478 and #1491).

This module widens that one link across files for the correction attempt's
fourth evidence gate (see ``comment_verdict_correction``): the callee names at
the anchored line are resolved against the *changed* files' module-reachable
definitions, and the range's diff must overlap the definition's span — touching
the file is not enough. Kept in its own module because
``pre_push_validation_fix_pass_ancestry`` sits at the first-party line budget,
and because the cross-file rule reads as one unit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
    _definition_head_is_assignment,
    _definition_span_is_class,
    _iter_definition_spans,
    _path_allows_js_private_fields,
)

# Receivers attempt 0 resolves in the reviewed file or its own class. Linking
# them to a same-named definition in another file would be guesswork without
# import/type resolution, so ``self`` / ``cls`` / ``this`` calls fail closed.
# Other qualifiers (``metrics.record(...)``) are exactly the cross-file shape.
_IN_FILE_CALLEE_QUALIFIERS = frozenset({"self", "cls", "this"})

# Upper bound on the changed paths this probe reads back. A single item's commit
# range touches one to three files; the cap only stops a pathological range from
# turning the fourth evidence gate into an unbounded fan of Git reads.
_MAX_CALLEE_EVIDENCE_CANDIDATE_PATHS = 25


def _definition_is_reachable_from_module_scope(
    file_text: str,
    all_spans: list[tuple[str, int, int, int]],
    *,
    start: int,
    indent: int,
) -> bool:
    """True when every definition enclosing ``start`` is a class.

    A method of a module-level class is importable as ``Class.method``; a
    closure defined inside a function is not reachable from another module at
    all, so it can never be the callee of a cross-file call site.
    """
    return all(
        _definition_span_is_class(file_text, span_start)
        for _name, span_start, span_end, span_indent in all_spans
        if span_start < start <= span_end and span_indent < indent
    )


def _importable_definition_spans_for_names(
    file_text: str,
    names: frozenset[str],
    *,
    path: str | None = None,
) -> list[tuple[int, int]]:
    """Spans of ``names``' definitions in ``file_text`` reachable from module scope.

    Returns ``(start, end)`` line spans for every ``def`` / ``class`` /
    ``function`` / arrow head whose name is in ``names`` and that another module
    could actually reach: module-level heads, and direct members of a
    module-level class. Function-local closures, indented JS/TS heads and
    indented assignment bindings are block-scoped or unreachable and fail closed
    — the same exclusions ``_resolve_callee_definition_span`` applies to
    module-scope candidates.
    """
    if not names or not file_text:
        return []
    all_spans = _iter_definition_spans(file_text, path=path)
    js_ts = _path_allows_js_private_fields(path)
    spans: list[tuple[int, int]] = []
    for name, start, end, indent in all_spans:
        if name not in names:
            continue
        if indent > 0 and (js_ts or _definition_head_is_assignment(file_text, start)):
            continue
        if not _definition_is_reachable_from_module_scope(
            file_text, all_spans, start=start, indent=indent
        ):
            continue
        spans.append((start, end))
    return spans


async def _path_diff_text_in_commit_range(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
    path: str,
) -> str | None:
    """``git diff -U0 left right -- path``, or None when the diff cannot be read."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _GIT_DIFF_FIND_RENAMES,
        _git_env_for_merge_safety_object_lookup,
    )

    result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "diff",
            _GIT_DIFF_FIND_RENAMES,
            "-U0",
            left,
            right,
            "--",
            path,
        ),
        env=_git_env_for_merge_safety_object_lookup(),
    )
    if not result.ok:
        return None
    raw = result.stdout_bytes
    if raw is not None:
        return cast(str, raw.decode("utf-8", errors="surrogateescape"))
    return cast(str, result.stdout or "")


def _cross_file_callee_names(file_text: str, line: int, *, path: str) -> frozenset[str]:
    """Callee names at ``line`` that may resolve in another file."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
        _callee_refs_from_file_line,
    )

    return frozenset(
        name
        for qualifier, name in _callee_refs_from_file_line(file_text, line, path=path)
        if qualifier is None or qualifier not in _IN_FILE_CALLEE_QUALIFIERS
    )


async def _commit_range_changes_callee_definition(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
    item_path: str,
    item_line: int,
) -> bool:
    """True when ``left``..``right`` changes a callee's definition in another file.

    Resolves the callee(s) referenced at ``item_line`` of ``item_path`` as of
    ``left`` — the side ``-U0`` hunk headers and the definition spans are both
    expressed in — then, for each other path the range changed, requires a
    module-reachable definition of one of those names whose span the range's
    diff overlaps. ``item_path`` itself is skipped: a same-path change is what
    the line-anchored and path-level gates already answer. Fails closed on any
    unreadable Git output.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _changed_paths_in_commit_range,
        _diff_hunk_overlaps_line_span,
        _normalize_evidence_item_path,
        _path_text_at_ref,
    )

    normalized_item = _normalize_evidence_item_path(item_path)
    if not normalized_item or item_line < 1:
        return False
    item_text = await _path_text_at_ref(
        self, worktree_path=worktree_path, ref=left, path=normalized_item
    )
    if not item_text:
        return False
    names = _cross_file_callee_names(item_text, item_line, path=normalized_item)
    if not names:
        return False
    candidates = [
        normalized
        for normalized in (
            _normalize_evidence_item_path(changed)
            for changed in await _changed_paths_in_commit_range(
                self, worktree_path=worktree_path, left=left, right=right
            )
        )
        if normalized and normalized != normalized_item
    ][:_MAX_CALLEE_EVIDENCE_CANDIDATE_PATHS]
    for candidate in candidates:
        candidate_text = await _path_text_at_ref(
            self, worktree_path=worktree_path, ref=left, path=candidate
        )
        if not candidate_text:
            continue
        spans = _importable_definition_spans_for_names(candidate_text, names, path=candidate)
        if not spans:
            continue
        diff_text = await _path_diff_text_in_commit_range(
            self, worktree_path=worktree_path, left=left, right=right, path=candidate
        )
        if diff_text is None:
            continue
        for start, end in spans:
            if _diff_hunk_overlaps_line_span(diff_text, start, end, file_text=candidate_text):
                return True
    return False
