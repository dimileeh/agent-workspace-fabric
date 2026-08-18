"""ASCII quote / delimiter scanning for same-line AWF-VERDICT markers."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Explicit self-correction before a later same-line marker
# (``…; correction: AWF-VERDICT: …`` / ``…; corrected to: AWF-VERDICT: …``).
# Bare past-participle ``corrected:`` is ordinary reason prose and must not
# split (PRRT_kwDOSJAM6s6Znq6K). Compound hyphenations like ``self-correction:``
# must not count either — the boundary hyphen would otherwise clear a
# NEEDS_HUMAN hard block (PRRT_kwDOSJAM6s6ZnuQ0).
_EXPLICIT_VERDICT_CORRECTION_SEPARATOR = re.compile(
    r"(?is)(?:^|[\s;,.:—–]|(?<![A-Za-z0-9])-)(?:correction|corrected\s+to)\s*:\s*\Z"
)

# \Z-anchored correction probes only need a short trailing window. Searching the
# full prefix per marker would stay quadratic after the delimiter scan fix
# (PRRT_kwDOSJAM6s6ZpHXP). Oversized gaps still fail closed (stay absorbed).
_EXPLICIT_VERDICT_CORRECTION_SEARCH_WINDOW = 256


def _prefix_has_explicit_verdict_correction_separator(verdict_line: str, match_start: int) -> bool:
    """Return whether the prefix before ``match_start`` ends with a correction separator."""
    if match_start <= 0:
        return False
    start = max(0, match_start - _EXPLICIT_VERDICT_CORRECTION_SEARCH_WINDOW)
    # ``endpos`` makes ``\Z`` match at ``match_start`` without copying the prefix.
    return bool(_EXPLICIT_VERDICT_CORRECTION_SEPARATOR.search(verdict_line, start, match_start))


@dataclass(slots=True)
class _AwfVerdictDelimiterState:
    """Mutable quote/code-span cursor for linear same-line marker scans."""

    inside_ascii_double: bool = False
    inside_ascii_single: bool = False
    inside_backtick: bool = False
    backtick_open_len: int = 0
    inside_curly_double: bool = False
    inside_curly_single: bool = False
    last_close_end: int = -1
    cursor: int = 0

    @property
    def inside_quote_or_code(self) -> bool:
        return (
            self.inside_ascii_double
            or self.inside_ascii_single
            or self.inside_backtick
            or self.inside_curly_double
            or self.inside_curly_single
        )


def _advance_awf_verdict_delimiter_state(
    verdict_line: str, state: _AwfVerdictDelimiterState, until: int
) -> None:
    """Advance ``state`` by scanning ``verdict_line[state.cursor:until]``.

    Callers must pass non-decreasing ``until`` values so multi-marker lines pay
    linear work in the line length instead of rescanning each prefix.
    """
    if until < state.cursor:
        msg = "delimiter scan cursor must advance forward"
        raise ValueError(msg)
    index = state.cursor
    while index < until:
        char = verdict_line[index]
        if char == '"':
            if _ascii_double_quote_is_delimiter(verdict_line, index, state.inside_ascii_double):
                if state.inside_ascii_double:
                    state.last_close_end = index + 1
                state.inside_ascii_double = not state.inside_ascii_double
            index += 1
        elif char == "'":
            if _ascii_single_quote_is_delimiter(verdict_line, index, state.inside_ascii_single):
                if state.inside_ascii_single:
                    state.last_close_end = index + 1
                state.inside_ascii_single = not state.inside_ascii_single
            index += 1
        elif char == "`":
            run_len = 1
            while index + run_len < until and verdict_line[index + run_len] == "`":
                run_len += 1
            if state.inside_backtick:
                if run_len == state.backtick_open_len:
                    state.inside_backtick = False
                    state.backtick_open_len = 0
                    state.last_close_end = index + run_len
            else:
                state.inside_backtick = True
                state.backtick_open_len = run_len
            index += run_len
        elif char == "“":
            state.inside_curly_double = True
            index += 1
        elif char == "”":
            if state.inside_curly_double:
                state.last_close_end = index + 1
            state.inside_curly_double = False
            index += 1
        elif char == "‘":
            state.inside_curly_single = True
            index += 1
        elif char == "’":
            if state.inside_curly_single:
                state.last_close_end = index + 1
            state.inside_curly_single = False
            index += 1
        else:
            index += 1
    state.cursor = until


def _awf_verdict_marker_unambiguously_separate_attempt(
    verdict_line: str,
    match_start: int,
    *,
    delimiter_state: _AwfVerdictDelimiterState | None = None,
) -> bool:
    """Return whether ``match_start`` is an unambiguous separate trailing attempt.

    Distinguishes a real trailing verdict jammed against a finished citation
    span (``cite "x"AWF-VERDICT: …``) or introduced by an explicit
    correction/separator (``…; correction: AWF-VERDICT: …``) from mid-reason
    marker-grammar prose.
    """
    if match_start <= 0:
        return False
    if delimiter_state is None:
        delimiter_state = _AwfVerdictDelimiterState()
        _advance_awf_verdict_delimiter_state(verdict_line, delimiter_state, match_start)
    elif delimiter_state.cursor != match_start:
        _advance_awf_verdict_delimiter_state(verdict_line, delimiter_state, match_start)
    if (
        delimiter_state.last_close_end >= 0
        and not delimiter_state.inside_quote_or_code
        and not verdict_line[delimiter_state.last_close_end : match_start].strip()
    ):
        return True
    # Explicit self-corrections split even without a closed quote span
    # (PRRT_kwDOSJAM6s6Znm-N). Stay absorbed when still inside an open span.
    return (not delimiter_state.inside_quote_or_code) and (
        _prefix_has_explicit_verdict_correction_separator(verdict_line, match_start)
    )


def _awf_verdict_marker_embedded_in_reason_prose(
    verdict_line: str,
    match_start: int,
    *,
    delimiter_state: _AwfVerdictDelimiterState | None = None,
) -> bool:
    """Return whether a same-line marker is quoted inside earlier reason text.

    Distinguishes prose citations of the marker grammar from real trailing
    verdict attempts so a blocking reason cannot be overridden by a quote.
    Tracks ASCII ``"``/``'`` and Markdown backtick *runs* plus curly ``“``/``”``
    and ``‘``/``’`` open/close independently so mid-quote citations (code spans,
    typographic prompt echoes) stay embedded, while a closing delimiter
    immediately before a real trailing marker does not.

    Backtick runs follow CommonMark code-span rules: a consecutive run of N
    backticks opens a span, and only a later run of the same length closes it.
    Toggling once per backtick character would close a double-backtick span at
    the opener and treat an embedded marker as a real trailing verdict.

    ASCII ``'`` is only a quote delimiter when it is not a word-internal
    apostrophe (``don't``, ``user's``) or a leading elision (``'em``, ``'til``,
    ``'cause``); ASCII ``"`` is only a delimiter at plausible quote boundaries
    (not inch/unit marks like ``5"``). Backslash-escaped ASCII quotes
    (``\"``, ``\'``; odd preceding ``\\`` run) are literal reason text and
    never toggle. Naive toggle would absorb a later unquoted same-line marker
    into an earlier resolvable verdict.
    """
    if match_start <= 0:
        return False
    if delimiter_state is None:
        delimiter_state = _AwfVerdictDelimiterState()
        _advance_awf_verdict_delimiter_state(verdict_line, delimiter_state, match_start)
    elif delimiter_state.cursor != match_start:
        _advance_awf_verdict_delimiter_state(verdict_line, delimiter_state, match_start)
    return delimiter_state.inside_quote_or_code


def _ascii_quote_is_backslash_escaped(verdict_line: str, index: int) -> bool:
    """Return whether ``verdict_line[index]`` is escaped by an odd ``\\`` run.

    Agents often cite expected literal quotes as ``\\"foo`` in a reason; those
    must not open ASCII quote state or a later unquoted same-line blocker is
    absorbed. An even run (``\\\\"``) leaves a real delimiter.
    """
    count = 0
    cursor = index - 1
    while cursor >= 0 and verdict_line[cursor] == "\\":
        count += 1
        cursor -= 1
    return count % 2 == 1


def _ascii_double_quote_is_delimiter(
    verdict_line: str, index: int, inside_ascii_double: bool
) -> bool:
    """Return whether ``verdict_line[index]`` is an ASCII double-quote delimiter.

    Inch/unit marks after an alphanumeric (``5"``, ``12"x``) must not open a
    quote span; naive toggle would absorb a later unquoted same-line marker.
    Backslash-escaped quotes (``\\"``) are never delimiters. Outside a quote,
    only open when the previous character is not alphanumeric so delimiters sit
    at plausible quote boundaries. Inside a quote, every unescaped ``"`` closes
    (including when jammed against a following token).
    """
    if _ascii_quote_is_backslash_escaped(verdict_line, index):
        return False
    if inside_ascii_double:
        return True
    prev = verdict_line[index - 1] if index > 0 else ""
    return not prev.isalnum()


def _ascii_single_quote_is_delimiter(
    verdict_line: str, index: int, inside_ascii_single: bool
) -> bool:
    """Return whether ``verdict_line[index]`` is an ASCII single-quote delimiter.

    Word-internal apostrophes before a lowercase continuation (``don't``,
    ``user's``, ``it's``) never toggle quote state. Leading elisions after a
    non-alphanumeric boundary (``'em``, ``'til``, ``'cause``) also never toggle
    — otherwise an open span never closes and a later unquoted same-line marker
    is absorbed, or an already-open citation closes early. Backslash-escaped
    quotes (``\\'``) are never delimiters. Outside a quote, only open when the
    previous character is not alphanumeric so plural possessives like ``users'``
    do not start a span. Inside a quote, a closer jammed against a following
    lowercase token (``'strict'by``) still closes unless the letters after the
    apostrophe are a short English contraction suffix (``n't``, ``'s``,
    ``'re``, …), so trailing unquoted markers are not absorbed.
    """
    if _ascii_quote_is_backslash_escaped(verdict_line, index):
        return False
    prev = verdict_line[index - 1] if index > 0 else ""
    nxt = verdict_line[index + 1] if index + 1 < len(verdict_line) else ""
    if prev.isalnum() and nxt.islower():
        return inside_ascii_single and not _ascii_apostrophe_is_contraction_suffix(
            verdict_line, index
        )
    if _ascii_apostrophe_is_leading_elision(verdict_line, index):
        return False
    return inside_ascii_single or not prev.isalnum()


# Short alphabetic tails after an ASCII apostrophe that mark English contractions
# / clitics (n't, 's, 're, 've, 'll, 'd, 'm). Longer jammed tokens ('strict'by)
# are closers, not apostrophes.
_ASCII_APOSTROPHE_CONTRACTION_SUFFIXES = frozenset({"t", "s", "re", "ve", "ll", "d", "m"})

# Colloquial leading elisions after a word boundary ('em, 'til, 'cause). These
# must not open or close ASCII single-quote spans in verdict reason prose.
_ASCII_LEADING_ELISION_SUFFIXES = frozenset(
    {
        "em",
        "tis",
        "twas",
        "twere",
        "twill",
        "twould",
        "twixt",
        "til",
        "till",
        "cause",
        "bout",
        "round",
        "nother",
        "cept",
        "gainst",
        "fore",
        "stead",
        "cross",
        "neath",
        "ere",
    }
)


def _ascii_apostrophe_is_contraction_suffix(verdict_line: str, index: int) -> bool:
    """Return whether letters after ``verdict_line[index]`` look like a contraction."""
    end = index + 1
    while end < len(verdict_line) and verdict_line[end].isalpha():
        end += 1
    suffix = verdict_line[index + 1 : end].lower()
    return suffix in _ASCII_APOSTROPHE_CONTRACTION_SUFFIXES


def _ascii_apostrophe_is_leading_elision(verdict_line: str, index: int) -> bool:
    """Return whether ``verdict_line[index]`` starts a leading elision (``'em``).

    Requires a non-alphanumeric previous character and an alphabetic run that
    matches a known elision suffix. Longer tokens like ``'emergency`` are not
    elisions and may still open a real quote span.
    """
    prev = verdict_line[index - 1] if index > 0 else ""
    if prev.isalnum():
        return False
    end = index + 1
    while end < len(verdict_line) and verdict_line[end].isalpha():
        end += 1
    suffix = verdict_line[index + 1 : end].lower()
    return suffix in _ASCII_LEADING_ELISION_SUFFIXES
