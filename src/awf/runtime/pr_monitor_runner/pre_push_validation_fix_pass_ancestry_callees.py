"""Callee/definition-span helpers for pre-push FIXED evidence linking."""

from __future__ import annotations

import ast
import functools
import re
from collections.abc import Iterator
from typing import Final

from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_callees_masking as _masking,
)

# The masking leaf lives in a sibling module so this one stays under the
# first-party line guardrail; re-export every moved name so existing imports
# and test references still resolve here. Rebinding (rather than ``from ...
# import``) keeps these real attributes of this module under mypy ``strict``.
_SPLITLINES_SEPARATOR_CHARS = _masking._SPLITLINES_SEPARATOR_CHARS
_JS_TS_PRIVATE_FIELD_SUFFIXES = _masking._JS_TS_PRIVATE_FIELD_SUFFIXES
_JS_REGEX_PREFIX_KEYWORDS = _masking._JS_REGEX_PREFIX_KEYWORDS
_mask_char_preserving_splitlines_separators = _masking._mask_char_preserving_splitlines_separators
_path_allows_js_private_fields = _masking._path_allows_js_private_fields
_path_is_jsx = _masking._path_is_jsx
_js_slash_can_start_regex = _masking._js_slash_can_start_regex
_append_masked_js_regex_at_for_callee_scan = _masking._append_masked_js_regex_at_for_callee_scan
_python_string_prefix_is_f = _masking._python_string_prefix_is_f
_append_comment_run_for_callee_scan = _masking._append_comment_run_for_callee_scan
_append_masked_block_comment_at_for_callee_scan = (
    _masking._append_masked_block_comment_at_for_callee_scan
)
_append_masked_quote_at_for_callee_scan = _masking._append_masked_quote_at_for_callee_scan
_append_retained_brace_expr = _masking._append_retained_brace_expr
_mask_quoted_region_for_callee_scan = _masking._mask_quoted_region_for_callee_scan
_mask_comments_and_string_literals_for_callee_scan = (
    _masking._mask_comments_and_string_literals_for_callee_scan
)
_mask_jsx_text_nodes_for_callee_scan = _masking._mask_jsx_text_nodes_for_callee_scan

# JS/TS identifiers may include ``$`` (e.g. ``$helper``). Python ``\b`` treats
# ``$`` as non-word, so ``$helper()`` would otherwise match as bare ``helper``
# and link an unrelated module-level ``helper``. Use lookbehind boundaries that
# treat ``$`` as an identifier character instead of ``\b``. Keep Unicode ``\w``
# so ``def 函数`` / ``class Café`` remain scope boundaries (ASCII-only would
# make nested ASCII helpers look module-scoped for FIXED evidence).
_JS_IDENT = r"(?:[^\W\d]|\$)[\w$]*"
_IDENT_BOUNDARY = r"(?<![\w$])"
_IDENT_END = r"(?![\w$])"
# Optional ``?`` before ``.`` so JS/TS ``client?.send()`` keeps the receiver
# (bare ``send`` would incorrectly link an unrelated module-level ``send``).
# Optional ``?.`` before ``(`` so bare/attr optional-call ``helper?.()`` /
# ``client?.send?.()`` still extracts the callee (body-only repairs stay
# FIXED-with-evidence).
_CALLEE_REF_RE = re.compile(
    rf"(?:({_IDENT_BOUNDARY}{_JS_IDENT})\s*\??\.\s*)?({_IDENT_BOUNDARY}{_JS_IDENT})"
    rf"\s*(?:\?\.)?\s*\("
)
_CALLEE_KEYWORD_BLOCKLIST = frozenset(
    {
        "if",
        "elif",
        "else",
        "for",
        "while",
        "with",
        "match",
        "case",
        "def",
        "class",
        "return",
        "yield",
        "await",
        "raise",
        "assert",
        "lambda",
        "not",
        "and",
        "or",
        "in",
        "is",
        "try",
        "except",
        "finally",
        "import",
        "from",
        "as",
        "pass",
        "break",
        "continue",
        "global",
        "nonlocal",
        "function",
        "typeof",
        "instanceof",
        "switch",
        "catch",
        "new",
        "delete",
        "void",
        "of",
        "let",
        "const",
        "var",
        "async",
    }
)
# Assignment heads shared by declaration-line and enclosing-body matchers:
# ``const helper = () =>``, ``helper = async () =>``, ``helper = function``, ``helper = lambda``.
# Name captures use ``_JS_IDENT`` so ``$helper = () =>`` matches the callee name.
# Optional TS return type between params and ``=>`` so
# ``const helper = (value: number): number =>`` / ``async (...): Promise<T> =>``
# still record a definition span (body-only repairs stay FIXED-with-evidence).
_TS_ARROW_RETURN_TYPE = r"(?::(?![ \t]*=>)[^;=\n]*?)?"
_ASSIGNMENT_DEFINITION_HEAD = (
    rf"(?:(?:const|let|var)[ \t]+)?({_JS_IDENT})[ \t]*="
    rf"[ \t]*(?:async[ \t]+)?(?:(?:\([^)]*\)|{_JS_IDENT})[ \t]*"
    rf"{_TS_ARROW_RETURN_TYPE}[ \t]*=>|function\b|lambda\b)"
)
# A plain assignment binding simple targets: ``validate = replacement``,
# ``const validate = replacement``, ``validate: Recorder = replacement``. Its
# right-hand side carries no definition head of its own — those heads are
# ``_ASSIGNMENT_DEFINITION_HEAD``'s subject and are read as definitions — so
# this is the rebinding form definition discovery cannot see.
_PLAIN_ASSIGNMENT_TARGETS_RE = re.compile(
    rf"^[ \t]*(?:(?:const|let|var)[ \t]+)?"
    rf"({_JS_IDENT}(?:[ \t]*,[ \t]*{_JS_IDENT})*)"
    rf"(?:[ \t]*:[^=\n]*)?[ \t]*=(?!=)"
)
_DEFINITION_NAME_LINE_RE = re.compile(
    r"^[-+](?!\+\+|--)[ \t]*(?:"
    rf"(?:async[ \t]+)?def[ \t]+({_JS_IDENT})\s*\("
    # Optional ``export`` so ``export function helper`` / ``export async function``
    # count as definition heads (body-only repairs stay FIXED-with-evidence).
    rf"|(?:export[ \t]+)?(?:async[ \t]+)?function[ \t]+({_JS_IDENT})\s*\("
    rf"|class[ \t]+({_JS_IDENT}){_IDENT_END}"
    r"|" + _ASSIGNMENT_DEFINITION_HEAD + r")"
)
_ENCLOSING_DEFINITION_RE = re.compile(
    rf"^[ \t]*(?:async[ \t]+)?def[ \t]+({_JS_IDENT})\s*\("
    rf"|^[ \t]*(?:export[ \t]+)?(?:async[ \t]+)?function[ \t]+({_JS_IDENT})\s*\("
    rf"|^[ \t]*class[ \t]+({_JS_IDENT}){_IDENT_END}"
    r"|^[ \t]*" + _ASSIGNMENT_DEFINITION_HEAD
)
# Single-character probes for the right-anchored receiver-chain scan in
# ``_trailing_receiver_chain``, using the same classes as the dotted-chain
# patterns it replaces: ``[\w$]`` continues an identifier, ``\s`` separates
# chain links, and ``_JS_IDENT_RE`` validates one link.
_IDENT_CHAR_RE = re.compile(r"[\w$]")
_SPACE_CHAR_RE = re.compile(r"\s")
_JS_IDENT_RE = re.compile(_JS_IDENT)
_ATTR_CALLEE_QUALIFIERS = frozenset({"self", "cls"})
# JS/TS class instance receiver (gated by ``_path_allows_js_private_fields``).
_JS_ATTR_CALLEE_QUALIFIERS = frozenset({"this"})
# Decorator basename (``@staticmethod`` / ``foo.classmethod`` → last segment).
_DECORATOR_BASENAME_RE = re.compile(rf"^[ \t]*@({_JS_IDENT}(?:\.{_JS_IDENT})*)")


