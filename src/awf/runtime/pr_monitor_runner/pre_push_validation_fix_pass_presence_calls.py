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

    Distinguishes division (``1 / guard.disable() / 2``) from literals such as
    ``const matcher = /guard.disable()/;`` so call scanning does not treat
    regex bodies as executable calls (PRRT_kwDOSJAM6s6Zs-Re).
    """
    j = slash_index - 1
    while j >= 0 and raw_line[j] in " \t":
        j -= 1
    if j < 0:
        return True
    prev = raw_line[j]
    if prev in ")]}" or prev == ".":
        return False
    if prev.isalnum() or prev == "_":
        start = j
        while start >= 0 and (raw_line[start].isalnum() or raw_line[start] == "_"):
            start -= 1
        word = raw_line[start + 1 : j + 1]
        return word in _JS_REGEX_PREFIX_KEYWORDS
    return True


def _blank_js_regex_literal(chars: list[str], raw_line: str, start: int) -> int:
    """Blank a JS regex literal starting at ``start``; return index past it."""
    n = len(raw_line)
    chars[start] = " "
    i = start + 1
    in_class = False
    while i < n:
        ch = raw_line[i]
        chars[i] = " "
        if ch == "\\" and i + 1 < n:
            chars[i + 1] = " "
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
                chars[i] = " "
                i += 1
            return i
        i += 1
    return i


def _executable_call_scan_text(raw_line: str) -> str:
    """Return ``raw_line`` with strings and comment regions replaced by spaces.

    Preserves indices so ``_CALL_SITE_RE.finditer`` aligns with the original line
    for definition-prefix checks. Nested calls inside ``print(guard.disable())``
    stay visible; ``"guard.disable()"``, ``code  # guard.disable()``, and
    same-line ``/* guard.disable() */`` / ``code; /* guard.disable() */`` do not
    (PRRT_kwDOSJAM6s6ZrYJk, PRRT_kwDOSJAM6s6Zrhbs). JS regex literals such as
    ``/guard.disable()/`` are blanked so they are not mistaken for calls
    (PRRT_kwDOSJAM6s6Zs-Re); division keeps real calls visible. Unclosed ``/*``
    blanks through end of line (multi-line ``/*`` state is handled by callers).
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
    (PRRT_kwDOSJAM6s6ZrYJk). ``#`` / ``//`` / same-line ``/* … */`` comments,
    string literals, and JS ``/…/`` regex literals are ignored
    (PRRT_kwDOSJAM6s6Zrhbs, PRRT_kwDOSJAM6s6Zs-Re). Definitions are not
    call sites: ``def name(`` / ``function name(`` / ``class Name(`` are skipped
    via ``_CALL_SITE_DEFINITION_PREFIX_RE``. Each call match is emitted once per
    occurrence (not collapsed by name) so same-line multiplicity is preserved
    when deriving salvage call-count diffs (PRRT_kwDOSJAM6s6ZriaK).
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
