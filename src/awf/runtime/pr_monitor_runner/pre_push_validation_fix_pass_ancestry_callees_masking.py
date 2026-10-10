"""Comment / string-literal / regex masking for the callee scan.

Split out of ``..._ancestry_callees.py`` so that module stays under the
first-party line guardrail (``MAX_FIRST_PARTY_FILE_LINES``); every name here is
re-exported there, so existing imports and test references keep working.

This is the leaf of the callee-scan stack: it turns one raw source line (or a
whole masked file prefix) into *code-only* text, so call-shaped text inside
comments, string/template literals, JS/TS regex literals, and JSX text nodes
cannot become FIXED-with-evidence callee references. Masking preserves
``str.splitlines()`` separators and string length so masked scan lines stay
index-aligned with the raw file.
"""

from __future__ import annotations

# ``str.splitlines()`` separators beyond ``\r``/``\n``. Masking must preserve
# these so masked scan lines stay index-aligned with raw ``splitlines()``.
_SPLITLINES_SEPARATOR_CHARS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")


def _mask_char_preserving_splitlines_separators(ch: str) -> str:
    """Blank ``ch`` unless it is a ``str.splitlines()`` separator."""
    return ch if ch in _SPLITLINES_SEPARATOR_CHARS else " "


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


def _path_is_jsx(path: str | None) -> bool:
    """Return True when ``path`` is a JSX/TSX source file (text nodes possible)."""
    if not path:
        return False
    name = path.lower().replace("\\", "/").rsplit("/", 1)[-1]
    return name.endswith((".jsx", ".tsx"))


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
    # ``>`` alone is not a regex opener (comparisons / generics); ``=>`` is.
    if prev in "([{;=,:!&|?~^%*+-":
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
    """Blank a JS/TS regex literal starting at ``slash_index``; return index after.

    Only JS line terminators (``\\r``/``\\n``/U+2028/U+2029) end a regex literal.
    Other ``str.splitlines()`` separators (e.g. form feed) stay in place so masked
    line indices stay aligned while blanking continues through the literal.
    """
    out.append(" ")
    i = slash_index + 1
    in_class = False
    while i < n:
        cur = line[i]
        if cur in "\r\n\u2028\u2029":
            break
        if cur in _SPLITLINES_SEPARATOR_CHARS:
            out.append(cur)
            i += 1
            continue
        if cur == "\\" and i + 1 < n:
            out.append(" ")
            out.append(_mask_char_preserving_splitlines_separators(line[i + 1]))
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


def _python_string_prefix_is_f(line: str, quote_index: int) -> bool:
    """True when ``line[quote_index]`` opens a Python f-string (``f`` / ``rf`` / …)."""
    j = quote_index - 1
    while j >= 0 and line[j] in "rRuUfFbB":
        j -= 1
    prefix = line[j + 1 : quote_index]
    return bool(prefix) and ("f" in prefix or "F" in prefix)


def _append_comment_run_for_callee_scan(line: str, start: int, out: list[str], *, n: int) -> int:
    """Blank a ``#`` / ``//`` comment run from ``start``; return index after.

    Non-``\r``/``\n`` ``splitlines`` separators (e.g. form feed) stay in place so
    masked line indices match raw ``splitlines()``; comment blanking continues
    until a real newline.
    """
    i = start
    while i < n and line[i] not in "\r\n":
        out.append(_mask_char_preserving_splitlines_separators(line[i]))
        i += 1
    return i


def _append_masked_block_comment_at_for_callee_scan(
    line: str, slash_index: int, out: list[str], *, n: int
) -> int:
    """Blank a ``/* ... */`` block comment starting at ``slash_index``; return index after.

    Newlines are preserved so multiline prefix masking keeps line alignment.
    Unclosed comments blank through EOF so interior quotes cannot poison later
    review lines when the file prefix is masked as one string.
    """
    out.extend((" ", " "))
    i = slash_index + 2
    while i < n:
        cur = line[i]
        if cur == "*" and i + 1 < n and line[i + 1] == "/":
            out.extend((" ", " "))
            return i + 2
        out.append(_mask_char_preserving_splitlines_separators(cur))
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
        if cur == "/" and i + 1 < n and line[i + 1] == "*":
            i = _append_masked_block_comment_at_for_callee_scan(line, i, out, n=n)
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
        out.append(_mask_char_preserving_splitlines_separators(cur))
        i += 1
    return i


def _mask_comments_and_string_literals_for_callee_scan(
    line: str, *, path: str | None = None
) -> str:
    """Blank comments and string/template/regex literals so callee regex stays code-only.

    Call-shaped text inside ``#`` / ``//`` / ``/* */`` comments, quoted literal
    text, or JS/TS regex literals must not become FIXED callee evidence.
    Executable interpolations are retained: Python f-string ``{...}`` bodies and
    JS/TS template ``${...}`` bodies stay scannable. Nested strings/comments/
    regexes inside those retained expressions are re-masked so inert literals
    such as ``f'{"helper()"}'`` or ``${/helper()/}`` do not become false callees.
    ``#ident`` is kept as code only for JS/TS paths (private fields). For Python
    and unknown paths, every ``#`` begins a comment (fail closed on ambiguity).
    JS/TS regex masking uses the same path gate. Block comments are blanked for
    every path so a quote inside ``/* ... */`` cannot poison later prefix lines.
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
        if ch == "/" and i + 1 < n and line[i + 1] == "*":
            i = _append_masked_block_comment_at_for_callee_scan(line, i, out, n=n)
            continue
        if allow_js_private_fields and ch == "/" and _js_slash_can_start_regex(line, i):
            i = _append_masked_js_regex_at_for_callee_scan(line, i, out, n=n)
            continue
        out.append(ch)
        i += 1
    masked = "".join(out)
    if _path_is_jsx(path):
        return _mask_jsx_text_nodes_for_callee_scan(masked)
    return masked


def _mask_jsx_text_nodes_for_callee_scan(line: str) -> str:
    """Blank JSX text between tags; keep ``{...}`` expression bodies scannable.

    Literal UI text such as ``<div>helper()</div>`` or ``<>helper()</>`` must
    not become FIXED call-site evidence. Attribute strings are already blanked
    by the prior quote pass. Comparisons without a ``<`` tag opener are left
    unchanged. Fragment openers ``<>`` are recognized (next char ``>``).
    """
    if not line or "<" not in line:
        return line
    chars = list(line)
    n = len(line)
    i = 0
    while i < n:
        # Named/close/comment tags, or fragment opener ``<>`` (next is ``>``).
        if line[i] == "<" and i + 1 < n and (line[i + 1].isalpha() or line[i + 1] in "/!>"):
            # Advance through the tag to its closing ``>`` (strings already blank).
            i += 1
            while i < n and line[i] != ">":
                i += 1
            if i >= n:
                break
            i += 1  # past ``>``
            # Text node until the next tag opener; retain ``{...}`` expressions.
            while i < n and line[i] != "<":
                if line[i] == "{":
                    depth = 1
                    i += 1
                    while i < n and depth > 0:
                        if line[i] == "{":
                            depth += 1
                        elif line[i] == "}":
                            depth -= 1
                        i += 1
                    continue
                chars[i] = _mask_char_preserving_splitlines_separators(line[i])
                i += 1
            continue
        i += 1
    return "".join(chars)
