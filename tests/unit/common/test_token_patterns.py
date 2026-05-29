"""Shared token-pattern redaction tests."""

from __future__ import annotations

import re

import pytest

from awf.common import audit, redaction
from awf.common.token_patterns import KNOWN_TOKEN_PATTERN
from awf.host_setup import rendering


@pytest.mark.unit
def test_redactors_share_known_token_pattern() -> None:
    """Verify security redactors compile from one known-token pattern source."""
    assert audit._KNOWN_TOKEN_RE.pattern == KNOWN_TOKEN_PATTERN  # noqa: SLF001
    assert redaction._KNOWN_TOKEN_RE.pattern == KNOWN_TOKEN_PATTERN  # noqa: SLF001
    assert rendering._FIRST_RUN_KNOWN_TOKEN_RE.pattern == KNOWN_TOKEN_PATTERN  # noqa: SLF001
    assert rendering._FIRST_RUN_KNOWN_TOKEN_RE.flags & re.IGNORECASE  # noqa: SLF001
