"""Markdown/HTML shielding for PR-monitor verdict line selection."""

from __future__ import annotations

__all__ = (
    "_BARE_VERDICT_LINE",
    "_VERDICT_REASON_TEMPLATE_PLACEHOLDER",
    "_VERDICT_REASON_REDACTION_ONLY",
    "_CODE_FORMATTED_VERDICT_LINE",
    "_VERDICT_REASON_INLINE_QUOTE_WRAPPER",
    "_VERDICT_REASON_INLINE_EMPHASIS_WRAPPER",
    "_VERDICT_REASON_INLINE_HTML_WRAPPER",
    "_VERDICT_REASON_PYTHON_DUNDER",
    "_MARKDOWN_FENCE_OPEN",
    "_MARKDOWN_INDENTED_CODE_LINE",
    "_HTML_CODE_BLOCK_OPEN",
    "_HTML_COMMENT_OPEN",
    "_HTML_COMMENT_CLOSE",
    "_HTML_PROCESSING_INSTRUCTION_OPEN",
    "_HTML_PROCESSING_INSTRUCTION_CLOSE",
    "_HTML_DECLARATION_OPEN",
    "_HTML_DECLARATION_CLOSE",
    "_HTML_CDATA_OPEN",
    "_HTML_CDATA_CLOSE",
    "_HTML_TYPE6_BLOCK_TAGS",
    "_HTML_TYPE6_BLOCK_OPEN",
    "_HTML_TYPE7_ATTR",
    "_HTML_TYPE7_BLOCK_OPEN",
    "_HTML_COMPLETE_CODE_OPEN",
    "_HTML_COMPLETE_CODE_SELF_CLOSING",
    "_HTML_BLANK_LINE",
    "_MARKDOWN_LIST_PREFIX",
    "_MARKDOWN_TASK_LIST_CHECKBOX",
    "_MARKDOWN_BLOCKQUOTE_PREFIX",
    "_FINAL_ANSWER_ATTEMPT_PREFIX",
    "_VERDICT_RESULT_LABEL_ATTEMPT_PREFIX",
    "_MARKDOWN_EMPHASIS_PREFIX",
    "_MARKDOWN_HEADING_PREFIX",
    "_MAX_VERDICT_REASON_LENGTH",
    "_normalize_markdown_fence_line",
    "_markdown_fence_list_container_indent",
    "_markdown_fence_open_marker",
    "_html_code_block_open_tag",
    "_html_code_block_closes",
    "_html_code_close_appears_later",
    "_html_comment_opens",
    "_html_comment_closes",
    "_html_processing_instruction_opens",
    "_html_processing_instruction_closes",
    "_html_declaration_opens",
    "_markdown_blockquote_container_depth",
    "_html_declaration_closes",
    "_html_cdata_opens",
    "_html_cdata_closes",
    "_html_type6_block_opens",
    "_html_type7_block_opens",
    "_html_type7_may_start_at",
    "_html_complete_code_opens",
    "_html_complete_code_self_closes",
    "_html_blank_terminated_block_closes",
    "_markdown_fence_closes",
    "_markdown_is_indented_code_line",
    "_iter_non_fenced_verdict_lines",
    "_verdict_reason_inline_link_label",
)

import re
from collections.abc import Iterable, Sequence

from awf.runtime.pr_monitor_runner.constants import (
    _REDACTION,
)

_BARE_VERDICT_LINE = re.compile(
    r"^(?P<label>FALSE\s+POSITIVE|DEFER|NEEDS[\s_]+HUMAN)\s*:\s*(?P<reason>[^\n\r]*)$",
    re.IGNORECASE,
)
# Match whole-reason prompt-template placeholders. An unanchored or start-only
# search falsely strips legitimate mid-reason or leading-content tags such as
# ``added the <summary> section`` / ``<summary> section rewritten``. Allow only
# trailing prompt boilerplate (e.g. `` and exit."``) after the placeholder so
# stored echoes normalize away. Whole-reason ellipsis echoes of the prompt form
# ``FIXED: …`` are also treated as placeholders — not ``...real content``.
# Callers that check reasons also decode HTML entities (``&lt;reason&gt;``,
# nested ``&amp;lt;…&amp;gt;``) and CommonMark backslash escapes
# (``\<reason\>``), including mixed layers (``\&lt;reason\&gt;``), against
# this same anchored pattern (PRRT_kwDOSJAM6s6Zoyj2, PRRT_kwDOSJAM6s6Zo4bG,
# PRRT_kwDOSJAM6s6ZpA-z, PRRT_kwDOSJAM6s6ZpHXM).
_VERDICT_REASON_TEMPLATE_PLACEHOLDER = re.compile(
    r"^\s*(?:"
    r"<\s*(?:what|one[-\s]?sentence|summary|reason|track|decision|defer|need)"
    r"\b[^>\n\r]{0,80}>"
    r"|…|\.{3}"
    r")"
    r"(?:\s+and\s+exit\.?)?"
    r"[\s\"'”’]*$",
    re.IGNORECASE,
)
_VERDICT_REASON_REDACTION_ONLY = re.compile(
    rf"^[\s,;:.!?'\"“”‘’]*(?:(?:[A-Za-z][A-Za-z0-9_-]*\s*[:=]\s*)?"
    rf"[\s,;:.!?'\"“”‘’]*{re.escape(_REDACTION)}[\s,;:.!?'\"“”‘’]*)+$",
    re.IGNORECASE,
)
_CODE_FORMATTED_VERDICT_LINE = re.compile(r"^(?P<ticks>`+)\s*(?P<line>.*?)\s*(?P=ticks)$")
_VERDICT_REASON_INLINE_QUOTE_WRAPPER = re.compile(
    r'^(?:"\s*(?P<dq>.*?)\s*"|\'\s*(?P<sq>.*?)\s*\'|'
    r"“\s*(?P<cdq>.*?)\s*”|‘\s*(?P<csq>.*?)\s*’)$"
)
# Balanced Markdown strong/emphasis/strikethrough wrappers around a reason
# (``**<…>**``, ``__<…>__``, ``~~<…>~~``, and single ``*<…>*`` / ``_<…>_``).
# Same peel loop as ticks/quotes so template-placeholder echoes still fail
# closed (PRRT_kwDOSJAM6s6ZoAz9, PRRT_kwDOSJAM6s6ZopxG). Strong and GFM
# strikethrough are always peeled; single emphasis is peeled only when the
# enclosed value is placeholder-shaped (whole-reason or absorbed same-line
# citation) so snake_case / ``_name_`` identifiers stay intact
# (PRRT_kwDOSJAM6s6ZoDQU, PRRT_kwDOSJAM6s6ZoGYD). Whole-reason Python dunders
# (``__init__``, ``__name__``) also match ``__…__``; those are skipped in the
# peel loop (PRRT_kwDOSJAM6s6ZoC7B). Strong/strike alts must precede
# single-emphasis alts.
_VERDICT_REASON_INLINE_EMPHASIS_WRAPPER = re.compile(
    r"^(?:"
    r"\*\*\s*(?P<strong_star>.*?)\s*\*\*|"
    r"__\s*(?P<strong_under>.*?)\s*__|"
    r"~~\s*(?P<strike>.*?)\s*~~|"
    r"\*\s*(?P<em_star>.*?)\s*\*|"
    r"_\s*(?P<em_under>.*?)\s*_"
    r")$"
)
# CommonMark HTML attribute fragment (unquoted / single- / double-quoted).
# Shared by type-7 block openers and safe inline verdict HTML wrappers so a
# quoted ``>`` does not truncate the open tag (PRRT_kwDOSJAM6s6ZnYwM,
# PRRT_kwDOSJAM6s6ZsvLy).
_HTML_TYPE7_ATTR = (
    r"(?:\s+[A-Za-z_:][A-Za-z0-9_.:-]*"
    r"(?:\s*=\s*(?:"
    r"[^\s\"'=<>`]+|"
    r"'[^']*'|"
    r'"[^"]*"'
    r"))?)"
)
# Balanced safe inline HTML wrappers around a reason (``<em>…</em>``,
# ``<strong>…</strong>``, ``<span>…</span>``, ``<code>…</code>``, optional
# attributes, case-insensitive). Peeled only when the enclosed value is
# placeholder-shaped — same gate as single emphasis / Markdown links — so
# rich-text echoes of ``<em>&lt;reason&gt;</em>`` / ``<code>&lt;reason&gt;</code>``
# fail closed while real HTML-wrapped prose stays usable
# (PRRT_kwDOSJAM6s6ZpdhJ, PRRT_kwDOSJAM6s6Zq76j). Attribute values use
# ``_HTML_TYPE7_ATTR`` so a quoted ``>`` (``title="1 > 0"``) does not truncate
# the open tag and leave a non-placeholder remnant (PRRT_kwDOSJAM6s6ZsvLy).
_VERDICT_REASON_INLINE_HTML_WRAPPER = re.compile(
    rf"^<(?P<tag>em|strong|span|code){_HTML_TYPE7_ATTR}*\s*>\s*(?P<inner>.*?)\s*</(?P=tag)\s*>$",
    re.IGNORECASE,
)
# Whole-reason Markdown links (``[<…>](https://example.com)``) and images
# (``![<…>](https://example.com)``), plus reference-style forms
# (``[<…>][ref]`` / ``[<…>][]`` / shortcut ``[<…>]``). Peeled only when the
# label is placeholder-shaped — same gate as single emphasis — so a real
# linked / imaged justification stays usable (PRRT_kwDOSJAM6s6Zos6S,
# PRRT_kwDOSJAM6s6Zo-5M, PRRT_kwDOSJAM6s6ZpXSL, PRRT_kwDOSJAM6s6Zp8jK).
# Destinations may contain balanced parentheses or backslash-escaped ``(`` /
# ``)``; a plain ``[^)]*`` stop would leave
# ``[<reason>](https://example.com/a_(b))`` unpeeled and accept the echo as a
# substantive reason (PRRT_kwDOSJAM6s6ZpLqR).
_VERDICT_REASON_PYTHON_DUNDER = re.compile(r"^__[A-Za-z_][A-Za-z0-9_]*__$")


