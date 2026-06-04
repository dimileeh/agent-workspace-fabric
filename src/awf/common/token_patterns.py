"""Shared token-shaped secret and provider-ref recognition patterns."""

from __future__ import annotations

import re
from typing import Final

KNOWN_TOKEN_PATTERN: Final = (
    # Python's regex alternation is left-to-right: keep provider-specific
    # ``sk-`` prefixes before the generic ``sk-`` catch-all.
    r"(?<![A-Za-z0-9_])("
    r"gh[apousr]_[A-Za-z0-9_]*|"
    r"github_pat_[A-Za-z0-9_]*|"
    r"glpat-[A-Za-z0-9_-]*|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
    r"sk-ant-[A-Za-z0-9_-]{8,}|"
    r"sk-proj-[A-Za-z0-9_-]{8,}|"
    r"sk-[A-Za-z0-9_-]{8,}|"
    r"AIza[A-Za-z0-9_-]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]*"
    r")(?![A-Za-z0-9_])"
)
TOKEN_ASSIGNMENT_PATTERN: Final = (
    r"\b(?P<key>"
    r"(?:[A-Za-z][A-Za-z0-9_]*_)?TOKEN"
    r"|(?:[A-Za-z][A-Za-z0-9_]*_)?(?:API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY)"
    r"|(?:AUTH|GITHUB|GH)[_-]?TOKEN"
    r"|PASSWORD|PASSWD|SECRET"
    r")\b"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<quote>[\"']?)"
    r"(?P<value>"
    r"-----BEGIN [A-Z0-9 -]*PRIVATE KEY-----[\s\S]*?"
    r"-----END [A-Z0-9 -]*PRIVATE KEY-----"
    r"|[^\s\"'`,;)}\]]+"
    r")"
    r"(?P=quote)"
)
PROVIDER_REF_PATTERN: Final = (
    # Match from the start of a URI-like scheme token so hyphen-prefixed
    # contexts like x-plain-file:// redact as a whole, while safeplain-file://
    # remains ordinary text.
    r"(?<![A-Za-z0-9_-])"
    r"(?:[A-Za-z][A-Za-z0-9+.-]*-)?(?:keyring|env|plain-file)://"
    r"[^\s\"'`,;)}\]]+"
)
PROVIDER_REF_KEY_PATTERN: Final = r"(?:credential|provider)[_-]refs?"
PROVIDER_REF_KEY_SUFFIX_PATTERN: Final = (
    r"(?P<base>(?:credential|provider)[_-]refs?)(?P<separator>[_:-])(?P<suffix>.+)"
)


STRICT_KNOWN_TOKEN_PATTERN: Final = (
    r"(?<![A-Za-z0-9_])("
    r"gh[apousr]_[A-Za-z0-9_]{8,}|"
    r"github_pat_[A-Za-z0-9_]{8,}|"
    r"glpat-[A-Za-z0-9_-]{6,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
    r"sk-ant-[A-Za-z0-9_-]{8,}|"
    r"sk-proj-[A-Za-z0-9_-]{8,}|"
    r"sk-[A-Za-z0-9_-]{8,}|"
    r"AIza[A-Za-z0-9_-]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{6,}"
    r")(?![A-Za-z0-9_])"
)


def compile_known_token_re(
    *,
    ignorecase: bool = False,
    match_truncated_provider_tokens: bool = True,
) -> re.Pattern[str]:
    """Compile the shared known-token pattern with optional case folding.

    Several provider-specific prefixes (``gh[apousr]_``, ``glpat-``, and
    ``xox[baprs]-``) can use ``*`` so truncated or rejected token values are
    still redacted. Passing ``match_truncated_provider_tokens=True`` makes this
    false-positive tradeoff explicit at security redaction call sites. Use
    ``False`` only for detection paths that need the historical minimum-length
    guards. Enable ``ignorecase=True`` for operator-facing first-run output
    where case-variant tokens must be caught.
    """
    flags = re.IGNORECASE if ignorecase else re.NOFLAG
    pattern = KNOWN_TOKEN_PATTERN if match_truncated_provider_tokens else STRICT_KNOWN_TOKEN_PATTERN
    return re.compile(pattern, flags)


def compile_token_assignment_re() -> re.Pattern[str]:
    """Compile the shared assignment-style token redaction pattern."""
    return re.compile(TOKEN_ASSIGNMENT_PATTERN, re.IGNORECASE)


def compile_provider_ref_re() -> re.Pattern[str]:
    """Compile the shared provider credential reference pattern."""
    return re.compile(PROVIDER_REF_PATTERN, re.IGNORECASE)


def compile_provider_ref_key_re() -> re.Pattern[str]:
    """Compile the shared provider credential reference key pattern."""
    return re.compile(PROVIDER_REF_KEY_PATTERN, re.IGNORECASE)


def compile_provider_ref_key_suffix_re() -> re.Pattern[str]:
    """Compile the shared provider credential reference key-suffix pattern."""
    return re.compile(PROVIDER_REF_KEY_SUFFIX_PATTERN, re.IGNORECASE)
