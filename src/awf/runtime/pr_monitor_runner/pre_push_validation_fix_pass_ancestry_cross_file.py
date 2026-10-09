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

import re
from pathlib import Path
from typing import Any, cast

from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
    _definition_head_is_assignment,
    _definition_is_nested_in_other,
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

# Suffixes whose bare-name bindings the import reader below understands. A bare
# callee in any other language keeps the name-only rule.
_PYTHON_CALL_SITE_SUFFIXES = frozenset({".py", ".pyi"})

# ``from pkg.mod import a, b as c`` — absolute targets only. A leading dot
# (relative) or a plain ``import pkg.mod`` binds nothing that narrows a
# candidate path, so neither is matched here.
_ABSOLUTE_FROM_IMPORT_RE = re.compile(
    r"^[ \t]*from[ \t]+([A-Za-z_]\w*(?:\.\w+)*)[ \t]+import[ \t]+(.+)$"
)


def _module_path_segments(path: str) -> list[str]:
    """``path`` as directory segments, with a Python module suffix dropped."""
    normalized = path.replace("\\", "/")
    stem, _dot, suffix = normalized.rpartition(".")
    if stem and f".{suffix.lower()}" in _PYTHON_CALL_SITE_SUFFIXES:
        normalized = stem
    return [segment for segment in normalized.split("/") if segment]


def _imported_binding_names(targets: str) -> list[str]:
    """Names an import target list binds, following ``orig as alias``."""
    names: list[str] = []
    for piece in targets.replace("(", " ").replace(")", " ").split(","):
        parts = piece.split()
        if not parts or parts[0] == "*":
            continue
        names.append(parts[2] if len(parts) >= 3 and parts[1] == "as" else parts[0])
    return names


def _bare_name_import_module_paths(file_text: str, *, path: str) -> dict[str, frozenset[str]]:
    """Module paths each name in ``file_text`` is bound to by an absolute import.

    Only absolute ``from M import ...`` statements are read, including the
    parenthesized multi-line form; the value is ``M`` as a ``/``-joined path
    prefix. Relative imports, star imports and plain ``import M`` carry no
    name→path link, so the names they bind stay out of the map and keep the
    name-only rule (PRRT_kwDOSJAM6s6q7bSI). An import head quoted inside a
    docstring can only *add* a binding, which narrows rather than widens the
    gate, so the scan is deliberately lexical.
    """
    if f".{path.rsplit('.', 1)[-1].lower()}" not in _PYTHON_CALL_SITE_SUFFIXES:
        return {}
    bindings: dict[str, set[str]] = {}
    lines = file_text.splitlines()
    index = 0
    while index < len(lines):
        match = _ABSOLUTE_FROM_IMPORT_RE.match(lines[index])
        index += 1
        if match is None:
            continue
        module, targets = match.group(1), match.group(2)
        while targets.count("(") > targets.count(")") and index < len(lines):
            targets += " " + lines[index].strip()
            index += 1
        module_path = module.replace(".", "/")
        for bound in _imported_binding_names(targets):
            bindings.setdefault(bound, set()).add(module_path)
    return {name: frozenset(paths) for name, paths in bindings.items()}


def _candidate_is_under_module_path(candidate: str, module_path: str) -> bool:
    """True when ``candidate`` is the imported module's file or sits inside it.

    Matched as a contiguous segment run so a source root (``src/``) is tolerated
    and a package import whose ``__init__`` re-exports the callee still reaches
    the submodule that defines it. A facade that re-exports *across* packages is
    not followed and fails closed, leaving the item on the #928 escalation path
    rather than accepting a path the call site cannot be shown to reach.
    """
    segments = _module_path_segments(candidate)
    wanted = _module_path_segments(module_path)
    if not wanted or len(wanted) > len(segments):
        return False
    return any(
        segments[start : start + len(wanted)] == wanted
        for start in range(len(segments) - len(wanted) + 1)
    )