def _verdict_reason_inline_link_label(reason: str) -> str | None:
    """Return the label when ``reason`` is a whole-reason Markdown link/image.

    Matches inline ``[label](destination)`` / ``![label](destination)``,
    reference-style ``[label][ref]`` / ``[label][]``, and shortcut
    ``[label]`` / ``![label]`` (and image variants) spanning the entire
    string. Label selection is non-greedy (earliest ``]\\s*(`` or ``]\\s*[``
    that yields a destination/ref consuming the rest of the string, else a
    bare ``]`` at end-of-string for shortcut form). Destinations allow nested
    balanced parentheses; reference ids allow nested balanced brackets; both
    allow backslash-escaped characters. Newlines abort matching
    (PRRT_kwDOSJAM6s6ZpLqR, PRRT_kwDOSJAM6s6ZpXSL, PRRT_kwDOSJAM6s6Zp8jK).
    """
    if reason.startswith("!["):
        after_open = 2
    elif reason.startswith("["):
        after_open = 1
    else:
        return None

    n = len(reason)
    label_start = after_open
    while label_start < n and reason[label_start] in " \t":
        label_start += 1

    # Non-greedy: try each ``]`` followed by optional whitespace and ``(`` or
    # ``[`` (inline destination vs reference-style id), or end-of-string
    # (shortcut reference). When a dest/ref opener is present but does not
    # consume the rest of the string, reject — do not fall through to a later
    # ``]`` as a false shortcut (PRRT_kwDOSJAM6s6Zp8jK).
    j = label_start
    while j < n:
        if reason[j] == "]":
            k = j + 1
            while k < n and reason[k] in " \t":
                k += 1
            if k < n and reason[k] in "([":
                open_ch = reason[k]
                close_ch = ")" if open_ch == "(" else "]"
                depth = 1
                p = k + 1
                while p < n and depth > 0:
                    ch = reason[p]
                    if ch == "\n":
                        break
                    if ch == "\\" and p + 1 < n:
                        p += 2
                        continue
                    if ch == open_ch:
                        depth += 1
                    elif ch == close_ch:
                        depth -= 1
                    p += 1
                if depth == 0 and p == n:
                    return reason[label_start:j].rstrip(" \t")
                return None
            if k == n:
                # Shortcut ``[label]`` / ``![label]`` with no destination
                # (PRRT_kwDOSJAM6s6Zp8jK).
                return reason[label_start:j].rstrip(" \t")
        j += 1
    return None


