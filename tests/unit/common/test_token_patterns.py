"""Shared token-pattern redaction tests."""

from __future__ import annotations

import re

import pytest

from awf.common import audit, redaction
from awf.common.token_patterns import (
    KNOWN_TOKEN_PATTERN,
    PROVIDER_REF_KEY_PATTERN,
    PROVIDER_REF_KEY_SUFFIX_PATTERN,
    PROVIDER_REF_PATTERN,
    compile_known_token_re,
)
from awf.host_setup import rendering


@pytest.mark.unit
def test_redactors_share_known_token_pattern() -> None:
    """Verify security redactors compile from one known-token pattern source."""
    assert audit._KNOWN_TOKEN_RE.pattern == KNOWN_TOKEN_PATTERN  # noqa: SLF001
    assert redaction._KNOWN_TOKEN_RE.pattern == KNOWN_TOKEN_PATTERN  # noqa: SLF001
    assert rendering._FIRST_RUN_KNOWN_TOKEN_RE.pattern == KNOWN_TOKEN_PATTERN  # noqa: SLF001
    assert rendering._FIRST_RUN_KNOWN_TOKEN_RE.flags & re.IGNORECASE  # noqa: SLF001


@pytest.mark.unit
def test_known_token_pattern_can_keep_historical_minimum_length_guards() -> None:
    """Verify callers can opt into strict provider token body lengths."""
    strict_re = compile_known_token_re(match_truncated_provider_tokens=False)

    assert strict_re.search("rejected ghp_ token") is None
    assert strict_re.search("rejected glpat-a token") is None
    assert strict_re.search("rejected xoxb-a token") is None
    assert strict_re.search("accepted ghp_12345678 token")
    assert strict_re.search("accepted glpat-123456 token")
    assert strict_re.search("accepted xoxb-123456 token")


@pytest.mark.unit
def test_first_run_redactor_uses_shared_provider_ref_patterns() -> None:
    """Verify first-run provider-ref redaction compiles from common patterns."""
    assert rendering._PROVIDER_REF_RE.pattern == PROVIDER_REF_PATTERN  # noqa: SLF001
    assert rendering._PROVIDER_REF_KEY_RE.pattern == PROVIDER_REF_KEY_PATTERN  # noqa: SLF001
    assert (  # noqa: SLF001
        rendering._PROVIDER_REF_KEY_SUFFIX_RE.pattern == PROVIDER_REF_KEY_SUFFIX_PATTERN
    )
    assert rendering._PROVIDER_REF_RE.flags & re.IGNORECASE  # noqa: SLF001
    assert rendering._PROVIDER_REF_KEY_RE.flags & re.IGNORECASE  # noqa: SLF001
    assert rendering._PROVIDER_REF_KEY_SUFFIX_RE.flags & re.IGNORECASE  # noqa: SLF001


@pytest.mark.unit
@pytest.mark.parametrize("raw_token", ["glpat-", "glpat-a"])
def test_shared_redactors_catch_truncated_gitlab_pats(raw_token: str) -> None:
    """Verify shortened rejected GitLab PAT values do not leak in diagnostics."""
    assert raw_token not in redaction.redact_secrets(f"rejected token {raw_token}")
    assert raw_token not in audit.redact_audit_text(f"rejected token {raw_token}")
    first_run_redacted = rendering.redact_first_run_value(
        {
            "message": f"rejected token {raw_token}",
            raw_token: "provider auth failed",
        }
    )
    assert raw_token not in str(first_run_redacted)


@pytest.mark.unit
@pytest.mark.parametrize("raw_token", ["ghp_", "ghp_a", "gho_a", "github_pat_a"])
def test_shared_redactors_catch_truncated_github_tokens(raw_token: str) -> None:
    """Verify shortened rejected GitHub token values do not leak in diagnostics."""
    assert raw_token not in redaction.redact_secrets(f"rejected token {raw_token}")
    assert raw_token not in audit.redact_audit_text(f"rejected token {raw_token}")
    first_run_redacted = rendering.redact_first_run_value(
        {
            "message": f"rejected token {raw_token}",
            raw_token: "provider auth failed",
        }
    )
    assert raw_token not in str(first_run_redacted)


@pytest.mark.unit
@pytest.mark.parametrize("raw_token", ["xoxb-", "xoxb-a", "xoxp-", "xoxp-a"])
def test_shared_redactors_catch_truncated_slack_tokens(raw_token: str) -> None:
    """Verify shortened rejected Slack token values do not leak in diagnostics."""
    assert raw_token not in redaction.redact_secrets(f"rejected token {raw_token}")
    assert raw_token not in audit.redact_audit_text(f"rejected token {raw_token}")
    first_run_redacted = rendering.redact_first_run_value(
        {
            "message": f"rejected token {raw_token}",
            raw_token: "slack auth failed",
        }
    )
    assert raw_token not in str(first_run_redacted)