def _leading_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


_BLOCK_CLOSER_RE = re.compile(r"^[ \t]*[\])}]+[ \t]*[;,]?[ \t]*$")
# Multiline Python ``def`` / ``async def`` signature tails (``):`` /
# ``) -> ReturnType:``) must stay inside the opening definition. Without this,
# ``_definition_span_end_line`` ends before the body and a nested helper looks
# module-scoped for FIXED callee linking.
_PYTHON_SIGNATURE_CLOSER_RE = re.compile(r"^[ \t]*\)[ \t]*(?:->.*)?:[ \t]*$")


def _is_ignorable_span_gap_line(line: str) -> bool:
    """Blank or full-line comment gaps do not end a definition span."""
    stripped = line.strip()
    if not stripped:
        return True
    return stripped.startswith("#") or stripped.startswith("//")


def _is_block_closer_line(line: str) -> bool:
    """JS/TS brace closers and Python signature closers stay in the def span."""
    return (
        _BLOCK_CLOSER_RE.match(line) is not None
        or _PYTHON_SIGNATURE_CLOSER_RE.match(line) is not None
    )


def _definition_span_end_line(lines: list[str], start_line: int, indent: int) -> int:
    """Inclusive end line via lexical dedent, not only the next definition head.

    Body lines are deeper than ``indent``. Same-indent brace closers (arrow /
    function blocks) remain part of the span. Any other equal-or-lower indent
    content — module assignments, sibling statements, the next def — ends the
    span on the prior line (trailing blank/comment gaps stay included).
    """
    end_line = len(lines)
    for idx in range(start_line, len(lines)):
        raw = lines[idx]
        if _is_ignorable_span_gap_line(raw):
            continue
        cand_indent = _leading_indent(raw)
        if cand_indent > indent:
            continue
        if cand_indent == indent and _is_block_closer_line(raw):
            # Include the closer; keep scanning so trailing gaps before the next
            # sibling remain inside the span (matches prior blank-inclusive ends).
            continue
        return idx  # 1-based end is the line before this content
    return end_line


def _line_belongs_to_definition_span(
    lines: list[str], line: int, start: int, end: int, indent: int
) -> bool:
    """True when ``line`` is the head, a deeper body line, or an interior gap.

    Lexical spans include trailing blank/comment gaps after the last body line.
    Those trailing gaps must stay uncontained so a module-level near-anchor
    insert after a definition does not inherit that definition's identity.
    Interior blanks between body lines (or before a same-indent block closer)
    remain contained — otherwise indent-0 blanks look module-level and break
    near-anchor FIXED evidence (false-accept of neighboring-def inserts).
    """
    if not (start <= line <= end):
        return False
    if line == start:
        return True
    raw = lines[line - 1]
    if _leading_indent(raw) > indent:
        return True
    if not _is_ignorable_span_gap_line(raw):
        return False
    # Interior only: later non-ignorable body/closer content remains in-span.
    for idx in range(line, end):
        later = lines[idx]
        if _is_ignorable_span_gap_line(later):
            continue
        later_indent = _leading_indent(later)
        if later_indent > indent:
            return True
        return later_indent == indent and _is_block_closer_line(later)
    return False


@functools.lru_cache(maxsize=32)
def _cached_masked_scan_lines(file_text: str, path: str | None) -> tuple[str, ...]:
    """Memoize masked split lines keyed on ``(file_text, path)``."""
    return tuple(
        _mask_comments_and_string_literals_for_callee_scan(file_text, path=path).splitlines()
    )


def _definition_head_scan_lines(file_text: str, *, path: str | None = None) -> list[str]:
    """Return lines with comments/strings masked for definition-head matching.

    Definition discovery must honor the same lexical context as callee scanning:
    ``def`` / ``function`` / ``class`` text inside multiline strings or comments
    is not an executable definition.

    Masking preserves every ``str.splitlines()`` separator (including form feed
    and U+2028), so masked and raw line indices stay aligned. Defensive pad /
    truncate remains so a future masking drift cannot raise on
    ``zip(..., strict=True)`` or out-of-range index lookups — empty padded lines
    fail closed on those indices.
    """
    raw_count = len(file_text.splitlines())
    scan_lines = list(_cached_masked_scan_lines(file_text, path))
    if len(scan_lines) < raw_count:
        scan_lines.extend([""] * (raw_count - len(scan_lines)))
    elif len(scan_lines) > raw_count:  # pragma: no cover - defensive
        scan_lines = scan_lines[:raw_count]
    return scan_lines