# Multiline Markdown fences (CommonMark-style). Backtick info strings may not
# contain backticks (so same-line wraps like `` ```verdict``` `` are not
# openers). Tilde info strings may include ``~`` (e.g. ``~~~ lang~option``).
# Markers inside an open fence must not participate in verdict selection.
_MARKDOWN_FENCE_OPEN = re.compile(
    r"^ {0,3}(?:"
    r"(?P<fence>`{3,})[^`\n]*|"
    r"(?P<fence_tilde>~{3,})[^\n]*"
    r")[ \t]*$"
)
# CommonMark indented code: four spaces of indent, treating tabs as stops of
# four columns — so a leading tab or 1–3 spaces plus a tab also qualifies.
# Unconditional ``str.strip`` would promote example markers inside these
# regions to authoritative finals. Blockquote- and list-nested indented code
# are detected by ``_markdown_is_indented_code_line`` (peel ``>`` / list
# markers first; PRRT_kwDOSJAM6s6Zoxg4, PRRT_kwDOSJAM6s6Zo4bL).
_MARKDOWN_INDENTED_CODE_LINE = re.compile(r"^(?: {4,}| {0,3}\t)")
# Raw Markdown HTML code regions. Agents quote example verdicts inside these;
# they are not fenced or indented code, so the line iterator must track them
# or an example can override an earlier hard block (PRRT_kwDOSJAM6s6ZnEAt).
# CommonMark HTML block type 1 covers ``pre`` / ``script`` / ``style`` /
# ``textarea``. ``code`` is kept here for incomplete / same-line example
# wrappers; complete ``<code>`` openers use a hybrid close-tag + blank-tail
# path (PRRT_kwDOSJAM6s6ZpLqP / PRRT_kwDOSJAM6s6ZpPQA / PRRT_kwDOSJAM6s6ZnQhP).
# Opener: optional 0–3 spaces, then the tag name followed by whitespace,
# ``>``, or EOL. Open matching peels list/blockquote containers first (same as
# fences) so ``- <pre>`` / ``> <code>`` still enter HTML mode
# (PRRT_kwDOSJAM6s6ZnHBW).
_HTML_CODE_BLOCK_OPEN = re.compile(
    r"^ {0,3}<(?P<tag>pre|code|script|style|textarea)(?=[\s>]|$)",
    re.IGNORECASE,
)
# CommonMark HTML comment blocks (type 2): optional 0–3 spaces then ``<!--``,
# ending on the first subsequent line that contains ``-->``. Agents paste
# example verdicts inside comments; without shielding a clean marker line
# inside ``<!-- … -->`` overrides an earlier hard block
# (PRRT_kwDOSJAM6s6ZnN2F). Peel list/blockquote containers on openers like
# ``<pre>`` / fences so ``- <!--`` / ``> <!--`` still enter comment mode.
# The closer also accepts ``--!>``: the HTML parser's "comment end bang state"
# ends a comment on it, so everything after ``--!>`` is visible text to whoever
# reads the rendered thread. Matching only ``-->`` left the shield open to end
# of output and silently masked that visible, authoritative marker
# (CodeQL py/bad-tag-filter).
_HTML_COMMENT_OPEN = re.compile(r"^ {0,3}<!--")
_HTML_COMMENT_CLOSE = re.compile(r"--!?>")
# CommonMark HTML blocks type 3–5 (processing instruction / declaration /
# CDATA). Same shield as comments: agents paste example markers inside
# ``<?…?>``, ``<!Letter…>``, and ``<![CDATA[…]]>``; without tracking them a
# clean marker overrides an earlier hard block (PRRT_kwDOSJAM6s6ZnSrG).
_HTML_PROCESSING_INSTRUCTION_OPEN = re.compile(r"^ {0,3}<\?")
_HTML_PROCESSING_INSTRUCTION_CLOSE = re.compile(r"\?>")
_HTML_DECLARATION_OPEN = re.compile(r"^ {0,3}<![A-Za-z]")
_HTML_DECLARATION_CLOSE = re.compile(r">")
_HTML_CDATA_OPEN = re.compile(r"^ {0,3}<!\[CDATA\[")
_HTML_CDATA_CLOSE = re.compile(r"\]\]>")
# CommonMark HTML blocks type 6 (block tags) and type 7 (complete open/close
# tags, including custom elements). Both continue until a blank line — not a
# matching close tag — so agents can wrap example verdicts in ``<div>`` /
# ``<custom-…>`` and override an earlier hard block unless we shield through
# the terminating blank line (PRRT_kwDOSJAM6s6ZnUxZ). Type-1 tags stay on the
# close-tag path above; peel list/blockquote containers on openers like
# other raw-HTML starts.
_HTML_TYPE6_BLOCK_TAGS = (
    "address|article|aside|base|basefont|blockquote|body|caption|center|col|"
    "colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|"
    "footer|form|frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|li|"
    "link|main|menu|menuitem|nav|noframes|ol|optgroup|option|p|param|search|"
    "section|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul"
)
_HTML_TYPE6_BLOCK_OPEN = re.compile(
    rf"^ {{0,3}}</?(?:{_HTML_TYPE6_BLOCK_TAGS})(?=[\s/>]|$)",
    re.IGNORECASE,
)
# Type 7: a complete open or closing tag on its own line, optional trailing
# whitespace only. CommonMark excludes type-1 names only for **open** tags —
# a standalone ``</pre>`` / ``</script>`` (etc.) still starts blank-terminated
# type 7. Keep the open-tag exclusion so type-1 openers stay on the close-tag
# path above; do not exclude closers or an example after ``</pre>`` overrides
# an earlier hard block (PRRT_kwDOSJAM6s6Zn6x4). ``code`` is not type 1 —
# complete ``<code>`` openers still match here but the iterator upgrades them
# to close-tag + blank-tail hybrid shielding (PRRT_kwDOSJAM6s6ZpLqP /
# PRRT_kwDOSJAM6s6ZpPQA). Custom elements such as ``<custom-example>`` land
# here; type-6 tags may also match but are recognized first. Attribute values
# follow CommonMark (unquoted / single- / double-quoted) via ``_HTML_TYPE7_ATTR``
# so a quoted ``>`` (``data-value=">">``) does not truncate the open tag and
# skip the blank-line shield (PRRT_kwDOSJAM6s6ZnYwM).
_HTML_TYPE7_BLOCK_OPEN = re.compile(
    r"^ {0,3}(?:"
    r"</[A-Za-z][A-Za-z0-9:-]*\s*>"
    r"|"
    r"<(?!(?:pre|script|style|textarea)\b)[A-Za-z][A-Za-z0-9:-]*"
    rf"{_HTML_TYPE7_ATTR}*"
    r"\s*/?>"
    r")[ \t]*$",
    re.IGNORECASE,
)
# Complete ``<code …>`` / ``<code/>`` on its own line (type-7 shape). Used to
# divert from pure blank-termination into hybrid close-tag + blank-tail
# shielding (PRRT_kwDOSJAM6s6ZpPQA). Self-closing ``<code/>`` stays on the
# blank-terminated path — hybrid never sees ``</code>`` (PRRT_kwDOSJAM6s6ZpTPI).
_HTML_COMPLETE_CODE_OPEN = re.compile(
    rf"^ {{0,3}}<code{_HTML_TYPE7_ATTR}*\s*/?>[ \t]*$",
    re.IGNORECASE,
)
_HTML_COMPLETE_CODE_SELF_CLOSING = re.compile(
    rf"^ {{0,3}}<code{_HTML_TYPE7_ATTR}*\s*/>[ \t]*$",
    re.IGNORECASE,
)
_HTML_BLANK_LINE = re.compile(r"^[ \t]*$")
# Leading Markdown list markers agents often emit before a canonical verdict line
# (``- AWF-VERDICT: …``, ``1. AWF-VERDICT: …``). Strip only for attempt
# classification so a final garbled list-prefixed marker still fails closed —
# never yield list-stripped forms as successful fullmatch candidates (that
# would make multiline option lists authoritative over an earlier hard block).
_MARKDOWN_LIST_PREFIX = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
# GFM task-list checkboxes (``- [ ] AWF-VERDICT: …``, ``- [x] …``). Plain list
# strip leaves ``[ ]`` before the marker, so the final line is not classified
# as an attempt and an earlier resolvable verdict can win incorrectly.
_MARKDOWN_TASK_LIST_CHECKBOX = re.compile(r"^\[(?: |x|X)\]\s+")
# Leading Markdown blockquote markers (``> AWF-VERDICT: …``, nested ``>>``).
# Same attempt-only strip as list prefixes — a trailing blockquoted marker must
# fail closed rather than leave an earlier resolvable verdict selected.
_MARKDOWN_BLOCKQUOTE_PREFIX = re.compile(r"^(?:>\s*)+")
# Common LLM "Final answer:" wrappers before a canonical marker. Attempt-only
# strip — a trailing ``Final answer: AWF-VERDICT: …`` must fail closed rather
# than leave an earlier resolvable verdict selected when the start-only
# attempt check would otherwise ignore the mid-segment marker.
_FINAL_ANSWER_ATTEMPT_PREFIX = re.compile(
    r"^(?:(?:my|the)\s+)?final\s+answer(?:\s+is)?\s*[:\-–—]\s*",
    re.IGNORECASE,
)
# Obvious ``Verdict:`` / ``Result:`` labels agents prepend before a canonical
# marker. Attempt-only strip — ``search()`` still finds the marker while a
# start-only attempt check would leave the label intact and keep an earlier
# resolvable verdict selected.
_VERDICT_RESULT_LABEL_ATTEMPT_PREFIX = re.compile(
    r"^(?:verdict|result)\s*[:\-–—]\s*",
    re.IGNORECASE,
)
# Leading Markdown emphasis / strong markers (``**AWF-VERDICT: …**``,
# ``*AWF-VERDICT: …*``, ``__…__``). Require a following non-space so list
# markers (``* item``) stay handled by ``_MARKDOWN_LIST_PREFIX``. Balanced,
# direct top-level wrappers may be normalized into successful candidates;
# malformed or nested forms use this attempt-only strip and still fail closed.
_MARKDOWN_EMPHASIS_PREFIX = re.compile(r"^(?:\*{1,3}|_{1,3})(?=\S)")
# Leading ATX Markdown headings (``# AWF-VERDICT: …``, ``### AWF-VERDICT: …``).
# CommonMark allows 1–6 hashes followed by whitespace. Attempt-only strip —
# ``search()`` still finds the marker while a start-only attempt check would
# leave ``###`` intact and keep an earlier resolvable verdict selected.
_MARKDOWN_HEADING_PREFIX = re.compile(r"^#{1,6}\s+")
_MAX_VERDICT_REASON_LENGTH = 500

