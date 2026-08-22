"""CommonMark block-level reference definitions for verdict parsing.

Extracted from ``helpers_verdict_emphasis`` so that module stays within the
first-party file line limit (``test_first_party_code_files_stay_under_line_limit``).
"""

from __future__ import annotations

import re

from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _COMMONMARK_ASCII_PUNCTUATION,
    _COMMONMARK_BACKSLASH_ESCAPED_PUNCT,
    _advance_past_markdown_link_reference_label,
    _iter_text_lines_with_offsets,
    _markdown_shielded_block_line_starts,
    _markdown_soft_shielded_block_line_starts,
    _peel_markdown_block_container_prefixes,
    _peel_markdown_reference_definition_container_pair,
    _peel_one_markdown_block_container_prefix,
)

__all__ = (
    "_MAX_MARKDOWN_REFERENCE_TITLE_ACCUMULATED_CHARS",
    "_MAX_MARKDOWN_REFERENCE_TITLE_CONTINUATION_LINES",
    "_markdown_link_label_text_is_bounded",
    "_markdown_normalize_link_reference_label",
    "_markdown_reference_definition_awaits_destination",
    "_markdown_reference_definition_awaits_title",
    "_match_markdown_reference_definition_line",
    "_markdown_line_is_setext_heading_underline",
    "_markdown_block_container_signature",
    "_markdown_block_container_transition_is_boundary",
    "_markdown_line_is_empty_list_item",
    "_markdown_line_is_leaf_block_boundary",
    "_markdown_reference_definition_spans",
)


def _markdown_link_label_text_is_bounded(label: str) -> bool:
    """Return whether ``label`` satisfies CommonMark link-label source limits.

    Used for inline / shortcut link text between ``[`` and ``]`` before
    reference resolution. Interior unescaped ``[`` and labels longer than 999
    source characters (backslash escapes count both; PRRT_kwDOSJAM6s6bVMBE)
    are invalid. Whitespace-only and empty labels are rejected.
    """
    if not label:
        return False
    wrapped = f"[{label}]"
    return _advance_past_markdown_link_reference_label(wrapped, 0) == len(wrapped)


def _markdown_normalize_link_reference_label(label: str) -> str:
    """Normalize a CommonMark link reference label for definition matching."""
    unescaped = _COMMONMARK_BACKSLASH_ESCAPED_PUNCT.sub(r"\1", label)
    return re.sub(r"[ \t\r\n]+", " ", unescaped.strip()).casefold()


def _markdown_reference_definition_awaits_destination(line: str) -> bool:
    """Return whether ``line`` is ``[label]:`` with only spaces/tabs after the colon.

    CommonMark §4.7 permits one line ending between the colon and the destination;
    such a prefix is not itself a complete definition
    (PRRT_kwDOSJAM6s6bVQlQ). Peel active blockquote/list containers first so a
    nested ``> [label]:`` opener is recognized (PRRT_kwDOSJAM6s6bVfyC).
    """
    line = _peel_markdown_block_container_prefixes(line)
    index = 0
    while index < len(line) and index < 3 and line[index] == " ":
        index += 1
    if index >= len(line) or line[index] != "[":
        return False
    label_end = _advance_past_markdown_link_reference_label(line, index)
    if label_end <= index:
        return False
    raw_label = line[index + 1 : label_end - 1]
    if raw_label == "":
        return False
    if label_end >= len(line) or line[label_end] != ":":
        return False
    return all(ch in " \t" for ch in line[label_end + 1 :])