def _plain_assignment_rebound_scope_names(
    file_text: str,
    all_spans: list[tuple[str, int, int, int]],
    *,
    path: str | None,
    names: frozenset[str],
    definition_head_starts: frozenset[int],
) -> set[tuple[int, str]]:
    """``(scope_start, name)`` pairs a plain assignment rebinds in that scope.

    Definition discovery recognizes only definition *heads*, so a scope that
    binds a name with ``validate = replacement`` after ``def validate`` leaves
    the head looking like the name's single effective binding while importing
    the name actually reaches ``replacement``. Every name a scope rebinds this
    way is reported so the caller can fail it closed: this lexical reader
    cannot order the assignment against the head any more than
    ``_module_scope_rebound_names`` can, and a guarded assignment need not be
    the binding that runs last (PRRT_kwDOSJAM6s6q_ywa).

    ``definition_head_starts`` are the lines already read as definitions of
    their own, which the last-head fold orders without help. Only statement
    positions count — bracket depth is tracked across the file so a keyword
    argument or a continuation-line parameter default (``validate=None,``) is
    not read as a binding — and each target is attributed to the scope whose
    body executes it, so a function-local assignment never shadows a
    module-level definition.
    """
    raw_lines = file_text.splitlines()
    scan_lines = _definition_head_scan_lines(file_text, path=path)
    rebound: set[tuple[int, str]] = set()
    depth = 0
    for idx, scan in enumerate(scan_lines):
        at_statement_start = depth == 0
        opened = scan.count("(") + scan.count("[") + scan.count("{")
        closed = scan.count(")") + scan.count("]") + scan.count("}")
        depth = max(0, depth + opened - closed)
        line = idx + 1
        if not at_statement_start or line in definition_head_starts:
            continue
        match = _PLAIN_ASSIGNMENT_TARGETS_RE.match(scan)
        if match is None:
            continue
        targets = {target.strip() for target in match.group(1).split(",")} & names
        if not targets:
            continue
        scope_start, _body_indent = _definition_binding_scope(
            file_text, all_spans, start=line, indent=_leading_indent(raw_lines[idx])
        )
        rebound.update((scope_start, target) for target in targets)
    return rebound


def _enclosing_definition_identity(
    file_text: str, line: int, *, path: str | None = None
) -> tuple[str, int] | None:
    """Return ``(name, start_line)`` for the nearest def/class/arrow at or above ``line``."""
    if line < 1 or not file_text:
        return None
    lines = file_text.splitlines()
    # Non-empty ``file_text`` always yields at least one splitlines entry
    # (even a lone ``\\n``), so this arm is unreachable after the guard above.
    if not lines:  # pragma: no cover
        return None
    scan_lines = _definition_head_scan_lines(file_text, path=path)
    idx = min(line, len(lines)) - 1
    while idx >= 0:
        match = _ENCLOSING_DEFINITION_RE.match(scan_lines[idx])
        if match is not None:
            # Every ``_ENCLOSING_DEFINITION_RE`` alternative captures a name.
            name = next((group for group in match.groups() if group), None)
            if name is None:  # pragma: no cover
                idx -= 1
                continue
            return (name, idx + 1)
        idx -= 1
    return None


def _iter_definition_spans(
    file_text: str, *, path: str | None = None
) -> list[tuple[str, int, int, int]]:
    """Return ``(name, start_line, end_line, indent)`` for each def/class/function/arrow."""
    lines = file_text.splitlines()
    scan_lines = _definition_head_scan_lines(file_text, path=path)
    starts: list[tuple[str, int, int]] = []
    # Masking preserves newlines, so raw and scan line counts stay aligned.
    for idx, (raw, scan) in enumerate(zip(lines, scan_lines, strict=True)):
        match = _ENCLOSING_DEFINITION_RE.match(scan)
        if match is None:
            continue
        # Every alternative captures a name; keep the None skip for type narrowing.
        name = next((group for group in match.groups() if group), None)
        if name is None:  # pragma: no cover
            continue
        starts.append((name, idx + 1, _leading_indent(raw)))
    spans: list[tuple[str, int, int, int]] = []
    for name, start_line, indent in starts:
        end_line = _definition_span_end_line(lines, start_line, indent)
        spans.append((name, start_line, end_line, indent))
    return spans


def _enclosing_class_span(
    file_text: str, line: int, *, path: str | None = None
) -> tuple[int, int] | None:
    """Return the ``(start, end)`` span of the nearest enclosing class for ``line``.

    Only return a class when ``line`` lies within its lexical span. A preceding
    class that ends before ``line`` (e.g. module-level code after the class, or
    an ordinary same-indent statement after a function-local class) is not
    enclosing — keep walking for an outer class, or return None.
    """
    lines = file_text.splitlines()
    if line < 1 or not lines:
        return None
    scan_lines = _definition_head_scan_lines(file_text, path=path)
    idx = min(line, len(lines)) - 1
    while idx >= 0:
        class_match = re.match(r"^[ \t]*class[ \t]+(\w+)\b", scan_lines[idx])
        if class_match is not None:
            class_indent = _leading_indent(lines[idx])
            start = idx + 1
            # Any nonblank equal-or-lower indent ends the class (not only the
            # next def/class head) so ``return self.helper()`` after a local
            # class is outside that class span.
            end = _definition_span_end_line(lines, start, class_indent)
            if start <= line <= end:
                return (start, end)
        idx -= 1
    return None


def _containing_definition_spans(
    file_text: str, line: int, *, path: str | None = None
) -> list[tuple[int, int, int]]:
    """Return ``(start, end, indent)`` spans containing ``line``, innermost first.

    A line belongs to a definition when it is the head, indented deeper than that
    head, or an interior blank/comment gap before later body content. Same-indent
    siblings after a nested def (e.g. ``return helper()`` after ``def helper``)
    and trailing span gaps stay outside the nested span.
    """
    if line < 1 or not file_text:
        return []
    lines = file_text.splitlines()
    # Same invariant as ``_enclosing_definition_identity``: non-empty text ⇒ lines.
    if not lines:  # pragma: no cover
        return []
    containing: list[tuple[int, int, int]] = []
    for _n, start, end, indent in _iter_definition_spans(file_text, path=path):
        if _line_belongs_to_definition_span(lines, line, start, end, indent):
            containing.append((start, end, indent))
    containing.sort(key=lambda item: (-item[2], -item[0]))
    return containing