# Blockquote fence closers peel opener depth; see ``_markdown_fence_closes``.


def _normalize_markdown_fence_line(line: str) -> str:
    """Strip container indent, blockquotes, and list markers for open matching.

    List- or blockquote-nested openers such as ``- ```text`` / ``> ```text`` /
    ``- <pre>`` / ``- <!--`` / ``- <?`` / ``- <![CDATA[`` / ``- <div>`` are not
    top-level fence, HTML-code, HTML-comment, type-3–5, or type-6/7 raw-HTML
    lines, and continuation content is often only two-space indented (so it
    misses the indented-code check). Normalize before matching so those
    regions stay shielded from verdict selection. Closers must not use this
    helper — peeling a list or blockquote marker would treat ``- ``` `` /
    ``> ``` `` inside a top-level fence as a closer.
    """
    rest = line.lstrip(" \t")
    # Peel blockquote and list in either order (``> - ``` `` / ``- > ``` ``).
    while True:
        prev = rest
        rest = _MARKDOWN_BLOCKQUOTE_PREFIX.sub("", rest, count=1)
        rest = _MARKDOWN_LIST_PREFIX.sub("", rest, count=1)
        if rest == prev:
            return rest


def _markdown_fence_list_container_indent(line: str) -> int:
    """Column width of leading indent plus list marker(s) on a fence opener.

    ``10. ```text`` → 4; ``10. 10. ```text`` → 8 (sum nested list widths);
    ``- ```text`` → 2; ``> - ```text`` → 2; top-level / blockquote-only
    fences → 0. Closers for list-nested openers may carry this indent plus
    CommonMark's optional 0–3 spaces relative to the container
    (PRRT_kwDOSJAM6s6ZmsZS, PRRT_kwDOSJAM6s6Zn6x6). Blockquote width is
    tracked separately and stripped before this indent is applied on closers.
    Expand tabs to column stops of four before measuring so tab-padded
    markers such as ``-\\t-\\t~~~text`` record width 8, not character-count 4
    (PRRT_kwDOSJAM6s6ZopxJ).
    """
    # CommonMark expands tabs before indentation is counted; measuring
    # ``lst.end()`` on raw ``-\\t`` under-counts each marker as two columns.
    expanded = line.expandtabs(4)
    rest = expanded.lstrip(" ")
    leading_ws = len(expanded) - len(rest)
    had_blockquote = False
    list_width = 0
    while True:
        bq = _MARKDOWN_BLOCKQUOTE_PREFIX.match(rest)
        if bq is not None:
            had_blockquote = True
            rest = rest[bq.end() :]
            continue
        lst = _MARKDOWN_LIST_PREFIX.match(rest)
        if lst is not None:
            # Nested list containers stack widths; retaining only the last
            # marker would under-indent closers for ``10. 10. ```text``.
            list_width += lst.end()
            rest = rest[lst.end() :]
            continue
        break
    if list_width == 0:
        return 0
    if had_blockquote:
        return list_width
    return leading_ws + list_width


def _markdown_fence_open_marker(line: str) -> str | None:
    """Return the fence marker that opens a multiline code fence on ``line``."""
    opened = _MARKDOWN_FENCE_OPEN.match(_normalize_markdown_fence_line(line))
    if opened is None:
        return None
    return opened.group("fence") or opened.group("fence_tilde")


def _html_code_block_open_tag(line: str) -> str | None:
    """Return a type-1 / example HTML tag when ``line`` opens a raw code block.

    Recognizes CommonMark type-1 tags (``pre`` / ``script`` / ``style`` /
    ``textarea``) plus ``code`` for incomplete / same-line example wrappers.
    Complete ``<code>`` openers are diverted to hybrid close-tag + blank-tail
    shielding in ``_iter_non_fenced_verdict_lines`` (PRRT_kwDOSJAM6s6ZpLqP /
    PRRT_kwDOSJAM6s6ZpPQA). Normalize list/blockquote containers first (same as
    fence openers) so ``- <pre>`` / ``> <code>`` enter HTML shielding; otherwise
    lightly indented example markers inside override an earlier hard block
    (PRRT_kwDOSJAM6s6ZnHBW).
    """
    opened = _HTML_CODE_BLOCK_OPEN.match(_normalize_markdown_fence_line(line))
    if opened is None:
        return None
    return opened.group("tag").lower()


def _html_code_block_closes(line: str, tag: str) -> bool:
    """Return whether ``line`` contains a closing tag for an open HTML code block."""
    return re.search(rf"</{re.escape(tag)}\s*>", line, re.IGNORECASE) is not None


def _html_comment_opens(line: str) -> bool:
    """Return whether ``line`` opens a CommonMark HTML comment block.

    Normalize list/blockquote containers first (same as fence / ``<pre>``
    openers) so ``- <!--`` / ``> <!--`` enter comment shielding; otherwise a
    clean marker line inside the comment overrides an earlier hard block
    (PRRT_kwDOSJAM6s6ZnN2F).
    """
    return _HTML_COMMENT_OPEN.match(_normalize_markdown_fence_line(line)) is not None


def _html_comment_closes(line: str) -> bool:
    """Return whether ``line`` contains an HTML comment closer (``-->`` / ``--!>``)."""
    return _HTML_COMMENT_CLOSE.search(line) is not None


