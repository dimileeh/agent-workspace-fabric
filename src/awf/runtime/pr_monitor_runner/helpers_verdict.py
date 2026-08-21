"""PR-monitor verdict parsing helpers."""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Iterable, Sequence

from awf.common.redaction import redact_secrets
from awf.runtime.pr_monitor_runner.comments import (
    Verdict,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.constants import (
    _AWF_VERDICT,
    _AWF_VERDICT_MARKER,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_delimiters import (
    _ASCII_APOSTROPHE_CONTRACTION_SUFFIXES as _ASCII_APOSTROPHE_CONTRACTION_SUFFIXES,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_delimiters import (
    _ASCII_LEADING_ELISION_SUFFIXES as _ASCII_LEADING_ELISION_SUFFIXES,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_delimiters import (
    _EXPLICIT_VERDICT_CORRECTION_SEARCH_WINDOW as _EXPLICIT_VERDICT_CORRECTION_SEARCH_WINDOW,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_delimiters import (
    _EXPLICIT_VERDICT_CORRECTION_SEPARATOR as _EXPLICIT_VERDICT_CORRECTION_SEPARATOR,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_delimiters import (
    _advance_awf_verdict_delimiter_state as _advance_awf_verdict_delimiter_state,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_delimiters import (
    _ascii_apostrophe_is_contraction_suffix as _ascii_apostrophe_is_contraction_suffix,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_delimiters import (
    _ascii_apostrophe_is_leading_elision as _ascii_apostrophe_is_leading_elision,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_delimiters import (
    _ascii_double_quote_is_delimiter as _ascii_double_quote_is_delimiter,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_delimiters import (
    _ascii_quote_is_backslash_escaped as _ascii_quote_is_backslash_escaped,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_delimiters import (
    _ascii_single_quote_is_delimiter as _ascii_single_quote_is_delimiter,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_delimiters import (
    _awf_verdict_marker_embedded_in_reason_prose as _awf_verdict_marker_embedded_in_reason_prose,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_delimiters import (
    _awf_verdict_marker_unambiguously_separate_attempt as _awf_verdict_marker_unambiguously_separate_attempt,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_delimiters import (
    _AwfVerdictDelimiterState as _AwfVerdictDelimiterState,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_delimiters import (
    _prefix_has_explicit_verdict_correction_separator as _prefix_has_explicit_verdict_correction_separator,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (  # noqa: F401
    _BARE_VERDICT_LINE as _BARE_VERDICT_LINE,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _CODE_FORMATTED_VERDICT_LINE as _CODE_FORMATTED_VERDICT_LINE,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _FINAL_ANSWER_ATTEMPT_PREFIX as _FINAL_ANSWER_ATTEMPT_PREFIX,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _HTML_BLANK_LINE as _HTML_BLANK_LINE,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _HTML_CDATA_CLOSE as _HTML_CDATA_CLOSE,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _HTML_CDATA_OPEN as _HTML_CDATA_OPEN,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _HTML_CODE_BLOCK_OPEN as _HTML_CODE_BLOCK_OPEN,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _HTML_COMMENT_CLOSE as _HTML_COMMENT_CLOSE,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _HTML_COMMENT_OPEN as _HTML_COMMENT_OPEN,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _HTML_COMPLETE_CODE_OPEN as _HTML_COMPLETE_CODE_OPEN,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _HTML_COMPLETE_CODE_SELF_CLOSING as _HTML_COMPLETE_CODE_SELF_CLOSING,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _HTML_DECLARATION_CLOSE as _HTML_DECLARATION_CLOSE,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _HTML_DECLARATION_OPEN as _HTML_DECLARATION_OPEN,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _HTML_PROCESSING_INSTRUCTION_CLOSE as _HTML_PROCESSING_INSTRUCTION_CLOSE,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _HTML_PROCESSING_INSTRUCTION_OPEN as _HTML_PROCESSING_INSTRUCTION_OPEN,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _HTML_TYPE6_BLOCK_OPEN as _HTML_TYPE6_BLOCK_OPEN,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _HTML_TYPE6_BLOCK_TAGS as _HTML_TYPE6_BLOCK_TAGS,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _HTML_TYPE7_ATTR as _HTML_TYPE7_ATTR,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _HTML_TYPE7_BLOCK_OPEN as _HTML_TYPE7_BLOCK_OPEN,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _MARKDOWN_BLOCKQUOTE_PREFIX as _MARKDOWN_BLOCKQUOTE_PREFIX,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _MARKDOWN_EMPHASIS_PREFIX as _MARKDOWN_EMPHASIS_PREFIX,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _MARKDOWN_FENCE_OPEN as _MARKDOWN_FENCE_OPEN,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _MARKDOWN_HEADING_PREFIX as _MARKDOWN_HEADING_PREFIX,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _MARKDOWN_INDENTED_CODE_LINE as _MARKDOWN_INDENTED_CODE_LINE,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _MARKDOWN_LIST_PREFIX as _MARKDOWN_LIST_PREFIX,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _MARKDOWN_TASK_LIST_CHECKBOX as _MARKDOWN_TASK_LIST_CHECKBOX,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _MAX_VERDICT_REASON_LENGTH as _MAX_VERDICT_REASON_LENGTH,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _VERDICT_REASON_INLINE_EMPHASIS_WRAPPER as _VERDICT_REASON_INLINE_EMPHASIS_WRAPPER,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _VERDICT_REASON_INLINE_HTML_WRAPPER as _VERDICT_REASON_INLINE_HTML_WRAPPER,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _VERDICT_REASON_INLINE_QUOTE_WRAPPER as _VERDICT_REASON_INLINE_QUOTE_WRAPPER,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _VERDICT_REASON_PYTHON_DUNDER as _VERDICT_REASON_PYTHON_DUNDER,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _VERDICT_REASON_REDACTION_ONLY as _VERDICT_REASON_REDACTION_ONLY,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _VERDICT_REASON_TEMPLATE_PLACEHOLDER as _VERDICT_REASON_TEMPLATE_PLACEHOLDER,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _VERDICT_RESULT_LABEL_ATTEMPT_PREFIX as _VERDICT_RESULT_LABEL_ATTEMPT_PREFIX,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _html_blank_terminated_block_closes as _html_blank_terminated_block_closes,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _html_cdata_closes as _html_cdata_closes,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _html_cdata_opens as _html_cdata_opens,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _html_code_block_closes as _html_code_block_closes,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _html_code_block_open_tag as _html_code_block_open_tag,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _html_code_close_appears_later as _html_code_close_appears_later,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _html_comment_closes as _html_comment_closes,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _html_comment_opens as _html_comment_opens,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _html_complete_code_opens as _html_complete_code_opens,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _html_complete_code_self_closes as _html_complete_code_self_closes,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _html_declaration_closes as _html_declaration_closes,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _html_declaration_opens as _html_declaration_opens,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _html_processing_instruction_closes as _html_processing_instruction_closes,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _html_processing_instruction_opens as _html_processing_instruction_opens,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _html_type6_block_opens as _html_type6_block_opens,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _html_type7_block_opens as _html_type7_block_opens,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _html_type7_may_start_at as _html_type7_may_start_at,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _iter_non_fenced_verdict_lines as _iter_non_fenced_verdict_lines,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _markdown_blockquote_container_depth as _markdown_blockquote_container_depth,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _markdown_fence_closes as _markdown_fence_closes,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _markdown_fence_list_container_indent as _markdown_fence_list_container_indent,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _markdown_fence_open_marker as _markdown_fence_open_marker,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _normalize_markdown_fence_line as _normalize_markdown_fence_line,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _verdict_reason_inline_link_label as _verdict_reason_inline_link_label,
)

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
    "_HTML_COMPLETE_CODE_OPEN",
    "_HTML_COMPLETE_CODE_SELF_CLOSING",
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
    "_html_complete_code_opens",
    "_html_complete_code_self_closes",
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
    "_html_blank_terminated_block_closes",
    "_markdown_fence_closes",
    "_iter_non_fenced_verdict_lines",
    "_parse_verdict",
    "_parse_verdict_result",
    "_stdout_mentions_awf_verdict",
    "_RESOLVABLE_PLACEHOLDER_LABELS",
    "_normalized_verdict_reason_is_template_placeholder",
    "_verdict_reason_is_template_placeholder",
    "_verdict_reason_inline_link_label",
    "_normalize_verdict_reason_inline_formatting",
    "_last_awf_resolvable_reason_is_placeholder",
    "_fail_closed_resolvable_placeholder_if_needed",
    "_select_bare_verdict",
    "_strip_markdown_list_prefix",
    "_strip_markdown_task_list_checkbox",
    "_strip_markdown_blockquote_prefix",
    "_strip_markdown_emphasis_prefix",
    "_strip_markdown_heading_prefix",
    "_verdict_line_candidates",
    "_awf_verdict_segments",
    "_awf_verdict_leading_hard_block",
    "_awf_verdict_leading_hard_block_absorbs_later_marker",
    "_FIXED_REASON_ABSORBABLE_LABELS",
    "_awf_verdict_leading_fixed_absorbs_later_marker",
    "_EXPLICIT_VERDICT_CORRECTION_SEPARATOR",
    "_EXPLICIT_VERDICT_CORRECTION_SEARCH_WINDOW",
    "_prefix_has_explicit_verdict_correction_separator",
    "_AwfVerdictDelimiterState",
    "_advance_awf_verdict_delimiter_state",
    "_awf_verdict_marker_unambiguously_separate_attempt",
    "_awf_verdict_marker_embedded_in_reason_prose",
    "_ascii_quote_is_backslash_escaped",
    "_ascii_double_quote_is_delimiter",
    "_ascii_single_quote_is_delimiter",
    "_ASCII_APOSTROPHE_CONTRACTION_SUFFIXES",
    "_ASCII_LEADING_ELISION_SUFFIXES",
    "_ascii_apostrophe_is_contraction_suffix",
    "_ascii_apostrophe_is_leading_elision",
    "_strip_final_answer_attempt_prefix",
    "_strip_verdict_result_label_attempt_prefix",
    "_strip_markdown_attempt_prefixes",
    "_awf_verdict_segment_is_attempt",
    "_verdict_result_from_match",
    "_sanitize_verdict_reason",
    "_needs_human_reason_missing",
)


def _parse_verdict(stdout: str) -> Verdict:
    """Map the CLI's final message to a structured verdict.

    The prompt templates instruct the CLI to report a structured stdout
    ``AWF-VERDICT:`` line. Markerless, bare-marker, empty, garbled, and FIXED
    placeholder-echo output fail closed to ``needs_human`` — never guess
    ``fix_committed``.
    """
    return _parse_verdict_result(stdout).verdict


def _parse_verdict_result(stdout: str) -> VerdictResult:
    if not stdout.strip():
        # Empty or whitespace-only agent output is a failure to produce, not a
        # considered deferral. Treat it as needs_human so it blocks the merge
        # instead of
        # triggering the follow-up defer capture (comment + filed issue +
        # resolve) on a thread the agent never actually addressed (#305).
        return VerdictResult(verdict="needs_human", reason="empty_verdict_output")
    # AWF-prefixed verdicts are canonical. Bare FALSE POSITIVE / DEFER /
    # NEEDS_HUMAN lines are collected only as hard-block fallbacks when an AWF
    # FIXED line has no usable reason — never as a standalone selected verdict.
    # When multiple AWF verdicts are present, the final AWF line wins. If that
    # line omits a reason (and is not a template placeholder), preserve an
    # earlier reason for the same verdict. A final resolvable placeholder echo
    # must not reuse an earlier same-label reason — that would still resolve or
    # defer contrary to fail-closed grammar.
    # Sanitized non-blocking placeholders (for example
    # ``AWF-VERDICT: FIXED: <one-sentence summary>``) may fall back only to an
    # earlier reasoned hard block (needs_human/defer) or a bare blocking
    # fallback so a prompt echo cannot clear a hard block; the same hard-block
    # fallback applies to FALSE POSITIVE / DEFER placeholders so escalation text
    # is preserved. A genuine no-reason final verdict is otherwise the agent's
    # last word and must not be trumped by an earlier non-blocking verdict
    # (e.g. false_positive).
    # Blocking final verdicts remain authoritative even with no usable reason.
    # Standalone resolvable placeholder echoes fail closed (never resolve).
    # Markerless / bare-only output fails closed to needs_human.
    awf_verdicts: list[VerdictResult] = []
    bare_verdicts: list[VerdictResult] = []
    last_awf_mention_recognized = False
    saw_awf_mention = False
    # Skip fenced, indented, raw HTML type-1/example code, HTML comment, and
    # CommonMark type-3–5 (PI / declaration / CDATA) regions so quoted example
    # verdicts cannot override an authoritative unfenced marker
    # (PRRT_kwDOSJAM6s6ZlqAE / PRRT_kwDOSJAM6s6ZlsjH / PRRT_kwDOSJAM6s6ZnEAt /
    # PRRT_kwDOSJAM6s6ZnN2F / PRRT_kwDOSJAM6s6ZnQhP / PRRT_kwDOSJAM6s6ZnSrG).
    for stripped in _iter_non_fenced_verdict_lines(stdout):
        for verdict_line in _verdict_line_candidates(stripped):
            # Multiple markers on one line are separate verdict units — do not
            # let the first reason group absorb a trailing second marker.
            for segment in _awf_verdict_segments(verdict_line):
                # Underscore emphasis (``_AWF-VERDICT: …``) glues to the marker
                # so ``\b`` search misses the raw form. Still count it as an
                # attempt: a balanced direct wrapper is followed by its
                # normalized candidate, while malformed wrappers fail closed.
                is_attempt = _awf_verdict_segment_is_attempt(segment)
                if _AWF_VERDICT_MARKER.search(segment) or is_attempt:
                    saw_awf_mention = True
                    awf_match = _AWF_VERDICT.fullmatch(segment)
                    if awf_match is not None:
                        last_awf_mention_recognized = True
                        awf_verdicts.append(
                            _verdict_result_from_match(
                                label=awf_match.group("label"),
                                reason=awf_match.group("reason"),
                            )
                        )
                    elif is_attempt:
                        # Leading / split-marker attempts that fail fullmatch are
                        # authoritative garbled finals. Mid-prose quotes of the
                        # marker grammar must not clear a prior recognized verdict.
                        last_awf_mention_recognized = False
                bare_match = _BARE_VERDICT_LINE.fullmatch(segment)
                if bare_match is not None:
                    bare_verdicts.append(
                        _verdict_result_from_match(
                            label=bare_match.group("label"),
                            reason=bare_match.group("reason"),
                        )
                    )
    # A garbled final leading ``AWF-VERDICT:`` attempt fails closed even when an
    # earlier recognized verdict exists — the agent's last marker attempt is
    # authoritative. Embedded prose quotes of the marker do not count.
    if saw_awf_mention and not last_awf_mention_recognized:
        return VerdictResult(verdict="needs_human", reason="garbled_verdict_marker")
    if awf_verdicts:
        latest = awf_verdicts[-1]
        if latest.reason is None:
            latest_verdict = latest.verdict
            # Check the final raw reason before same-label reuse: a template
            # placeholder must not inherit an earlier reasoned same-label verdict
            # (that would still resolve/defer, contrary to fail-closed grammar).
            final_is_resolvable_placeholder = (
                latest_verdict in _RESOLVABLE_PLACEHOLDER_LABELS
                and _last_awf_resolvable_reason_is_placeholder(stdout, verdict=latest_verdict)
            )
            if not final_is_resolvable_placeholder:
                for parsed in reversed(awf_verdicts[:-1]):
                    if parsed.verdict == latest_verdict and parsed.reason is not None:
                        return parsed
            # Template-placeholder echoes (reason sanitized to None) fail closed for
            # every resolvable verdict. Any resolvable placeholder may still fall back
            # to an earlier reasoned hard block (#676 / #822 PRRT_kwDOSJAM6s6ZlxgI),
            # but never to the same label as the placeholder (that would still
            # resolve/defer). FALSE POSITIVE / DEFER placeholders never resolve alone.
            if final_is_resolvable_placeholder:
                for parsed in reversed(awf_verdicts[:-1]):
                    if (
                        parsed.verdict in {"needs_human", "defer"}
                        and parsed.verdict != latest_verdict
                        and parsed.reason is not None
                    ):
                        return parsed
                bare_blocking = _select_bare_verdict(
                    bare_verdicts,
                    # Exclude same-label DEFER so a DEFER placeholder cannot
                    # reuse an earlier reasoned DEFER (fail-closed same-label).
                    priorities=(
                        ("needs_human",) if latest_verdict == "defer" else ("needs_human", "defer")
                    ),
                )
                if bare_blocking is not None:
                    return bare_blocking
                return _fail_closed_resolvable_placeholder_if_needed(stdout, latest)
            if latest_verdict in {"defer", "needs_human"}:
                return latest
            for parsed in reversed(awf_verdicts[:-1]):
                if parsed.verdict in {"needs_human", "defer"} and parsed.reason is not None:
                    return parsed
            bare_blocking = _select_bare_verdict(
                bare_verdicts,
                priorities=("needs_human", "defer"),
            )
            if bare_blocking is not None:
                return bare_blocking
            return latest
        return latest
    if _stdout_mentions_awf_verdict(stdout):
        return VerdictResult(verdict="needs_human", reason="garbled_verdict_marker")
    return VerdictResult(verdict="needs_human", reason="unrecognized_or_markerless_verdict")


def _stdout_mentions_awf_verdict(stdout: str) -> bool:
    for stripped in _iter_non_fenced_verdict_lines(stdout):
        if _AWF_VERDICT_MARKER.search(stripped):
            return True
    return False


_RESOLVABLE_PLACEHOLDER_LABELS = {
    "fix_committed": "fixed",
    "false_positive": "false positive",
    "defer": "defer",
}


def _normalized_verdict_reason_is_template_placeholder(cleaned: str) -> bool:
    """Return whether an already-normalized reason is a template-placeholder echo.

    Whole-reason matches cover the common case. Same-line absorption can also
    fold later ``AWF-VERDICT`` citations into a leading placeholder prefix; that
    must still count as placeholder-shaped even though the absorbed markers
    prevent a whole-reason match (PRRT_kwDOSJAM6s6Znin1). Leading template-
    shaped tags that continue with real prose (no absorbed marker) stay usable.

    HTML entity-escaped whole-reason echoes (``&lt;reason&gt;``, numeric
    ``&#60;…&#62;``, and nested ``&amp;lt;…&amp;gt;``), CommonMark
    backslash-escaped echoes (``\\<reason\\>``), and mixed layers
    (``\\&lt;reason\\&gt;``) are decoded successively only for this anchored
    check so escaped template paste cannot resolve as a substantive reason,
    while mid-reason prose containing entity- or backslash-escaped tags stays
    usable (PRRT_kwDOSJAM6s6Zoyj2, PRRT_kwDOSJAM6s6Zo4bG, PRRT_kwDOSJAM6s6ZpA-z,
    PRRT_kwDOSJAM6s6ZpHXM).

    Callers must pass text that has already been through
    ``_normalize_verdict_reason_inline_formatting`` (or an equivalent peel) so
    the single-emphasis peel gate can reuse this without re-entering normalize
    (PRRT_kwDOSJAM6s6ZoGYD).
    """
    if not cleaned:
        return False
    if _text_matches_verdict_reason_template_placeholder(cleaned):
        return True
    marker = _AWF_VERDICT_MARKER.search(cleaned)
    if marker is None or marker.start() == 0:
        return False
    prefix = _normalize_verdict_reason_inline_formatting(cleaned[: marker.start()])
    return bool(prefix) and _text_matches_verdict_reason_template_placeholder(prefix)


# Bound nested ``html.unescape`` so ``&amp;lt;reason&amp;gt;`` reaches
# ``<reason>`` without unbounded loops on adversarial entity chains.
_VERDICT_REASON_HTML_UNESCAPE_MAX_PASSES = 4
# Same bound for nested CommonMark backslash escapes (``\\\<reason\\\>`` →
# ``\<reason\>`` → ``<reason>``).
_VERDICT_REASON_BACKSLASH_UNESCAPE_MAX_PASSES = 4
# Alternating HTML + CommonMark passes for mixed layers such as
# ``\&lt;reason\&gt;`` (each transform alone leaves one encoding behind).
_VERDICT_REASON_MIXED_ESCAPE_MAX_PASSES = 4
# CommonMark: a backslash before any ASCII punctuation yields the literal.
# Same ASCII set is punctuation for flanking (Pc–Ps alone miss Sc/Sm/Sk chars
# such as ``$``, ``+``, ``^`` — PRRT_kwDOSJAM6s6bSZP4).
_COMMONMARK_ASCII_PUNCTUATION = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_COMMONMARK_BACKSLASH_ESCAPED_PUNCT = re.compile(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])")


def _html_unescape_to_stable(text: str) -> str:
    """Decode HTML entities until stable or the pass bound is hit."""
    decoded = text
    for _ in range(_VERDICT_REASON_HTML_UNESCAPE_MAX_PASSES):
        nxt = html.unescape(decoded)
        if nxt == decoded:
            return decoded
        decoded = nxt
    return decoded


def _commonmark_backslash_unescape_to_stable(text: str) -> str:
    """Decode CommonMark backslash-escaped ASCII punctuation until stable.

    Agents Markdown-escape template echoes as ``\\<reason\\>``; one regex pass
    peels a single escape layer, so nested forms need a bounded loop
    (PRRT_kwDOSJAM6s6ZpA-z).
    """
    decoded = text
    for _ in range(_VERDICT_REASON_BACKSLASH_UNESCAPE_MAX_PASSES):
        nxt = _COMMONMARK_BACKSLASH_ESCAPED_PUNCT.sub(r"\1", decoded)
        if nxt == decoded:
            return decoded
        decoded = nxt
    return decoded


def _mixed_escape_unescape_to_stable(text: str) -> tuple[str, bool]:
    """Decode interleaved HTML entities and CommonMark backslash escapes.

    Independent HTML-only or backslash-only normalization leaves one layer on
    mixed echoes such as ``\\&lt;reason\\&gt;`` (HTML → ``\\<reason\\>``,
    backslash → ``&lt;reason&gt;``). Alternate both until stable so the
    placeholder matcher sees ``<reason>`` (PRRT_kwDOSJAM6s6ZpHXM).

    Returns ``(decoded, cap_exhausted)``. ``cap_exhausted`` is True when the
    pass budget is spent and another mixed pass would still change the value
    — callers must fail closed rather than treating a still-encoded remnant as
    substantive (PRRT_kwDOSJAM6s6Zqip7).
    """
    decoded = text
    for _ in range(_VERDICT_REASON_MIXED_ESCAPE_MAX_PASSES):
        nxt = _html_unescape_to_stable(decoded)
        nxt = _commonmark_backslash_unescape_to_stable(nxt)
        if nxt == decoded:
            return decoded, False
        decoded = nxt
    # Budget spent without an in-loop stability hit: probe whether more layers
    # remain without consuming another peel (exact-budget cases stay usable).
    probe = _html_unescape_to_stable(decoded)
    probe = _commonmark_backslash_unescape_to_stable(probe)
    return decoded, probe != decoded


def _text_matches_verdict_reason_template_placeholder(text: str) -> bool:
    """Return whether ``text`` matches the anchored template-placeholder pattern.

    Tries the raw text first, then a bounded copy with HTML entities and
    CommonMark backslash escapes applied successively until stable so
    ``&lt;reason&gt;`` / ``&#60;reason&#62;`` / nested ``&amp;lt;…&amp;gt;``,
    ``\\<reason\\>``, and mixed ``\\&lt;reason\\&gt;`` whole-reason echoes fail
    closed — without loosening the anchored match for mid-reason prose
    (PRRT_kwDOSJAM6s6Zoyj2, PRRT_kwDOSJAM6s6Zo4bG, PRRT_kwDOSJAM6s6ZpA-z,
    PRRT_kwDOSJAM6s6ZpHXM). When the pass cap is exhausted before stability,
    treat the text as an unresolved placeholder rather than a substantive
    reason (PRRT_kwDOSJAM6s6Zqip7).
    """
    if _VERDICT_REASON_TEMPLATE_PLACEHOLDER.search(text):
        return True
    decoded, cap_exhausted = _mixed_escape_unescape_to_stable(text)
    if cap_exhausted:
        return True
    return decoded != text and _VERDICT_REASON_TEMPLATE_PLACEHOLDER.search(decoded) is not None


def _verdict_reason_is_template_placeholder(reason: str) -> bool:
    """Return whether ``reason`` is a prompt-template placeholder echo."""
    cleaned = _normalize_verdict_reason_inline_formatting(reason.strip())
    return _normalized_verdict_reason_is_template_placeholder(cleaned)


def _peel_one_unconditional_verdict_reason_wrapper(cleaned: str) -> str | None:
    """Peel one outer tick/quote/strong/strike wrapper, or return None.

    Prefer ``_peel_all_outer_unconditional_verdict_reason_wrappers`` for nested
    peels — repeated calls here are quadratic on deep nests
    (PRRT_kwDOSJAM6s6ZqS4V).
    """
    tick_match = _CODE_FORMATTED_VERDICT_LINE.fullmatch(cleaned)
    if tick_match is not None:
        return tick_match.group("line").strip()
    quote_match = _VERDICT_REASON_INLINE_QUOTE_WRAPPER.fullmatch(cleaned)
    if quote_match is not None:
        return next(g for g in quote_match.groups() if g is not None).strip()
    emphasis_match = _VERDICT_REASON_INLINE_EMPHASIS_WRAPPER.fullmatch(cleaned)
    if emphasis_match is None:
        return None
    # Underscore strong collides with Python dunders; keep those intact.
    if emphasis_match.group("strong_under") is not None and _span_is_python_dunder(
        cleaned, 0, len(cleaned)
    ):
        return None
    # Single emphasis is placeholder-gated; callers handle it separately.
    if emphasis_match.group("em_star") is not None or emphasis_match.group("em_under") is not None:
        return None
    return next(g for g in emphasis_match.groups() if g is not None).strip()


# Paired quote delimiters for whole-reason unconditional peels (ASCII + curly).
_VERDICT_REASON_UNCONDITIONAL_QUOTE_PAIRS: tuple[tuple[str, str], ...] = (
    ('"', '"'),
    ("'", "'"),
    ("\u201c", "\u201d"),
    ("\u2018", "\u2019"),
)
# Strong / GFM strikethrough markers peeled without placeholder gating.
_VERDICT_REASON_UNCONDITIONAL_MARKERS: tuple[str, ...] = ("**", "__", "~~")


def _span_is_python_dunder(s: str, start: int, end: int) -> bool:
    """Return whether ``s[start:end]`` is a Python dunder (``__ident__``).

    Matches ``_VERDICT_REASON_PYTHON_DUNDER`` with cursor-local checks so nested
    ``__`` peels do not allocate slices or ``fullmatch``-rescan the remaining
    span on every layer (PRRT_kwDOSJAM6s6ZqZoz).
    """
    # Minimum ``__a__``; charset is ASCII ``[A-Za-z_][A-Za-z0-9_]*``.
    if end - start < 5:
        return False
    if s[start] != "_" or s[start + 1] != "_" or s[end - 2] != "_" or s[end - 1] != "_":
        return False
    i = start + 2
    stop = end - 2
    first = s[i]
    if first != "_" and not ("A" <= first <= "Z" or "a" <= first <= "z"):
        return False
    i += 1
    while i < stop:
        ch = s[i]
        if ch != "_" and not ("A" <= ch <= "Z" or "a" <= ch <= "z" or "0" <= ch <= "9"):
            return False
        i += 1
    return True


def _peel_all_outer_unconditional_verdict_reason_wrappers(cleaned: str) -> str:
    """Peel consecutive outer tick/quote/strong/strike wrappers in linear time.

    Same whole-reason semantics as repeated
    ``_peel_one_unconditional_verdict_reason_wrapper`` matches, without per-layer
    full-string ``fullmatch`` rescans that are quadratic on deep nests
    (PRRT_kwDOSJAM6s6ZqS4V, PRRT_kwDOSJAM6s6ZqZoz).
    """
    s = cleaned
    left = 0
    right = len(s)
    while left < right and s[left].isspace():
        left += 1
    while right > left and s[right - 1].isspace():
        right -= 1

    def _strip_inner() -> None:
        nonlocal left, right
        while left < right and s[left].isspace():
            left += 1
        while right > left and s[right - 1].isspace():
            right -= 1

    while left < right:
        lead = 0
        while left + lead < right and s[left + lead] == "`":
            lead += 1
        if lead > 0:
            trail = 0
            while right - trail > left and s[right - 1 - trail] == "`":
                trail += 1
            n = min(lead, trail)
            span = right - left
            while n >= 1 and 2 * n > span:
                n -= 1
            if n >= 1:
                left += n
                right -= n
                _strip_inner()
                continue

        quote_peeled = False
        for open_q, close_q in _VERDICT_REASON_UNCONDITIONAL_QUOTE_PAIRS:
            open_len = len(open_q)
            close_len = len(close_q)
            if (
                right - left >= open_len + close_len
                and s[left : left + open_len] == open_q
                and s[right - close_len : right] == close_q
            ):
                left += open_len
                right -= close_len
                _strip_inner()
                quote_peeled = True
                break
        if quote_peeled:
            continue

        marker_peeled = False
        for marker in _VERDICT_REASON_UNCONDITIONAL_MARKERS:
            mlen = len(marker)
            if right - left < 2 * mlen:
                continue
            if s[left : left + mlen] != marker or s[right - mlen : right] != marker:
                continue
            if marker == "__":
                # One dunder check, then peel consecutive ``__`` layers without
                # rescanning unless whitespace strip can reveal ``__ident__``.
                if _span_is_python_dunder(s, left, right):
                    continue
                while True:
                    left += 2
                    right -= 2
                    before_left, before_right = left, right
                    _strip_inner()
                    if not (
                        right - left >= 4
                        and s[left : left + 2] == "__"
                        and s[right - 2 : right] == "__"
                    ):
                        break
                    if (left != before_left or right != before_right) and _span_is_python_dunder(
                        s, left, right
                    ):
                        break
                marker_peeled = True
                break
            left += mlen
            right -= mlen
            _strip_inner()
            marker_peeled = True
            break
        if not marker_peeled:
            break

    return s[left:right].strip()


def _conditional_verdict_reason_wrapper_inner(cleaned: str) -> str | None:
    """Return the inner text of one outer placeholder-gated wrapper, or None.

    Covers single emphasis, safe inline HTML (em/strong/span/code), and
    whole-reason Markdown links/images (inline, full/collapsed reference, or
    shortcut).
    """
    emphasis_match = _VERDICT_REASON_INLINE_EMPHASIS_WRAPPER.fullmatch(cleaned)
    if emphasis_match is not None and (
        emphasis_match.group("em_star") is not None or emphasis_match.group("em_under") is not None
    ):
        return next(g for g in emphasis_match.groups() if g is not None).strip()
    html_match = _VERDICT_REASON_INLINE_HTML_WRAPPER.fullmatch(cleaned)
    if html_match is not None:
        return html_match.group("inner").strip()
    link_label = _verdict_reason_inline_link_label(cleaned)
    if link_label is not None:
        return link_label.strip()
    return None


# Open tag at a cursor for safe whole-reason HTML wrappers. Attributes allowed
# via ``_HTML_TYPE7_ATTR`` so a quoted ``>`` does not truncate the open tag
# (PRRT_kwDOSJAM6s6ZsvLy); trailing whitespace after ``>`` is consumed so
# layered peels stay tight.
_VERDICT_REASON_HTML_WRAPPER_OPEN_AT = re.compile(
    rf"<(em|strong|span|code){_HTML_TYPE7_ATTR}*\s*>\s*",
    re.IGNORECASE,
)


def _html_wrapper_close_suffix_start(s: str, start: int, end: int, tag: str) -> int | None:
    """Return index where ``\\s*</tag\\s*>`` begins in ``s[start:end]``, or None.

    Suffix matching is O(tag length) so deep nested peels stay linear
    (PRRT_kwDOSJAM6s6Zpjww).
    """
    if end <= start or s[end - 1] != ">":
        return None
    i = end - 1
    while i > start and s[i - 1].isspace():
        i -= 1
    tag_len = len(tag)
    if i - tag_len < start or s[i - tag_len : i].lower() != tag.lower():
        return None
    i -= tag_len
    if i <= start or s[i - 1] != "/":
        return None
    i -= 1
    if i <= start or s[i - 1] != "<":
        return None
    i -= 1
    while i > start and s[i - 1].isspace():
        i -= 1
    return i


def _peel_all_outer_html_verdict_reason_wrappers(cleaned: str) -> str:
    """Peel consecutive outer ``em``/``strong``/``span``/``code`` wrappers in linear time.

    Same whole-reason semantics as repeated ``_VERDICT_REASON_INLINE_HTML_WRAPPER``
    matches, without per-layer full-string ``fullmatch`` rescans that are
    quadratic on deep nests (PRRT_kwDOSJAM6s6Zpjww).
    """
    s = cleaned
    left = 0
    right = len(s)
    while left < right and s[left].isspace():
        left += 1
    while right > left and s[right - 1].isspace():
        right -= 1
    while left < right:
        open_m = _VERDICT_REASON_HTML_WRAPPER_OPEN_AT.match(s, left, right)
        if open_m is None:
            break
        close_start = _html_wrapper_close_suffix_start(s, open_m.end(), right, open_m.group(1))
        if close_start is None:
            break
        if close_start < open_m.end():  # pragma: no cover - overlap is defensive
            break
        left = open_m.end()
        right = close_start
    return s[left:right].strip()


def _aggressively_peel_verdict_reason_wrappers(reason: str) -> str:
    """Iteratively peel every wrapper type without placeholder gating.

    Used only to decide whether a conditional peel would expose a
    placeholder-shaped value. Bound iterations by input length so deeply
    nested ``<em>``/``<strong>``/``<span>``/``<code>`` chains cannot recurse
    (PRRT_kwDOSJAM6s6Zpg0B). HTML wrapper chains are stripped first in a
    linear pass so speculative peels stay O(n) rather than quadratic
    (PRRT_kwDOSJAM6s6Zpjww). Unconditional tick/quote/strong/strike chains
    use the same linear cursor peel (PRRT_kwDOSJAM6s6ZqS4V).
    """
    cleaned = _peel_all_outer_unconditional_verdict_reason_wrappers(
        _peel_all_outer_html_verdict_reason_wrappers(reason.strip())
    )
    for _ in range(max(len(cleaned), 1)):
        inner = _conditional_verdict_reason_wrapper_inner(cleaned)
        if inner is None:
            break
        # Mixed wrappers may reintroduce HTML / unconditional layers
        # (e.g. ``**<em>…</em>**`` / ``*"…"*``); re-run linear peels.
        cleaned = _peel_all_outer_unconditional_verdict_reason_wrappers(
            _peel_all_outer_html_verdict_reason_wrappers(inner)
        )
    return cleaned


def _normalize_verdict_reason_inline_formatting(reason: str) -> str:
    """Peel balanced outer quote/backtick/strong/strike/link/HTML wrappers from a reason.

    Agents often echo prompt placeholders inside inline code, quotes,
    Markdown strong/emphasis/strikethrough markers, Markdown links,
    Markdown images, or safe inline HTML wrappers
    (`` `<one-sentence justification>` `` / ``"<…>"`` / ``**<…>**`` /
    ``__<…>__`` / ``~~<…>~~`` / ``*<…>*`` / ``_<…>_`` /
    ``[<…>](https://example.com)`` / ``![<…>](https://example.com)`` /
    ``[<…>][ref]`` / ``[<…>][]`` / ``[<…>]`` / ``<em>&lt;…&gt;</em>`` /
    ``<strong>…</strong>`` / ``<span>…</span>`` / ``<code>…</code>``).
    Those wrappers must not defeat whole-reason placeholder detection
    (PRRT_kwDOSJAM6s6Zn-VK, PRRT_kwDOSJAM6s6ZoAz9, PRRT_kwDOSJAM6s6ZoDQU,
    PRRT_kwDOSJAM6s6ZopxG, PRRT_kwDOSJAM6s6Zos6S, PRRT_kwDOSJAM6s6Zo-5M,
    PRRT_kwDOSJAM6s6ZpLqR, PRRT_kwDOSJAM6s6ZpXSL, PRRT_kwDOSJAM6s6Zp8jK,
    PRRT_kwDOSJAM6s6ZpdhJ, PRRT_kwDOSJAM6s6Zq76j).

    Conditional peels (single emphasis / safe HTML / links) stay
    placeholder-gated, but the gate uses bounded iterative peeling rather than
    recursion so ~1k nested wrappers fail closed instead of raising
    ``RecursionError`` before the 500-character reason bound
    (PRRT_kwDOSJAM6s6Zpg0B). Nested HTML chains are speculative-peeled in a
    linear pass so deep ``<em>``/``<strong>``/``<span>``/``<code>`` nests
    cannot burn quadratic regex time before that bound
    (PRRT_kwDOSJAM6s6Zpjww). Nested tick/quote/strong/strike chains use the
    same linear peel (PRRT_kwDOSJAM6s6ZqS4V).
    """
    cleaned = _peel_all_outer_unconditional_verdict_reason_wrappers(reason.strip())
    # Each successful gated peel shortens ``cleaned``; ``len(cleaned)`` therefore
    # bounds nested wrapper depth without a separate magic constant.
    for _ in range(max(len(cleaned), 1)):
        # Speculative HTML peel is linear and placeholder-gated — do it before
        # a one-layer ``fullmatch`` on an attacker-inflated nest.
        html_core = _peel_all_outer_html_verdict_reason_wrappers(cleaned)
        if html_core != cleaned:
            speculative = _aggressively_peel_verdict_reason_wrappers(html_core)
            if not _normalized_verdict_reason_is_template_placeholder(speculative):
                break
            cleaned = speculative
            continue
        inner = _conditional_verdict_reason_wrapper_inner(cleaned)
        if inner is None:
            break
        # Speculative full peel (iterative) decides whether this gated wrapper
        # encloses a placeholder-shaped value — whole-reason or absorbed
        # same-line citation (PRRT_kwDOSJAM6s6ZoDQU, PRRT_kwDOSJAM6s6ZoGYD,
        # PRRT_kwDOSJAM6s6Zos6S, PRRT_kwDOSJAM6s6Zo-5M, PRRT_kwDOSJAM6s6ZpXSL,
        # PRRT_kwDOSJAM6s6ZpLqR, PRRT_kwDOSJAM6s6ZpdhJ, PRRT_kwDOSJAM6s6Zpg0B).
        speculative = _aggressively_peel_verdict_reason_wrappers(inner)
        if not _normalized_verdict_reason_is_template_placeholder(speculative):
            break
        cleaned = speculative
    return cleaned


def _last_awf_resolvable_reason_is_placeholder(stdout: str, *, verdict: Verdict) -> bool:
    """Return whether the final AWF line for ``verdict`` is a template-placeholder echo."""
    wanted = _RESOLVABLE_PLACEHOLDER_LABELS.get(verdict)
    if wanted is None:
        return False
    last_reason: str | None = None
    found = False
    for stripped in _iter_non_fenced_verdict_lines(stdout):
        for verdict_line in _verdict_line_candidates(stripped):
            for segment in _awf_verdict_segments(verdict_line):
                awf_match = _AWF_VERDICT.fullmatch(segment)
                if awf_match is None:
                    continue
                normalized_label = re.sub(r"[\s_]+", " ", awf_match.group("label").strip().lower())
                if normalized_label != wanted:
                    continue
                found = True
                last_reason = awf_match.group("reason")
    if not found:
        return False
    cleaned = (last_reason or "").strip()
    if not cleaned:
        return False
    return _verdict_reason_is_template_placeholder(cleaned)


def _fail_closed_resolvable_placeholder_if_needed(
    stdout: str,
    result: VerdictResult,
) -> VerdictResult:
    """Convert a standalone resolvable placeholder echo into fail-closed needs_human.

    Prompt-template echoes such as ``AWF-VERDICT: FALSE POSITIVE: <one-sentence
    justification>`` sanitize to a reasonless resolvable verdict; those must not
    clear review items. Explicit reasonless non-placeholder verdicts still pass.
    """
    if result.verdict not in _RESOLVABLE_PLACEHOLDER_LABELS or result.reason is not None:
        return result
    if not _last_awf_resolvable_reason_is_placeholder(stdout, verdict=result.verdict):
        return result
    reason = (
        "fixed_placeholder_echo"
        if result.verdict == "fix_committed"
        else "verdict_placeholder_echo"
    )
    return VerdictResult(verdict="needs_human", reason=reason)


def _select_bare_verdict(
    verdicts: Sequence[VerdictResult],
    *,
    priorities: Sequence[Verdict],
) -> VerdictResult | None:
    for verdict in priorities:
        selected: VerdictResult | None = None
        for parsed in reversed(verdicts):
            if parsed.verdict != verdict:
                continue
            if parsed.reason is not None:
                return parsed
            if selected is None:
                selected = parsed
        if selected is not None:
            return selected
    return None


def _strip_markdown_list_prefix(stripped: str) -> str:
    """Remove a single leading Markdown list marker, if present."""
    return _MARKDOWN_LIST_PREFIX.sub("", stripped, count=1)


def _strip_markdown_task_list_checkbox(stripped: str) -> str:
    """Remove a leading GFM task-list checkbox (``[ ]`` / ``[x]`` / ``[X]``)."""
    return _MARKDOWN_TASK_LIST_CHECKBOX.sub("", stripped, count=1)


def _strip_markdown_blockquote_prefix(stripped: str) -> str:
    """Remove leading Markdown blockquote markers (``>``, ``>>``), if present."""
    return _MARKDOWN_BLOCKQUOTE_PREFIX.sub("", stripped, count=1)


def _strip_markdown_emphasis_prefix(stripped: str) -> str:
    """Remove a leading Markdown emphasis / strong run (``*`` / ``_``), if present."""
    return _MARKDOWN_EMPHASIS_PREFIX.sub("", stripped, count=1)


def _strip_markdown_heading_prefix(stripped: str) -> str:
    """Remove a leading ATX Markdown heading marker (``#``–``######``), if present."""
    return _MARKDOWN_HEADING_PREFIX.sub("", stripped, count=1)


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
    if start > 0 and text[start - 1].isspace():
        return False
    # CommonMark right-flanking (2b): a run preceded by punctuation closes only
    # when also followed by EOS, whitespace, or punctuation. Punctuation-to-
    # alphanumeric runs (``.**x``) are opening-only (PRRT_kwDOSJAM6s6bShqh).
    if start > 0 and _markdown_char_is_unicode_punctuation(text[start - 1]):
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

    Caller must pass ``start`` at ``(`` after a label closer ``]`` (optional
    whitespace already consumed by the caller). Only CommonMark-valid link
    destinations are opaque: angle-bracket form may contain spaces; the
    non-bracket form must be nonempty and free of ASCII space/controls, with
    parentheses only when balanced or escaped. An optional quoted/parenthesized
    title may follow. ``*`` / ``_`` inside a valid destination or title are
    literal and must not participate in emphasis pairing
    (PRRT_kwDOSJAM6s6bTLZq). Invalid destinations (whitespace in non-bracket
    form, newline, unclosed ``)``, leftover junk) leave ``start`` unchanged so
    mid-span markers remain emphasis (PRRT_kwDOSJAM6s6bTgB6).
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
    elif text[index] not in ")\"'(":
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
            if ch == "\\" and index + 1 < n:
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

    index = _skip_link_ws(index)
    if index >= n or text[index] == "\n":
        return start

    if text[index] == ")":
        return index + 1

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
        index += 1
        depth = 1
        while index < n and depth > 0:
            ch = text[index]
            if ch == "\n":
                return start
            if ch == "\\" and index + 1 < n:
                index += 2
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            index += 1
        if depth != 0:
            return start
    else:
        # Leftover content after an invalid/partial destination (e.g. ``foo **bar``).
        return start

    index = _skip_link_ws(index)
    if index < n and text[index] == ")":
        return index + 1
    return start


def _verdict_reason_trailing_emphasis_is_balanced(reason: str, opener: str) -> bool:
    """Return whether a trailing closer pairs inside ``reason``.

    After a prefix-only emphasis strip, a candidate may still end on a valid
    same-delimiter closer. Track left-flanking openers and right-flanking
    closers so a separately balanced span (``This is **expected**``) is kept,
    while unmatched leftovers (``rationale**``) and even counts of closing-only
    runs (``rationale** more**``) are rejected (PRRT_kwDOSJAM6s6bRROQ,
    PRRT_kwDOSJAM6s6bRfTo).

    CommonMark may split a longer same-character run across shorter closers
    (``***lead* rest**`` consumes one star with ``*`` and two with the trailing
    ``**``). Pair across all same-character run lengths so a trailing
    wrapper-length closer stolen by that partial match is detected
    (PRRT_kwDOSJAM6s6bR2FM). Leftover unmatched opener runs that would
    literalize (``***lead* and **done**``) do not block ``trailing_paired``.
    Rule 9 must consult both delimiter sides: a both-flanking opener blocked
    against a closing-only complementary-length run (``a*x**``, ``**lead*``)
    must not claim the trailing wrapper closer (PRRT_kwDOSJAM6s6bTW7t).

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
    Markdown link (PRRT_kwDOSJAM6s6bTW7q). Non-angle-bracket destinations with
    ASCII spaces are not links; their markers remain emphasis
    (PRRT_kwDOSJAM6s6bTgB6).
    """
    closer_start = len(reason) - len(opener)
    if not _markdown_emphasis_closer_is_valid(reason, closer_start, opener):
        return False
    marker = opener[0]
    # Each stack entry is (remaining_len, run_can_close) so rule 9 can consult
    # opener-side both-flanking when the closer is closing-only
    # (PRRT_kwDOSJAM6s6bTW7t).
    open_stack: list[tuple[int, bool]] = []
    trailing_paired = False
    label_depth = 0
    i = 0
    while i < len(reason):
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
            label_depth += 1
            i += 1
            continue
        if reason[i] == "]" and not _markdown_char_is_escaped(reason, i):
            if label_depth > 0:
                label_depth -= 1
                k = i + 1
                while k < len(reason) and reason[k] in " \t":
                    k += 1
                if k < len(reason) and reason[k] == "(":
                    next_i = _advance_past_markdown_link_destination(reason, k)
                    if next_i > k:
                        i = next_i
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
            while remaining > 0 and open_stack:
                opener_len, opener_can_close = open_stack[-1]
                if _emphasis_run_pair_blocked_by_multiple_of_three(
                    opener_len, remaining, can_open, opener_can_close
                ):
                    break
                # Prefer strong (2) when both runs still have at least two.
                consumed = 2 if opener_len >= 2 and remaining >= 2 else 1
                open_stack[-1] = (opener_len - consumed, opener_can_close)
                remaining -= consumed
                if open_stack[-1][0] == 0:
                    open_stack.pop()
                if is_trailing:
                    trailing_paired = True
        if remaining > 0 and can_open:
            open_stack.append((remaining, can_close))
        i = j
    return trailing_paired


def _normalize_markdown_emphasized_verdict_line(line: str) -> str | None:
    """Return a canonical verdict wrapped in balanced top-level emphasis.

    Agents commonly emphasize either the whole verdict line or only the
    ``AWF-VERDICT: LABEL:`` prefix. Accept only matching one-to-three character
    ``*`` / ``_`` delimiters and require the normalized line to satisfy the
    canonical verdict grammar. Nested containers and prose wrappers remain
    untouched and therefore continue to fail closed.

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
    (PRRT_kwDOSJAM6s6bRrWv). Longer same-character runs that CommonMark splits
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
    (PRRT_kwDOSJAM6s6bTgB-). Inline link destinations are opaque so
    ``**… see [link](foo**bar)**`` stays a valid whole-line wrap
    (PRRT_kwDOSJAM6s6bTLZq). A bare unmatched ``](…)``
    is not a link, so destination stars still steal the closer
    (PRRT_kwDOSJAM6s6bTW7q). Invalid destinations with whitespace
    (``[link](foo **bar)``) likewise leave markers as emphasis
    (PRRT_kwDOSJAM6s6bTgB6).
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
                    matched.group("reason"), opener
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
        # must not participate (PRRT_kwDOSJAM6s6bRy5w).
        reason_with_trailing = f"{matched.group('reason')}{opener}"
        if _verdict_reason_trailing_emphasis_is_balanced(reason_with_trailing, opener):
            return None
        return candidate
    return None


def _verdict_line_candidates(stripped: str) -> Iterable[str]:
    """Yield line forms that may carry a canonical ``AWF-VERDICT:`` match.

    Balanced, direct top-level emphasis is normalized because agents commonly
    render canonical verdicts that way. Do not yield Markdown-list-, task-list-,
    blockquote-, heading-, prose-wrapper-, or malformed-emphasis-stripped
    variants here. Those prefixes are stripped only in
    ``_awf_verdict_segment_is_attempt`` so garbled finals fail closed; treating
    quoted/option-list lines as successful matches would let them override an
    earlier hard block.
    """
    yield stripped
    emphasized = _normalize_markdown_emphasized_verdict_line(stripped)
    if emphasized is not None:
        yield emphasized
    code_match = _CODE_FORMATTED_VERDICT_LINE.fullmatch(stripped)
    if code_match is None:
        return
    inner = code_match.group("line").strip()
    if inner:
        yield inner
        emphasized = _normalize_markdown_emphasized_verdict_line(inner)
        if emphasized is not None:
            yield emphasized


def _awf_verdict_segments(verdict_line: str) -> list[str]:
    """Split a candidate so each ``AWF-VERDICT:`` occurrence is its own unit.

    Same-line trailing markers must not be absorbed into an earlier reason
    group; each marker is authoritative in order (final marker wins / fails
    closed), matching multiline parsing.

    When the first marker is preceded by non-whitespace prose, keep the whole
    line as one unit. Splitting would drop that prose and make later quoted
    markers look like leading attempts (mid-prose option lists must not
    override an earlier real verdict).

    When the leading marker is already a hard block (``NEEDS_HUMAN``), later
    markers — quoted or unquoted mid-reason citations — stay reason prose so
    they cannot resolve a thread the agent explicitly blocked
    (PRRT_kwDOSJAM6s6Zl4Ra). Explicit self-corrections and unambiguous trailing
    attempts after a closed quote/code span still split
    (``correction:`` / ``corrected to:``, PRRT_kwDOSJAM6s6ZnrAH).

    When the leading marker is ``FIXED``, ``DEFER``, or ``FALSE POSITIVE``,
    later resolvable markers cited in the reason (quoted or unquoted) stay
    rationale unless they are an unambiguously separate trailing attempt after
    a closed quote/code span or an explicit correction/separator
    (``correction:`` / ``corrected to:``, PRRT_kwDOSJAM6s6Znm-N) — otherwise a
    later citation would win and either bypass the HEAD-advance evidence gate
    (``FIXED`` citation after ``FALSE POSITIVE``, PRRT_kwDOSJAM6s6ZngUH;
    ``FALSE POSITIVE`` citation after ``FIXED``, PRRT_kwDOSJAM6s6Zmggp) or skip
    DEFER tracking-artifact creation (``DEFER``, PRRT_kwDOSJAM6s6Zm6F4). Later
    ``NEEDS_HUMAN`` / unrecognized labels still split (fail closed).

    Subsequent markers embedded in quoted reason prose after a resolvable
    leading verdict (for example a ``FIXED``, ``DEFER``, or ``FALSE POSITIVE``
    reason that cites the marker grammar inside ASCII/curly quotes or Markdown
    backticks) are not split into new attempts — only unquoted trailing
    markers and explicit self-corrections are.
    """
    matches = list(_AWF_VERDICT_MARKER.finditer(verdict_line))
    if len(matches) <= 1:
        return [verdict_line]
    if verdict_line[: matches[0].start()].strip():
        return [verdict_line]
    # One forward delimiter pass for every later marker. Rescanning the prefix
    # per token is quadratic in attacker-/agent-controlled output size
    # (PRRT_kwDOSJAM6s6ZpHXP).
    delimiter_state = _AwfVerdictDelimiterState()
    split_starts = [matches[0].start()]
    leading_start = matches[0].start()
    for match in matches[1:]:
        _advance_awf_verdict_delimiter_state(verdict_line, delimiter_state, match.start())
        if delimiter_state.inside_quote_or_code:
            continue
        if _awf_verdict_leading_hard_block_absorbs_later_marker(
            verdict_line,
            leading_start,
            match.start(),
            delimiter_state=delimiter_state,
        ):
            continue
        if _awf_verdict_leading_fixed_absorbs_later_marker(
            verdict_line,
            leading_start,
            match.start(),
            delimiter_state=delimiter_state,
        ):
            continue
        split_starts.append(match.start())
    if len(split_starts) == 1:
        return [verdict_line]
    segments: list[str] = []
    for index, start in enumerate(split_starts):
        end = split_starts[index + 1] if index + 1 < len(split_starts) else len(verdict_line)
        segment = verdict_line[start:end].strip()
        if segment:
            segments.append(segment)
    return segments or [verdict_line]


def _awf_verdict_leading_hard_block(verdict_line: str, match_start: int) -> bool:
    """Return whether the leading same-line marker is ``NEEDS_HUMAN``."""
    leading = _AWF_VERDICT.match(verdict_line, match_start)
    if leading is None:
        return False
    normalized_label = re.sub(r"[\s_]+", " ", leading.group("label").strip().lower())
    return normalized_label == "needs human"


def _awf_verdict_leading_hard_block_absorbs_later_marker(
    verdict_line: str,
    leading_start: int,
    later_start: int,
    *,
    delimiter_state: _AwfVerdictDelimiterState | None = None,
) -> bool:
    """Return whether a ``NEEDS_HUMAN`` leader keeps a later same-line marker as rationale.

    Mid-reason citations (quoted or unquoted) stay absorbed so they cannot
    resolve a blocked thread (PRRT_kwDOSJAM6s6Zl4Ra). Markers that follow a
    closed quote/code span with only optional whitespace, or an explicit
    correction/separator, are unambiguous trailing attempts and still split
    (PRRT_kwDOSJAM6s6ZnrAH).
    """
    if not _awf_verdict_leading_hard_block(verdict_line, leading_start):
        return False
    return not _awf_verdict_marker_unambiguously_separate_attempt(
        verdict_line, later_start, delimiter_state=delimiter_state
    )


_FIXED_REASON_ABSORBABLE_LABELS = frozenset({"fixed", "false positive", "defer"})


def _awf_verdict_leading_fixed_absorbs_later_marker(
    verdict_line: str,
    leading_start: int,
    later_start: int,
    *,
    delimiter_state: _AwfVerdictDelimiterState | None = None,
) -> bool:
    """Return whether a nonblocking leader keeps a later same-line marker as rationale.

    Unquoted (or mid-prose) resolvable marker citations inside a ``FIXED``,
    ``DEFER``, or ``FALSE POSITIVE`` reason must not become the selected
    verdict — a later ``FIXED`` after ``FALSE POSITIVE`` becomes
    ``fixed_without_head_advance`` with no HEAD advance
    (PRRT_kwDOSJAM6s6ZngUH); a later ``false_positive`` after ``FIXED`` would
    bypass the HEAD-advance evidence gate (PRRT_kwDOSJAM6s6Zmggp); a later
    ``false_positive`` after ``DEFER`` would skip tracking-artifact creation
    (PRRT_kwDOSJAM6s6Zm6F4).     Later ``NEEDS_HUMAN`` and unrecognized labels
    still split (fail closed). Markers that follow a closed quote/code span
    with only optional whitespace, or an explicit correction/separator, are
    unambiguously separate trailing attempts and still split.
    """
    leading = _AWF_VERDICT.match(verdict_line, leading_start)
    if leading is None:
        return False
    leading_label = re.sub(r"[\s_]+", " ", leading.group("label").strip().lower())
    if leading_label not in _FIXED_REASON_ABSORBABLE_LABELS:
        return False
    later = _AWF_VERDICT.match(verdict_line, later_start)
    if later is None:
        return False
    later_label = re.sub(r"[\s_]+", " ", later.group("label").strip().lower())
    if later_label not in _FIXED_REASON_ABSORBABLE_LABELS:
        return False
    return not _awf_verdict_marker_unambiguously_separate_attempt(
        verdict_line, later_start, delimiter_state=delimiter_state
    )


def _strip_final_answer_attempt_prefix(stripped: str) -> str:
    """Remove a leading ``Final answer:``-style wrapper, if present."""
    return _FINAL_ANSWER_ATTEMPT_PREFIX.sub("", stripped, count=1)


def _strip_verdict_result_label_attempt_prefix(stripped: str) -> str:
    """Remove a leading ``Verdict:`` / ``Result:`` label, if present."""
    return _VERDICT_RESULT_LABEL_ATTEMPT_PREFIX.sub("", stripped, count=1)


# Cursor-local attempt-prefix patterns (no ``^``) so ``match(s, pos)`` can peel
# nested wrappers without per-layer string rebuilds. Same bodies as the
# ``^``-anchored helpers used by single-prefix ``.sub`` callers
# (PRRT_kwDOSJAM6s6Zq2nB).
_MARKDOWN_ATTEMPT_PREFIXES_AT: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:>\s*)+"),
    re.compile(r"(?:[-*+]|\d+[.)])\s+"),
    re.compile(r"\[(?: |x|X)\]\s+"),
    re.compile(
        r"(?:(?:my|the)\s+)?final\s+answer(?:\s+is)?\s*[:\-–—]\s*",
        re.IGNORECASE,
    ),
    re.compile(r"(?:verdict|result)\s*[:\-–—]\s*", re.IGNORECASE),
    re.compile(r"(?:\*{1,3}|_{1,3})(?=\S)"),
    re.compile(r"#{1,6}\s+"),
)


def _strip_markdown_attempt_prefixes(segment: str) -> str:
    """Strip leading Markdown / prose-label wrappers until none apply.

    Agents may nest them in either order (``- > AWF-VERDICT: …``,
    ``> - > AWF-VERDICT: …``, ``- [ ] AWF-VERDICT: …``,
    ``Final answer: AWF-VERDICT: …``, ``Verdict: AWF-VERDICT: …``,
    ``Result: AWF-VERDICT: …``, ``**AWF-VERDICT: …**``,
    ``### AWF-VERDICT: …``). One-pass blockquote-then-list leaves a residual
    ``>`` after ``- >``, plain list strip leaves ``[ ]`` after a GFM task-list
    item, and a bare start-only check misses ``Final answer:`` /
    ``Verdict:`` / ``Result:`` / emphasis / heading wrappers — so the marker
    no longer looks like a leading attempt.

    Peel with a cursor on the original string so deeply nested list prefixes
    stay O(n) rather than rebuilding via per-layer ``re.sub``
    (PRRT_kwDOSJAM6s6Zq2nB).
    """
    s = segment.lstrip()
    pos = 0
    n = len(s)
    # Same per-round apply-all order as the former nested ``.sub`` chain
    # (blockquote → list → task-list → final-answer → verdict/result →
    # emphasis → heading); repeat until a round makes no progress.
    while pos < n:
        start = pos
        for pattern in _MARKDOWN_ATTEMPT_PREFIXES_AT:
            match = pattern.match(s, pos)
            if match is not None:
                pos = match.end()
        if pos == start:
            break
    return s[pos:]


def _awf_verdict_segment_is_attempt(segment: str) -> bool:
    """Return whether ``segment`` is a leading/split ``AWF-VERDICT:`` attempt.

    Mid-prose *quoted* citations of the marker grammar (prompt echoes in chat)
    are not attempts. Any other marker occurrence — including decorative emoji
    prefixes (``✅ AWF-VERDICT: …``) and unknown wrappers (``Status: …``) —
    counts toward the final-marker fail-closed gate so a closed prefix
    allowlist cannot leave an earlier resolvable verdict selected
    (PRRT_kwDOSJAM6s6ZmTD6). Leading Markdown blockquote, list, task-list
    checkbox, emphasis, ATX heading, ``Final answer:``, and ``Verdict:`` /
    ``Result:`` wrappers are stripped first so those forms still match at the
    start when present.

    Every marker on the segment is inspected: a quoted citation first must not
    hide a later unquoted marker on a mid-prose line kept whole because the
    first match had leading text (PRRT_kwDOSJAM6s6ZmWN6).
    """
    stripped = _strip_markdown_attempt_prefixes(segment)
    delimiter_state = _AwfVerdictDelimiterState()
    for match in _AWF_VERDICT_MARKER.finditer(stripped):
        if match.start() == 0:
            return True
        # Unquoted later markers fail closed; only confident quote/backtick
        # embeddings are treated as prose citations of the grammar. Advance the
        # shared delimiter cursor so multi-marker segments stay linear
        # (PRRT_kwDOSJAM6s6ZpHXP).
        _advance_awf_verdict_delimiter_state(stripped, delimiter_state, match.start())
        if not delimiter_state.inside_quote_or_code:
            return True
    return False


def _verdict_result_from_match(*, label: str, reason: str | None) -> VerdictResult:
    # Canonicalize any run of whitespace/underscores to a single space, so
    # every separator variant the label regex accepts (NEEDS_HUMAN,
    # NEEDS HUMAN, NEEDS_ HUMAN, ...) maps to one label. The regex and this
    # normalization must stay equally permissive, or a matched NEEDS_HUMAN
    # could silently fall through to fix_committed — the unsafe direction (#305).
    normalized_label = re.sub(r"[\s_]+", " ", label.strip().lower())
    cleaned_reason = _sanitize_verdict_reason(reason)
    if normalized_label == "false positive":
        return VerdictResult(verdict="false_positive", reason=cleaned_reason)
    if normalized_label == "needs human":
        return VerdictResult(verdict="needs_human", reason=cleaned_reason)
    if normalized_label == "defer":
        return VerdictResult(verdict="defer", reason=cleaned_reason)
    return VerdictResult(verdict="fix_committed", reason=cleaned_reason)


def _sanitize_verdict_reason(reason: str | None) -> str | None:
    """Redact, bound, and normalize a verdict reason, dropping unusable content."""
    if reason is None:
        return None
    cleaned = _normalize_verdict_reason_inline_formatting(redact_secrets(reason).strip())
    if not cleaned:
        return None
    if _VERDICT_REASON_REDACTION_ONLY.fullmatch(cleaned):
        return None
    if _verdict_reason_is_template_placeholder(cleaned):
        return None
    if len(cleaned) > _MAX_VERDICT_REASON_LENGTH:
        return f"{cleaned[: _MAX_VERDICT_REASON_LENGTH - 1].rstrip()}…"
    return cleaned


def _needs_human_reason_missing(result: VerdictResult) -> bool:
    """Return whether a blocking needs-human result lacks a usable reason."""
    return result.verdict == "needs_human" and _sanitize_verdict_reason(result.reason) is None
