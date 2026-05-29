"""Shared token-shaped secret recognition patterns."""

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
    r"xox[baprs]-[A-Za-z0-9-]{8,}"
    r")(?![A-Za-z0-9_])"
)
TOKEN_ASSIGNMENT_PATTERN: Final = (
    r"\b(?P<key>"
    r"(?:[A-Za-z][A-Za-z0-9_]*_)?TOKEN"
    r"|(?:[A-Za-z][A-Za-z0-9_]*_)?(?:API[_-]?KEY|ACCESS[_-]?KEY)"
    r"|(?:AUTH|GITHUB|GH)[_-]?TOKEN"
    r"|PASSWORD|PASSWD|SECRET"
    r")\b"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<quote>[\"']?)"
    r"(?P<value>[^\s\"'`,;)}\]]+)"
    r"(?P=quote)"
)


def compile_known_token_re(*, ignorecase: bool = False) -> re.Pattern[str]:
    """Compile the shared known-token pattern with optional case folding."""
    flags = re.IGNORECASE if ignorecase else re.NOFLAG
    return re.compile(KNOWN_TOKEN_PATTERN, flags)


def compile_token_assignment_re() -> re.Pattern[str]:
    """Compile the shared assignment-style token redaction pattern."""
    return re.compile(TOKEN_ASSIGNMENT_PATTERN, re.IGNORECASE)
