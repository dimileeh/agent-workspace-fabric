"""Small compatibility decoder for PR-monitor verdict records.

Agent stdout is presentation text, not Markdown input. AWF therefore inspects
only the final non-empty line and accepts the canonical ``AWF-VERDICT:`` record
plus Cursor's two observed literal ``**`` record layouts. This is a closed
compatibility grammar, not emphasis parsing.
"""

from __future__ import annotations

import re
from html import unescape

from awf.common.redaction import redact_secrets
from awf.runtime.pr_monitor_runner.comments import Verdict, VerdictResult
from awf.runtime.pr_monitor_runner.constants import (
    _AWF_VERDICT,
    _AWF_VERDICT_MARKER,
    _REDACTION,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_delimiters import (
    _ascii_double_quote_is_delimiter as _ascii_double_quote_is_delimiter,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_delimiters import (
    _ascii_single_quote_is_delimiter as _ascii_single_quote_is_delimiter,
)

__all__ = (
    "_ascii_double_quote_is_delimiter",
    "_ascii_single_quote_is_delimiter",
    "_needs_human_reason_missing",
    "_parse_verdict",
    "_parse_verdict_result",
    "_sanitize_verdict_reason",
)

_CURSOR_PREFIX_DECORATED_VERDICT = re.compile(
    r"^\*\*AWF-VERDICT\s*:\s*"
    r"(?P<label>FIXED|FALSE\s+POSITIVE|DEFER|NEEDS[\s_]+HUMAN)"
    r"\s*:\*\*(?:[ \t]+(?P<reason>[^\n\r]*))?$",
    re.IGNORECASE,
)
_CURSOR_RECORD_DECORATED_VERDICT = re.compile(
    r"^\*\*AWF-VERDICT\s*:\s*"
    r"(?P<label>FIXED|FALSE\s+POSITIVE|DEFER|NEEDS[\s_]+HUMAN)"
    r"\s*:\s*(?P<reason>[^\n\r]*?)\*\*$",
    re.IGNORECASE,
)
_VERDICT_REASON_TEMPLATE_PLACEHOLDER = re.compile(
    r"<\s*(?:what|one[-\s]?sentence|summary|reason|track|decision|defer|need)"
    r"\b[^>\n\r]{0,80}>",
    re.IGNORECASE,
)
_VERDICT_REASON_TEMPLATE_ELLIPSIS = re.compile(
    r"(?:…|\.{3})",
    re.IGNORECASE,
)
_VERDICT_REASON_TEMPLATE_EXIT_SUFFIX = re.compile(r"\s+and\s+exit\.?$", re.IGNORECASE)
_VERDICT_REASON_EDGE_DECORATION = " \t*_~`\"'“”‘’"
_VERDICT_REASON_HTML_DECODE_MAX_PASSES = 4
_VERDICT_REASON_REDACTION_ONLY = re.compile(
    rf"^[\s,;:.!?'\"“”‘’]*(?:(?:[A-Za-z][A-Za-z0-9_-]*\s*[:=]\s*)?"
    rf"[\s,;:.!?'\"“”‘’]*{re.escape(_REDACTION)}[\s,;:.!?'\"“”‘’]*)+$",
    re.IGNORECASE,
)
_MAX_VERDICT_REASON_LENGTH = 500


def _parse_verdict(stdout: str) -> Verdict:
    """Return the typed disposition from the final stdout record."""
    return _parse_verdict_result(stdout).verdict


def _parse_verdict_result(stdout: str) -> VerdictResult:
    """Decode one explicit final verdict record, failing closed otherwise."""
    final_line = _final_non_empty_line(stdout)
    if final_line is None:
        return VerdictResult(verdict="needs_human", reason="empty_verdict_output")

    first_marker = _AWF_VERDICT_MARKER.search(final_line)
    if (
        first_marker is not None
        and _AWF_VERDICT_MARKER.search(final_line, first_marker.end()) is not None
    ):
        return VerdictResult(verdict="needs_human", reason="garbled_verdict_marker")

    matched = _AWF_VERDICT.fullmatch(final_line)
    if matched is None:
        matched = _CURSOR_PREFIX_DECORATED_VERDICT.fullmatch(final_line)
    if matched is None:
        matched = _CURSOR_RECORD_DECORATED_VERDICT.fullmatch(final_line)
    if matched is None:
        reason = (
            "garbled_verdict_marker"
            if _AWF_VERDICT_MARKER.search(final_line)
            else "unrecognized_or_markerless_verdict"
        )
        return VerdictResult(verdict="needs_human", reason=reason)

    result = _verdict_result_from_match(
        label=matched.group("label"),
        reason=matched.group("reason"),
    )
    if result.reason is not None or result.verdict == "needs_human":
        return result
    if result.verdict == "fix_committed":
        return VerdictResult(verdict="needs_human", reason="fixed_placeholder_echo")
    return VerdictResult(verdict="needs_human", reason="verdict_placeholder_echo")


def _final_non_empty_line(stdout: str) -> str | None:
    """Return the final non-empty line without accepting leading indentation."""
    for line in reversed(stdout.splitlines()):
        if line.strip():
            return line.rstrip()
    return None


def _verdict_result_from_match(*, label: str, reason: str | None) -> VerdictResult:
    """Map the closed protocol label set to AWF's persisted verdict values."""
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
    """Redact, bound, and normalize a verdict reason without parsing formatting."""
    if reason is None:
        return None
    cleaned = redact_secrets(reason).strip()
    if not cleaned:
        return None
    if _VERDICT_REASON_REDACTION_ONLY.fullmatch(cleaned.strip(_VERDICT_REASON_EDGE_DECORATION)):
        return None
    if _verdict_reason_is_template_placeholder(cleaned):
        return None
    if len(cleaned) > _MAX_VERDICT_REASON_LENGTH:
        return f"{cleaned[: _MAX_VERDICT_REASON_LENGTH - 1].rstrip()}…"
    return cleaned


def _verdict_reason_is_template_placeholder(reason: str) -> bool:
    """Reject a whole template echo without interpreting its presentation syntax."""
    candidate = reason
    for _ in range(_VERDICT_REASON_HTML_DECODE_MAX_PASSES):
        decoded = unescape(candidate)
        if decoded == candidate:
            break
        candidate = decoded
    else:
        if unescape(candidate) != candidate:
            return True
    candidate = candidate.strip(_VERDICT_REASON_EDGE_DECORATION)
    candidate = _VERDICT_REASON_TEMPLATE_EXIT_SUFFIX.sub("", candidate).strip(
        _VERDICT_REASON_EDGE_DECORATION
    )
    return bool(
        _VERDICT_REASON_TEMPLATE_PLACEHOLDER.fullmatch(candidate)
        or _VERDICT_REASON_TEMPLATE_ELLIPSIS.fullmatch(candidate)
    )


def _needs_human_reason_missing(result: VerdictResult) -> bool:
    """Return whether a blocking needs-human result lacks a usable reason."""
    return result.verdict == "needs_human" and _sanitize_verdict_reason(result.reason) is None
