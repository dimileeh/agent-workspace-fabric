"""Secret redaction and redacted-logging helpers for provider readiness.

Extracted from ``provider_readiness_helpers`` to keep each first-party module
under the maintainability line limit. These helpers are a leaf concern: they
depend only on the redaction constants defined in ``provider_readiness`` and are
called by the probe helpers, never the other way around.
"""

from __future__ import annotations

import traceback


def _redact(value: str, secrets: frozenset[str]) -> str:
    redacted = value
    for secret in sorted(secrets, key=len, reverse=True):
        redacted = redacted.replace(secret, _REDACTION)
    redacted = URL_CREDENTIAL_RE.sub(r"\1<redacted>@", redacted)
    return TOKEN_RE.sub(_REDACTION, redacted)


def _redact_with_redaction_parts(
    value: str,
    secrets: frozenset[str],
) -> tuple[str, list[str] | None]:
    segments: list[_RedactionSegment] = [("literal", value)]
    for secret in sorted(secrets, key=len, reverse=True):
        segments = _replace_literal_redaction_spans(segments, secret)
    segments = _replace_url_credential_redaction_spans(segments)
    segments = _replace_token_redaction_spans(segments)
    return _render_redaction_segments(segments), _redaction_parts(segments)


def _replace_literal_redaction_spans(
    segments: list[_RedactionSegment],
    text: str,
) -> list[_RedactionSegment]:
    if not text:
        return segments
    replaced: list[_RedactionSegment] = []
    for kind, segment_text in segments:
        if kind == "redaction":
            replaced.append((kind, segment_text))
            continue
        parts = segment_text.split(text)
        if len(parts) == 1:
            replaced.append((kind, segment_text))
            continue
        for index, part in enumerate(parts):
            if part:
                replaced.append(("literal", part))
            if index < len(parts) - 1:
                replaced.append(("redaction", ""))
    return _merge_literal_redaction_segments(replaced)


def _replace_url_credential_redaction_spans(
    segments: list[_RedactionSegment],
) -> list[_RedactionSegment]:
    rendered = _render_redaction_segments(segments)
    replacements: list[tuple[int, int, list[_RedactionSegment]]] = []
    for match in URL_CREDENTIAL_RE.finditer(rendered):
        replacements.append((match.start(2), match.end(2), [("redaction", ""), ("literal", "@")]))
    return _replace_rendered_redaction_spans(segments, replacements)


def _replace_token_redaction_spans(
    segments: list[_RedactionSegment],
) -> list[_RedactionSegment]:
    rendered = _render_redaction_segments(segments)
    replacements: list[tuple[int, int, list[_RedactionSegment]]] = []
    for match in TOKEN_RE.finditer(rendered):
        replacements.append((match.start(1), match.end(1), [("redaction", "")]))
    return _replace_rendered_redaction_spans(segments, replacements)


def _replace_rendered_redaction_spans(
    segments: list[_RedactionSegment],
    replacements: list[tuple[int, int, list[_RedactionSegment]]],
) -> list[_RedactionSegment]:
    if not replacements:
        return segments

    rendered_length = len(_render_redaction_segments(segments))
    cursor = 0
    replaced: list[_RedactionSegment] = []
    for start, end, replacement in replacements:
        replaced.extend(_slice_redaction_segments(segments, cursor, start))
        replaced.extend(replacement)
        cursor = end
    replaced.extend(_slice_redaction_segments(segments, cursor, rendered_length))
    return _merge_literal_redaction_segments(replaced)


def _slice_redaction_segments(
    segments: list[_RedactionSegment],
    start: int,
    end: int,
) -> list[_RedactionSegment]:
    if start >= end:
        return []

    sliced: list[_RedactionSegment] = []
    position = 0
    for kind, segment_text in segments:
        rendered = segment_text if kind == "literal" else _REDACTION
        next_position = position + len(rendered)
        overlap_start = max(start, position)
        overlap_end = min(end, next_position)
        if overlap_start < overlap_end:
            inner_start = overlap_start - position
            inner_end = overlap_end - position
            if kind == "redaction" and inner_start == 0 and inner_end == len(_REDACTION):
                sliced.append(("redaction", ""))
            else:
                sliced.append(("literal", rendered[inner_start:inner_end]))
        position = next_position
        if position >= end:
            break
    return sliced


def _merge_literal_redaction_segments(
    segments: list[_RedactionSegment],
) -> list[_RedactionSegment]:
    merged: list[_RedactionSegment] = []
    for kind, text in segments:
        if kind == "literal" and not text:
            continue
        if kind == "literal" and merged and merged[-1][0] == "literal":
            merged[-1] = ("literal", f"{merged[-1][1]}{text}")
            continue
        merged.append((kind, text))
    return merged


def _render_redaction_segments(segments: list[_RedactionSegment]) -> str:
    return "".join(text if kind == "literal" else _REDACTION for kind, text in segments)


def _redaction_parts(segments: list[_RedactionSegment]) -> list[str] | None:
    if not any(kind == "redaction" for kind, _text in segments):
        return None

    parts = [""]
    for kind, text in segments:
        if kind == "redaction":
            parts.append("")
        else:
            parts[-1] += text
    return parts


def _log_redacted_exception(
    event: str,
    exc: Exception,
    secrets: frozenset[str],
) -> None:
    detail = _redact(_truncate(f"{type(exc).__name__}: {exc}"), secrets)
    trace = _redact(
        _truncate(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            limit=_TRACEBACK_LOG_LIMIT,
        ),
        secrets,
    )
    _log.error("%s: %s\n%s", event, detail, trace)


def _log_redacted_terminal_failure(
    event: str,
    detail: str,
    secrets: frozenset[str],
) -> None:
    _log.error(
        "%s: %s",
        event,
        _truncate(_redact(detail, secrets), limit=_TRACEBACK_LOG_LIMIT),
    )


def _truncate(value: str, *, limit: int = 240) -> str:
    stripped = value.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 1] + "…"


# Imported at module end to mirror the established mutual-import ordering with
# ``provider_readiness``: that module fully defines the redaction constants
# below before it pulls these helpers back in, and these helpers only reference
# the constants at call time, so the late binding is safe.
from awf.service.provider_readiness import (  # noqa: E402
    _REDACTION,
    _TRACEBACK_LOG_LIMIT,
    TOKEN_RE,
    URL_CREDENTIAL_RE,
    _log,
    _RedactionSegment,
)