def _containing_definition_identity(
    file_text: str, line: int, *, path: str | None = None
) -> tuple[str, int] | None:
    """Return ``(name, start_line)`` for the innermost def/class/arrow containing ``line``.

    Unlike ``_enclosing_definition_identity`` (nearest head at or above ``line``),
    module-level lines that merely follow a preceding definition return ``None``.
    Near-anchor FIXED evidence must use this so an insert inside a neighboring
    function cannot share identity with a module-level review anchor. Interior
    blank lines keep the enclosing identity; trailing span gaps do not.
    """
    if line < 1 or not file_text:
        return None
    lines = file_text.splitlines()
    # Same invariant as ``_containing_definition_spans``: non-empty text ⇒ lines.
    if not lines:  # pragma: no cover
        return None
    containing: list[tuple[str, int, int]] = []
    for name, start, end, indent in _iter_definition_spans(file_text, path=path):
        if _line_belongs_to_definition_span(lines, line, start, end, indent):
            containing.append((name, start, indent))
    if not containing:
        return None
    containing.sort(key=lambda item: (-item[2], -item[1]))
    name, start, _indent = containing[0]
    return (name, start)


def _definition_span_is_class(file_text: str, start_line: int) -> bool:
    """Return True when the definition head at ``start_line`` is a class."""
    return re.match(r"^[ \t]*class[ \t]+\w+\b", file_text.splitlines()[start_line - 1]) is not None


def _definition_is_nested_in_other(
    all_spans: list[tuple[str, int, int, int]],
    *,
    start: int,
    indent: int,
) -> bool:
    """True when ``start`` lies in the body of a shallower def/class/arrow."""
    return any(s < start <= e and i < indent for _n, s, e, i in all_spans)


def _definition_binding_scope(
    file_text: str,
    all_spans: list[tuple[str, int, int, int]],
    *,
    start: int,
    indent: int,
) -> tuple[int, int]:
    """``(scope_start, body_indent)`` of the scope whose body binds ``start``.

    ``scope_start`` is the innermost definition head enclosing ``start`` — the
    class whose attribute a method becomes — and ``0`` for a module-scope head.
    ``body_indent`` is the indent that scope's own statements sit at (``0`` at
    module scope), so a head deeper than it is guarded by some intervening
    block rather than run unconditionally with the scope's body. Taking the
    minimum over the body keeps that reading conservative: an unusual
    continuation line only ever makes a head look guarded, never unguarded.
    """
    enclosing = [
        (span_indent, span_start, span_end)
        for _name, span_start, span_end, span_indent in all_spans
        if span_start < start <= span_end and span_indent < indent
    ]
    if not enclosing:
        return (0, 0)
    _scope_indent, scope_start, scope_end = max(enclosing)
    body = [
        _leading_indent(line)
        for line in file_text.splitlines()[scope_start:scope_end]
        if line.strip() and not line.lstrip().startswith(("#", "//"))
    ]
    return (scope_start, min(body, default=indent))


def _definition_head_is_assignment(file_text: str, start_line: int) -> bool:
    """True when the definition head is an assignment (``const``/``let``/``var``/bare).

    Indented assignment bindings under control-flow blocks are block-scoped in
    JS/TS and must not be treated as module candidates. ``def``/``function``/
    ``class`` heads return False so Python helpers under ``if`` stay callable.
    """
    lines = file_text.splitlines()
    if start_line < 1 or start_line > len(lines):
        return False
    raw = lines[start_line - 1]
    # Match only the assignment alternative of ``_ENCLOSING_DEFINITION_RE``.
    return re.match(r"^[ \t]*" + _ASSIGNMENT_DEFINITION_HEAD, raw) is not None


def _decorator_basenames_above(file_text: str, def_start_line: int) -> frozenset[str]:
    """Return decorator basenames immediately above ``def_start_line``.

    Walks the decorator stack above the def (blank/comment gaps and multiline
    decorator call tails allowed). Only the final dotted segment is kept
    (``foo.staticmethod`` → ``staticmethod``). Stops at the previous enclosing
    definition head so sibling methods' decorators are not stolen.
    """
    lines = file_text.splitlines()
    if def_start_line < 2 or def_start_line > len(lines):
        return frozenset()
    names: list[str] = []
    idx = def_start_line - 2
    while idx >= 0:
        raw = lines[idx]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            idx -= 1
            continue
        match = _DECORATOR_BASENAME_RE.match(raw)
        if match is not None:
            names.append(match.group(1).rsplit(".", 1)[-1])
            idx -= 1
            continue
        # Multiline decorator call tails (``)``, kwargs) are not binding
        # boundaries — keep walking. Stop at a prior def/class/arrow head.
        if _ENCLOSING_DEFINITION_RE.match(raw) is not None:
            break
        idx -= 1
    return frozenset(names)


def _definition_head_has_js_dynamic_this(
    file_text: str, start_line: int, *, path: str | None = None
) -> bool:
    """True when the definition head at ``start_line`` establishes dynamic ``this``.

    JS ``function`` declarations/expressions bind ``this`` at call time. Arrow
    assignments lexically inherit the enclosing ``this`` and return False.

    Classification uses the same comment/string-masked scan lines as definition
    discovery so ``/* c */ function nested()`` still counts as dynamic ``this``.
    """
    lines = file_text.splitlines()
    if start_line < 1 or start_line > len(lines):
        return False
    scan = _definition_head_scan_lines(file_text, path=path)[start_line - 1]
    if re.match(
        rf"^[ \t]*(?:export[ \t]+)?(?:async[ \t]+)?function[ \t]+{_JS_IDENT}\s*\(",
        scan,
    ):
        return True
    return (
        re.match(
            rf"^[ \t]*(?:(?:const|let|var)[ \t]+)?{_JS_IDENT}[ \t]*="
            rf"[ \t]*(?:async[ \t]+)?function\b",
            scan,
        )
        is not None
    )


def _js_nested_dynamic_this_between(
    file_text: str,
    *,
    method_start: int,
    line: int,
    path: str | None = None,
) -> bool:
    """True when a dynamic-``this`` nested function lies between method and ``line``."""
    for start, _end, _indent in _containing_definition_spans(file_text, line, path=path):
        if start <= method_start:
            continue
        if _definition_span_is_class(file_text, start):
            continue
        if _definition_head_has_js_dynamic_this(file_text, start, path=path):
            return True
    return False


