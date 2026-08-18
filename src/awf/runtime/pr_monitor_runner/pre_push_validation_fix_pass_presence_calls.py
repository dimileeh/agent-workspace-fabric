"""Call-site scanning helpers for salvage presence / tip-extra supersession."""

from __future__ import annotations

import re

from awf.runtime.pr_monitor_runner.helpers_verdict import (
    _ascii_double_quote_is_delimiter,
    _ascii_single_quote_is_delimiter,
)

# Bare / dotted / optional-chain / computed-member / ``await`` call sites
# (``disable_guard()`` / ``guard.disable()`` / ``guard?.disable()`` /
# ``guard?.()`` / ``guard["disable"]()`` / ``guard[key]()`` /
# ``await guard.disable();`` / nested ``if ready: guard.disable()``). Identifier
# chain (``.`` or ``?.`` separators) then optional trailing ``?.`` and ``(``
# anywhere in executable text (not only statement-leading). Without ``?.`` as a
# separator the match restarts after ``?.`` and yields a bare method leaf, so
# tip ``guard?.disable()`` never intersects a salvaged ``guard`` binding
# (PRRT_kwDOSJAM6s6ZriaJ). Computed-member forms need a separate pattern: after
# ``_executable_call_scan_text`` blanks quoted props, ``guard["disable"]()``
# becomes ``guard[         ]()`` and this regex cannot span brackets
# (PRRT_kwDOSJAM6s6ZroRa). Parenthesized receivers (``(guard).disable()``) are
# handled by ``_PAREN_MEMBER_CALL_SITE_RE`` / ``_PAREN_COMPUTED_CALL_SITE_RE``
# (PRRT_kwDOSJAM6s6Zrr7R). ``def``/``function``/``class`` forms are filtered via
# ``_CALL_SITE_DEFINITION_PREFIX_RE``. Used so tip-extra calls that invoke
# salvage-bound names fail closed even though calls produce no binding key
# (PRRT_kwDOSJAM6s6ZrJ3a, PRRT_kwDOSJAM6s6ZrSYE, PRRT_kwDOSJAM6s6ZrYJk).
_CALL_SITE_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))(?:await[ \t]+)?"
    r"([A-Za-z_][A-Za-z0-9_]*(?:(?:\?\.|\.)[A-Za-z_][A-Za-z0-9_]*)*)"
    r"(?:\?\.)?[ \t]*\("
)
# Computed-member invocations on an identifier chain (``guard["disable"]()`` /
# ``guard['disable']()`` / ``guard[key]()`` / ``guard?.["disable"]()`` /
# ``a.b["c"]()`` / ``obj["a"]["b"]()``). Matched on executable scan text where
# quoted indices are spaces; bracket bodies may therefore be whitespace-only.
_COMPUTED_CALL_SITE_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))(?:await[ \t]+)?"
    r"([A-Za-z_][A-Za-z0-9_]*(?:(?:\?\.|\.)[A-Za-z_][A-Za-z0-9_]*)*)"
    r"(?:(?:\?\.)?\[[^\]]*\])+"
    r"(?:\?\.)?[ \t]*\("
)
# Parenthesized receivers (``(guard).disable()`` / ``((guard)).disable()`` /
# ``(guard)?.disable()`` / ``(a.b).c()``). Without this, matching restarts after
# the closing ``)`` and reports only the bare method leaf, which misses a
# salvaged ``guard`` binding and retains stale FIXED evidence; a bare ``disable``
# leaf can also falsely suffix-match ``guard.disable`` for unrelated
# ``(other).disable()`` (PRRT_kwDOSJAM6s6Zrr7R).
_PAREN_MEMBER_CALL_SITE_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))(?:await[ \t]+)?"
    r"\(+"
    r"[ \t]*"
    r"([A-Za-z_][A-Za-z0-9_]*(?:(?:\?\.|\.)[A-Za-z_][A-Za-z0-9_]*)*)"
    r"[ \t]*"
    r"\)+"
    r"((?:(?:\?\.|\.)[A-Za-z_][A-Za-z0-9_]*)*)"
    r"(?:\?\.)?[ \t]*\("
)
# Parenthesized receiver + computed member (``(guard)["disable"]()`` /
# ``(guard)?.["disable"]()`` / ``(a.b)[key]()``). Same restart-after-``)`` gap as
# dotted paren forms; blanked scan text leaves ``(guard)[         ]()``, which
# neither dotted nor plain computed patterns span (PRRT_kwDOSJAM6s6Zrr7R).
_PAREN_COMPUTED_CALL_SITE_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z0-9_]))(?:await[ \t]+)?"
    r"\(+"
    r"[ \t]*"
    r"([A-Za-z_][A-Za-z0-9_]*(?:(?:\?\.|\.)[A-Za-z_][A-Za-z0-9_]*)*)"
    r"[ \t]*"
    r"\)+"
    r"(?:(?:\?\.)?\[[^\]]*\])+"
    r"(?:\?\.)?[ \t]*\("
)
# Prefix ending at a call-site match start that means the name is a definition
# binding, not an invocation (``def`` / ``async def`` / ``function`` / ``class``).
_CALL_SITE_DEFINITION_PREFIX_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_])(?:(?:export[ \t]+(?:default[ \t]+)?)?(?:async[ \t]+)?(?:def|function)"
    r"|(?:export[ \t]+(?:default[ \t]+)?)?class)[ \t]+$"
)
# Keywords that introduce an expression so a following ``/`` is a regex literal
# rather than division (``return /x/``, ``case /x/:``, ``typeof /x/``).
_JS_REGEX_PREFIX_KEYWORDS = frozenset(
    {
        "return",
        "case",
        "throw",
        "delete",
        "void",
        "typeof",
        "new",
        "await",
        "yield",
        "in",
        "of",
        "instanceof",
        "else",
        "do",
    }
)


