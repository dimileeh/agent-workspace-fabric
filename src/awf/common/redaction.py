"""Shared text redaction for operator-facing logs."""

from __future__ import annotations

import re
from collections.abc import Iterable

from awf.common.token_patterns import (
    compile_known_token_re,
    compile_provider_ref_re,
    compile_token_assignment_re,
)

# Runtime logs intentionally use angle brackets; audit and first-run JSON use
# ``awf.common.audit.REDACTION_MARKER`` as their separate stable contract.
REDACTION_MARKER = "<redacted>"

_URL_CREDENTIAL_RE = re.compile(r"(\bhttps?://)([^/\s:@]+(?::[^/\s@]+)?@)", re.IGNORECASE)
_AUTHORIZATION_RE = re.compile(
    r"(\bAuthorization\s*:\s*(?:Bearer|Basic)\s+)([A-Za-z0-9._~+/=-]{8,})",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(\bBearer\s+)([A-Za-z0-9._~+/=-]{8,})", re.IGNORECASE)
_TOKEN_ASSIGNMENT_RE = compile_token_assignment_re()
_PROVIDER_REF_RE = compile_provider_ref_re()
# Runtime logs use the same explicit truncated-token policy as audit records:
# prefer a visible false positive over leaking rejected credential fragments.
_KNOWN_TOKEN_RE = compile_known_token_re(match_truncated_provider_tokens=True)
_RedactionSpan = tuple[int, int]


def redact_secrets(text: str, *, extra_secrets: Iterable[str] = ()) -> str:
    """Replace common secret value bodies with a stable marker."""
    if not text:
        return text

    # Compute exact and regex spans on the original text before rendering so an
    # exact configured secret containing a token-shaped substring is masked as a
    # whole instead of being missed after a regex rewrite.
    spans = _secret_redaction_spans(text, extra_secrets=extra_secrets)
    if not spans:
        return text
    return _render_redacted_slice(text, 0, len(text), spans)


def redact_secrets_slice(
    text: str,
    start: int,
    end: int,
    *,
    extra_secrets: Iterable[str] = (),
) -> str:
    """Return ``text[start:end]`` with any overlapping secret spans redacted."""
    if not text or start >= end:
        return ""

    safe_start = min(max(start, 0), len(text))
    safe_end = min(max(end, safe_start), len(text))
    if safe_start >= safe_end:
        return ""

    spans = _secret_redaction_spans(text, extra_secrets=extra_secrets)
    if not spans:
        return text[safe_start:safe_end]

    return _render_redacted_slice(text, safe_start, safe_end, spans)


def redact_secrets_byte_slice(
    text: str,
    start: int,
    end: int,
    *,
    extra_secrets: Iterable[str] = (),
) -> str:
    """Return the UTF-8 byte slice ``text[start:end]`` with secrets redacted."""
    if not text or start >= end:
        return ""

    text_bytes = text.encode("utf-8", errors="surrogateescape")
    safe_start = min(max(start, 0), len(text_bytes))
    safe_end = min(max(end, safe_start), len(text_bytes))
    if safe_start >= safe_end:
        return ""

    spans = _secret_redaction_spans(text, extra_secrets=extra_secrets)
    if not spans:
        return text_bytes[safe_start:safe_end].decode("utf-8", errors="replace")

    byte_offsets = _utf8_byte_offsets_for_text_indices(
        text,
        (index for span in spans for index in span),
    )
    byte_spans = [
        (byte_offsets[span_start], byte_offsets[span_end]) for span_start, span_end in spans
    ]
    return _render_redacted_byte_slice(text_bytes, safe_start, safe_end, byte_spans)


def redact_exact_secret_bytes(
    content: bytes,
    *,
    extra_secrets: Iterable[str] = (),
) -> bytes:
    """Replace exact caller-supplied secret byte sequences with the stable marker."""
    if not content:
        return content

    spans = _exact_secret_byte_redaction_spans(content, extra_secrets)
    if not spans:
        return content
    return _render_redacted_bytes(content, 0, len(content), spans)


def _secret_redaction_spans(
    text: str,
    *,
    extra_secrets: Iterable[str],
) -> list[_RedactionSpan]:
    """Find all secret spans that should be masked in the original text."""
    spans: list[_RedactionSpan] = []
    spans.extend(_exact_secret_redaction_spans(text, extra_secrets))

    # Group 2 ends with the literal "@"; exclude it so the URL keeps the separator.
    spans.extend((match.start(2), match.end(2) - 1) for match in _URL_CREDENTIAL_RE.finditer(text))
    spans.extend((match.start(2), match.end(2)) for match in _AUTHORIZATION_RE.finditer(text))
    spans.extend(match.span() for match in _PROVIDER_REF_RE.finditer(text))
    spans.extend(
        (match.start("value"), match.end("value")) for match in _TOKEN_ASSIGNMENT_RE.finditer(text)
    )
    spans.extend((match.start(2), match.end(2)) for match in _BEARER_RE.finditer(text))
    spans.extend(match.span() for match in _KNOWN_TOKEN_RE.finditer(text))
    return _merge_redaction_spans(spans)


def _exact_secret_redaction_spans(
    text: str,
    extra_secrets: Iterable[str],
) -> list[_RedactionSpan]:
    """Find exact caller-supplied secret values in text."""
    spans: list[_RedactionSpan] = []
    for secret in sorted({secret for secret in extra_secrets if len(secret) >= 4}, key=len):
        cursor = 0
        while True:
            start = text.find(secret, cursor)
            if start == -1:
                break
            end = start + len(secret)
            spans.append((start, end))
            cursor = start + 1
    return spans


def _exact_secret_byte_redaction_spans(
    content: bytes,
    extra_secrets: Iterable[str],
) -> list[_RedactionSpan]:
    """Find exact caller-supplied secret byte values in content."""
    spans: list[_RedactionSpan] = []
    for secret in sorted({secret for secret in extra_secrets if len(secret) >= 4}, key=len):
        secret_bytes = secret.encode("utf-8")
        cursor = 0
        while True:
            start = content.find(secret_bytes, cursor)
            if start == -1:
                break
            end = start + len(secret_bytes)
            spans.append((start, end))
            cursor = start + 1
    return _merge_redaction_spans(spans)


def _merge_redaction_spans(spans: Iterable[_RedactionSpan]) -> list[_RedactionSpan]:
    """Coalesce overlapping or adjacent redaction spans."""
    merged: list[_RedactionSpan] = []
    for start, end in sorted(span for span in spans if span[0] < span[1]):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _render_redacted_slice(
    text: str,
    start: int,
    end: int,
    spans: list[_RedactionSpan],
) -> str:
    """Render a requested text slice while masking intersecting secret spans."""
    pieces: list[str] = []
    cursor = start
    for span_start, span_end in spans:
        if span_end <= cursor:
            continue
        if span_start >= end:
            break
        literal_end = min(max(span_start, cursor), end)
        if cursor < literal_end:
            pieces.append(text[cursor:literal_end])
        pieces.append(REDACTION_MARKER)
        cursor = max(cursor, min(span_end, end))
    if cursor < end:
        pieces.append(text[cursor:end])
    return "".join(pieces)


def _render_redacted_bytes(
    content: bytes,
    start: int,
    end: int,
    spans: list[_RedactionSpan],
) -> bytes:
    """Render requested bytes while masking intersecting secret spans."""
    pieces: list[bytes] = []
    cursor = start
    marker = REDACTION_MARKER.encode("ascii")
    for span_start, span_end in spans:
        if span_end <= cursor:
            continue
        if span_start >= end:
            break
        literal_end = min(max(span_start, cursor), end)
        if cursor < literal_end:
            pieces.append(content[cursor:literal_end])
        pieces.append(marker)
        cursor = max(cursor, min(span_end, end))
    if cursor < end:
        pieces.append(content[cursor:end])
    return b"".join(pieces)


def _utf8_byte_offsets_for_text_indices(
    text: str,
    indices: Iterable[int],
) -> dict[int, int]:
    """Map requested Python string indices to UTF-8 byte offsets."""
    text_length = len(text)
    pending = sorted({index for index in indices if 0 <= index <= text_length})
    if not pending:  # pragma: no cover - caller passes nonempty redaction spans.
        return {}

    offsets: dict[int, int] = {}
    pending_index = 0
    while pending_index < len(pending) and pending[pending_index] == 0:
        offsets[0] = 0
        pending_index += 1

    target_index = pending[-1]
    byte_index = 0
    for text_index, char in enumerate(text[:target_index], start=1):
        codepoint = ord(char)
        if codepoint < 0x80 or 0xDC80 <= codepoint <= 0xDCFF:
            byte_width = 1
        elif codepoint < 0x800:
            byte_width = 2
        elif codepoint < 0x10000:
            byte_width = 3
        else:
            byte_width = 4

        byte_index += byte_width
        while pending_index < len(pending) and pending[pending_index] == text_index:
            offsets[pending[pending_index]] = byte_index
            pending_index += 1
    return offsets


def _render_redacted_byte_slice(
    text: bytes,
    start: int,
    end: int,
    spans: list[_RedactionSpan],
) -> str:
    """Render a requested byte slice while masking intersecting secret spans."""
    pieces: list[str] = []
    cursor = start
    for span_start, span_end in spans:
        if span_end <= cursor:
            continue
        if span_start >= end:
            break
        literal_end = min(max(span_start, cursor), end)
        if cursor < literal_end:
            pieces.append(text[cursor:literal_end].decode("utf-8", errors="replace"))
        pieces.append(REDACTION_MARKER)
        cursor = max(cursor, min(span_end, end))
    if cursor < end:
        pieces.append(text[cursor:end].decode("utf-8", errors="replace"))
    return "".join(pieces)
