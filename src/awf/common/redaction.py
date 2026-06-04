"""Shared text redaction for operator-facing logs."""

from __future__ import annotations

import re

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


def redact_secrets(text: str) -> str:
    """Replace common secret value bodies with a stable marker."""
    if not text:
        return text

    redacted = _URL_CREDENTIAL_RE.sub(r"\1" + REDACTION_MARKER + "@", text)
    redacted = _AUTHORIZATION_RE.sub(r"\1" + REDACTION_MARKER, redacted)
    redacted = _PROVIDER_REF_RE.sub(REDACTION_MARKER, redacted)
    redacted = _TOKEN_ASSIGNMENT_RE.sub(_redact_assignment, redacted)
    redacted = _BEARER_RE.sub(r"\1" + REDACTION_MARKER, redacted)
    return _KNOWN_TOKEN_RE.sub(REDACTION_MARKER, redacted)


def _redact_assignment(match: re.Match[str]) -> str:
    quote = match.group("quote")
    return f"{match.group('key')}{match.group('separator')}{quote}{REDACTION_MARKER}{quote}"