def _html_processing_instruction_opens(line: str) -> bool:
    """Return whether ``line`` opens a CommonMark HTML processing instruction.

    Normalize list/blockquote containers first so ``- <?`` / ``> <?`` enter
    shielding (PRRT_kwDOSJAM6s6ZnSrG).
    """
    return _HTML_PROCESSING_INSTRUCTION_OPEN.match(_normalize_markdown_fence_line(line)) is not None


def _html_processing_instruction_closes(line: str) -> bool:
    """Return whether ``line`` contains a processing-instruction closer (``?>``)."""
    return _HTML_PROCESSING_INSTRUCTION_CLOSE.search(line) is not None


def _html_declaration_opens(line: str) -> bool:
    """Return whether ``line`` opens a CommonMark HTML declaration block.

    Type 4 starts with ``<!`` followed by an ASCII letter (e.g. ``<!DOCTYPE``).
    Comments (``<!--``) and CDATA (``<![CDATA[``) are matched separately.
    Normalize list/blockquote containers first (PRRT_kwDOSJAM6s6ZnSrG).
    """
    return _HTML_DECLARATION_OPEN.match(_normalize_markdown_fence_line(line)) is not None


def _markdown_blockquote_container_depth(line: str) -> int:
    """Count leading blockquote markers after indent and list containers.

    Used so type-4 declaration closers, type-6/7 blank terminators, and
    Markdown fence closers can ignore container ``>`` prefixes on
    ``> <!DOCTYPE`` / ``>> <!DOCTYPE`` / ``> ``` `` / ``>> ``` `` openers
    without treating those markers as the CommonMark end
    (PRRT_kwDOSJAM6s6ZnUwf / PRRT_kwDOSJAM6s6Zn213). List markers may sit
    before or between quote levels (``- >`` / ``> -``).
    """
    rest = line.lstrip(" \t")
    depth = 0
    while True:
        bq = re.match(r"^>[ \t]?", rest)
        if bq is not None:
            depth += 1
            rest = rest[bq.end() :]
            continue
        lst = _MARKDOWN_LIST_PREFIX.match(rest)
        if lst is not None:
            rest = rest[lst.end() :]
            continue
        return depth


def _html_declaration_closes(line: str, *, blockquote_depth: int = 0) -> bool:
    """Return whether ``line`` contains a declaration closer (``>``).

    Top-level declarations end on any raw ``>`` (CommonMark type 4). When the
    opener was blockquote-nested, peel exactly ``blockquote_depth`` single
    ``>`` container markers (and intervening list markers) before testing so
    the opener's own ``>`` / continuation prefixes are not false closers
    (PRRT_kwDOSJAM6s6ZnUwf). Do not use full blockquote-prefix stripping —
    ``> >`` must leave a residual ``>`` as the real closer.
    """
    rest = line.lstrip(" \t")
    remaining = blockquote_depth
    while remaining > 0:
        bq = re.match(r"^>[ \t]?", rest)
        if bq is not None:
            rest = rest[bq.end() :]
            remaining -= 1
            continue
        lst = _MARKDOWN_LIST_PREFIX.match(rest)
        if lst is not None:
            rest = rest[lst.end() :]
            continue
        break
    return _HTML_DECLARATION_CLOSE.search(rest) is not None


def _html_cdata_opens(line: str) -> bool:
    """Return whether ``line`` opens a CommonMark HTML CDATA block.

    Normalize list/blockquote containers first so ``- <![CDATA[`` /
    ``> <![CDATA[`` enter shielding (PRRT_kwDOSJAM6s6ZnSrG).
    """
    return _HTML_CDATA_OPEN.match(_normalize_markdown_fence_line(line)) is not None


def _html_cdata_closes(line: str) -> bool:
    """Return whether ``line`` contains a CDATA closer (``]]>``)."""
    return _HTML_CDATA_CLOSE.search(line) is not None


def _html_type6_block_opens(line: str) -> bool:
    """Return whether ``line`` opens a CommonMark type-6 HTML block.

    Block tags such as ``div`` continue until a blank line. Normalize
    list/blockquote containers first so ``- <div>`` / ``> <div>`` enter
    shielding (PRRT_kwDOSJAM6s6ZnUxZ).
    """
    return _HTML_TYPE6_BLOCK_OPEN.match(_normalize_markdown_fence_line(line)) is not None


def _html_type7_block_opens(line: str) -> bool:
    """Return whether ``line`` opens a CommonMark type-7 HTML block.

    Complete open/close tags (including custom elements) continue until a
    blank line. Type-1 **open** tags are excluded (handled by the close-tag
    path); standalone type-1 **closers** still enter type 7
    (PRRT_kwDOSJAM6s6ZnUxZ / PRRT_kwDOSJAM6s6Zn6x4). Normalize containers
    like other raw-HTML openers.
    """
    return _HTML_TYPE7_BLOCK_OPEN.match(_normalize_markdown_fence_line(line)) is not None


def _html_type7_may_start_at(lines: Sequence[str], idx: int) -> bool:
    """Return whether a type-7 HTML block may start at ``lines[idx]``.

    CommonMark type 7 cannot interrupt a paragraph, so the opener must be at
    document start or immediately preceded by a blank line. Blockquote content
    blanks (``>`` / ``>   ``) also end paragraphs — reuse the same peeling
    type-6/7 termination already accepts via ``_html_blank_terminated_block_closes``
    at the prior line's blockquote depth (PRRT_kwDOSJAM6s6ZqYPp). Without that,
    a type-7 opener after ``>`` is refused and example markers stay visible,
    overriding an earlier ``NEEDS_HUMAN``. Type 6 may still interrupt and is
    not gated here.
    """
    if idx <= 0:
        return True
    prev = lines[idx - 1]
    return _html_blank_terminated_block_closes(
        prev, blockquote_depth=_markdown_blockquote_container_depth(prev)
    )


def _html_complete_code_opens(line: str) -> bool:
    """Return whether ``line`` is a complete standalone ``<code>`` opener.

    Complete ``<code>`` tags match the type-7 line shape but need hybrid
    shielding: close-tag through ``</code>`` so interior blanks do not end
    early, then blank-terminated tail after the closer (PRRT_kwDOSJAM6s6ZpPQA /
    PRRT_kwDOSJAM6s6ZpLqP). Self-closing and never-closed openers stay on pure
    blank termination (PRRT_kwDOSJAM6s6ZpTPI). Normalize containers like other
    raw-HTML openers.
    """
    return _HTML_COMPLETE_CODE_OPEN.match(_normalize_markdown_fence_line(line)) is not None


def _html_complete_code_self_closes(line: str) -> bool:
    """Return whether ``line`` is a complete self-closing ``<code/>`` tag.

    Self-closing complete ``<code/>`` never reaches ``</code>``, so hybrid
    close-tag + blank-tail shielding would suppress every later marker through
    EOF (PRRT_kwDOSJAM6s6ZpTPI). Normalize containers like other raw-HTML
    openers.
    """
    return _HTML_COMPLETE_CODE_SELF_CLOSING.match(_normalize_markdown_fence_line(line)) is not None


