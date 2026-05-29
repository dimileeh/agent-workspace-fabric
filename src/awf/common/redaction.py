"""Shared text redaction for operator-facing logs."""

from __future__ import annotations

import re

from awf.common.token_patterns import compile_known_token_re

REDACTION_MARKER = "<redacted>"

_URL_CREDENTIAL_RE = re.compile(r"(\bhttps?://)([^/\s:@]+(?::[^/\s@]+)?@)", re.IGNORECASE)
_AUTHORIZATION_RE = re.compile(
    r"(\bAuthorization\s*:\s*(?:Bearer|Basic)\s+)([A-Za-z0-9._~+/=-]{8,})",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(\bBearer\s+)([A-Za-z0-9._~+/=-]{8,})", re.IGNORECASE)
_TOKEN_ASSIGNMENT_RE = re.compile(
    r"\b(?P<key>"
    r"(?:[A-Za-z][A-Za-z0-9_]*_)?(?:API_TOKEN|AUTH_TOKEN|GITHUB_TOKEN)"
    r"|GH_TOKEN"
    r")\b"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<quote>[\"']?)"
    r"(?P<value>[^\s\"'`,;)}\]]+)"
    r"(?P=quote)",
    re.IGNORECASE,
)
_KNOWN_TOKEN_RE = compile_known_token_re()


def redact_secrets(text: str) -> str:
    """Replace common secret value bodies with a stable marker."""
    if not text:
        return text

    redacted = _URL_CREDENTIAL_RE.sub(r"\1" + REDACTION_MARKER + "@", text)
    redacted = _AUTHORIZATION_RE.sub(r"\1" + REDACTION_MARKER, redacted)
    redacted = _TOKEN_ASSIGNMENT_RE.sub(_redact_assignment, redacted)
    redacted = _BEARER_RE.sub(r"\1" + REDACTION_MARKER, redacted)
    return _KNOWN_TOKEN_RE.sub(REDACTION_MARKER, redacted)


def _redact_assignment(match: re.Match[str]) -> str:
    quote = match.group("quote")
    return f"{match.group('key')}{match.group('separator')}{quote}{REDACTION_MARKER}{quote}"
