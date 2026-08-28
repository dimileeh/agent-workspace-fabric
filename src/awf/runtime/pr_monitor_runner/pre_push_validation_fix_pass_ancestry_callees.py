"""Callee/definition-span helpers for pre-push FIXED evidence linking."""

from __future__ import annotations

import re

_CALLEE_REF_RE = re.compile(
    r"(?:(\b[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*)?(\b[A-Za-z_][A-Za-z0-9_]*)\s*\("
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
_ASSIGNMENT_DEFINITION_HEAD = (
    r"(?:(?:const|let|var)[ \t]+)?(\w+)[ \t]*="
    r"[ \t]*(?:async[ \t]+)?(?:(?:\([^)]*\)|\w+)[ \t]*=>|function\b|lambda\b)"
)
_DEFINITION_NAME_LINE_RE = re.compile(
    r"^[-+](?!\+\+|--)[ \t]*(?:"
    r"(?:async[ \t]+)?def[ \t]+(\w+)\s*\("
    # Optional ``export`` so ``export function helper`` / ``export async function``
    # count as definition heads (body-only repairs stay FIXED-with-evidence).
    r"|(?:export[ \t]+)?(?:async[ \t]+)?function[ \t]+(\w+)\s*\("
    r"|class[ \t]+(\w+)\b"
    r"|" + _ASSIGNMENT_DEFINITION_HEAD + r")"
)
_ENCLOSING_DEFINITION_RE = re.compile(
    r"^[ \t]*(?:async[ \t]+)?def[ \t]+(\w+)\s*\("
    r"|^[ \t]*(?:export[ \t]+)?(?:async[ \t]+)?function[ \t]+(\w+)\s*\("
    r"|^[ \t]*class[ \t]+(\w+)\b"
    r"|^[ \t]*" + _ASSIGNMENT_DEFINITION_HEAD
)
_ATTR_CALLEE_QUALIFIERS = frozenset({"self", "cls"})
# JS/TS private fields (`#ident`) are code; elsewhere `#` begins a comment.
# Unknown/missing path fails closed (treat `#` as comment) to avoid Python
# no-space comments like `#TODO helper()` becoming FIXED callee evidence.
_JS_TS_PRIVATE_FIELD_SUFFIXES = frozenset(
    {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
)
# Tokens after which a ``/`` may open a JS/TS regex literal (not division).
_JS_REGEX_PREFIX_KEYWORDS = frozenset(
    {
        "return",
        "throw",
        "case",
        "typeof",
        "void",
        "delete",
        "await",
        "yield",
        "in",
        "of",
        "instanceof",
        "new",
        "else",
        "do",
    }
)


def _path_allows_js_private_fields(path: str | None) -> bool:
    """Return True only when ``path`` is clearly a JS/TS source file."""
    if not path:
        return False
    name = path.lower().replace("\\", "/").rsplit("/", 1)[-1]
    if name.endswith((".d.ts", ".d.mts", ".d.cts")):
        return True
    return any(name.endswith(suffix) for suffix in _JS_TS_PRIVATE_FIELD_SUFFIXES)


def _js_slash_can_start_regex(line: str, slash_index: int) -> bool:
    """True when ``line[slash_index]`` may open a JS/TS regex literal."""
    n = len(line)
    if slash_index + 1 < n and line[slash_index + 1] in "=*/":
        # ``/=`` assign, ``/*`` block comment, ``//`` line comment — not a regex.
        return False
    j = slash_index - 1
    while j >= 0 and line[j] in " \t":
        j -= 1
    if j < 0 or line[j] in "\r\n":
        return True
    prev = line[j]
    if prev in "([{;=,:!&|?~^%*+-<>":
        return True
    if prev == ">" and j >= 1 and line[j - 1] == "=":
        return True
    if not (prev.isalnum() or prev in "_$"):
        return False
    k = j
    while k >= 0 and (line[k].isalnum() or line[k] in "_$"):
        k -= 1
    return line[k + 1 : j + 1] in _JS_REGEX_PREFIX_KEYWORDS


def _append_masked_js_regex_at_for_callee_scan(
    line: str, slash_index: int, out: list[str], *, n: int
) -> int:
    """Blank a JS/TS regex literal starting at ``slash_index``; return index after."""
    out.append(" ")
    i = slash_index + 1
    in_class = False
    while i < n:
        cur = line[i]
        if cur in "\r\n":
            break
        if cur == "\\" and i + 1 < n:
            out.extend((" ", " "))
            i += 2
            continue
        if cur == "[" and not in_class:
            in_class = True
            out.append(" ")
            i += 1
            continue
        if cur == "]" and in_class:
            in_class = False
            out.append(" ")
            i += 1
            continue
        if cur == "/" and not in_class:
            out.append(" ")
            i += 1
            while i < n and line[i].isalpha():
                out.append(" ")
                i += 1
            return i
        out.append(" ")
        i += 1
    return i


def _leading_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


_BLOCK_CLOSER_RE = re.compile(r"^[ \t]*[\])}]+[ \t]*[;,]?[ \t]*$")


def _is_ignorable_span_gap_line(line: str) -> bool:
    """Blank or full-line comment gaps do not end a definition span."""
    stripped = line.strip()
    if not stripped:
        return True
    return stripped.startswith("#") or stripped.startswith("//")


def _is_block_closer_line(line: str) -> bool:
    """JS/TS brace/bracket closers stay inside the opening definition span."""
    return _BLOCK_CLOSER_RE.match(line) is not None


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


def _enclosing_definition_identity(file_text: str, line: int) -> tuple[str, int] | None:
    """Return ``(name, start_line)`` for the nearest def/class/arrow at or above ``line``."""
    if line < 1 or not file_text:
        return None
    lines = file_text.splitlines()
    # Non-empty ``file_text`` always yields at least one splitlines entry
    # (even a lone ``\\n``), so this arm is unreachable after the guard above.
    if not lines:  # pragma: no cover
        return None
    idx = min(line, len(lines)) - 1
    while idx >= 0:
        match = _ENCLOSING_DEFINITION_RE.match(lines[idx])
        if match is not None:
            # Every ``_ENCLOSING_DEFINITION_RE`` alternative captures a name.
            name = next((group for group in match.groups() if group), None)
            if name is None:  # pragma: no cover
                idx -= 1
                continue
            return (name, idx + 1)
        idx -= 1
    return None


def _iter_definition_spans(file_text: str) -> list[tuple[str, int, int, int]]:
    """Return ``(name, start_line, end_line, indent)`` for each def/class/function/arrow."""
    lines = file_text.splitlines()
    starts: list[tuple[str, int, int]] = []
    for idx, raw in enumerate(lines):
        match = _ENCLOSING_DEFINITION_RE.match(raw)
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


def _enclosing_class_span(file_text: str, line: int) -> tuple[int, int] | None:
    """Return the ``(start, end)`` span of the nearest enclosing class for ``line``.

    Only return a class when ``line`` lies within its lexical span. A preceding
    class that ends before ``line`` (e.g. module-level code after the class, or
    an ordinary same-indent statement after a function-local class) is not
    enclosing — keep walking for an outer class, or return None.
    """
    lines = file_text.splitlines()
    if line < 1 or not lines:
        return None
    idx = min(line, len(lines)) - 1
    while idx >= 0:
        raw = lines[idx]
        class_match = re.match(r"^[ \t]*class[ \t]+(\w+)\b", raw)
        if class_match is not None:
            class_indent = _leading_indent(raw)
            start = idx + 1
            # Any nonblank equal-or-lower indent ends the class (not only the
            # next def/class head) so ``return self.helper()`` after a local
            # class is outside that class span.
            end = _definition_span_end_line(lines, start, class_indent)
            if start <= line <= end:
                return (start, end)
        idx -= 1
    return None


def _containing_definition_spans(file_text: str, line: int) -> list[tuple[int, int, int]]:
    """Return ``(start, end, indent)`` spans containing ``line``, innermost first.

    A line belongs to a definition body only when it is the definition head itself or is
    indented deeper than that head. Same-indent siblings after a nested def (e.g.
    ``return helper()`` after ``def helper``) stay outside the nested span.
    """
    if line < 1 or not file_text:
        return []
    lines = file_text.splitlines()
    # Same invariant as ``_enclosing_definition_identity``: non-empty text ⇒ lines.
    if not lines:  # pragma: no cover
        return []
    line_indent = _leading_indent(lines[min(line, len(lines)) - 1])
    containing: list[tuple[int, int, int]] = []
    for _n, start, end, indent in _iter_definition_spans(file_text):
        if not (start <= line <= end):
            continue
        if line == start or line_indent > indent:
            containing.append((start, end, indent))
    containing.sort(key=lambda item: (-item[2], -item[0]))
    return containing


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


def _resolve_callee_definition_span(
    file_text: str,
    *,
    call_line: int,
    qualifier: str | None,
    name: str,
) -> tuple[int, int] | None:
    """Return the in-scope ``(start, end)`` span for a callee at ``call_line``."""
    if call_line < 1 or not file_text or not name:
        return None
    all_spans = _iter_definition_spans(file_text)
    spans = [span for span in all_spans if span[0] == name]
    if not spans:
        return None
    if qualifier is not None:
        if qualifier not in _ATTR_CALLEE_QUALIFIERS:
            # Receivers other than ``self``/``cls`` (e.g. ``client.send()``) are
            # ambiguous without import/type resolution; fail closed rather than
            # treating them as bare names and linking an unrelated ``def``.
            return None
        class_span = _enclosing_class_span(file_text, call_line)
        if class_span is None:
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
    # including forward references. Class bodies are not LEGB scopes for bare names.
    # Indented JS/TS assignment bindings under control-flow are block-scoped —
    # fail closed rather than treating them as module candidates.
    for parent_start, parent_end, parent_indent in _containing_definition_spans(
        file_text, call_line
    ):
        if _definition_span_is_class(file_text, parent_start):
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
    module_scope = [
        (start, end)
        for _n, start, end, indent in spans
        if not _definition_is_nested_in_other(all_spans, start=start, indent=indent)
        and not (indent > 0 and _definition_head_is_assignment(file_text, start))
    ]
    if not module_scope:
        return None
    preceding = [span for span in module_scope if span[0] < call_line]
    if preceding:
        return max(preceding, key=lambda item: item[0])
    return min(module_scope, key=lambda item: item[0])


def _python_string_prefix_is_f(line: str, quote_index: int) -> bool:
    """True when ``line[quote_index]`` opens a Python f-string (``f`` / ``rf`` / …)."""
    j = quote_index - 1
    while j >= 0 and line[j] in "rRuUfFbB":
        j -= 1
    prefix = line[j + 1 : quote_index]
    return bool(prefix) and ("f" in prefix or "F" in prefix)


def _append_comment_run_for_callee_scan(line: str, start: int, out: list[str], *, n: int) -> int:
    """Blank a ``#`` / ``//`` comment run from ``start``; return index after."""
    i = start
    while i < n and line[i] not in "\r\n":
        out.append(" ")
        i += 1
    return i


def _append_masked_quote_at_for_callee_scan(
    line: str,
    quote_index: int,
    out: list[str],
    *,
    n: int,
    allow_js_private_fields: bool,
) -> int:
    """Blank a string/template starting at ``quote_index``; return index after."""
    quote = line[quote_index]
    retain_fstring = quote in "'\"" and _python_string_prefix_is_f(line, quote_index)
    retain_template = quote == "`"
    out.append(" ")
    i = quote_index + 1
    if quote in "'\"" and i + 1 < n and line[i] == quote and line[i + 1] == quote:
        out.extend((" ", " "))
        return _mask_quoted_region_for_callee_scan(
            line,
            i + 2,
            out,
            n=n,
            quote=quote,
            triple=True,
            retain_fstring=retain_fstring,
            retain_template=False,
            allow_js_private_fields=allow_js_private_fields,
        )
    return _mask_quoted_region_for_callee_scan(
        line,
        i,
        out,
        n=n,
        quote=quote,
        triple=False,
        retain_fstring=retain_fstring,
        retain_template=retain_template,
        allow_js_private_fields=allow_js_private_fields,
    )


def _append_retained_brace_expr(
    line: str,
    start: int,
    out: list[str],
    *,
    n: int,
    allow_js_private_fields: bool,
) -> int:
    """Retain ``{...}`` with nested strings/comments masked; return index after."""
    depth = 0
    i = start
    while i < n:
        cur = line[i]
        if cur == "{":
            depth += 1
            out.append(cur)
            i += 1
            continue
        if cur == "}":
            depth -= 1
            out.append(cur)
            i += 1
            if depth == 0:
                break
            continue
        if cur in "'\"`":
            i = _append_masked_quote_at_for_callee_scan(
                line, i, out, n=n, allow_js_private_fields=allow_js_private_fields
            )
            continue
        if cur == "#":
            next_is_ident_start = i + 1 < n and (line[i + 1].isalpha() or line[i + 1] == "_")
            if allow_js_private_fields and next_is_ident_start:
                out.append(cur)
                i += 1
                continue
            out.append(" ")
            i = _append_comment_run_for_callee_scan(line, i + 1, out, n=n)
            continue
        if cur == "/" and i + 1 < n and line[i + 1] == "/":
            out.extend((" ", " "))
            i = _append_comment_run_for_callee_scan(line, i + 2, out, n=n)
            continue
        if allow_js_private_fields and cur == "/" and _js_slash_can_start_regex(line, i):
            i = _append_masked_js_regex_at_for_callee_scan(line, i, out, n=n)
            continue
        out.append(cur)
        i += 1
    return i


def _mask_quoted_region_for_callee_scan(
    line: str,
    start: int,
    out: list[str],
    *,
    n: int,
    quote: str,
    triple: bool,
    retain_fstring: bool,
    retain_template: bool,
    allow_js_private_fields: bool,
) -> int:
    """Blank literal text in a quoted region; retain f-string / ``${...}`` exprs."""
    i = start
    while i < n:
        cur = line[i]
        if triple and cur == quote and i + 2 < n and line[i + 1] == quote and line[i + 2] == quote:
            out.extend((" ", " ", " "))
            return i + 3
        if not triple and cur == "\\" and i + 1 < n:
            out.extend((" ", " "))
            i += 2
            continue
        if not triple and cur == quote:
            out.append(" ")
            return i + 1
        if retain_fstring and cur == "{":
            if i + 1 < n and line[i + 1] == "{":
                out.extend((" ", " "))
                i += 2
                continue
            i = _append_retained_brace_expr(
                line, i, out, n=n, allow_js_private_fields=allow_js_private_fields
            )
            continue
        if retain_template and cur == "$" and i + 1 < n and line[i + 1] == "{":
            out.append("$")
            i = _append_retained_brace_expr(
                line, i + 1, out, n=n, allow_js_private_fields=allow_js_private_fields
            )
            continue
        out.append(" " if cur not in "\r\n" else cur)
        i += 1
    return i


def _mask_comments_and_string_literals_for_callee_scan(
    line: str, *, path: str | None = None
) -> str:
    """Blank comments and string/template/regex literals so callee regex stays code-only.

    Call-shaped text inside ``#`` / ``//`` comments, quoted literal text, or JS/TS
    regex literals must not become FIXED callee evidence. Executable interpolations
    are retained: Python f-string ``{...}`` bodies and JS/TS template ``${...}``
    bodies stay scannable. Nested strings/comments/regexes inside those retained
    expressions are re-masked so inert literals such as ``f'{"helper()"}'`` or
    ``${/helper()/}`` do not become false callees.
    ``#ident`` is kept as code only for JS/TS paths (private fields). For Python
    and unknown paths, every ``#`` begins a comment (fail closed on ambiguity).
    JS/TS regex masking uses the same path gate.
    """
    if not line:
        return line
    allow_js_private_fields = _path_allows_js_private_fields(path)
    out: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch in "'\"`":
            i = _append_masked_quote_at_for_callee_scan(
                line, i, out, n=n, allow_js_private_fields=allow_js_private_fields
            )
            continue
        if ch == "#":
            next_is_ident_start = i + 1 < n and (line[i + 1].isalpha() or line[i + 1] == "_")
            if allow_js_private_fields and next_is_ident_start:
                out.append(ch)
                i += 1
                continue
            out.append(" ")
            i = _append_comment_run_for_callee_scan(line, i + 1, out, n=n)
            continue
        if ch == "/" and i + 1 < n and line[i + 1] == "/":
            out.extend((" ", " "))
            i = _append_comment_run_for_callee_scan(line, i + 2, out, n=n)
            continue
        if allow_js_private_fields and ch == "/" and _js_slash_can_start_regex(line, i):
            i = _append_masked_js_regex_at_for_callee_scan(line, i, out, n=n)
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _callee_refs_from_anchor_line(
    anchor_line: str, *, path: str | None = None
) -> frozenset[tuple[str | None, str]]:
    """Extract ``(qualifier|None, name)`` call refs from a review-anchor source line."""
    if not anchor_line:
        return frozenset()
    scan_from = 0
    # A leading def/class/function signature's name is not a callee. Only scan
    # call expressions after the signature (one-liner bodies) when present.
    if _ENCLOSING_DEFINITION_RE.match(anchor_line) is not None:
        colon = anchor_line.find(":")
        if colon < 0:
            return frozenset()
        scan_from = colon + 1
    scan_text = _mask_comments_and_string_literals_for_callee_scan(anchor_line, path=path)
    refs: set[tuple[str | None, str]] = set()
    for match in _CALLEE_REF_RE.finditer(scan_text, scan_from):
        qualifier, name = match.group(1), match.group(2)
        if name.lower() in _CALLEE_KEYWORD_BLOCKLIST:
            continue
        if qualifier is not None and qualifier.lower() in _CALLEE_KEYWORD_BLOCKLIST:
            qualifier = None
        refs.add((qualifier, name))
    return frozenset(refs)


def _callee_refs_from_file_line(
    file_text: str, line: int, *, path: str | None = None
) -> frozenset[tuple[str | None, str]]:
    """Extract callee refs from ``line`` using preceding file lexical context.

    Masking only the isolated review line loses open multiline string/docstring
    state from earlier lines, so call-shaped decoy text can become FIXED
    call-site→definition evidence. Mask the file prefix through ``line`` first.
    """
    if line < 1 or not file_text:
        return frozenset()
    lines = file_text.splitlines()
    if line > len(lines):
        return frozenset()
    prefix = "\n".join(lines[:line])
    masked_prefix = _mask_comments_and_string_literals_for_callee_scan(prefix, path=path)
    masked_lines = masked_prefix.splitlines()
    if line > len(masked_lines):
        return frozenset()
    return _callee_refs_from_anchor_line(masked_lines[line - 1], path=path)


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


def _enclosing_definition_name(file_text: str, line: int) -> str | None:
    """Return the nearest def/function/class name at or above ``line``."""
    identity = _enclosing_definition_identity(file_text, line)
    return None if identity is None else identity[0]