def _independent_region_mask_for_code_close_lookahead(
    lines: Sequence[str],
) -> tuple[bool, ...]:
    """Mark lines inside shields independent of complete ``<code>`` hybrid.

    Look-ahead for a hybrid ``</code>`` must ignore closers that only appear
    inside Markdown fences, HTML comments, and other regions that exist whether
    or not hybrid ``<code>`` mode is chosen — otherwise a fenced/comment example
    ``</code>`` falsely enables hybrid shielding and can suppress a later
    top-level ``NEEDS_HUMAN`` (PRRT_kwDOSJAM6s6ZpoBt). Complete ``<code>``
    openers are not entered here so a real unshielded ``</code>`` still counts.
    """
    n = len(lines)
    masked = [False] * n
    fence: str | None = None
    fence_container_indent = 0
    fence_blockquote_depth = 0
    html_tag: str | None = None
    html_comment = False
    html_pi = False
    html_declaration = False
    html_declaration_blockquote_depth = 0
    html_cdata = False
    html_blank_terminated = False
    html_blank_terminated_blockquote_depth = 0
    for idx, line in enumerate(lines):
        if fence is not None:
            masked[idx] = True
            if _markdown_fence_closes(
                line,
                fence=fence,
                container_indent=fence_container_indent,
                blockquote_depth=fence_blockquote_depth,
            ):
                fence = None
                fence_container_indent = 0
                fence_blockquote_depth = 0
            continue
        if html_comment:
            masked[idx] = True
            if _html_comment_closes(line):
                html_comment = False
            continue
        if html_pi:
            masked[idx] = True
            if _html_processing_instruction_closes(line):
                html_pi = False
            continue
        if html_declaration:
            masked[idx] = True
            if _html_declaration_closes(line, blockquote_depth=html_declaration_blockquote_depth):
                html_declaration = False
                html_declaration_blockquote_depth = 0
            continue
        if html_cdata:
            masked[idx] = True
            if _html_cdata_closes(line):
                html_cdata = False
            continue
        if html_tag is not None:
            masked[idx] = True
            if _html_code_block_closes(line, html_tag):
                html_tag = None
            continue
        if html_blank_terminated:
            masked[idx] = True
            if _html_blank_terminated_block_closes(
                line, blockquote_depth=html_blank_terminated_blockquote_depth
            ):
                html_blank_terminated = False
                html_blank_terminated_blockquote_depth = 0
            continue
        if _markdown_is_indented_code_line(line):
            masked[idx] = True
            continue
        if _html_comment_opens(line):
            masked[idx] = True
            if not _html_comment_closes(line):
                html_comment = True
            continue
        if _html_processing_instruction_opens(line):
            masked[idx] = True
            if not _html_processing_instruction_closes(line):
                html_pi = True
            continue
        if _html_cdata_opens(line):
            masked[idx] = True
            if not _html_cdata_closes(line):
                html_cdata = True
            continue
        if _html_declaration_opens(line):
            masked[idx] = True
            decl_bq_depth = _markdown_blockquote_container_depth(line)
            if not _html_declaration_closes(line, blockquote_depth=decl_bq_depth):
                html_declaration = True
                html_declaration_blockquote_depth = decl_bq_depth
            continue
        type6_opens = _html_type6_block_opens(line)
        type7_opens = _html_type7_block_opens(line)
        if type6_opens or (type7_opens and _html_type7_may_start_at(lines, idx)):
            # Do not treat complete ``<code>`` as an independent blank-terminated
            # region — that would mask a real later ``</code>`` and defeat hybrid
            # look-ahead. Self-closing / never-closed complete openers stay free.
            if _html_complete_code_opens(line):
                continue
            # Standalone ``</code>`` is itself a type-7 line; leave it unmasked so
            # hybrid look-ahead can count it (PRRT_kwDOSJAM6s6ZpoBt /
            # PRRT_kwDOSJAM6s6ZpLqP).
            if _html_code_block_closes(line, "code"):
                continue
            opened_bq_depth = _markdown_blockquote_container_depth(line)
            masked[idx] = True
            if not _html_blank_terminated_block_closes(line, blockquote_depth=opened_bq_depth):
                html_blank_terminated = True
                html_blank_terminated_blockquote_depth = opened_bq_depth
            continue
        if type7_opens:
            # Type-7 shape that cannot interrupt a paragraph: skip without
            # masking and without falling through to type-1/example ``code``
            # openers (complete ``<code>`` would otherwise close-tag shield).
            continue
        opened_html = _html_code_block_open_tag(line)
        if opened_html is not None:
            masked[idx] = True
            if not _html_code_block_closes(line, opened_html):
                html_tag = opened_html
            continue
        opened = _markdown_fence_open_marker(line)
        if opened is not None:
            masked[idx] = True
            fence = opened
            fence_container_indent = _markdown_fence_list_container_indent(line)
            fence_blockquote_depth = _markdown_blockquote_container_depth(line)
            continue
    return tuple(masked)


def _precompute_html_code_close_appears_from(lines: Sequence[str]) -> tuple[bool, ...]:
    """Return whether an unshielded ``</code>`` appears at or after each index.

    Reverse scan once so complete ``<code>`` openers can O(1)-check for a later
    closer instead of rescanning the remaining suffix on every opener
    (PRRT_kwDOSJAM6s6ZpaIp). Closers inside independent Markdown/HTML regions
    (fences, comments, …) are ignored so they cannot falsely enable hybrid
    shielding (PRRT_kwDOSJAM6s6ZpoBt).
    """
    n = len(lines)
    appears_from = [False] * n
    independent = _independent_region_mask_for_code_close_lookahead(lines)
    seen_close = False
    for i in range(n - 1, -1, -1):
        if not independent[i] and _html_code_block_closes(lines[i], "code"):
            seen_close = True
        appears_from[i] = seen_close
    return tuple(appears_from)


def _html_code_close_appears_later(lines: Sequence[str], start: int) -> bool:
    """Return whether an unshielded ``</code>`` appears at or after ``start``.

    Used so never-closed complete ``<code>`` openers keep type-7 blank
    termination instead of hybrid forever-shielding (PRRT_kwDOSJAM6s6ZpTPI).
    Prefer ``_precompute_html_code_close_appears_from`` inside the line
    iterator so repeated openers stay linear (PRRT_kwDOSJAM6s6ZpaIp).
    Shielded closers (fences/comments/…) do not count (PRRT_kwDOSJAM6s6ZpoBt).
    """
    if start < 0 or start >= len(lines):
        return False
    return _precompute_html_code_close_appears_from(lines)[start]


def _html_blank_terminated_block_closes(line: str, *, blockquote_depth: int = 0) -> bool:
    """Return whether ``line`` is a blank line ending a type-6/7 HTML block.

    Blockquote-nested type-6/7 blocks also terminate on ``>`` / ``>   `` content
    blank lines; peel exactly ``blockquote_depth`` single ``>`` container markers
    (and intervening list markers) before the blank check (PRRT_kwDOSJAM6s6ZnYwP).
    Do not use full blockquote-prefix stripping — nested ``> >`` / ``>>`` must
    not falsely end a single-level ``> <div>`` shield (PRRT_kwDOSJAM6s6ZnblF).
    Top-level blocks must not treat a bare ``>`` as blank.
    """
    if _HTML_BLANK_LINE.match(line) is not None:
        return True
    if blockquote_depth <= 0:
        return False
    rest = line.lstrip(" \t")
    remaining = blockquote_depth
    while remaining > 0:
        bq = re.match(r"^>[ \t]?", rest)
        if bq is not None:
            rest = rest[bq.end() :]
            remaining -= 1
            continue
        lst = _MARKDOWN_LIST_PREFIX.match(rest)
        if lst is not None:
            rest = rest[lst.end() :]
            continue
        return False
    return _HTML_BLANK_LINE.match(rest) is not None