def _markdown_reference_definition_awaits_title(line: str) -> bool:
    """Return whether ``line`` opens a link title that is not closed on this line.

    CommonMark §4.7 permits titles to extend over subsequent lines (no blank
    lines). An opened but unclosed title is not itself a complete definition
    (PRRT_kwDOSJAM6s6bVrCq). Peel active blockquote/list containers first.
    """
    line = _peel_markdown_block_container_prefixes(line)
    index = 0
    while index < len(line) and index < 3 and line[index] == " ":
        index += 1
    if index >= len(line) or line[index] != "[":
        return False
    label_end = _advance_past_markdown_link_reference_label(line, index)
    if label_end <= index:
        return False
    raw_label = line[index + 1 : label_end - 1]
    if raw_label == "":
        return False
    if label_end >= len(line) or line[label_end] != ":":
        return False
    rest = line[label_end + 1 :]
    cursor = 0
    while cursor < len(rest) and rest[cursor] in " \t":
        cursor += 1
    if cursor >= len(rest):
        return False
    if rest[cursor] == "<":
        # First unescaped ``>`` closes; escaped ``<`` / ``>`` are literal
        # (CommonMark §6.3; PRRT_kwDOSJAM6s6bV80o).
        cursor += 1
        while cursor < len(rest):
            ch = rest[cursor]
            if ch == "\n":
                return False
            if ch == "\\" and cursor + 1 < len(rest):
                cursor += 2
                continue
            if ch == ">":
                cursor += 1
                break
            if ch == "<":
                return False
            cursor += 1
        else:
            return False
    else:
        depth = 0
        dest_chars = 0
        while cursor < len(rest):
            ch = rest[cursor]
            if ch in " \t" and depth == 0:
                break
            code = ord(ch)
            if code <= 0x20 or code == 0x7F:
                return False
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
                    return False
                depth -= 1
            cursor += 1
            dest_chars += 1
        if dest_chars == 0 or depth != 0:
            return False
    title_ws = cursor
    while cursor < len(rest) and rest[cursor] in " \t":
        cursor += 1
    if cursor >= len(rest) or title_ws == cursor:
        return False
    title_opener = rest[cursor]
    if title_opener not in "\"'(":
        return False
    title_closer = ")" if title_opener == "(" else title_opener
    cursor += 1
    while cursor < len(rest):
        ch = rest[cursor]
        if ch == "\\" and cursor + 1 < len(rest):
            cursor += 2
            continue
        if title_opener == "(" and ch == "(":
            return False
        if ch == title_closer:
            return False
        cursor += 1
    return True


def _match_markdown_reference_definition_line(line: str) -> str | None:
    """Return a normalized label when ``line`` is a single-line reference definition.

    Peel active blockquote/list prefixes before the 0–3 space indent rule so
    definitions nested in containers still resolve document-wide
    (PRRT_kwDOSJAM6s6bVfyC).
    """
    line = _peel_markdown_block_container_prefixes(line)
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
        # First unescaped ``>`` closes; escaped ``<`` / ``>`` are literal.
        # A naive ``find(">")`` would accept ``[label]: <foo\>`` and register
        # the label, hiding stars in a malformed emphasized verdict
        # (PRRT_kwDOSJAM6s6bV80o). Match ``_advance_past_markdown_link_destination``.
        cursor += 1
        while cursor < len(rest):
            ch = rest[cursor]
            if ch == "\n":
                return None
            if ch == "\\" and cursor + 1 < len(rest):
                cursor += 2
                continue
            if ch == ">":
                cursor += 1
                break
            if ch == "<":
                return None
            cursor += 1
        else:
            return None
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
            # CommonMark §6.3: parenthesized titles contain no unescaped ``(``;
            # match the inline-link title parser (PRRT_kwDOSJAM6s6bVIzS).
            if title_opener == "(" and ch == "(":
                return None
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


# Cap multiline link-title continuation so rebuild+reparse of ``accumulated``
# cannot go quadratic on crafted agent stdout (PRRT_kwDOSJAM6s6bWCnP). Real
# CommonMark titles span only a few lines; values are generous for fixtures.
_MAX_MARKDOWN_REFERENCE_TITLE_CONTINUATION_LINES = 32
_MAX_MARKDOWN_REFERENCE_TITLE_ACCUMULATED_CHARS = 2048