def _js_regex_literal_start(raw_line: str, slash_index: int) -> bool:
    """True when ``raw_line[slash_index]`` looks like a JS ``/…/`` regex opener.

    Distinguishes division (``1 / guard.disable() / 2``,
    ``retries++ / guard.disable()``) from literals such as
    ``const matcher = /guard.disable()/;`` so call scanning does not treat
    regex bodies as executable calls (PRRT_kwDOSJAM6s6Zs-Re,
    PRRT_kwDOSJAM6s6ZtHbn).
    """
    j = slash_index - 1
    while j >= 0 and raw_line[j] in " \t":
        j -= 1
    if j < 0:
        return True
    prev = raw_line[j]
    if prev in ")]}" or prev == ".":
        return False
    # Postfix ``++`` / ``--`` yield a value, so a following ``/`` is division
    # (``retries++ / guard.disable()``), not a regex opener that would blank the
    # call (PRRT_kwDOSJAM6s6ZtHbn). A lone ``+`` / ``-`` still allows regex
    # (``x + /re/``).
    if prev in "+-" and j > 0 and raw_line[j - 1] == prev:
        return False
    if prev.isalnum() or prev == "_":
        start = j
        while start >= 0 and (raw_line[start].isalnum() or raw_line[start] == "_"):
            start -= 1
        word = raw_line[start + 1 : j + 1]
        return word in _JS_REGEX_PREFIX_KEYWORDS
    return True