def _bare_names_bound_to_candidate(
    bare_names: frozenset[str],
    bindings: dict[str, frozenset[str]],
    candidate: str,
) -> frozenset[str]:
    """Bare callees whose import binding, when readable, admits ``candidate``."""
    return frozenset(
        name
        for name in bare_names
        if not bindings.get(name)
        or any(
            _candidate_is_under_module_path(candidate, module_path)
            for module_path in bindings[name]
        )
    )


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
    bare_names: frozenset[str] = frozenset(),
) -> list[tuple[int, int]]:
    """Spans in ``file_text`` another module could reach as the named callee.

    Returns ``(start, end)`` line spans for every ``def`` / ``class`` /
    ``function`` / arrow head whose name matches, under the reachability rule
    its *call shape* allows. ``names`` are attribute-qualified callees
    (``metrics.record()``), whose receiver may be a module or an instance, so a
    direct member of a module-level class counts alongside a module-level head.
    ``bare_names`` are bare callees (``record()``), which another module can
    only reach through a module-scope binding — ``Class.method`` is reachable as
    an attribute but never as bare ``method`` — so a class member fails closed
    for them (PRRT_kwDOSJAM6s6q699Q), matching the module-scope candidate rule
    attempt 0's ``_resolve_callee_definition_span`` applies to bare calls. A
    name called both ways on the anchored line keeps the attribute rule.

    Function-local closures, indented JS/TS heads and indented assignment
    bindings are block-scoped or unreachable and fail closed under both rules.
    """
    if not (names or bare_names) or not file_text:
        return []
    all_spans = _iter_definition_spans(file_text, path=path)
    js_ts = _path_allows_js_private_fields(path)
    spans: list[tuple[int, int]] = []
    for name, start, end, indent in all_spans:
        qualified = name in names
        if not qualified and name not in bare_names:
            continue
        if indent > 0 and (js_ts or _definition_head_is_assignment(file_text, start)):
            continue
        if qualified:
            if not _definition_is_reachable_from_module_scope(
                file_text, all_spans, start=start, indent=indent
            ):
                continue
        elif _definition_is_nested_in_other(all_spans, start=start, indent=indent):
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
    rename_path: str | None = None,
) -> str | None:
    """``git diff -U0 left right -- path [rename_path]``, or None when unreadable.

    ``rename_path`` is ``path``'s rename target in the range, when it has one.
    Both sides must sit in the pathspec or git cannot pair the move: pathspec
    filtering runs before rename detection, so an old-path-only diff of a pure
    rename reads as a whole-file deletion whose hunk overlaps every definition
    span in the file. Passing both paths keeps a move with an unchanged body
    hunkless, and keeps a move that also edits the body expressed as old-side
    hunks in ``path``'s line numbering (PRRT_kwDOSJAM6s6q65JH).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _GIT_DIFF_FIND_RENAMES,
        _git_env_for_merge_safety_object_lookup,
    )

    pathspec = [path] if rename_path is None else [path, rename_path]
    result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "diff",
            _GIT_DIFF_FIND_RENAMES,
            "-U0",
            left,
            right,
            "--",
            *pathspec,
        ),
        env=_git_env_for_merge_safety_object_lookup(),
    )
    if not result.ok:
        return None
    raw = result.stdout_bytes
    if raw is not None:
        return cast(str, raw.decode("utf-8", errors="surrogateescape"))
    return cast(str, result.stdout or "")


def _cross_file_callee_names(
    file_text: str, line: int, *, path: str
) -> tuple[frozenset[str], frozenset[str]]:
    """Callee names at ``line`` that may resolve in another file.

    Returns ``(attribute_qualified, bare)`` separately because the two call
    shapes reach different definitions across a module boundary — see
    ``_importable_definition_spans_for_names``.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
        _callee_refs_from_file_line,
    )

    refs = _callee_refs_from_file_line(file_text, line, path=path)
    qualified = frozenset(
        name
        for qualifier, name in refs
        if qualifier is not None and qualifier not in _IN_FILE_CALLEE_QUALIFIERS
    )
    bare = frozenset(name for qualifier, name in refs if qualifier is None)
    return qualified, bare


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
    diff overlaps. A bare callee is additionally held to the module its own
    ``from`` import names, so a same-named definition in a module the call site
    never imported is not evidence (PRRT_kwDOSJAM6s6q7bSI); attribute-qualified
    callees keep resolving by name, their receiver being an instance as often as
    a module. A candidate that the range renamed is diffed against its
    rename target too, so a pure move of the callee's file is not mistaken for a
    change to its body. ``item_path`` itself is skipped: a same-path change is
    what the line-anchored and path-level gates already answer. Fails closed on
    any unreadable Git output.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _changed_paths_in_commit_range,
        _diff_hunk_overlaps_line_span,
        _normalize_evidence_item_path,
        _path_text_at_ref,
        _rename_map_in_commit_range,
    )

    normalized_item = _normalize_evidence_item_path(item_path)
    if not normalized_item or item_line < 1:
        return False
    item_text = await _path_text_at_ref(
        self, worktree_path=worktree_path, ref=left, path=normalized_item
    )
    if not item_text:
        return False
    names, bare_names = _cross_file_callee_names(item_text, item_line, path=normalized_item)
    if not (names or bare_names):
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
    if not candidates:
        return False
    rename_map, _name_status_z = await _rename_map_in_commit_range(
        self, worktree_path=worktree_path, left=left, right=right
    )
    bare_bindings = _bare_name_import_module_paths(item_text, path=normalized_item)
    for candidate in candidates:
        candidate_bare = _bare_names_bound_to_candidate(bare_names, bare_bindings, candidate)
        if not (names or candidate_bare):
            continue
        candidate_text = await _path_text_at_ref(
            self, worktree_path=worktree_path, ref=left, path=candidate
        )
        if not candidate_text:
            continue
        spans = _importable_definition_spans_for_names(
            candidate_text, names, path=candidate, bare_names=candidate_bare
        )
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
        for start, end in spans:
            if _diff_hunk_overlaps_line_span(diff_text, start, end, file_text=candidate_text):
                return True
    return False
