"""CommonMark emphasis and inline opaque spans for verdict line normalization."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Set
from typing import NamedTuple

from awf.runtime.pr_monitor_runner.constants import (
    _AWF_VERDICT,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _HTML_TYPE7_ATTR,
    _MARKDOWN_EMPHASIS_PREFIX,
    _markdown_shielded_block_line_starts,
)


class _OpenStackSnap(NamedTuple):
    """Persistent opener-stack tip for label-open snapshots.

    Append-only growth shares prior nodes so alternating unmatched ``*`` / ``[``
    stays linear (PRRT_kwDOSJAM6s6bU8Th). Mid-stack mutations invalidate and
    rebuild from the live list.
    """

    entry: tuple[int, bool, bool]
    prev: _OpenStackSnap | None


__all__ = (
    "_COMMONMARK_ASCII_PUNCTUATION",
    "_COMMONMARK_BACKSLASH_ESCAPED_PUNCT",
    "_MARKDOWN_INLINE_HTML_TOKEN",
    "_MARKDOWN_URI_AUTOLINK",
    "_MARKDOWN_EMAIL_AUTOLINK",
    "_markdown_char_is_escaped",
    "_markdown_char_is_unicode_punctuation",
    "_markdown_emphasis_closer_is_valid",
    "_markdown_emphasis_prefix_closer_is_valid",
    "_verdict_reason_begins_with_emphasis_opener",
    "_markdown_emphasis_run_can_close",
    "_markdown_emphasis_run_can_open",
    "_emphasis_run_pair_blocked_by_multiple_of_three",
    "_advance_past_markdown_code_span",
    "_advance_past_markdown_inline_html",
    "_advance_past_markdown_autolink",
    "_advance_past_markdown_link_destination",
    "_advance_past_markdown_link_reference_label",
    "_markdown_normalize_link_reference_label",
    "_match_markdown_reference_definition_line",
    "_markdown_reference_definition_spans",
    "_verdict_reason_trailing_emphasis_is_balanced",
    "_normalize_markdown_emphasized_verdict_line",
)

# CommonMark: a backslash before any ASCII punctuation yields the literal.
# Same ASCII set is punctuation for flanking (Pc–Ps alone miss Sc/Sm/Sk chars
# such as ``$``, ``+``, ``^`` — PRRT_kwDOSJAM6s6bSZP4).
_COMMONMARK_ASCII_PUNCTUATION = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_COMMONMARK_BACKSLASH_ESCAPED_PUNCT = re.compile(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])")


def _markdown_char_is_escaped(text: str, index: int) -> bool:
    """Return True when ``text[index]`` is preceded by an odd backslash run."""
    count = 0
    i = index - 1
    while i >= 0 and text[i] == "\\":
        count += 1
        i -= 1
    return count % 2 == 1


def _markdown_char_is_unicode_punctuation(ch: str) -> bool:
    """Return whether ``ch`` is a CommonMark Unicode punctuation character.

    CommonMark defines that as any ASCII punctuation character or a character
    in Unicode general categories Pc–Ps. ASCII-only checks miss nothing in
    ``P*``; ``P*``-only checks miss Sc/Sm/Sk ASCII punct such as ``$``/``+``/``^``.
    """
    return ch in _COMMONMARK_ASCII_PUNCTUATION or unicodedata.category(ch).startswith("P")


def _markdown_emphasis_closer_is_valid(text: str, closer_start: int, opener: str) -> bool:
    """Return whether ``opener`` at ``closer_start`` is a usable emphasis closer.

    CommonMark closing delimiter runs must be right-flanking (not preceded by
    whitespace) and must not include backslash-escaped marker characters. Also
    reject when the run is longer than ``opener`` on either side.

    Underscore closers additionally follow CommonMark's intra-word rule: a
    ``_`` / ``__`` / ``___`` run cannot close when also left-flanking and not
    followed by Unicode punctuation. That covers alphanumeric neighbors and
    non-punctuation symbols such as emoji (PRRT_kwDOSJAM6s6bRy5w,
    PRRT_kwDOSJAM6s6bSs2f).
    """
    closer_end = closer_start + len(opener)
    if closer_start < 0 or closer_end > len(text):
        return False
    if text[closer_start:closer_end] != opener:
        return False
    if (
        closer_start > 0
        and text[closer_start - 1] == opener[0]
        and not _markdown_char_is_escaped(text, closer_start - 1)
    ):
        return False
    if closer_end < len(text) and text[closer_end] == opener[0]:
        return False
    if closer_start > 0 and text[closer_start - 1].isspace():
        return False
    if (
        opener[0] == "_"
        and closer_end < len(text)
        and not text[closer_end].isspace()
        and not _markdown_char_is_unicode_punctuation(text[closer_end])
    ):
        return False
    return not any(_markdown_char_is_escaped(text, i) for i in range(closer_start, closer_end))


def _markdown_emphasis_prefix_closer_is_valid(text: str, closer_start: int, opener: str) -> bool:
    """Return whether ``opener`` at ``closer_start`` closes a label-prefix wrap.

    Prefix closers sit at the start of the reason group. Require end-of-string or
    whitespace after the run so a reason that begins with the same ``*`` / ``_``
    markers (``:**committed``, ``:**<placeholder>``) is not treated as a closer.
    """
    if not _markdown_emphasis_closer_is_valid(text, closer_start, opener):
        return False
    after = closer_start + len(opener)
    return after >= len(text) or text[after].isspace()


def _verdict_reason_begins_with_emphasis_opener(reason: str, opener: str) -> bool:
    """Return whether ``reason`` begins with an exact ``opener`` delimiter run."""
    if not reason.startswith(opener):
        return False
    # A longer run (``***`` vs ``**``) is a different delimiter, not the opener.
    return not (len(reason) > len(opener) and reason[len(opener)] == opener[0])


def _markdown_emphasis_run_can_close(text: str, start: int, length: int, marker: str) -> bool:
    """Return whether a maximal ``marker`` run at ``start`` is right-flanking."""
    end = start + length
    if start < 0 or length < 1 or end > len(text):
        return False
    if text[start:end] != marker * length:
        return False
    # CommonMark: beginning and end of the line count as Unicode whitespace for
    # flanking, so a run at BOS is not right-flanking (PRRT_kwDOSJAM6s6bTi4S).
    if start == 0 or text[start - 1].isspace():
        return False
    # CommonMark right-flanking (2b): a run preceded by punctuation closes only
    # when also followed by EOS, whitespace, or punctuation. Punctuation-to-
    # alphanumeric runs (``.**x``) are opening-only (PRRT_kwDOSJAM6s6bShqh).
    if _markdown_char_is_unicode_punctuation(text[start - 1]):
        followed_ok = end >= len(text) or text[end].isspace()
        if not followed_ok and not _markdown_char_is_unicode_punctuation(text[end]):
            return False
    # Underscore: both-flanking runs cannot close unless followed by punctuation.
    # ``isalnum()`` alone misses Unicode symbols (emoji) that are neither
    # whitespace nor punctuation (PRRT_kwDOSJAM6s6bSs2f).
    if (
        marker == "_"
        and end < len(text)
        and not text[end].isspace()
        and not _markdown_char_is_unicode_punctuation(text[end])
    ):
        return False
    return not any(_markdown_char_is_escaped(text, i) for i in range(start, end))


def _markdown_emphasis_run_can_open(text: str, start: int, length: int, marker: str) -> bool:
    """Return whether a maximal ``marker`` run at ``start`` is left-flanking."""
    end = start + length
    if start < 0 or length < 1 or end > len(text):
        return False
    if text[start:end] != marker * length:
        return False
    # CommonMark: beginning and end of the line count as Unicode whitespace for
    # flanking, so a run at EOS is not left-flanking (PRRT_kwDOSJAM6s6bTBv4).
    if end >= len(text) or text[end].isspace():
        return False
    # CommonMark left-flanking (2b): a run followed by punctuation opens only
    # when also preceded by BOS, whitespace, or punctuation. Alphanumeric-to-
    # punctuation runs (``a**.``) are closing-only (PRRT_kwDOSJAM6s6bSOmb).
    if end < len(text) and _markdown_char_is_unicode_punctuation(text[end]):
        preceded_ok = start == 0 or text[start - 1].isspace()
        if not preceded_ok and not _markdown_char_is_unicode_punctuation(text[start - 1]):
            return False
    # Underscore: both-flanking runs cannot open unless preceded by punctuation.
    # Symmetric with the closer rule for non-alnum Unicode symbols
    # (PRRT_kwDOSJAM6s6bSs2f).
    if (
        marker == "_"
        and start > 0
        and not text[start - 1].isspace()
        and not _markdown_char_is_unicode_punctuation(text[start - 1])
    ):
        return False
    return not any(_markdown_char_is_escaped(text, i) for i in range(start, end))


def _emphasis_run_pair_blocked_by_multiple_of_three(
    opener_len: int,
    closer_len: int,
    closer_can_open: bool,
    opener_can_close: bool,
) -> bool:
    """Return whether CommonMark rule 9 blocks pairing these run lengths.

    Rule 9 applies when either delimiter can both open and close emphasis
    (PRRT_kwDOSJAM6s6bTW7t): checking only the closer misses both-flanking
    openers that must not pair with a closing-only run of complementary length.
    """
    if not closer_can_open and not opener_can_close:
        return False
    total = opener_len + closer_len
    return total % 3 == 0 and opener_len % 3 != 0 and closer_len % 3 != 0


def _advance_past_markdown_code_span(text: str, start: int) -> int:
    """Return index after a closed backtick code span, or after an unclosed opener.

    Caller must pass ``start`` at an unescaped backtick. CommonMark code spans
    use a run of N backticks as the opener; only a later run of the same length
    closes. Markers inside a closed span are literal (PRRT_kwDOSJAM6s6bShql). An
    unclosed opener run is itself literal, so scanning resumes after that run.
    """
    open_len = 1
    while start + open_len < len(text) and text[start + open_len] == "`":
        open_len += 1
    index = start + open_len
    while index < len(text):
        if text[index] != "`":
            index += 1
            continue
        close_len = 1
        while index + close_len < len(text) and text[index + close_len] == "`":
            close_len += 1
        if close_len == open_len:
            return index + close_len
        index += close_len
    return start + open_len


# CommonMark inline HTML tokens (spec §6.6). Attribute values reuse
# ``_HTML_TYPE7_ATTR`` so a quoted ``>`` / ``*`` / ``_`` inside an attribute does
# not truncate the tag or participate in emphasis pairing
# (PRRT_kwDOSJAM6s6bTBv6).
_MARKDOWN_INLINE_HTML_TOKEN = re.compile(
    rf"<(?:[A-Za-z][A-Za-z0-9-]*{_HTML_TYPE7_ATTR}*\s*/?>|"
    r"/[A-Za-z][A-Za-z0-9-]*\s*>|"
    r"!--(?:-?>|.*?-->)|"
    r"\?(?:.*?\?)>|"
    r"![A-Z]+\s+[^>]*>|"
    r"!\[CDATA\[.*?\]\]>)",
    re.DOTALL,
)


def _advance_past_markdown_inline_html(text: str, start: int) -> int:
    """Return index after a CommonMark inline HTML token, or ``start`` if none.

    Caller must pass ``start`` at an unescaped ``<``. Incomplete or non-matching
    markup is left alone so the scanner advances one character and keeps treating
    subsequent ``*`` / ``_`` as emphasis (PRRT_kwDOSJAM6s6bTBv6).
    """
    match = _MARKDOWN_INLINE_HTML_TOKEN.match(text, start)
    if match is None:
        return start
    return match.end()


# CommonMark URI autolinks (§6.5): scheme (2–32 chars) + ``:`` + URI chars
# excluding ASCII controls/space and ``<>``. Email autolinks use the HTML5
# address production. Interior ``*`` / ``_`` are literal link content
# (PRRT_kwDOSJAM6s6bTgB-).
_MARKDOWN_URI_AUTOLINK = re.compile(r"<[A-Za-z][A-Za-z0-9+.-]{1,31}:[^<>\x00-\x20\x7f]*>")
_MARKDOWN_EMAIL_AUTOLINK = re.compile(
    r"<[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*>"
)


def _advance_past_markdown_autolink(text: str, start: int) -> int:
    """Return index after a CommonMark URI/email autolink, or ``start`` if none.

    Caller must pass ``start`` at an unescaped ``<``. Incomplete or non-matching
    angle-bracket text is left alone so subsequent ``*`` / ``_`` stay emphasis
    (PRRT_kwDOSJAM6s6bTgB-).
    """
    match = _MARKDOWN_URI_AUTOLINK.match(text, start)
    if match is None:
        match = _MARKDOWN_EMAIL_AUTOLINK.match(text, start)
    if match is None:
        return start
    return match.end()


def _advance_past_markdown_link_destination(text: str, start: int) -> int:
    """Return index after a Markdown inline link ``(…)``, or ``start`` if none.

    Caller must pass ``start`` at ``(`` immediately after a label closer ``]``
    (no intervening whitespace — CommonMark inline links require adjacency;
    PRRT_kwDOSJAM6s6bTtr6). Only CommonMark-valid link destinations are opaque:
    angle-bracket form may contain spaces; the non-bracket form must be
    nonempty and free of ASCII space/controls, with parentheses only when
    balanced or escaped. A non-bracket destination may itself begin with ``(``
    when that ``(`` is adjacent to the link opener (no leading title-separating
    whitespace); leading whitespace before ``(`` selects a parenthesized title
    instead (PRRT_kwDOSJAM6s6bUx1F). An optional quoted/parenthesized title may
    follow, but CommonMark requires whitespace between destination and title —
    a glued title after ``>`` is not opaque (PRRT_kwDOSJAM6s6bTvK5).
    Parenthesized titles end at the first unescaped ``)`` and must not contain
    an unescaped ``(`` (CommonMark §6.3; PRRT_kwDOSJAM6s6bUOZ9).
    ``*`` / ``_`` inside a valid destination or title are literal and must not
    participate in emphasis pairing (PRRT_kwDOSJAM6s6bTLZq). Invalid
    destinations (whitespace in non-bracket form, newline, unclosed ``)``,
    leftover junk) leave ``start`` unchanged so mid-span markers remain
    emphasis (PRRT_kwDOSJAM6s6bTgB6). Non-bracket backslash skips only apply
    when the successor is CommonMark-escapable ASCII punctuation — a
    backslash before ASCII space does not escape that space
    (PRRT_kwDOSJAM6s6bT50A).
    """
    if start >= len(text) or text[start] != "(":
        return start
    n = len(text)
    index = start + 1

    def _skip_link_ws(i: int) -> int:
        while i < n and text[i] in " \t":
            i += 1
        return i

    index = _skip_link_ws(index)
    if index >= n or text[index] == "\n":
        return start

    # Optional destination (CommonMark §6.3).
    saw_destination = False
    # ``(`` after leading whitespace is a parenthesized title (empty dest);
    # ``(`` adjacent to the link opener is a destination that begins with
    # balanced parentheses (PRRT_kwDOSJAM6s6bUx1F).
    leading_paren_is_destination = index == start + 1
    if text[index] == "<":
        index += 1
        while index < n:
            ch = text[index]
            if ch == "\n":
                return start
            if ch == "\\" and index + 1 < n:
                index += 2
                continue
            if ch == ">":
                index += 1
                break
            if ch == "<":
                return start
            index += 1
        else:
            return start
        saw_destination = True
    elif text[index] not in ")\"'" and (text[index] != "(" or leading_paren_is_destination):
        # Non-bracket destination: no ASCII space/controls; balanced parens.
        depth = 0
        dest_chars = 0
        while index < n:
            ch = text[index]
            if ch == "\n":
                return start
            if ch in " \t" and depth == 0:
                break
            code = ord(ch)
            if code <= 0x20 or code == 0x7F:
                return start
            if ch == "\\" and index + 1 < n and text[index + 1] in _COMMONMARK_ASCII_PUNCTUATION:
                index += 2
                dest_chars += 1
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            index += 1
            dest_chars += 1
        if dest_chars == 0 or depth != 0:
            return start
        saw_destination = True

    after_dest = index
    index = _skip_link_ws(index)
    if index >= n or text[index] == "\n":
        return start

    if text[index] == ")":
        return index + 1

    # Title requires whitespace after a destination (PRRT_kwDOSJAM6s6bTvK5).
    if saw_destination and index == after_dest:
        return start

    # Optional link title (double-quote, single-quote, or parentheses).
    if text[index] == '"':
        closer = '"'
        index += 1
        while index < n:
            ch = text[index]
            if ch == "\n":
                return start
            if ch == "\\" and index + 1 < n:
                index += 2
                continue
            if ch == closer:
                index += 1
                break
            index += 1
        else:
            return start
    elif text[index] == "'":
        closer = "'"
        index += 1
        while index < n:
            ch = text[index]
            if ch == "\n":
                return start
            if ch == "\\" and index + 1 < n:
                index += 2
                continue
            if ch == closer:
                index += 1
                break
            index += 1
        else:
            return start
    elif text[index] == "(":
        # CommonMark §6.3: parenthesized titles contain no unescaped ``(`` /
        # ``)``; close at the first unescaped ``)`` (PRRT_kwDOSJAM6s6bUOZ9).
        index += 1
        while index < n:
            ch = text[index]
            if ch == "\n":
                return start
            if ch == "\\" and index + 1 < n:
                index += 2
                continue
            if ch == "(":
                return start
            if ch == ")":
                index += 1
                break
            index += 1
        else:
            return start
    else:
        # Leftover content after an invalid/partial destination (e.g. ``foo **bar``).
        return start

    index = _skip_link_ws(index)
    if index < n and text[index] == ")":
        return index + 1
    return start


def _advance_past_markdown_link_reference_label(text: str, start: int) -> int:
    """Return index after a CommonMark link reference label, or ``start`` if none.

    Caller must pass ``start`` at ``[`` immediately after a label closer ``]``
    (full or collapsed reference form; no intervening whitespace). Label rules
    follow CommonMark §6.3: ends at the first unescaped ``]``; interior
    unescaped ``[`` is invalid; at most 999 characters inside; nonempty labels
    need at least one non-whitespace character; empty ``[]`` is the collapsed
    form. Callers must only treat the label as opaque when it resolves to a
    document reference definition (PRRT_kwDOSJAM6s6bUCMm); syntactic validity
    alone is not enough (PRRT_kwDOSJAM6s6bT50C). Invalid or incomplete labels
    leave ``start`` unchanged so markers remain emphasis.
    """
    if start >= len(text) or text[start] != "[":
        return start
    n = len(text)
    index = start + 1
    content_len = 0
    saw_non_ws = False
    while index < n:
        ch = text[index]
        if ch == "\n":
            return start
        if ch == "\\" and index + 1 < n:
            index += 2
            content_len += 1
            saw_non_ws = True
            if content_len > 999:
                return start
            continue
        if ch == "[":
            return start
        if ch == "]":
            if content_len > 0 and not saw_non_ws:
                return start
            return index + 1
        if ch not in " \t":
            saw_non_ws = True
        content_len += 1
        if content_len > 999:
            return start
        index += 1
    return start


def _markdown_normalize_link_reference_label(label: str) -> str:
    """Normalize a CommonMark link reference label for definition matching."""
    unescaped = _COMMONMARK_BACKSLASH_ESCAPED_PUNCT.sub(r"\1", label)
    return re.sub(r"[ \t\r\n]+", " ", unescaped.strip()).casefold()


def _match_markdown_reference_definition_line(line: str) -> str | None:
    """Return a normalized label when ``line`` is a single-line reference definition."""
    index = 0
    while index < len(line) and index < 3 and line[index] == " ":
        index += 1
    if index >= len(line) or line[index] != "[":
        return None
    label_end = _advance_past_markdown_link_reference_label(line, index)
    if label_end <= index:
        return None
    raw_label = line[index + 1 : label_end - 1]
    if raw_label == "":
        return None
    if label_end >= len(line) or line[label_end] != ":":
        return None
    rest = line[label_end + 1 :]
    cursor = 0
    while cursor < len(rest) and rest[cursor] in " \t":
        cursor += 1
    if cursor >= len(rest):
        return None
    if rest[cursor] == "<":
        close = rest.find(">", cursor + 1)
        if close < 0 or any(ch in rest[cursor + 1 : close] for ch in "\n<"):
            return None
        cursor = close + 1
    else:
        # Non-angle destination: nonempty, no ASCII space/controls, parentheses
        # only when balanced or escaped (CommonMark §4.7 / §6.3). ``\S+`` would
        # accept ``foo(bar`` and wrongly resolve ``[details][issue**ref]`` in a
        # malformed emphasized verdict (PRRT_kwDOSJAM6s6bVBWV).
        depth = 0
        dest_chars = 0
        while cursor < len(rest):
            ch = rest[cursor]
            if ch in " \t" and depth == 0:
                break
            code = ord(ch)
            if code <= 0x20 or code == 0x7F:
                return None
            if (
                ch == "\\"
                and cursor + 1 < len(rest)
                and rest[cursor + 1] in _COMMONMARK_ASCII_PUNCTUATION
            ):
                cursor += 2
                dest_chars += 1
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    return None
                depth -= 1
            cursor += 1
            dest_chars += 1
        if dest_chars == 0 or depth != 0:
            return None
    title_ws = cursor
    while cursor < len(rest) and rest[cursor] in " \t":
        cursor += 1
    if cursor < len(rest):
        if title_ws == cursor:
            return None
        title_opener = rest[cursor]
        if title_opener not in "\"'(":
            return None
        title_closer = ")" if title_opener == "(" else title_opener
        cursor += 1
        while cursor < len(rest):
            ch = rest[cursor]
            if ch == "\\" and cursor + 1 < len(rest):
                cursor += 2
                continue
            if ch == title_closer:
                cursor += 1
                break
            cursor += 1
        else:
            return None
        while cursor < len(rest) and rest[cursor] in " \t":
            cursor += 1
        if cursor < len(rest):
            return None
    return _markdown_normalize_link_reference_label(raw_label)


def _markdown_reference_definition_spans(
    text: str,
    *,
    bos_is_block_boundary: bool = True,
) -> list[tuple[int, int, str]]:
    """Return ``(start, end, normalized_label)`` for block-level reference definitions.

    Definitions are recognized only at block boundaries (beginning of string or
    after a blank line), matching CommonMark's rule that they cannot interrupt a
    paragraph. Consecutive definitions may follow each other. First definition
    for a normalized label wins.

    Lines inside inactive Markdown/HTML block regions (fenced code, indented
    code, raw HTML example/comment/type-3–7 blocks) are skipped so quoted
    ``[label]: dest`` examples cannot resolve emphasis on a real verdict
    (PRRT_kwDOSJAM6s6bVBWU). Shielded lines do not update the blank-line
    boundary cursor.

    Set ``bos_is_block_boundary=False`` when ``text`` is a mid-paragraph fragment
    (for example a verdict reason after ``AWF-VERDICT: LABEL: ``) so a
    reason-leading ``[label]: dest`` is not treated as a definition
    (PRRT_kwDOSJAM6s6bUPZ6).
    """
    spans: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    shielded_starts = _markdown_shielded_block_line_starts(text)
    offset = 0
    prev_blank = bos_is_block_boundary
    length = len(text)
    while offset <= length:
        if offset == length:
            break
        nl = text.find("\n", offset)
        line_end = length if nl < 0 else nl
        line = text[offset:line_end]
        if line.endswith("\r"):
            line = line[:-1]
        next_offset = length if nl < 0 else nl + 1
        if offset in shielded_starts:
            # Inactive regions are not definition hosts and must not create or
            # clear block boundaries for surrounding active Markdown.
            offset = next_offset
            continue
        is_blank = all(ch in " \t" for ch in line)
        if prev_blank and not is_blank:
            label = _match_markdown_reference_definition_line(line)
            if label is not None:
                if label not in seen:
                    seen.add(label)
                    spans.append((offset, next_offset, label))
                prev_blank = True
                offset = next_offset
                continue
        prev_blank = is_blank
        offset = next_offset
    return spans


def _verdict_reason_trailing_emphasis_is_balanced(
    reason: str,
    opener: str,
    *,
    seed_outer_opener: bool = False,
    extra_reference_definitions: Set[str] | None = None,
) -> bool:
    """Return whether a trailing closer pairs inside ``reason``.

    After a prefix-only emphasis strip, a candidate may still end on a valid
    same-delimiter closer. Track left-flanking openers and right-flanking
    closers so a separately balanced span (``This is **expected**``) is kept,
    while unmatched leftovers (``rationale**``) and even counts of closing-only
    runs (``rationale** more**``) are rejected (PRRT_kwDOSJAM6s6bRROQ,
    PRRT_kwDOSJAM6s6bRfTo).

    When ``seed_outer_opener`` is True, the scan models a whole-line wrapper:
    the line-leading delimiter is pushed as a BOS opening-only seed, and the
    return value is whether the trailing closer fully consumes that seed
    (PRRT_kwDOSJAM6s6bUx1A). Without a seed, an earlier closing-only run is
    ignored (empty stack) and a trailing unclaimed closer would wrongly look
    like a safe whole-line strip.

    CommonMark may split a longer same-character run across shorter closers
    (``***lead* rest**`` consumes one star with ``*`` and two with the trailing
    ``**``). Pair across all same-character run lengths so a trailing
    wrapper-length closer stolen by that partial match is detected
    (PRRT_kwDOSJAM6s6bR2FM). Leftover unmatched opener runs that would
    literalize (``***lead* and **done**``) do not block ``trailing_paired``.
    Rule 9 must consult both delimiter sides: a both-flanking opener blocked
    against a closing-only complementary-length run (``a*x**``, ``x**lead*``)
    must not claim the trailing wrapper closer (PRRT_kwDOSJAM6s6bTW7t). When
    rule 9 blocks the nearest opener, CommonMark continues searching earlier
    compatible openers (``reason **lower a*b**`` pairs the trailing ``**`` with
    the earlier ``**``, stealing the wrapper closer — PRRT_kwDOSJAM6s6bTtr5).
    Reason-leading runs at BOS are opening-only (BOS is whitespace), so
    complementary lengths such as ``*foo**`` and ``**lead* rest*`` do steal
    and reject a false whole-line wrap (PRRT_kwDOSJAM6s6bTi4S).

    Closed Markdown code spans are opaque: ``*`` / ``_`` runs inside them are
    literal content and must not claim the trailing wrapper closer
    (PRRT_kwDOSJAM6s6bShql). Backslash-escaped backticks are literal openers
    under CommonMark and must not start that skip — otherwise a later real
    tick can swallow mid-reason stealers (PRRT_kwDOSJAM6s6bSsnj). Inline HTML
    tokens are likewise opaque so attribute stars (``title="**"``) do not steal
    the outer closer (PRRT_kwDOSJAM6s6bTBv6). Backslash-escaped ``\\<`` is not an
    HTML opener — attribute markers remain emphasis and can steal the closer
    (PRRT_kwDOSJAM6s6bTLZk). URI and email autolinks are opaque so stars inside
    ``<https://…/**>`` or ``<user**name@…>`` do not steal the closer
    (PRRT_kwDOSJAM6s6bTgB-). Incomplete autolinks leave markers as emphasis.
    Inline link destinations (``](…)``) are opaque so
    URL stars do not steal the closer (PRRT_kwDOSJAM6s6bTLZq), but only when an
    active unmatched ``[`` label opener is present — a bare ``](…)`` is not a
    Markdown link (PRRT_kwDOSJAM6s6bTW7q). ``]`` must be followed immediately by
    ``(`` — intervening whitespace is not a CommonMark inline link, so stars
    inside the parentheses remain emphasis (PRRT_kwDOSJAM6s6bTtr6).
    Parenthesized link titles reject unescaped nested ``(`` so markers in
    ``[link](url (a(**bar)))`` remain emphasis (PRRT_kwDOSJAM6s6bUOZ9). A
    destination that begins with balanced ``(`` adjacent to the link opener
    (``[x]((a(**b)))``) stays opaque; leading whitespace before ``(`` selects
    the title parser instead (PRRT_kwDOSJAM6s6bUx1F).
    Links cannot contain links: after an inline/reference link is formed, earlier
    link (non-image) label openers are deactivated. An inactive opener still
    matches a later ``]`` as literal brackets, so a nested
    ``[outer [inner](url)](foo**bar)`` leaves destination stars as emphasis that
    can steal the wrapper closer (PRRT_kwDOSJAM6s6bUCMq). Image openers stay
    active (images may contain links; links may contain images).
    When a link or image is formed, CommonMark processes emphasis inside the
    label in isolation: restore the opener stack to its state at the matching
    ``[`` so a label closer cannot pair with an opener before the link
    (``**see [x**](url) rest**`` — PRRT_kwDOSJAM6s6bUs3M). Non-formed brackets
    keep free pairing; inactive ``]`` matches do not restore.
    Full/collapsed reference labels (``][…]`` / ``][]``) are opaque only when the
    label resolves to a block-level reference definition in the scanned text
    or in ``extra_reference_definitions`` from the complete stdout document
    (PRRT_kwDOSJAM6s6bUCMm / PRRT_kwDOSJAM6s6bU8Tf); otherwise stars in the ref
    id remain emphasis and may steal the closer. Syntactic label shape alone is
    not enough (PRRT_kwDOSJAM6s6bT50C). Whitespace between ``]`` and ``[`` is not
    a full reference link. Block-level reference definition lines themselves
    are skipped so definition-label markers do not participate in pairing.
    Non-angle-bracket destinations with ASCII spaces are not links; their
    markers remain emphasis (PRRT_kwDOSJAM6s6bTgB6). An angle-bracket
    destination glued to a quoted/parenthesized title (no whitespace) is
    likewise invalid, so title markers remain emphasis
    (PRRT_kwDOSJAM6s6bTvK5).
    Reason fragments are mid-paragraph (after ``AWF-VERDICT: LABEL: ``), so
    reference definitions are recognized only after blank lines inside the
    reason — not at reason BOS. Otherwise a reason-leading ``[label]: dest``
    with emphasis in the label is skipped while a valid destination absorbs
    the trailing wrapper closer and whole-line emphasis fails open
    (PRRT_kwDOSJAM6s6bUPZ6). Non-angle destinations must use balanced or
    escaped parentheses; unbalanced ``foo(bar`` is not a definition
    (PRRT_kwDOSJAM6s6bVBWV).
    """
    closer_start = len(reason) - len(opener)
    if not _markdown_emphasis_closer_is_valid(reason, closer_start, opener):
        return False
    marker = opener[0]
    # Each stack entry is (remaining_len, run_can_close, is_outer_seed) so rule 9
    # can consult opener-side both-flanking when the closer is closing-only
    # (PRRT_kwDOSJAM6s6bTW7t). ``is_outer_seed`` marks the line-leading wrapper
    # when ``seed_outer_opener`` is set (PRRT_kwDOSJAM6s6bUx1A).
    open_stack: list[tuple[int, bool, bool]] = []
    if seed_outer_opener:
        # Line-leading opener is at BOS (whitespace context): opening-only.
        open_stack.append((len(opener), False, True))
    trailing_paired = False
    seed_closed_by_trailing = 0
    # (open_at, is_image, active, stack_snapshot) — CommonMark deactivates earlier
    # link openers when a link (not image) is formed so links cannot nest
    # (PRRT_kwDOSJAM6s6bUCMq). On formation, restore open_stack to the snapshot
    # taken at ``[`` so label-internal emphasis is isolated
    # (PRRT_kwDOSJAM6s6bUs3M). Persist the live stack as a linked tip so appends
    # share prior nodes: many unmatched ``[`` after a large opener stack, and
    # alternating unmatched opener + ``[``, stay linear (PRRT_kwDOSJAM6s6bU4CA,
    # PRRT_kwDOSJAM6s6bU8Th). Mid-stack mutations invalidate the tip.
    label_opens: list[tuple[int, bool, bool, _OpenStackSnap | None]] = []
    shared_snap_tip: _OpenStackSnap | None = None
    snap_len = 0
    snap_valid = True

    def _invalidate_open_stack_snap() -> None:
        nonlocal snap_valid
        snap_valid = False

    def _frozen_open_stack() -> _OpenStackSnap | None:
        nonlocal shared_snap_tip, snap_len, snap_valid
        stack_len = len(open_stack)
        if snap_valid and snap_len == stack_len:
            return shared_snap_tip
        if snap_valid and snap_len < stack_len:
            for idx in range(snap_len, stack_len):
                shared_snap_tip = _OpenStackSnap(open_stack[idx], shared_snap_tip)
            snap_len = stack_len
            return shared_snap_tip
        shared_snap_tip = None
        for entry in open_stack:
            shared_snap_tip = _OpenStackSnap(entry, shared_snap_tip)
        snap_len = stack_len
        snap_valid = True
        return shared_snap_tip

    def _restore_open_stack(snapshot: _OpenStackSnap | None) -> None:
        nonlocal shared_snap_tip, snap_len, snap_valid
        entries: list[tuple[int, bool, bool]] = []
        node = snapshot
        while node is not None:
            entries.append(node.entry)
            node = node.prev
        entries.reverse()
        open_stack[:] = entries
        shared_snap_tip = snapshot
        snap_len = len(open_stack)
        snap_valid = True

    # Reason is a mid-paragraph extract; do not treat BOS as a definition
    # boundary (PRRT_kwDOSJAM6s6bUPZ6). Document-level definitions from the
    # complete stdout (passed via ``extra_reference_definitions``) resolve
    # full/collapsed reference labels whose ``[label]: dest`` lines sit after
    # the verdict line and are therefore invisible to a line-only scan
    # (PRRT_kwDOSJAM6s6bU8Tf).
    def_spans = _markdown_reference_definition_spans(reason, bos_is_block_boundary=False)
    definitions = {label for _, _, label in def_spans}
    if extra_reference_definitions:
        definitions.update(extra_reference_definitions)
    i = 0
    while i < len(reason):
        jumped_def = False
        for def_start, def_end, _ in def_spans:
            if i == def_start:
                i = def_end
                jumped_def = True
                break
        if jumped_def:
            continue
        if reason[i] == "`" and not _markdown_char_is_escaped(reason, i):
            i = _advance_past_markdown_code_span(reason, i)
            continue
        if reason[i] == "<" and not _markdown_char_is_escaped(reason, i):
            next_i = _advance_past_markdown_inline_html(reason, i)
            if next_i > i:
                i = next_i
                continue
            next_i = _advance_past_markdown_autolink(reason, i)
            if next_i > i:
                i = next_i
                continue
        if reason[i] == "[" and not _markdown_char_is_escaped(reason, i):
            is_image = (
                i > 0 and reason[i - 1] == "!" and not _markdown_char_is_escaped(reason, i - 1)
            )
            label_opens.append((i, is_image, True, _frozen_open_stack()))
            i += 1
            continue
        if reason[i] == "]" and not _markdown_char_is_escaped(reason, i):
            if label_opens:
                open_at, is_image, active, stack_snapshot = label_opens.pop()
                if not active:
                    # Inactive opener matches ``]`` as literal brackets only.
                    i += 1
                    continue
                link_text = reason[open_at + 1 : i]
                # CommonMark: destination ``(`` or reference label ``[`` must
                # immediately follow ``]``.
                k = i + 1
                formed = False
                if k < len(reason) and reason[k] == "(":
                    next_i = _advance_past_markdown_link_destination(reason, k)
                    if next_i > k:
                        i = next_i
                        formed = True
                elif k < len(reason) and reason[k] == "[":
                    next_i = _advance_past_markdown_link_reference_label(reason, k)
                    if next_i > k:
                        ref_body = reason[k + 1 : next_i - 1]
                        resolve_label = link_text if ref_body == "" else ref_body
                        if _markdown_normalize_link_reference_label(resolve_label) in definitions:
                            i = next_i
                            formed = True
                if formed:
                    # Isolate label emphasis from outer pairing
                    # (PRRT_kwDOSJAM6s6bUs3M).
                    _restore_open_stack(stack_snapshot)
                    if not is_image:
                        # Deactivate earlier link openers (not images).
                        for idx, (pos, img, _act, snap) in enumerate(label_opens):
                            if not img:
                                label_opens[idx] = (pos, img, False, snap)
                    continue
            i += 1
            continue
        if reason[i] != marker or _markdown_char_is_escaped(reason, i):
            i += 1
            continue
        # Consecutive markers after an unescaped start cannot be escaped
        # (a backslash would interrupt the run).
        j = i
        while j < len(reason) and reason[j] == marker:
            j += 1
        length = j - i
        can_close = _markdown_emphasis_run_can_close(reason, i, length, marker)
        can_open = _markdown_emphasis_run_can_open(reason, i, length, marker)
        is_trailing = j == len(reason)
        remaining = length
        if can_close:
            # Search nearest-first; rule-of-three skips do not stop the search
            # (PRRT_kwDOSJAM6s6bTtr5). Matched earlier openers literalize any
            # skipped openers above them.
            stack_idx = len(open_stack) - 1
            while remaining > 0 and stack_idx >= 0:
                opener_len, opener_can_close, is_outer_seed = open_stack[stack_idx]
                if _emphasis_run_pair_blocked_by_multiple_of_three(
                    opener_len, remaining, can_open, opener_can_close
                ):
                    stack_idx -= 1
                    continue
                del open_stack[stack_idx + 1 :]
                # Prefer strong (2) when both runs still have at least two.
                consumed = 2 if opener_len >= 2 and remaining >= 2 else 1
                open_stack[stack_idx] = (opener_len - consumed, opener_can_close, is_outer_seed)
                remaining -= consumed
                if is_trailing and is_outer_seed:
                    seed_closed_by_trailing += consumed
                if open_stack[stack_idx][0] == 0:
                    open_stack.pop(stack_idx)
                    stack_idx -= 1
                _invalidate_open_stack_snap()
                if is_trailing:
                    trailing_paired = True
        if remaining > 0 and can_open:
            open_stack.append((remaining, can_close, False))
            # Append-only: leave snap_valid so freeze extends the tip
            # (PRRT_kwDOSJAM6s6bU8Th).
        i = j
    if seed_outer_opener:
        return seed_closed_by_trailing == len(opener)
    return trailing_paired


def _normalize_markdown_emphasized_verdict_line(
    line: str,
    *,
    extra_reference_definitions: Set[str] | None = None,
) -> str | None:
    """Return a canonical verdict wrapped in balanced top-level emphasis.

    Agents commonly emphasize either the whole verdict line or only the
    ``AWF-VERDICT: LABEL:`` prefix. Accept only matching one-to-three character
    ``*`` / ``_`` delimiters and require the normalized line to satisfy the
    canonical verdict grammar. Nested containers and prose wrappers remain
    untouched and therefore continue to fail closed.

    ``extra_reference_definitions`` carries normalized labels from the complete
    stdout document so full/collapsed reference links whose definitions appear
    on later lines still resolve during line-level normalization
    (PRRT_kwDOSJAM6s6bU8Tf).

    A prefix closer plus a later unmatched same-delimiter closer (for example
    ``**AWF-VERDICT: FALSE POSITIVE:** rationale**``) is malformed: do not
    absorb leftover markers into the reason, and do not fall back to a
    whole-line strip that would leave the prefix closer inside the reason.
    Closing-only mid-reason runs that leave an even delimiter count (for
    example ``**AWF-VERDICT: FALSE POSITIVE:** rationale** more**``) are
    likewise rejected — balance tracks opener/closer state, not run parity
    (PRRT_kwDOSJAM6s6bRfTo).

    A prefix closer plus a separately balanced reason span that ends on the
    same delimiter (``**AWF-VERDICT: FALSE POSITIVE:** This is **expected**``)
    remains valid: the trailing closer belongs to the reason, not a second
    line wrapper (PRRT_kwDOSJAM6s6bRROQ).

    Whole-line stripping must also fail closed when the remaining reason begins
    with the same opener run: the trailing closer then belongs to reason
    emphasis (or a placeholder echo), not the line wrapper
    (PRRT_kwDOSJAM6s6bQqbC). The same applies when a mid-reason same-delimiter
    opener steals the trailing closer (``**… rationale **unclosed**``) — pair
    across the whole candidate before accepting the strip
    (PRRT_kwDOSJAM6s6bRrWv). Closing-only mid-reason runs that would close the
    line-leading wrapper (``**… rationale** more**``) are likewise rejected by
    seeding that outer opener into the balance scan (PRRT_kwDOSJAM6s6bUx1A).
    Longer same-character runs that CommonMark splits
    across shorter closers (``**… ***lead* rest**``) likewise steal the trailing
    wrapper closer (PRRT_kwDOSJAM6s6bR2FM). Underscore balance checks use the
    reason span only and ignore word-internal ``_`` so ``NEEDS_HUMAN`` /
    snake_case reasons do not falsely reject a valid whole-line ``_…_`` wrap
    (PRRT_kwDOSJAM6s6bRy5w). Inline code-span markers are opaque to that
    balance scan so ``**… see `**`**`` stays a valid whole-line wrap
    (PRRT_kwDOSJAM6s6bShql). Escaped backticks are not code-span openers, so
    ``\\` **unclosed`x**`` still fails closed (PRRT_kwDOSJAM6s6bSsnj). Inline
    HTML tokens are similarly opaque so attribute stars do not steal the
    closer (PRRT_kwDOSJAM6s6bTBv6). Escaped ``\\<`` is not an HTML token, so
    ``\\<span title="**">x**`` still fails closed (PRRT_kwDOSJAM6s6bTLZk).
    URI and email autolinks are opaque so
    ``**… see <https://example.test/a**b>**`` stays a valid whole-line wrap
    (PRRT_kwDOSJAM6s6bTgB-).     Inline link destinations are opaque so
    ``**… see [link](foo**bar)**`` stays a valid whole-line wrap
    (PRRT_kwDOSJAM6s6bTLZq).     Full reference labels are opaque only when the
    label resolves to a document definition, so undefined
    ``**… see [details][issue**ref]**`` fails closed (PRRT_kwDOSJAM6s6bUCMm).
    Callers that normalize one stdout line at a time must pass
    ``extra_reference_definitions`` scanned from the complete document so a
    later ``[issue**ref]: /url`` still resolves (PRRT_kwDOSJAM6s6bU8Tf).
    Nested links deactivate the outer opener, so
    ``**… see [outer [inner](url)](foo**bar)**`` likewise fails closed
    (PRRT_kwDOSJAM6s6bUCMq). Formed-link labels isolate emphasis, so
    ``**… reason **see [x**](url) rest**`` fails closed when the trailing
    closer pairs with the mid-reason opener (PRRT_kwDOSJAM6s6bUs3M). A bare
    unmatched ``](…)``
    is not a link, so destination stars still steal the closer
    (PRRT_kwDOSJAM6s6bTW7q). Whitespace between ``]`` and ``(`` is not an
    inline link (``[link] (foo**bar)``), so markers steal the closer
    (PRRT_kwDOSJAM6s6bTtr6). Invalid destinations with whitespace
    (``[link](foo **bar)``) likewise leave markers as emphasis
    (PRRT_kwDOSJAM6s6bTgB6). Reason-leading ``[label]: dest`` lookalikes are
    not block definitions in the parent paragraph, so label emphasis still
    steals the trailing whole-line closer (PRRT_kwDOSJAM6s6bUPZ6).
    """
    emphasis_match = _MARKDOWN_EMPHASIS_PREFIX.match(line)
    if emphasis_match is None:
        return None
    opener = emphasis_match.group(0)
    inner = line[len(opener) :]

    prefix_closer_valid = False
    marker_match = _AWF_VERDICT.match(inner)
    if marker_match is not None:
        reason_start = marker_match.start("reason")
        if _markdown_emphasis_prefix_closer_is_valid(inner, reason_start, opener):
            prefix_closer_valid = True
            candidate = inner[:reason_start] + inner[reason_start + len(opener) :]
            matched = _AWF_VERDICT.fullmatch(candidate)
            if matched is not None:
                trailing_closer_start = len(candidate) - len(opener)
                trailing_is_closer = _markdown_emphasis_closer_is_valid(
                    candidate, trailing_closer_start, opener
                )
                # Unmatched leftover closer ⇒ reject; balanced reason span ⇒ keep.
                if not trailing_is_closer or _verdict_reason_trailing_emphasis_is_balanced(
                    matched.group("reason"),
                    opener,
                    extra_reference_definitions=extra_reference_definitions,
                ):
                    return candidate

    closer_start = len(inner) - len(opener)
    if _markdown_emphasis_closer_is_valid(inner, closer_start, opener):
        # Prefix closer plus unmatched trailing closer ⇒ malformed emphasis.
        if prefix_closer_valid:
            return None
        candidate = inner[:closer_start]
        matched = _AWF_VERDICT.fullmatch(candidate)
        if matched is None:
            return None
        # Trailing closer paired with a reason-leading same run — not whole-line.
        if _verdict_reason_begins_with_emphasis_opener(matched.group("reason"), opener):
            return None
        # Mid-reason opener that claims the trailing closer leaves the line
        # wrapper unbalanced — reject before stripping (PRRT_kwDOSJAM6s6bRrWv).
        # Scope to reason + trailing closer: label tokens such as NEEDS_HUMAN
        # must not participate (PRRT_kwDOSJAM6s6bRy5w). Seed the line-leading
        # opener so an earlier closing-only run cannot be ignored while the
        # trailing delimiter looks unclaimed (PRRT_kwDOSJAM6s6bUx1A).
        reason_with_trailing = f"{matched.group('reason')}{opener}"
        if not _verdict_reason_trailing_emphasis_is_balanced(
            reason_with_trailing,
            opener,
            seed_outer_opener=True,
            extra_reference_definitions=extra_reference_definitions,
        ):
            return None
        return candidate
    return None