def _enclosing_class_method_def_start(
    file_text: str, line: int, *, path: str | None = None
) -> int | None:
    """Return the start line of the class body method that contains ``line``.

    Nested functions inside a method still map to that method's start for
    structural lookup (Python closes over ``self``/``cls``). JS/TS dynamic-
    ``this`` nested ``function`` bodies are rejected later by receiver binding —
    only arrows inherit enclosing ``this``. Nested classes' methods are not
    attributed to the outer class method — the inner class's own method wins
    via enclosing-class walk.
    """
    class_span = _enclosing_class_span(file_text, line, path=path)
    if class_span is None:
        return None
    class_start, class_end = class_span
    class_indent = _leading_indent(file_text.splitlines()[class_start - 1])
    all_spans = _iter_definition_spans(file_text, path=path)
    method_starts: list[int] = []
    for _n, start, end, indent in all_spans:
        if not (class_start < start <= class_end and start <= line <= end):
            continue
        # Lexical class ends already exclude equal-or-lower indent siblings; keep
        # the guard for malformed trees that still place a shallow span inside.
        if indent <= class_indent:  # pragma: no cover
            continue
        if _definition_span_is_class(file_text, start):  # pragma: no cover
            # Unreachable while ``_enclosing_class_span`` returns the innermost
            # class: any nested class containing ``line`` would be enclosing.
            continue
        # Direct class body member only — skip locals nested under another def.
        if any(class_indent < i < indent and s < start <= e for _n2, s, e, i in all_spans):
            continue
        method_starts.append(start)
    if not method_starts:
        return None
    # At most one direct class-body method can contain ``line``.
    return max(method_starts)


def _class_method_receiver_binding(
    file_text: str, line: int, *, path: str | None = None
) -> str | None:
    """Return ``\"instance\"`` / ``\"class\"`` when ``line`` is under a bound method.

    ``@staticmethod`` establishes no ``self``/``cls`` receiver binding. Undecorated
    class body methods are instance-bound; ``@classmethod`` is class-bound.
    Python nested defs inherit the enclosing class method's binding. On JS/TS
    paths, nested ``function`` bodies have dynamic ``this`` and fail closed;
    nested arrows keep the enclosing instance binding. Returns None when there
    is no enclosing class method or binding is absent (fail closed).
    """
    def_start = _enclosing_class_method_def_start(file_text, line, path=path)
    if def_start is None:
        return None
    if _path_allows_js_private_fields(path) and _js_nested_dynamic_this_between(
        file_text, method_start=def_start, line=line, path=path
    ):
        return None
    decorators = _decorator_basenames_above(file_text, def_start)
    if "staticmethod" in decorators:
        return None
    if "classmethod" in decorators:
        return "class"
    return "instance"


def _resolve_callee_definition_span(
    file_text: str,
    *,
    call_line: int,
    qualifier: str | None,
    name: str,
    path: str | None = None,
) -> tuple[int, int] | None:
    """Return the in-scope ``(start, end)`` span for a callee at ``call_line``."""
    if call_line < 1 or not file_text or not name:
        return None
    all_spans = _iter_definition_spans(file_text, path=path)
    spans = [span for span in all_spans if span[0] == name]
    if not spans:
        return None
    if qualifier is not None:
        js_this = qualifier in _JS_ATTR_CALLEE_QUALIFIERS and _path_allows_js_private_fields(path)
        if qualifier not in _ATTR_CALLEE_QUALIFIERS and not js_this:
            # Receivers other than ``self``/``cls`` (and JS/TS ``this``) are
            # ambiguous without import/type resolution (e.g. ``client.send()``);
            # fail closed rather than treating them as bare names and linking
            # an unrelated ``def``.
            return None
        # ``self``/``cls``/``this`` are receivers only when the enclosing class
        # method establishes instance/class binding. ``@staticmethod def
        # reviewed(self)`` makes ``self`` an ordinary argument — fail closed
        # rather than linking the class's ``helper`` when ``Foo.reviewed(other)``
        # calls ``other.helper``.
        binding = _class_method_receiver_binding(file_text, call_line, path=path)
        if binding is None:
            return None
        if qualifier in {"self", "this"} and binding != "instance":
            return None
        if qualifier == "cls" and binding != "class":
            return None
        class_span = _enclosing_class_span(file_text, call_line, path=path)
        # Binding non-None already required an enclosing class method, so a
        # missing class span here is inconsistent / defensive only.
        if class_span is None:  # pragma: no cover
            return None
        class_start, class_end = class_span
        class_indent = _leading_indent(file_text.splitlines()[class_start - 1])
        # Same-class methods resolve by lexical class scope; declaration order
        # does not affect ``self``/``cls`` lookup, so do not require start < call_line.
        # Only methods owned directly by this class — not nested-class methods or
        # locals of nested defs that happen to lie inside the outer class span.
        in_class: list[tuple[int, int]] = []
        for _n, start, end, indent in spans:
            if not (class_start < start <= class_end):
                continue
            if any(class_indent < i < indent and s < start <= e for _n2, s, e, i in all_spans):
                continue
            in_class.append((start, end))
        if not in_class:
            return None
        return max(in_class, key=lambda item: item[0])
    # Bare calls follow LEGB-ish scope: nested locals in the innermost enclosing
    # *function* that defines the name before the call, then enclosing functions,
    # then module-scope defs (indent 0 or under non-def blocks like ``if``),
    # including forward references. Class bodies are not LEGB scopes for bare
    # names in method *bodies*. Python method default expressions on a direct
    # class member's ``def`` line are evaluated in the class namespace while the
    # class body runs, so those call sites must resolve preceding class-local
    # bindings instead of skipping to an unrelated module helper.
    # Indented JS/TS assignment bindings under control-flow are block-scoped —
    # fail closed rather than treating them as module candidates.
    for parent_start, parent_end, parent_indent in _containing_definition_spans(
        file_text, call_line, path=path
    ):
        if _definition_span_is_class(file_text, parent_start):
            # JS/TS defaults are not evaluated in the class body namespace.
            if _path_allows_js_private_fields(path):
                continue
            on_direct_member_head = any(
                start == call_line
                and parent_start < start <= parent_end
                and not any(
                    parent_indent < i < indent and s < start <= e for _n2, s, e, i in all_spans
                )
                for _n, start, end, indent in all_spans
            )
            if not on_direct_member_head:
                continue
            class_local: list[tuple[int, int]] = []
            for _n, start, end, indent in spans:
                if not (parent_start < start <= parent_end and start < call_line):
                    continue
                if any(parent_indent < i < indent and s < start <= e for _n2, s, e, i in all_spans):
                    continue
                class_local.append((start, end))
            if class_local:
                return max(class_local, key=lambda item: item[0])
            continue
        local: list[tuple[int, int]] = []
        for _n, start, end, indent in spans:
            if not (
                parent_start < start <= parent_end and indent > parent_indent and start < call_line
            ):
                continue
            # Only names bound directly in this function — not locals of a
            # sibling nested def between ``parent`` and the candidate.
            if any(parent_indent < i < indent and s < start <= e for _n2, s, e, i in all_spans):
                continue
            local.append((start, end))
        if local:
            return max(local, key=lambda item: item[0])
    # JS/TS indented heads under control-flow (including ``function``) are
    # block-scoped — exclude all non-nested indented candidates on those paths.
    # Python keeps indented ``def`` under ``if`` as a module binding.
    js_ts = _path_allows_js_private_fields(path)
    module_scope = [
        (start, end)
        for _n, start, end, indent in spans
        if not _definition_is_nested_in_other(all_spans, start=start, indent=indent)
        and not (indent > 0 and (_definition_head_is_assignment(file_text, start) or js_ts))
    ]
    if not module_scope:
        return None
    # Python's module binding (and JS function declarations) is the last
    # executed ``def`` / assignment of the name. Prefer that final binding so a
    # change confined to an earlier dead same-named body cannot count as FIXED
    # evidence — including when candidates both precede and follow the call.
    return max(module_scope, key=lambda item: item[0])


