"""Exact provider-neutral decoder for PR-monitor verdict records."""

from __future__ import annotations

import re
from html import unescape

from awf.common.redaction import redact_secrets
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdict,
    AgentVerdictProtocolError,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.constants import _REDACTION

__all__ = (
    "_final_non_empty_line",
    "_parse_verdict",
    "_parse_verdict_result",
    "_sanitize_verdict_reason",
)

_EXACT_VERDICT = re.compile(
    r"AWF-VERDICT: "
    r"(?P<label>FIXED|FALSE POSITIVE|DEFER|NEEDS_HUMAN): "
    r"(?P<reason>[^\r\n]+)"
)
_MAX_VERDICT_REASON_LENGTH = 500
_VERDICT_REASON_TEMPLATE_PLACEHOLDER = re.compile(
    r"<\s*(?:what|one[-\s]?sentence|summary|reason|track|decision|defer|need)"
    r"\b[^>\n\r]{0,80}>",
    re.IGNORECASE,
)
_VERDICT_REASON_TEMPLATE_ELLIPSIS = re.compile(r"(?:…|\.{3})")
_VERDICT_REASON_TEMPLATE_EXIT_SUFFIX = re.compile(r"\s+and\s+exit\.?$", re.IGNORECASE)
_VERDICT_REASON_TEMPLATE_BACKSLASH_ESCAPE = re.compile(r"\\([<>])")
_VERDICT_REASON_EDGE_DECORATION = " \t*_~`\"'“”‘’"
_VERDICT_REASON_TEMPLATE_EDGE_DECORATION = f"{_VERDICT_REASON_EDGE_DECORATION}[]"
_VERDICT_REASON_HTML_DECODE_MAX_PASSES = 4
_LABEL_TO_VERDICT: dict[str, AgentVerdict] = {
    "FIXED": "fix_committed",
    "FALSE POSITIVE": "false_positive",
    "DEFER": "defer",
    "NEEDS_HUMAN": "needs_human",
}


def _protocol_error() -> AgentVerdictProtocolError:
    return AgentVerdictProtocolError(reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION)


def _parse_verdict(stdout: str) -> AgentVerdict:
    """Return the typed disposition from the exact terminal stdout record."""
    return _parse_verdict_result(stdout).verdict


def _parse_verdict_result(stdout: str) -> VerdictResult:
    """Decode exactly one canonical verdict record or raise a typed error."""
    final_line = _final_non_empty_line(stdout)
    if final_line is None:
        raise _protocol_error()

    matched = _EXACT_VERDICT.fullmatch(final_line)
    if matched is None or _count_exact_verdict_records(stdout) != 1:
        raise _protocol_error()

    reason = _sanitize_verdict_reason(
        matched.group("reason"),
        reject_template_placeholders=True,
    )
    if reason is None:
        raise _protocol_error()
    return VerdictResult(
        verdict=_LABEL_TO_VERDICT[matched.group("label")],
        reason=reason,
    )


def _final_non_empty_line(stdout: str) -> str | None:
    """Return the final non-empty line with constant additional memory."""
    end = len(stdout)
    while end > 0 and stdout[end - 1] in "\r\n":
        end -= 1
    while end > 0:
        start = stdout.rfind("\n", 0, end) + 1
        line_end = end
        if line_end > start and stdout[line_end - 1] == "\r":
            line_end -= 1
        line = stdout[start:line_end]
        if line.strip():
            return line
        end = max(0, start - 1)
        while end > 0 and stdout[end - 1] == "\r":
            end -= 1
    return None


def _count_exact_verdict_records(stdout: str) -> int:
    """Count canonical record lines without allocating a full line list."""
    count = 0
    start = 0
    size = len(stdout)
    while start <= size:
        newline = stdout.find("\n", start)
        end = size if newline < 0 else newline
        if end > start and stdout[end - 1] == "\r":
            end -= 1
        if _EXACT_VERDICT.fullmatch(stdout, start, end) is not None:
            count += 1
            if count > 1:
                return count
        if newline < 0:
            break
        start = newline + 1
    return count


def _sanitize_verdict_reason(
    reason: str | None,
    *,
    reject_template_placeholders: bool = False,
) -> str | None:
    """Redact and bound an opaque reason; do not interpret its formatting."""
    if reason is None:
        return None
    cleaned = redact_secrets(reason).strip()
    if not cleaned or cleaned == _REDACTION:
        return None
    if reject_template_placeholders and _verdict_reason_is_template_placeholder(cleaned):
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
    candidate = candidate.strip(_VERDICT_REASON_TEMPLATE_EDGE_DECORATION)
    candidate = _VERDICT_REASON_TEMPLATE_EXIT_SUFFIX.sub("", candidate).strip(
        _VERDICT_REASON_TEMPLATE_EDGE_DECORATION
    )
    candidate = _VERDICT_REASON_TEMPLATE_BACKSLASH_ESCAPE.sub(r"\1", candidate)
    return bool(
        _VERDICT_REASON_TEMPLATE_PLACEHOLDER.fullmatch(candidate)
        or _VERDICT_REASON_TEMPLATE_ELLIPSIS.fullmatch(candidate)
    )