def _skip_js_regex_literal(raw_line: str, start: int) -> int:
    """Return index past a JS regex literal starting at ``start``."""
    n = len(raw_line)
    i = start + 1
    in_class = False
    while i < n:
        ch = raw_line[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "[" and not in_class:
            in_class = True
            i += 1
            continue
        if ch == "]" and in_class:
            in_class = False
            i += 1
            continue
        if ch == "/" and not in_class:
            i += 1
            while i < n and raw_line[i].isalpha():
                i += 1
            return i
        i += 1
    return i


def _blank_js_regex_literal(chars: list[str], raw_line: str, start: int) -> int:
    """Blank a JS regex literal starting at ``start``; return index past it."""
    end = _skip_js_regex_literal(raw_line, start)
    for j in range(start, end):
        chars[j] = " "
    return end


def _skip_js_template_literal(raw_line: str, start: int) -> int:
    """Return index past a JS template literal starting at ``start`` (no blanking)."""
    i = start + 1
    n = len(raw_line)
    while i < n:
        ch = raw_line[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "`":
            return i + 1
        if ch == "$" and i + 1 < n and raw_line[i + 1] == "{":
            i = _find_js_template_interpolation_end(raw_line, i + 2)
            if i < n and raw_line[i] == "}":
                i += 1
            continue
        i += 1
    return i


def _find_js_template_interpolation_end(raw_line: str, start: int) -> int:
    """Return index of the ``}`` that closes a ``${`` body starting at ``start``.

    Tracks brace depth while skipping strings, nested templates, line/block
    comments, and regex literals so braces inside those regions do not end the
    interpolation early (PRRT_kwDOSJAM6s6ZtYk3).
    """
    i = start
    n = len(raw_line)
    depth = 1
    in_double = False
    in_single = False
    while i < n:
        ch = raw_line[i]
        if in_double:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"' and _ascii_double_quote_is_delimiter(raw_line, i, True):
                in_double = False
            i += 1
            continue
        if in_single:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == "'" and _ascii_single_quote_is_delimiter(raw_line, i, True):
                in_single = False
            i += 1
            continue
        if ch == "/":
            if i + 1 < n and raw_line[i + 1] == "/":
                i += 2
                while i < n and raw_line[i] != "\n":
                    i += 1
                continue
            if i + 1 < n and raw_line[i + 1] == "*":
                i += 2
                while i < n:
                    if raw_line.startswith("*/", i):
                        i += 2
                        break
                    i += 1
                continue
            if (i + 1 >= n or raw_line[i + 1] != "=") and _js_regex_literal_start(raw_line, i):
                i = _skip_js_regex_literal(raw_line, i)
                continue
        if ch == "`":
            i = _skip_js_template_literal(raw_line, i)
            continue
        if ch == '"' and _ascii_double_quote_is_delimiter(raw_line, i, False):
            in_double = True
            i += 1
            continue
        if ch == "'" and _ascii_single_quote_is_delimiter(raw_line, i, False):
            in_single = True
            i += 1
            continue
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return i
            i += 1
            continue
        i += 1
    return i


def _blank_js_template_literal(chars: list[str], raw_line: str, start: int) -> int:
    """Blank a JS template literal; keep ``${...}`` expressions scannable.

    Static segments (and escapes) become spaces so `` `guard.disable()` `` is
    not a call site. Interpolation bodies are re-scanned with the same
    executable-blanking rules so `` `${guard.disable()}` `` still matches
    (PRRT_kwDOSJAM6s6ZtJG8).
    """
    n = len(raw_line)
    chars[start] = " "
    i = start + 1
    while i < n:
        ch = raw_line[i]
        if ch == "\\" and i + 1 < n:
            chars[i] = " "
            chars[i + 1] = " "
            i += 2
            continue
        if ch == "`":
            chars[i] = " "
            return i + 1
        if ch == "$" and i + 1 < n and raw_line[i + 1] == "{":
            chars[i] = " "
            chars[i + 1] = " "
            expr_start = i + 2
            expr_end = _find_js_template_interpolation_end(raw_line, expr_start)
            blanked = _executable_call_scan_text(raw_line[expr_start:expr_end])
            for offset, blank_ch in enumerate(blanked):
                chars[expr_start + offset] = blank_ch
            if expr_end < n and raw_line[expr_end] == "}":
                chars[expr_end] = " "
                i = expr_end + 1
            else:
                i = expr_end
            continue
        chars[i] = " "
        i += 1
    return i


def _py_fstring_prefix_len(raw_line: str, quote_index: int) -> int:
    """Return length of an ``f``/``F`` string prefix before ``quote_index``, or 0.

    Accepts ``f``/``F`` alone and ``rf``/``fr`` (any case). Rejects prefixes that
    continue an identifier (``xf\"...\"``) so ordinary tokens are not treated as
    f-strings (PRRT_kwDOSJAM6s6Zt7Go).
    """
    if quote_index <= 0:
        return 0
    j = quote_index - 1
    while j >= 0 and raw_line[j] in "rRuUfF":
        j -= 1
    prefix = raw_line[j + 1 : quote_index]
    if not prefix or "f" not in prefix.lower():
        return 0
    if j >= 0 and (raw_line[j].isalnum() or raw_line[j] == "_"):
        return 0
    return len(prefix)


def _skip_py_triple_quoted_string(raw_line: str, start: int) -> int:
    """Return index past a ``'''`` / ``\"\"\"`` string starting at ``start``."""
    quote = raw_line[start : start + 3]
    i = start + 3
    n = len(raw_line)
    while i < n:
        if raw_line.startswith(quote, i):
            return i + 3
        if raw_line[i] == "\\" and i + 1 < n:
            i += 2
            continue
        i += 1
    return i


def _skip_py_fstring(raw_line: str, prefix_start: int) -> int:
    """Return index past a Python f-string starting at ``prefix_start``."""
    i = prefix_start
    n = len(raw_line)
    while i < n and raw_line[i] in "rRuUfF":
        i += 1
    if i >= n or raw_line[i] not in "\"'":
        return prefix_start + 1
    quote_len = 3 if raw_line.startswith(('"""', "'''"), i) else 1
    quote = raw_line[i : i + quote_len]
    i += quote_len
    while i < n:
        ch = raw_line[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if raw_line.startswith(quote, i):
            return i + quote_len
        if ch == "{" and i + 1 < n and raw_line[i + 1] == "{":
            i += 2
            continue
        if ch == "}":
            if i + 1 < n and raw_line[i + 1] == "}":
                i += 2
                continue
            i += 1
            continue
        if ch == "{":
            i = _find_py_fstring_expr_end(raw_line, i + 1)
            if i < n and raw_line[i] == "}":
                i += 1
            continue
        i += 1
    return i


def _find_py_fstring_expr_end(raw_line: str, start: int) -> int:
    """Return index of the ``}`` that closes an f-string field body at ``start``.

    Tracks brace depth while skipping nested strings (including nested
    f-strings) and ``#`` comments so braces inside those regions do not end the
    field early (PRRT_kwDOSJAM6s6Zt7Go).
    """
    i = start
    n = len(raw_line)
    depth = 1
    in_double = False
    in_single = False
    while i < n:
        ch = raw_line[i]
        if in_double:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"' and _ascii_double_quote_is_delimiter(raw_line, i, True):
                in_double = False
            i += 1
            continue
        if in_single:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == "'" and _ascii_single_quote_is_delimiter(raw_line, i, True):
                in_single = False
            i += 1
            continue
        if ch == "#":
            while i < n and raw_line[i] != "\n":
                i += 1
            continue
        if ch in "\"'":
            prefix_len = _py_fstring_prefix_len(raw_line, i)
            if prefix_len:
                i = _skip_py_fstring(raw_line, i - prefix_len)
                continue
            if raw_line.startswith('"""', i) or raw_line.startswith("'''", i):
                i = _skip_py_triple_quoted_string(raw_line, i)
                continue
            if ch == '"' and _ascii_double_quote_is_delimiter(raw_line, i, False):
                in_double = True
                i += 1
                continue
            if ch == "'" and _ascii_single_quote_is_delimiter(raw_line, i, False):
                in_single = True
                i += 1
                continue
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return i
            i += 1
            continue
        i += 1
    return i


def _blank_py_fstring(chars: list[str], raw_line: str, quote_index: int) -> int:
    """Blank a Python f-string; keep ``{...}`` replacement expressions scannable.

    Static segments (and ``{{`` / ``}}`` escapes) become spaces so
    ``f\"guard.disable()\"`` is not a call site. Replacement bodies are
    re-scanned with the same executable-blanking rules so
    ``f\"{guard.disable()}\"`` still matches (PRRT_kwDOSJAM6s6Zt7Go).
    """
    prefix_len = _py_fstring_prefix_len(raw_line, quote_index)
    for j in range(quote_index - prefix_len, quote_index):
        chars[j] = " "
    quote_len = 3 if raw_line.startswith(('"""', "'''"), quote_index) else 1
    quote = raw_line[quote_index : quote_index + quote_len]
    for j in range(quote_len):
        chars[quote_index + j] = " "
    i = quote_index + quote_len
    n = len(raw_line)
    while i < n:
        ch = raw_line[i]
        if ch == "\\" and i + 1 < n:
            chars[i] = " "
            chars[i + 1] = " "
            i += 2
            continue
        if raw_line.startswith(quote, i):
            for j in range(quote_len):
                chars[i + j] = " "
            return i + quote_len
        if ch == "{" and i + 1 < n and raw_line[i + 1] == "{":
            chars[i] = " "
            chars[i + 1] = " "
            i += 2
            continue
        if ch == "}":
            if i + 1 < n and raw_line[i + 1] == "}":
                chars[i] = " "
                chars[i + 1] = " "
                i += 2
                continue
            chars[i] = " "
            i += 1
            continue
        if ch == "{":
            chars[i] = " "
            expr_start = i + 1
            expr_end = _find_py_fstring_expr_end(raw_line, expr_start)
            blanked = _executable_call_scan_text(raw_line[expr_start:expr_end])
            for offset, blank_ch in enumerate(blanked):
                chars[expr_start + offset] = blank_ch
            if expr_end < n and raw_line[expr_end] == "}":
                chars[expr_end] = " "
                i = expr_end + 1
            else:
                i = expr_end
            continue
        chars[i] = " "
        i += 1
    return i


def _executable_call_scan_text(raw_line: str) -> str:
    """Return ``raw_line`` with strings and comment regions replaced by spaces.

    Preserves indices so ``_CALL_SITE_RE.finditer`` aligns with the original line
    for definition-prefix checks. Nested calls inside ``print(guard.disable())``
    stay visible; ``"guard.disable()"``, ``code  # guard.disable()``, and
    same-line ``/* guard.disable() */`` / ``code; /* guard.disable() */`` do not
    (PRRT_kwDOSJAM6s6ZrYJk, PRRT_kwDOSJAM6s6Zrhbs). JS template literals blank
    static text but keep ``${...}`` expressions scannable
    (PRRT_kwDOSJAM6s6ZtJG8). Python f-strings blank static text but keep
    ``{...}`` replacement expressions scannable (PRRT_kwDOSJAM6s6Zt7Go). JS
    regex literals such as ``/guard.disable()/`` are blanked so they are not
    mistaken for calls (PRRT_kwDOSJAM6s6Zs-Re); division keeps real calls
    visible. Unclosed ``/*`` blanks through end of line (multi-line ``/*``
    state is handled by callers).
    """
    chars = list(raw_line)
    i = 0
    n = len(raw_line)
    in_double = False
    in_single = False
    while i < n:
        ch = raw_line[i]
        if in_double:
            chars[i] = " "
            if ch == "\\" and i + 1 < n:
                chars[i + 1] = " "
                i += 2
                continue
            if ch == '"' and _ascii_double_quote_is_delimiter(raw_line, i, True):
                in_double = False
            i += 1
            continue
        if in_single:
            chars[i] = " "
            if ch == "\\" and i + 1 < n:
                chars[i + 1] = " "
                i += 2
                continue
            if ch == "'" and _ascii_single_quote_is_delimiter(raw_line, i, True):
                in_single = False
            i += 1
            continue
        # Detect f-strings before ordinary / triple-quote blanking so
        # ``f\"{guard.disable()}\"`` / ``f\"\"\"{...}\"\"\"`` keep replacement
        # fields scannable (PRRT_kwDOSJAM6s6Zt7Go).
        if ch in "\"'" and _py_fstring_prefix_len(raw_line, i):
            i = _blank_py_fstring(chars, raw_line, i)
            continue
        if raw_line.startswith('"""', i) or raw_line.startswith("'''", i):
            quote = raw_line[i : i + 3]
            for _ in range(3):
                chars[i] = " "
                i += 1
            while i < n:
                if raw_line.startswith(quote, i):
                    for _ in range(3):
                        chars[i] = " "
                        i += 1
                    break
                chars[i] = " "
                i += 1
            continue
        if ch == "#":
            while i < n:
                chars[i] = " "
                i += 1
            break
        if ch == "/":
            if i + 1 < n and raw_line[i + 1] == "/":
                while i < n:
                    chars[i] = " "
                    i += 1
                break
            if i + 1 < n and raw_line[i + 1] == "*":
                chars[i] = " "
                chars[i + 1] = " "
                i += 2
                while i < n:
                    if raw_line.startswith("*/", i):
                        chars[i] = " "
                        chars[i + 1] = " "
                        i += 2
                        break
                    chars[i] = " "
                    i += 1
                continue
            # ``/=`` is assignment, not a regex. Other ``/`` openers blank the
            # literal so bodies like ``/guard.disable()/`` are not call sites
            # (PRRT_kwDOSJAM6s6Zs-Re); division context leaves ``/`` visible.
            if (i + 1 >= n or raw_line[i + 1] != "=") and _js_regex_literal_start(raw_line, i):
                i = _blank_js_regex_literal(chars, raw_line, i)
                continue
        if ch == "`":
            i = _blank_js_template_literal(chars, raw_line, i)
            continue
        if ch == '"' and _ascii_double_quote_is_delimiter(raw_line, i, False):
            chars[i] = " "
            in_double = True
            i += 1
            continue
        if ch == "'" and _ascii_single_quote_is_delimiter(raw_line, i, False):
            chars[i] = " "
            in_single = True
            i += 1
            continue
        i += 1
    return "".join(chars)


def _is_member_call_continuation(stripped_line: str) -> bool:
    """True when ``stripped_line`` continues a prior receiver (``.m()`` / ``?.m()`` / ``[…]()``).

    Bracket forms require a call after ``]`` so TOML table headers (``[logging]``)
    are not treated as computed-member continuations.
    """
    if stripped_line.startswith((".", "?.")):
        return True
    if not stripped_line.startswith("["):
        return False
    close = stripped_line.find("]")
    if close < 0:
        return False
    after = stripped_line[close + 1 :].lstrip(" \t")
    return after.startswith("(") or after.startswith("?.")


def _join_member_call_continuation_line(lines: list[str], idx: int) -> str:
    """Join ``lines[idx]`` with preceding expression lines for multiline member calls.

    Formatters commonly split ``guard.disable()`` across lines as ``guard`` +
    ``  .disable()``. Per-line scanning then sees no call on the receiver line
    and only a bare ``disable`` leaf on the continuation, missing salvaged
    ``guard`` bindings (PRRT_kwDOSJAM6s6ZuG-J). Walk back through blank / line-
    comment lines, attaching ``.`` / ``?.`` / ``[`` continuations, and stop at
    the first non-continuation expression root. A prior line that already ends
    a statement (``;``) is not a receiver root.
    """
    raw_line = lines[idx]
    stripped = raw_line.lstrip(" \t")
    if not _is_member_call_continuation(stripped):
        return raw_line
    parts: list[str] = [stripped]
    j = idx - 1
    while j >= 0:
        prev = lines[j]
        prev_stripped = prev.strip()
        if prev_stripped == "" or prev_stripped.startswith("//") or prev_stripped.startswith("#"):
            j -= 1
            continue
        prev_code = prev.lstrip(" \t")
        if _is_member_call_continuation(prev_code):
            parts.insert(0, prev_code)
            j -= 1
            continue
        if prev_stripped.endswith(";"):
            break
        parts.insert(0, prev.rstrip())
        break
    joined = parts[0]
    for part in parts[1:]:
        joined = joined.rstrip() + part.lstrip(" \t")
    return joined


def _call_site_names_for_line(raw_line: str) -> tuple[str, ...]:
    """Return receiver/callee names for call sites on a line, or empty.

    Bare ``disable_guard()`` yields ``(disable_guard,)``. Dotted
    ``guard.disable()`` / ``a.b.c()`` yields the receiver and the full dotted
    callee (``(guard, guard.disable)`` / ``(a, a.b.c)``) so tip member calls
    intersect salvage bindings and the same qualified callee — not an unpaired
    method leaf that would collide with ``other.disable()`` or scoped
    ``Guards.disable_guard`` (PRRT_kwDOSJAM6s6ZrSYE, PRRT_kwDOSJAM6s6ZrWwo).
    Optional-chain forms (``guard?.disable()`` / ``guard?.()``) normalize
    ``?.`` to ``.`` so the same receiver identity is preserved; otherwise the
    match restarts after ``?.`` and only the bare method leaf is seen
    (PRRT_kwDOSJAM6s6ZriaJ). Computed-member forms (``guard["disable"]()`` /
    ``guard['disable']()`` / ``guard[key]()`` / ``guard?.["disable"]()``)
    emit the receiver chain: blanking quoted indices leaves
    ``guard[         ]()``, which dotted matching cannot span
    (PRRT_kwDOSJAM6s6ZroRa). Parenthesized receivers (``(guard).disable()`` /
    ``((guard)).disable()`` / ``(guard)?.disable()`` / ``(guard)["disable"]()``)
    keep the inner identifier chain; otherwise matching restarts after ``)`` and
    reports only the bare method leaf (PRRT_kwDOSJAM6s6Zrr7R). Nested /
    mid-expression calls (``if ready: guard.disable()``,
    ``result = guard.disable()``, ``print(guard.disable())``) are included
    (PRRT_kwDOSJAM6s6ZrYJk).     ``#`` / ``//`` / same-line ``/* … */`` comments,
    string literals, JS template-literal static text, Python f-string static
    text, and JS ``/…/`` regex literals are ignored (PRRT_kwDOSJAM6s6Zrhbs,
    PRRT_kwDOSJAM6s6Zs-Re, PRRT_kwDOSJAM6s6ZtJG8, PRRT_kwDOSJAM6s6Zt7Go);
    ``${...}`` interpolations and f-string ``{...}`` fields remain scannable.
    Definitions are not call sites: ``def name(`` / ``function name(`` /
    ``class Name(`` are skipped via ``_CALL_SITE_DEFINITION_PREFIX_RE``. Each
    call match is emitted once per occurrence (not collapsed by name) so
    same-line multiplicity is preserved when deriving salvage call-count diffs
    (PRRT_kwDOSJAM6s6ZriaK).
    """
    stripped = raw_line.lstrip(" \t")
    if stripped.startswith("//") or stripped.startswith("#"):
        return ()
    names: list[str] = []
    scan = _executable_call_scan_text(raw_line)

    def _emit_receiver_chain(raw_chain: str) -> None:
        dotted = raw_chain.replace("?.", ".")
        parts = dotted.split(".")
        if len(parts) == 1:
            names.append(parts[0])
        else:
            names.append(parts[0])
            names.append(dotted)

    def _blank_match_span(match: re.Match[str]) -> None:
        nonlocal scan
        # Drop the matched paren-call span so later dotted/computed scans do not
        # re-report a bare trailing callee (``disable`` after ``(guard).``) that
        # would suffix-collide with ``guard.disable`` (PRRT_kwDOSJAM6s6Zrr7R).
        scan = scan[: match.start()] + (" " * (match.end() - match.start())) + scan[match.end() :]

    for match in _PAREN_MEMBER_CALL_SITE_RE.finditer(scan):
        prefix = raw_line[: match.start()]
        if _CALL_SITE_DEFINITION_PREFIX_RE.search(prefix):
            continue
        inner = match.group(1)
        trailing = match.group(2) or ""
        _emit_receiver_chain(inner + trailing)
        _blank_match_span(match)
    for match in _PAREN_COMPUTED_CALL_SITE_RE.finditer(scan):
        prefix = raw_line[: match.start()]
        if _CALL_SITE_DEFINITION_PREFIX_RE.search(prefix):
            continue
        _emit_receiver_chain(match.group(1))
        _blank_match_span(match)
    for match in _CALL_SITE_RE.finditer(scan):
        prefix = raw_line[: match.start()]
        if _CALL_SITE_DEFINITION_PREFIX_RE.search(prefix):
            continue
        # Normalize JS/TS optional chaining so identity matches dotted form.
        _emit_receiver_chain(match.group(1))
    for match in _COMPUTED_CALL_SITE_RE.finditer(scan):
        prefix = raw_line[: match.start()]
        if _CALL_SITE_DEFINITION_PREFIX_RE.search(prefix):
            continue
        _emit_receiver_chain(match.group(1))
    return tuple(names)


def _candidate_keys_include_call_name(
    candidate_keys: set[str],
    name: str,
    *,
    receiver_prefix_keys: set[str] | frozenset[str] = frozenset(),
) -> bool:
    """True when ``name`` matches a candidate key, scoped leaf, or dotted root.

    Exact and ``*.{name}`` leaf matches cover bare and scoped callees. A
    computed-member tip (``guard["disable"]()``) emits only the receiver
    ``guard`` while salvage call-count diffs may list ``guard.disable`` /
    ``guard.enable``; treat ``name`` as matching any ``name.*`` key in
    ``receiver_prefix_keys`` (call-count candidates only) so those restores
    still supersede (PRRT_kwDOSJAM6s6ZroRa). Prefix must not run against
    scoped binding keys such as ``feature.enabled``: tip-extra ``feature()``
    would otherwise drop still-present salvage (PRRT_kwDOSJAM6s6ZrsE0).
    """
    if name in candidate_keys:
        return True
    suffix = f".{name}"
    if any(key.endswith(suffix) for key in candidate_keys):
        return True
    if not receiver_prefix_keys:
        return False
    prefix = f"{name}."
    return any(key.startswith(prefix) for key in receiver_prefix_keys)