# Bare name preceded by ``.`` / ``?.`` after a non-ident receiver
# (``factory().helper()``, ``items[0].helper()``, ``super().helper()``).
_BARE_CALLEE_AFTER_ATTR_DOT_RE = re.compile(r"\?\.\s*$|\.\s*$")


def _bare_callee_follows_attribute_dot(scan_text: str, match_start: int) -> bool:
    """True when a bare callee name is preceded by ``.`` / ``?.`` (fail closed)."""
    if match_start <= 0:
        return False
    return _BARE_CALLEE_AFTER_ATTR_DOT_RE.search(scan_text[:match_start]) is not None


def _callee_ref_matches_from_anchor_line(
    anchor_line: str, *, path: str | None = None
) -> tuple[str, list[re.Match[str]]]:
    """``(masked_scan_text, matches)`` for the call refs on a review-anchor line.

    Shared by the ref reader and the receiver-chain reader so both see exactly
    the same calls, and the same masked text their positions index into.
    """
    if not anchor_line:
        return "", []
    scan_from = 0
    # A leading def/class/function signature's name is not a callee. Scan from
    # the signature's opening ``(`` so default-expression calls before the
    # definition colon (e.g. ``def reviewed(value=helper()):``) count as
    # evidence, along with one-liner bodies after the colon. Lines without a
    # colon still fail closed (JS/TS brace heads).
    if _ENCLOSING_DEFINITION_RE.match(anchor_line) is not None:
        colon = anchor_line.find(":")
        if colon < 0:
            return "", []
        open_paren = anchor_line.find("(")
        scan_from = open_paren if 0 <= open_paren < colon else colon + 1
    scan_text = _mask_comments_and_string_literals_for_callee_scan(anchor_line, path=path)
    matches: list[re.Match[str]] = []
    for match in _CALLEE_REF_RE.finditer(scan_text, scan_from):
        qualifier, name = match.group(1), match.group(2)
        if name.lower() in _CALLEE_KEYWORD_BLOCKLIST:
            continue
        # ``factory().helper()`` / ``items[0].helper()`` / ``super().helper()``
        # only capture simple-ident qualifiers, so the method would otherwise
        # emit as bare ``helper`` and link an unrelated module ``def helper``.
        if qualifier is None and _bare_callee_follows_attribute_dot(scan_text, match.start()):
            continue
        matches.append(match)
    return scan_text, matches


def _callee_refs_from_anchor_line(
    anchor_line: str, *, path: str | None = None
) -> frozenset[tuple[str | None, str]]:
    """Extract ``(qualifier|None, name)`` call refs from a review-anchor source line.

    Keyword-like receivers are kept (e.g. ``match.helper()``). Erasing them to
    a bare name would let an unrelated module-level ``helper`` satisfy FIXED
    evidence; non-self/cls/this qualifiers already fail closed at resolve
    (``this`` only resolves on JS/TS paths).
    """
    _scan_text, matches = _callee_ref_matches_from_anchor_line(anchor_line, path=path)
    return frozenset((match.group(1), match.group(2)) for match in matches)


def _scan_back_while(probe: re.Pattern[str], text: str, end: int) -> int:
    """Leftmost index ``i <= end`` where every character of ``text[i:end]`` matches."""
    while end > 0 and probe.match(text, end - 1):
        end -= 1
    return end


def _trailing_receiver_chain(text: str) -> tuple[str, bool] | None:
    """The dotted receiver chain ``text`` ends with, and whether a ``.`` follows it.

    In ``root.mid.receiver.`` the chain is ``root.mid.receiver`` and the flag is
    True; in ``root.mid.receiver`` the flag is False. Only runs of plain
    identifiers are read, so ``factory().receiver.`` yields just ``receiver``
    and ``factory().`` yields None.

    The scan is end-anchored and walks backwards, so it costs one pass over the
    chain. An equivalent forward ``re.search`` re-scans the whole chain from
    every identifier in it whenever the tail cannot match (``a.a.…a()``), which
    is quadratic in a source line whose length is not bounded here
    (PRRT_kwDOSJAM6s6rAhm4).
    """
    end = _scan_back_while(_SPACE_CHAR_RE, text, len(text))
    ends_with_dot = end > 0 and text[end - 1] == "."
    if ends_with_dot:
        end -= 1
        if end > 0 and text[end - 1] == "?":
            end -= 1
        end = _scan_back_while(_SPACE_CHAR_RE, text, end)
    chain_end = end
    chain_start: int | None = None
    while True:
        link_start = _scan_back_while(_IDENT_CHAR_RE, text, end)
        if _JS_IDENT_RE.fullmatch(text, link_start, end) is None:
            # Not an identifier (empty, or digit-led like ``1x``): the chain
            # starts to its right, exactly where a leftmost search would start.
            break
        chain_start = link_start
        # ``\s*\??\.\s*`` joining this link to the one before it.
        cursor = _scan_back_while(_SPACE_CHAR_RE, text, link_start)
        if cursor == 0 or text[cursor - 1] != ".":
            break
        cursor -= 1
        if cursor > 0 and text[cursor - 1] == "?":
            cursor -= 1
        end = _scan_back_while(_SPACE_CHAR_RE, text, cursor)
    if chain_start is None:
        return None
    return text[chain_start:chain_end], ends_with_dot