def _markdown_is_indented_code_line(line: str) -> bool:
    """Return whether ``line`` is CommonMark indented code (incl. containers).

    Top-level indented code matches ``_MARKDOWN_INDENTED_CODE_LINE`` directly.
    Blockquote- or list-nested examples such as ``>     AWF-VERDICT: …`` /
    ``-     AWF-VERDICT: …`` begin with a container marker, so the raw-line
    regex misses them; peel each ``>`` or list marker (plus at most one
    following space/tab for ``>``, exactly one space/tab after a list marker,
    and optional 0–3 spaces before the next nested container) before
    re-testing. Do not use greedy ``(?:>\\s*)+`` or list ``\\s+`` stripping —
    that would consume the four-column code indent itself and let attempt
    detection treat the example as a garbled final (PRRT_kwDOSJAM6s6Zoxg4,
    PRRT_kwDOSJAM6s6Zo4bL).
    """
    if _MARKDOWN_INDENTED_CODE_LINE.match(line):
        return True
    rest = line
    peeled = False
    while True:
        lead = re.match(r"^ {0,3}", rest)
        if lead is None:  # pragma: no cover - `` {0,3}`` always matches
            return False
        after_lead = rest[lead.end() :]
        bq = re.match(r"^>[ \t]?", after_lead)
        if bq is not None:
            rest = after_lead[bq.end() :]
            peeled = True
            continue
        lst = re.match(r"^(?:[-*+]|\d+[.)])[ \t]", after_lead)
        if lst is not None:
            rest = after_lead[lst.end() :]
            peeled = True
            continue
        break
    return peeled and _MARKDOWN_INDENTED_CODE_LINE.match(rest) is not None


def _markdown_fence_closes(
    line: str,
    *,
    fence: str,
    container_indent: int = 0,
    blockquote_depth: int = 0,
) -> bool:
    """Return whether ``line`` closes a code fence opened with ``fence``.

    CommonMark allows at most three leading spaces on a closing fence relative
    to the opener's list container. Tabs expand to column stops of four before
    that indent check, so a list-nested closer like ``\\t~~~`` under
    ``- ~~~text`` matches (PRRT_kwDOSJAM6s6Zolex). Do not peel list markers: a
    line like ``- ``` `` inside an open fence is fence content, not a closer.
    Do not unrestricted-lstrip: four-space-indented content under a top-level
    opener (container_indent 0) that looks like a fence must stay inside the
    open region. Blockquote-opened fences peel exactly ``blockquote_depth``
    single ``>`` container markers (and intervening list markers) before the
    indent check — unrestricted ``>+`` would treat nested ``>> ``` `` as a
    closer for a depth-1 opener (PRRT_kwDOSJAM6s6Zn213). Top-level fences must
    not peel ``>`` (PRRT_kwDOSJAM6s6ZnAyG). Before that strip, allow
    ``0 .. container_indent+3`` spaces ahead of ``>`` so ordered-list+blockquote
    continuations (``    > ``` `` under ``10. > ```text``) match
    (PRRT_kwDOSJAM6s6ZnHH2). After that strip, allow indent
    ``0 .. container_indent+3`` so a normal ``> ``` `` closer still matches
    list+blockquote openers whose list width was recorded as
    ``container_indent`` (PRRT_kwDOSJAM6s6ZnDm7).
    """
    # List-nested closers (``    ``` `` under ``10. ```text``) need the
    # container indent; absolute 0–3 alone never closes them
    # (PRRT_kwDOSJAM6s6ZmsZS). Unlimited strip would still treat top-level
    # ``    ``` `` as a closer (PRRT_kwDOSJAM6s6ZmqRo). Expand tabs first so
    # ``\\t~~~`` under a list opener is measured in columns, not as a literal
    # tab that the spaces-only indent regex would miss (PRRT_kwDOSJAM6s6Zolex).
    expanded = line.expandtabs(4)
    candidate = expanded
    if blockquote_depth > 0:
        # Absolute `` {0,3}`` before ``>`` misses list-continuation closers
        # such as ``    > ``` `` when ``container_indent`` is 4.
        lead = re.match(rf"^ {{{0},{container_indent + 3}}}", expanded)
        if lead is None:  # pragma: no cover - `` {0,N}`` always matches
            return False
        rest = expanded[lead.end() :]
        remaining = blockquote_depth
        while remaining > 0:
            bq = re.match(r"^>[ \t]?", rest)
            if bq is not None:
                rest = rest[bq.end() :]
                remaining -= 1
                continue
            lst = _MARKDOWN_LIST_PREFIX.match(rest)
            if lst is not None:
                rest = rest[lst.end() :]
                continue
            return False
        candidate = rest
        # ``>`` peel removes the column occupied by list markers that sat
        # inside/after the quote (``- > ``` `` / ``> - ``` ``). Requiring
        # ``container_indent`` spaces on the residual would reject ``> ``` ``.
        min_indent = 0
    else:
        min_indent = container_indent
    max_indent = container_indent + 3
    return (
        re.match(
            rf"^ {{{min_indent},{max_indent}}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$",
            candidate,
        )
        is not None
    )