def _markdown_line_is_setext_heading_underline(line: str) -> bool:
    """Return whether ``line`` is a CommonMark Setext heading underline.

    A Setext underline is 0–3 leading spaces, then one or more ``=`` or ``-``
    (not mixed), then optional trailing spaces/tabs — no interior whitespace
    between markers (unlike thematic breaks).
    """
    index = 0
    while index < len(line) and index < 3 and line[index] == " ":
        index += 1
    if index >= len(line):
        return False
    marker = line[index]
    if marker not in "=-":
        return False
    cursor = index
    while cursor < len(line) and line[cursor] == marker:
        cursor += 1
    while cursor < len(line) and line[cursor] in " \t":
        cursor += 1
    return cursor >= len(line)


def _markdown_block_container_signature(line: str) -> tuple[str, ...]:
    """Return leading blockquote/list container markers for ``line``.

    Each ``">"`` is one blockquote level. List markers are ``"l"`` when they may
    interrupt a paragraph (unordered, or ordered start value ``1``, including
    zero-padded forms such as ``01``) and ``"L"`` for ordered lists whose start
    is not ``1`` (PRRT_kwDOSJAM6s6bVyA3, PRRT_kwDOSJAM6s6bWS6u). Indent-only
    continuation lines yield ``()``.
    """
    markers: list[str] = []
    rest = line
    while True:
        lead = re.match(r"^ {0,3}", rest)
        if lead is None:  # pragma: no cover - `` {0,3}`` always matches
            return tuple(markers)
        after_lead = rest[lead.end() :]
        bq = re.match(r"^>[ \t]?", after_lead)
        if bq is not None:
            markers.append(">")
            rest = after_lead[bq.end() :]
            continue
        # Empty list items may end at EOL with no trailing space
        # (PRRT_kwDOSJAM6s6bWi6y). Mid-paragraph empty items are not LRD
        # blanks (PRRT_kwDOSJAM6s6bWpD7).
        ul = re.match(r"^[-*+](?:[ \t]|$)", after_lead)
        if ul is not None:
            markers.append("l")
            rest = after_lead[ul.end() :]
            continue
        ol = re.match(r"^([0-9]{1,9})[.)](?:[ \t]|$)", after_lead)
        if ol is not None:
            # CommonMark start is the integer value: ``01`` / ``001`` are start 1
            # and may interrupt a paragraph (PRRT_kwDOSJAM6s6bWS6u).
            markers.append("l" if int(ol.group(1)) == 1 else "L")
            rest = after_lead[ol.end() :]
            continue
        break
    return tuple(markers)


def _markdown_block_container_transition_is_boundary(
    prev_sig: tuple[str, ...],
    curr_sig: tuple[str, ...],
) -> bool:
    """Return whether container change on ``curr`` opens an LRD boundary.

    Entering or switching into a blockquote/list ends the prior block so a
    nested ``[label]: dest`` is valid without a blank line
    (PRRT_kwDOSJAM6s6bVrCs). Same-depth blockquote continuation is not a
    boundary. A list marker on the current line starts a new list item even
    when the signature matches the previous item. Bare leave to a
    non-container line (``> quote\\n[label]:``) must stay non-boundary —
    CommonMark lazy-continues that text into the paragraph.

    Non-1 ordered lists cannot interrupt a paragraph, but may replace an
    existing list marker at the same depth (PRRT_kwDOSJAM6s6bVyA3).
    """
    if not curr_sig:
        return False
    if curr_sig == prev_sig:
        return any(marker in "lL" for marker in curr_sig)
    index = 0
    while index < len(prev_sig) and index < len(curr_sig) and prev_sig[index] == curr_sig[index]:
        index += 1
    if index >= len(curr_sig):
        return False
    if curr_sig[index] == "L":
        return index < len(prev_sig) and prev_sig[index] in "lL"
    return True


def _markdown_line_is_empty_list_item(line: str) -> bool:
    """Return whether ``line`` is an empty list item after container peels.

    True when peeling blockquote/list markers leaves only a blank residual and
    at least one list marker was present — including bare EOL markers with no
    trailing space (``*`` / ``1.``). Empty blockquotes (``>``) are not list
    items (PRRT_kwDOSJAM6s6bWpD7).
    """
    peeled = _peel_markdown_block_container_prefixes(line)
    if not all(ch in " \t" for ch in peeled):
        return False
    return any(marker in "lL" for marker in _markdown_block_container_signature(line))


