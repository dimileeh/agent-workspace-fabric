"""Shared token-shaped secret recognition patterns."""

from __future__ import annotations

import re
from typing import Final

KNOWN_TOKEN_PATTERN: Final = (
    # Python's regex alternation is left-to-right: keep provider-specific
    # ``sk-`` prefixes before the generic ``sk-`` catch-all.
    r"(?<![A-Za-z0-9_])("
    r"gh[apousr]_[A-Za-z0-9_]{6,}|"
    r"github_pat_[A-Za-z0-9_]{6,}|"
    r"glpat-[A-Za-z0-9_-]{8,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
    r"sk-ant-[A-Za-z0-9_-]{8,}|"
    r"sk-proj-[A-Za-z0-9_-]{8,}|"
    r"sk-[A-Za-z0-9_-]{8,}|"
    r"AIza[A-Za-z0-9_-]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{8,}"
    r")(?![A-Za-z0-9_])"
)


def compile_known_token_re(*, ignorecase: bool = False) -> re.Pattern[str]:
    """Compile the shared known-token pattern with optional case folding."""
    flags = re.IGNORECASE if ignorecase else re.NOFLAG
    return re.compile(KNOWN_TOKEN_PATTERN, flags)