def _iter_non_fenced_verdict_lines(stdout: str) -> Iterable[str]:
    """Yield stripped stdout lines outside Markdown code regions.

    Skips multiline fenced blocks (including list- and blockquote-nested
    openers after normalizing container prefixes), CommonMark indented-code
    lines (four spaces of indent, including a leading tab or 1–3 spaces plus a
    tab, and the same indent after peeling blockquote and list containers so
    ``>     example`` / ``-     example`` stay shielded — PRRT_kwDOSJAM6s6Zoxg4,
    PRRT_kwDOSJAM6s6Zo4bL), raw HTML type-1 / example code blocks (``pre`` /
    ``script`` / ``style`` / ``textarea``, plus incomplete / same-line
    ``code`` wrappers; complete ``<code>`` openers use hybrid close-tag +
    blank-tail shielding — PRRT_kwDOSJAM6s6ZpLqP / PRRT_kwDOSJAM6s6ZpPQA),
    including list- and blockquote-nested openers, HTML comment blocks
    (``<!-- … -->``, including nested openers), CommonMark type-3–5 raw HTML
    blocks (processing instruction ``<?…?>``, declaration ``<!Letter…>``,
    CDATA ``<![CDATA[…]]>``, including nested openers), and CommonMark
    type-6/7 raw HTML blocks (block tags such as ``div`` and custom tags,
    continuing through their terminating blank line) so quoted example markers
    cannot override an authoritative unfenced verdict. Same-line wrapped
    fences (`` ```verdict``` ``) are still yielded so
    ``_CODE_FORMATTED_VERDICT_LINE`` can accept them; same-line HTML wrappers
    and comments are skipped entirely. Unclosed fences, HTML code blocks, HTML
    comments, type-3–5 raw HTML blocks, or type-6/7 blank-terminated blocks
    shield every subsequent line. Complete ``<code>`` openers use hybrid
    close-tag + blank-tail only when a later ``</code>`` exists; self-closing
    ``<code/>`` and never-closed ``<code>`` stay blank-terminated
    (PRRT_kwDOSJAM6s6ZpTPI).
    """
    fence: str | None = None
    fence_container_indent = 0
    fence_blockquote_depth = 0
    html_tag: str | None = None
    html_comment = False
    html_pi = False
    html_declaration = False
    html_declaration_blockquote_depth = 0
    html_cdata = False
    html_blank_terminated = False
    html_blank_terminated_blockquote_depth = 0
    html_code_blank_tail = False
    lines = stdout.splitlines()
    # One reverse pass: later unshielded ``</code>`` lookups stay O(1) per
    # opener so blank-separated unclosed ``<code>`` lines stay linear
    # (PRRT_kwDOSJAM6s6ZpaIp); fenced/comment closers are ignored
    # (PRRT_kwDOSJAM6s6ZpoBt).
    html_code_close_from = _precompute_html_code_close_appears_from(lines)
    for idx, line in enumerate(lines):
        if fence is not None:
            if _markdown_fence_closes(
                line,
                fence=fence,
                container_indent=fence_container_indent,
                blockquote_depth=fence_blockquote_depth,
            ):
                fence = None
                fence_container_indent = 0
                fence_blockquote_depth = 0
            continue
        if html_comment:
            if _html_comment_closes(line):
                html_comment = False
            continue
        if html_pi:
            if _html_processing_instruction_closes(line):
                html_pi = False
            continue
        if html_declaration:
            if _html_declaration_closes(line, blockquote_depth=html_declaration_blockquote_depth):
                html_declaration = False
                html_declaration_blockquote_depth = 0
            continue
        if html_cdata:
            if _html_cdata_closes(line):
                html_cdata = False
            continue
        if html_tag is not None:
            if _html_code_block_closes(line, html_tag):
                # Complete ``<code>`` openers continue blank-terminated after
                # ``</code>`` so post-close markers before the blank stay
                # shielded (PRRT_kwDOSJAM6s6ZpLqP) without ending early on
                # interior blanks (PRRT_kwDOSJAM6s6ZpPQA).
                if html_tag == "code" and html_code_blank_tail:
                    html_blank_terminated = True
                html_code_blank_tail = False
                html_tag = None
            continue
        if html_blank_terminated:
            if _html_blank_terminated_block_closes(
                line, blockquote_depth=html_blank_terminated_blockquote_depth
            ):
                html_blank_terminated = False
                html_blank_terminated_blockquote_depth = 0
            continue
        if _markdown_is_indented_code_line(line):
            continue
        if _html_comment_opens(line):
            # Same-line ``<!-- … -->`` is an example wrapper — skip it.
            if not _html_comment_closes(line):
                html_comment = True
            continue
        if _html_processing_instruction_opens(line):
            # Same-line ``<?…?>`` is an example wrapper — skip it.
            if not _html_processing_instruction_closes(line):
                html_pi = True
            continue
        if _html_cdata_opens(line):
            # Same-line ``<![CDATA[…]]>`` is an example wrapper — skip it.
            if not _html_cdata_closes(line):
                html_cdata = True
            continue
        if _html_declaration_opens(line):
            # Same-line ``<!DOCTYPE …>`` is an example wrapper — skip it.
            # Peel opener blockquote depth so ``> <!DOCTYPE`` is not a false
            # same-line closer (PRRT_kwDOSJAM6s6ZnUwf).
            decl_bq_depth = _markdown_blockquote_container_depth(line)
            if not _html_declaration_closes(line, blockquote_depth=decl_bq_depth):
                html_declaration = True
                html_declaration_blockquote_depth = decl_bq_depth
            continue
        # Type 6/7 before type-1 / example ``code`` openers. Complete
        # ``<code>`` lines match type 7 but take hybrid close-tag + blank-tail
        # shielding so interior blanks do not end early while markers after
        # ``</code>`` stay shielded until the blank (PRRT_kwDOSJAM6s6ZpLqP /
        # PRRT_kwDOSJAM6s6ZpPQA) — only when a later ``</code>`` exists.
        # Self-closing ``<code/>`` and never-closed ``<code>`` stay on pure
        # blank termination (PRRT_kwDOSJAM6s6ZpTPI). Incomplete / same-line
        # ``<code…`` still fall through to the close-tag path below.
        # Type 7 cannot interrupt a paragraph: require document start or a
        # preceding blank before blank-terminated / hybrid shielding
        # (PRRT_kwDOSJAM6s6ZqS4U). Type 6 may still interrupt.
        type6_opens = _html_type6_block_opens(line)
        type7_opens = _html_type7_block_opens(line)
        if type6_opens or (type7_opens and _html_type7_may_start_at(lines, idx)):
            # Type 6/7 continue until a blank line (same-line wrappers still
            # skip the opener line itself). Track opener blockquote depth so
            # matching ``>`` / ``>   `` content blanks can terminate without
            # treating nested ``> >`` / ``>>`` as blanks (PRRT_kwDOSJAM6s6ZnYwP /
            # PRRT_kwDOSJAM6s6ZnblF).
            opened_bq_depth = _markdown_blockquote_container_depth(line)
            if _html_complete_code_opens(line):
                # Same-line ``<code…></code>`` (rare attribute-embedded closer)
                # stays a one-line skip, matching the prior hybrid gate.
                if _html_code_block_closes(line, "code"):
                    continue
                next_idx = idx + 1
                if (
                    not _html_complete_code_self_closes(line)
                    and next_idx < len(html_code_close_from)
                    and html_code_close_from[next_idx]
                ):
                    html_tag = "code"
                    html_code_blank_tail = True
                    html_blank_terminated_blockquote_depth = opened_bq_depth
                elif not _html_blank_terminated_block_closes(
                    line, blockquote_depth=opened_bq_depth
                ):
                    html_blank_terminated = True
                    html_blank_terminated_blockquote_depth = opened_bq_depth
                continue
            if not _html_blank_terminated_block_closes(line, blockquote_depth=opened_bq_depth):
                html_blank_terminated = True
                html_blank_terminated_blockquote_depth = opened_bq_depth
            continue
        if type7_opens:
            # Cannot interrupt a paragraph — skip the tag line without shielding
            # and without falling through to type-1/example ``code`` openers.
            continue
        opened_html = _html_code_block_open_tag(line)
        if opened_html is not None:
            # Same-line ``<pre>…</pre>`` / ``<script>…</script>`` (etc.) is an
            # example wrapper, not an accepted formatted-verdict form — skip it.
            if not _html_code_block_closes(line, opened_html):
                html_tag = opened_html
            continue
        opened = _markdown_fence_open_marker(line)
        if opened is not None:
            fence = opened
            fence_container_indent = _markdown_fence_list_container_indent(line)
            # Track opener depth so closers peel exactly that many ``>``
            # markers (PRRT_kwDOSJAM6s6Zn213).
            fence_blockquote_depth = _markdown_blockquote_container_depth(line)
            continue
        yield line.strip()