def _markdown_line_is_leaf_block_boundary(
    line: str,
    *,
    after_paragraph: bool = False,
) -> bool:
    """Return whether ``line`` ends an ordinary leaf block at the newline.

    ATX headings and thematic breaks always qualify. Setext underlines qualify
    only when they follow paragraph content (``after_paragraph=True``), matching
    CommonMark: the underline completes the heading leaf so a following
    ``[label]: dest`` is valid without an intervening blank line
    (PRRT_kwDOSJAM6s6bVZvh, PRRT_kwDOSJAM6s6bVkD0). Bare ``===`` at BOS or
    after a blank line is a paragraph, not a Setext underline.

    ``after_paragraph`` must reflect paragraph content in the *same* container
    context as ``line``. Callers must not set it across a blockquote/list
    entry: peeling reuses the flag for Setext on the peeled content, and an
    outer paragraph must not make ``> ===`` look like a Setext leaf
    (PRRT_kwDOSJAM6s6bWOTK).

    When the raw line is not a leaf, peel one blockquote/list marker at a time
    and re-test so nested leaves such as ``> # context`` or ``> * * *`` still
    establish a boundary for a same-container definition
    (PRRT_kwDOSJAM6s6bWLeD). Peel one layer per attempt: stripping every marker
    at once would turn ``> * * *`` into ``*`` and miss the thematic break.
    """
    candidate = line
    column_offset = 0
    while True:
        if _markdown_line_content_is_leaf_block_boundary(
            candidate,
            after_paragraph=after_paragraph,
        ):
            return True
        peeled = _peel_one_markdown_block_container_prefix(
            candidate,
            column_offset=column_offset,
        )
        if peeled is None:
            return False
        candidate, column_offset = peeled


def _markdown_line_content_is_leaf_block_boundary(
    line: str,
    *,
    after_paragraph: bool = False,
) -> bool:
    """Return whether container-free ``line`` is an ATX / thematic / Setext leaf."""
    index = 0
    while index < len(line) and index < 3 and line[index] == " ":
        index += 1
    if index >= len(line):
        return False
    # Thematic break: three or more matching -, _, or * with optional spaces.
    marker = line[index]
    if marker in "-_*":
        count = 0
        cursor = index
        while cursor < len(line):
            ch = line[cursor]
            if ch == marker:
                count += 1
                cursor += 1
                continue
            if ch in " \t":
                cursor += 1
                continue
            break
        if cursor >= len(line) and count >= 3:
            return True
    # ATX heading: 1–6 #, then space/tab or end of line.
    if line[index] == "#":
        hashes = 0
        cursor = index
        while cursor < len(line) and hashes < 7 and line[cursor] == "#":
            hashes += 1
            cursor += 1
        if 1 <= hashes <= 6 and (cursor >= len(line) or line[cursor] in " \t"):
            return True
    # Setext underline after paragraph content (PRRT_kwDOSJAM6s6bVkD0).
    return after_paragraph and _markdown_line_is_setext_heading_underline(line)