def _receiver_chain_segments(prefix_text: str) -> tuple[str, ...] | None:
    """The dotted receiver chain ``prefix_text`` ends with, or None.

    ``prefix_text`` is the text before a chained callee's receiver, so in
    ``root.mid.receiver.name()`` the segments are ``("root", "mid")`` — the part
    of the receiver the callee's immediate qualifier hides. A chain is only read
    when the prefix ends on a ``.``/``?.`` link, so ``factory().receiver.name()``
    leaves it unreadable. ``?`` cannot appear in an identifier, so dropping it
    leaves the optional chaining operator as a plain ``.`` separator.
    """
    chain = _trailing_receiver_chain(prefix_text)
    if chain is None or not chain[1]:
        return None
    return tuple(segment.strip() for segment in chain[0].replace("?", "").split("."))


def _receiver_chain_segments_from_anchor_line(
    anchor_line: str, *, path: str | None = None
) -> dict[str, tuple[str, ...] | None]:
    """Receiver chains of the *chained* callees on a review-anchor source line.

    ``_callee_refs_from_anchor_line`` keeps only a callee's immediate
    qualifier, so ``api.metrics.record()`` reports the receiver as ``metrics``
    — a name no import binds, which would leave the callee on the name-only
    rule and let any reachable ``record`` stand in as evidence
    (PRRT_kwDOSJAM6s6q-6LK). Each chained receiver is mapped to the full dotted
    chain it is reached through, ``("api", "metrics")`` here, whose first
    segment is the name an import can bind and whose whole run may itself spell
    an imported module. The chain is ``None`` when it is not a run of plain
    identifiers (``factory().metrics.record()``), when the line gives one
    receiver two different chains, or when the same receiver also appears
    unchained — readings the caller holds closed instead of guessing.
    """
    scan_text, matches = _callee_ref_matches_from_anchor_line(anchor_line, path=path)
    chained: dict[str, tuple[str, ...] | None] = {}
    unchained: set[str] = set()
    for match in matches:
        qualifier = match.group(1)
        if qualifier is None:
            continue
        if not _bare_callee_follows_attribute_dot(scan_text, match.start(1)):
            unchained.add(qualifier)
            continue
        prefix = _receiver_chain_segments(scan_text[: match.start(1)])
        chain = None if prefix is None else (*prefix, qualifier)
        if chained.get(qualifier, chain) != chain:
            chain = None
        chained[qualifier] = chain
    return {
        qualifier: (None if qualifier in unchained else chain)
        for qualifier, chain in chained.items()
    }


# Leading ``.`` or ``?.`` after a receiver split onto the prior line.
_LEADING_DOT_RE = re.compile(r"^([ \t]*)\??\.")
_LEADING_BARE_CALL_RE = re.compile(rf"^([ \t]*)({_JS_IDENT})\s*(?:\?\.)?\s*\(")


def _prior_nonblank_masked_line(masked_lines: list[str], before_index: int) -> str | None:
    """Nearest prior masked line with non-whitespace content (skip blanked comments)."""
    idx = before_index - 1
    while idx >= 0:
        if masked_lines[idx].strip():
            return masked_lines[idx]
        idx -= 1
    return None


def _anchor_line_with_split_receiver(masked_lines: list[str], line_index: int) -> str:
    """Reattach a receiver split onto a prior line before callee parsing.

    Formatters may place ``self`` / ``cls`` / ``client`` on one line and
    ``.helper()`` / ``?.helper()`` (or ``self.`` / ``client?.`` + ``helper()``)
    on the next. Parsing only the anchored line yields an unqualified name and
    can link an unrelated module ``def`` as FIXED evidence.
    """
    # Callers pass ``line - 1`` after validating ``1 <= line <= len(lines)``.
    if line_index < 0 or line_index >= len(masked_lines):  # pragma: no cover
        return ""
    line = masked_lines[line_index]
    # Whether the anchor line continues a receiver is the cheap, anchored read;
    # take it before scanning the prior line for a chain to reattach.
    leading_dot = _LEADING_DOT_RE.match(line)
    bare = None if leading_dot is not None else _LEADING_BARE_CALL_RE.match(line)
    if leading_dot is None and bare is None:
        return line
    prior = _prior_nonblank_masked_line(masked_lines, line_index)
    if prior is None:
        return line
    # The whole dotted chain is reattached, not just its last link, so a
    # receiver chain a formatter split across lines still reaches the reader
    # that resolves it to the name an import binds (PRRT_kwDOSJAM6s6q-6LK).
    trailing = _trailing_receiver_chain(prior)
    if trailing is None:
        return line
    receiver, prior_has_dot = trailing
    if leading_dot is not None:
        # ``self`` / ``self.`` / ``client`` above, ``.helper()`` / ``?.helper()`` below.
        return f"{leading_dot.group(1)}{receiver}.{line[leading_dot.end() :]}"
    if prior_has_dot and bare is not None:
        # ``self.`` / ``client?.`` above, ``helper()`` on the anchor line.
        return f"{bare.group(1)}{receiver}.{line[bare.start(2) :]}"
    return line


def _masked_anchor_line(file_text: str, line: int, *, path: str | None = None) -> str | None:
    """``line``'s masked text with a split receiver reattached, or None.

    Masking only the isolated review line loses open multiline string/docstring
    state from earlier lines, so call-shaped decoy text can become FIXED
    call-site→definition evidence. Mask the file prefix through ``line`` first.
    Also reattach receivers split across lines (``self`` / ``.helper()``) so
    attribute calls are not misread as bare names.
    """
    if line < 1 or not file_text:
        return None
    lines = file_text.splitlines()
    if line > len(lines):
        return None
    prefix = "\n".join(lines[:line])
    masked_prefix = _mask_comments_and_string_literals_for_callee_scan(prefix, path=path)
    masked_lines = masked_prefix.splitlines()
    # Masking preserves newlines, so line counts stay aligned with ``lines``.
    if line > len(masked_lines):  # pragma: no cover
        return None
    return _anchor_line_with_split_receiver(masked_lines, line - 1)