def _markdown_reference_definition_spans(
    text: str,
    *,
    bos_is_block_boundary: bool = True,
) -> list[tuple[int, int, str]]:
    """Return ``(start, end, normalized_label)`` for block-level reference definitions.

    Definitions are recognized only at block boundaries (beginning of string or
    after a blank line), matching CommonMark's rule that they cannot interrupt a
    paragraph. Container-only content blanks (``>`` / ``>    `` / list markers
    with empty residual, including bare EOL empty items ``-`` / ``1.``) count as
    blank after peeling active prefixes when already at a boundary
    (PRRT_kwDOSJAM6s6bWcMX, PRRT_kwDOSJAM6s6bWi6y). Mid-paragraph empty list
    items cannot interrupt a paragraph, so bare ``*`` / ``+`` / ``1.`` must not
    set ``prev_blank`` or open an LRD (PRRT_kwDOSJAM6s6bWpD7,
    PRRT_kwDOSJAM6s6bWpPB); bare ``-`` still can via Setext leaf-boundary.
    A blockquote/list prefix on the same line may still interrupt for a
    following same-container definition (``> *\\n> [label]: dest``), but an
    empty list marker must not peel into a document-level blank when the marker
    itself could not start a list. Consecutive definitions may follow each other.
    First definition
    for a normalized label wins.     When a boundary line is ``[label]:`` with only
    optional spaces/tabs after the colon, CommonMark permits one line ending
    before the destination: the immediate next non-blank line is consumed as the
    destination (and optional title) continuation (PRRT_kwDOSJAM6s6bVQlQ), except
    when that line opens a hard shield (fence / raw HTML) — those start a new
    block rather than supplying the destination (PRRT_kwDOSJAM6s6bVfyB). When a
    boundary line opens a title that is not closed, CommonMark permits the title
    to continue onto subsequent non-blank lines until the closer
    (PRRT_kwDOSJAM6s6bVrCq), subject to
    ``_MAX_MARKDOWN_REFERENCE_TITLE_CONTINUATION_LINES`` /
    ``_MAX_MARKDOWN_REFERENCE_TITLE_ACCUMULATED_CHARS`` so rebuild+reparse
    cannot stall on crafted output (PRRT_kwDOSJAM6s6bWCnP). Ordinary leaf
    blocks (ATX headings, thematic breaks) on a continuation end the
    unfinished definition instead of supplying title/destination text
    (PRRT_kwDOSJAM6s6bVyKH); Setext underlines are not leaf interrupts here
    (no preceding paragraph).

    Lines inside inactive Markdown/HTML block regions (fenced code, indented
    code, raw HTML example/comment/type-3–7 blocks) are skipped so quoted
    ``[label]: dest`` examples cannot resolve emphasis on a real verdict
    (PRRT_kwDOSJAM6s6bVBWU). Interior shielded lines do not update the
    blank-line boundary cursor, but leaving a closed hard shield (fence /
    raw HTML block) is a CommonMark block boundary: a following
    ``[label]: dest`` is valid without an extra blank line
    (PRRT_kwDOSJAM6s6bVMBG), including when the next line is soft-shielded
    indented code — the hard exit still records the boundary and the soft
    region preserves it (PRRT_kwDOSJAM6s6bVaBX). Soft shields — indented-code
    lines and non-interrupting type-7 HTML — are inactive for verdict
    selection but are not block boundaries; exiting them preserves
    ``prev_blank`` so a mid-paragraph ``[label]: dest`` cannot fail-open
    (PRRT_kwDOSJAM6s6bVP6L).     Ordinary leaf blocks such as ATX headings and
    thematic breaks likewise establish a boundary without a blank line
    (PRRT_kwDOSJAM6s6bVZvh). Setext underlines (``===`` / ``---`` / short
    ``-`` runs) complete a heading leaf when they follow paragraph content,
    so a following definition is valid without an extra blank line
    (PRRT_kwDOSJAM6s6bVkD0). Leaf-boundary detection peels blockquote/list
    prefixes so ``> # heading`` / ``> ---`` / nested Setext still open a
    boundary for a same-container definition (PRRT_kwDOSJAM6s6bWLeD).
    Setext ``after_paragraph`` is suppressed when the current line opens or
    switches into a new container so an outer paragraph cannot make
    ``> ===`` / ``- ===`` a false leaf boundary (PRRT_kwDOSJAM6s6bWOTK).
    Definitions nested in blockquotes or list items
    are recognized after peeling those container prefixes
    (PRRT_kwDOSJAM6s6bVfyC). Entering or switching into a blockquote/list
    (including a sibling list item) is itself a block boundary, so
    ``paragraph\\n> [label]: dest`` is valid without a blank line
    (PRRT_kwDOSJAM6s6bVrCs); same-depth blockquote continuation and bare
    leave-to-plain lazy continuation are not. Lazy continuation lines without
    container markers preserve the active blockquote/list context so a
    restored marker on the next line is not misclassified as a new container
    entry (PRRT_kwDOSJAM6s6bWzcZ). Non-1 ordered lists cannot
    interrupt a paragraph (PRRT_kwDOSJAM6s6bVyA3). A continuation that opens
    a *new* blockquote or list relative to the opener does not supply the
    destination (PRRT_kwDOSJAM6s6bVjt_).

    Set ``bos_is_block_boundary=False`` when ``text`` is a mid-paragraph fragment
    (for example a verdict reason after ``AWF-VERDICT: LABEL: ``) so a
    reason-leading ``[label]: dest`` is not treated as a definition
    (PRRT_kwDOSJAM6s6bUPZ6).
    """
    spans: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    shielded_starts = _markdown_shielded_block_line_starts(text)
    soft_shielded_starts = _markdown_soft_shielded_block_line_starts(text)
    offset = 0
    prev_blank = bos_is_block_boundary
    prev_container_sig: tuple[str, ...] = ()
    seen_prior_line = False
    length = len(text)
    indexed_lines = list(_iter_text_lines_with_offsets(text))
    line_at: dict[int, tuple[str, int]] = {}
    for i, (start, line) in enumerate(indexed_lines):
        next_start = indexed_lines[i + 1][0] if i + 1 < len(indexed_lines) else length
        line_at[start] = (line, next_start)
    while offset < length:
        line, next_offset = line_at[offset]
        if offset in shielded_starts:
            # Inactive regions are not definition hosts. Interior lines keep
            # the prior boundary cursor. Hard-shield exits (closed fence /
            # HTML) establish a block boundary even when the next line is
            # soft-shielded (PRRT_kwDOSJAM6s6bVaBX); soft-shield exits
            # (indented code, non-interrupting type-7) do not
            # (PRRT_kwDOSJAM6s6bVP6L).
            exited_soft = offset in soft_shielded_starts
            offset = next_offset
            next_still_hard = offset in shielded_starts and offset not in soft_shielded_starts
            if not exited_soft and not next_still_hard:
                prev_blank = True
                prev_container_sig = ()
            seen_prior_line = True
            continue
        # Content blanks after active container prefixes (``>`` / ``>    `` /
        # ``- ``) are blank boundaries the same way raw space-only lines are;
        # raw-line blankness would keep the container signature unchanged and
        # omit a following same-container LRD (PRRT_kwDOSJAM6s6bWcMX). Empty
        # list items mid-paragraph cannot interrupt (PRRT_kwDOSJAM6s6bWpD7).
        content_blank = all(ch in " \t" for ch in _peel_markdown_block_container_prefixes(line))
        empty_list_item = content_blank and _markdown_line_is_empty_list_item(line)
        if empty_list_item and not prev_blank:
            full_sig = _markdown_block_container_signature(line)
            sibling_list = (
                seen_prior_line
                and any(marker in "lL" for marker in prev_container_sig)
                and _markdown_block_container_transition_is_boundary(
                    prev_container_sig,
                    full_sig,
                )
            )
            if sibling_list:
                # Sibling empty list items end the prior item; container entry via
                # ``> *`` alone must not peel into a document-level blank
                # (PRRT_kwDOSJAM6s6bWpPB).
                is_blank = True
                curr_container_sig: tuple[str, ...] = ()
                container_boundary = True
            else:
                # Paragraph continuation (or Setext via leaf check below for ``-``).
                is_blank = False
                curr_container_sig = prev_container_sig
                container_boundary = False
        else:
            is_blank = content_blank
            curr_container_sig = () if is_blank else _markdown_block_container_signature(line)
            # Container transitions need a real prior line so a mid-paragraph
            # fragment (``bos_is_block_boundary=False``) cannot treat a leading
            # ``> [label]:`` as entering from an empty signature.
            container_boundary = (
                seen_prior_line
                and _markdown_block_container_transition_is_boundary(
                    prev_container_sig,
                    curr_container_sig,
                )
            )
        at_boundary = prev_blank or container_boundary
        if at_boundary and not is_blank:
            label = _match_markdown_reference_definition_line(line)
            span_end = next_offset
            if (
                label is None
                and next_offset < length
                and (
                    _markdown_reference_definition_awaits_destination(line)
                    or _markdown_reference_definition_awaits_title(line)
                )
            ):
                # One permitted line ending between colon and destination
                # (PRRT_kwDOSJAM6s6bVQlQ), and/or title lines after an opened
                # but unclosed title (PRRT_kwDOSJAM6s6bVrCq). Do not skip
                # soft-shielded continuations: leading spaces before a
                # destination are definition whitespace, not an indented-code
                # block. Hard-shielded openers (fences, raw HTML) start a new
                # block instead of supplying the destination/title
                # (PRRT_kwDOSJAM6s6bVfyB). Ordinary leaf blocks (ATX /
                # thematic) on the peeled continuation likewise interrupt
                # rather than fold into the title (PRRT_kwDOSJAM6s6bVyKH).
                # Peel containers on both lines before
                # combining so a nested ``> [label]:`` / ``> /url`` pair still
                # forms a definition (PRRT_kwDOSJAM6s6bVfyC). Do not peel a
                # *new* blockquote/list that starts on the continuation — that
                # ends the incomplete opener instead of supplying a
                # destination (PRRT_kwDOSJAM6s6bVjt_).
                cont_offset = next_offset
                accumulated: str | None = None
                title_continuation_lines = 0
                while cont_offset < length:
                    title_continuation_lines += 1
                    if title_continuation_lines > _MAX_MARKDOWN_REFERENCE_TITLE_CONTINUATION_LINES:
                        break
                    cont_line, cont_next = line_at[cont_offset]
                    hard_shield_opener = (
                        cont_offset in shielded_starts and cont_offset not in soft_shielded_starts
                    )
                    if not cont_line or all(ch in " \t" for ch in cont_line) or hard_shield_opener:
                        break
                    peeled_pair = _peel_markdown_reference_definition_container_pair(
                        line, cont_line
                    )
                    if peeled_pair is None:
                        break
                    peeled_opener, peeled_cont = peeled_pair
                    if _markdown_line_is_leaf_block_boundary(peeled_cont):
                        break
                    if accumulated is None:
                        accumulated = peeled_opener.rstrip(" \t") + " " + peeled_cont
                    else:
                        accumulated = accumulated + " " + peeled_cont
                    if len(accumulated) > _MAX_MARKDOWN_REFERENCE_TITLE_ACCUMULATED_CHARS:
                        break
                    cont_offset = cont_next
                    label = _match_markdown_reference_definition_line(accumulated)
                    if label is not None:
                        span_end = cont_offset
                        break
                    if not _markdown_reference_definition_awaits_title(accumulated):
                        break
            if label is not None:
                if label not in seen:
                    seen.add(label)
                    spans.append((offset, span_end, label))
                prev_blank = True
                prev_container_sig = curr_container_sig
                seen_prior_line = True
                offset = span_end
                continue
        # Setext needs paragraph content in the same container; do not inherit
        # outer paragraph state across blockquote/list entry
        # (PRRT_kwDOSJAM6s6bWOTK). ATX/thematic ignore this flag.
        prev_blank = is_blank or _markdown_line_is_leaf_block_boundary(
            line,
            after_paragraph=not prev_blank and not container_boundary,
        )
        # Markerless lazy continuations keep the prior container signature so a
        # restored ``>`` on the next line is not a false boundary
        # (PRRT_kwDOSJAM6s6bWzcZ).
        if not (not is_blank and not curr_container_sig and prev_container_sig and not prev_blank):
            prev_container_sig = curr_container_sig
        seen_prior_line = True
        offset = next_offset
    return spans