def _callee_refs_from_file_line(
    file_text: str, line: int, *, path: str | None = None
) -> frozenset[tuple[str | None, str]]:
    """Extract callee refs from ``line`` using preceding file lexical context."""
    anchor = _masked_anchor_line(file_text, line, path=path)
    if anchor is None:
        return frozenset()
    return _callee_refs_from_anchor_line(anchor, path=path)


def _receiver_chain_segments_from_file_line(
    file_text: str, line: int, *, path: str | None = None
) -> dict[str, tuple[str, ...] | None]:
    """Receiver chains of ``line``'s chained callees, read with file context.

    The companion of :func:`_callee_refs_from_file_line`: same anchor text, but
    reporting which of its qualified callees reach their receiver through a
    dotted chain, and what that chain spells (PRRT_kwDOSJAM6s6q-6LK).
    """
    anchor = _masked_anchor_line(file_text, line, path=path)
    if anchor is None:
        return {}
    return _receiver_chain_segments_from_anchor_line(anchor, path=path)


def _callee_names_from_anchor_line(anchor_line: str, *, path: str | None = None) -> frozenset[str]:
    """Extract call-like identifiers from a review-anchor source line."""
    return frozenset(
        name for _qualifier, name in _callee_refs_from_anchor_line(anchor_line, path=path)
    )


def _diff_text_changes_definition_names(diff_text: str, names: frozenset[str]) -> bool:
    """Return True when a +/- line declares a definition for one of ``names``."""
    if not names:
        return False
    for raw_line in diff_text.splitlines():
        match = _DEFINITION_NAME_LINE_RE.match(raw_line)
        if match is None:
            continue
        defined = next((group for group in match.groups() if group), None)
        if defined is not None and defined in names:
            return True
    return False


def _enclosing_definition_name(file_text: str, line: int, *, path: str | None = None) -> str | None:
    """Return the nearest def/function/class name at or above ``line``."""
    identity = _enclosing_definition_identity(file_text, line, path=path)
    return None if identity is None else identity[0]


# A comprehension is its own scope: its generator targets bind only inside it,
# while a ``:=`` in its body assigns in the scope that holds it.
_COMPREHENSION_SCOPES: Final = (ast.DictComp, ast.GeneratorExp, ast.ListComp, ast.SetComp)
# Scopes an anchored line can sit in. A class body is absent on purpose: its
# bindings are attributes, invisible to the calls inside its methods.
_FUNCTION_SCOPES: Final = (ast.AsyncFunctionDef, ast.FunctionDef, ast.Lambda)
_ANCHORED_SCOPES: Final = _FUNCTION_SCOPES + _COMPREHENSION_SCOPES


def _names_bound_in_scope(scope: ast.AST) -> Iterator[str]:
    """Names the scope ``scope`` binds inside its own body.

    Python locals are function-wide — a name assigned anywhere in a function is
    local throughout it — so the whole body is read rather than only the lines
    above the anchor. Parameters, assignment / loop / ``with`` targets, caught
    exceptions and nested definitions all count, and so do ``match`` / ``case``
    capture, star and mapping-rest targets: those carry their name on the
    pattern node rather than storing an ``ast.Name``, so a reader that watched
    only ``Store`` names would leave a ``case record:`` still holding its import
    (PRRT_kwDOSJAM6s6q-N1B). A wildcard ``_`` binds nothing and is skipped, and
    neither is an ``import`` statement in the body — it is a binding the import
    readers hold themselves, see ``_locally_rebound_names_at_line``
    (PRRT_kwDOSJAM6s6rAhm2).

    Child scopes are *not* descended into. A nested function's or lambda's
    parameters and locals, a class body's attributes and a comprehension's
    targets are bound in those scopes, not here, so counting them as locals
    would mark an import shadowed that the anchored line still holds and reject
    a correction that does change the callee it reaches (PRRT_kwDOSJAM6s6q_M0s).
    What a child binds in *this* scope is still reported: a nested ``def`` /
    ``class`` name, and every ``:=`` target in a child, because a comprehension
    assigns its walruses in the scope holding it and a definition's decorators
    and parameter defaults are evaluated where the definition appears. A walrus
    buried in a child's *body* binds only there, but telling the two apart costs
    more than the name is worth, so it keeps failing closed. ``scope``'s own
    name is not reported — a definition's name is bound in the scope that
    *holds* it, so a method named like an imported helper does not shadow that
    import for the calls in its own body. Neither is a ``nonlocal`` rebinding:
    it is legal only where an enclosing function already binds the name, which
    that scope's own pass reports. Callers anchor every scope enclosing the
    line, so an enclosing function's locals still reach a call nested inside it.
    """
    pending: list[ast.AST] = list(ast.iter_child_nodes(scope))
    if isinstance(scope, _COMPREHENSION_SCOPES):
        pending.extend(generator.target for generator in scope.generators)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef, ast.Lambda)):
            if not isinstance(node, ast.Lambda):
                yield node.name
            pending.extend(
                found.target for found in ast.walk(node) if isinstance(found, ast.NamedExpr)
            )
            continue
        if isinstance(node, ast.comprehension):
            pending.extend((node.iter, *node.ifs))
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            yield node.id
        elif isinstance(node, ast.arg):
            yield node.arg
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            yield node.name
        elif isinstance(node, ast.MatchMapping) and node.rest:
            yield node.rest
        pending.extend(ast.iter_child_nodes(node))


def _anchored_class_scope(tree: ast.AST, line: int) -> ast.ClassDef | None:
    """The class body ``line`` executes directly in, if any.

    Such a line resolves through the class namespace, so the class body's own
    bindings shadow an import for it (PRRT_kwDOSJAM6s6q_ywe). The innermost
    scope wins, which is what keeps class attributes out of a method-body
    lookup: a method, lambda or comprehension holds the lines inside *it*, a
    class body never sees the one around it, and a tie resolves to the class.
    """
    holding = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (*_ANCHORED_SCOPES, ast.ClassDef))
        and node.lineno <= line <= (node.end_lineno or node.lineno)
    ]
    innermost = max(
        holding, key=lambda node: (node.lineno, -(node.end_lineno or node.lineno)), default=None
    )
    return innermost if isinstance(innermost, ast.ClassDef) else None
