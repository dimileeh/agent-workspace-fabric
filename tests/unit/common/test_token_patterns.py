"""Shared token-pattern redaction tests."""

from __future__ import annotations

import re

import pytest

from awf.common import audit, redaction, token_patterns
from awf.common.token_patterns import (
    KNOWN_TOKEN_PATTERN,
    PROVIDER_REF_KEY_PATTERN,
    PROVIDER_REF_KEY_SUFFIX_PATTERN,
    PROVIDER_REF_PATTERN,
    compile_known_token_re,
    compile_token_assignment_re,
)
from awf.host_setup import rendering
from awf.service import logs as service_logs


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
@pytest.mark.parametrize(
    ("prefix", "suffix", "runtime_prefix", "audit_prefix"),
    [
        ("SSH_PRIVATE_KEY=", "", "SSH_PRIVATE_KEY=<redacted>", "SSH_PRIVATE_KEY=[redacted]"),
        ('PRIVATE_KEY="', '"', 'PRIVATE_KEY="<redacted>"', 'PRIVATE_KEY="[redacted]"'),
    ],
)
def test_shared_assignment_redactors_mask_multiline_private_key_values(
    prefix: str,
    suffix: str,
    runtime_prefix: str,
    audit_prefix: str,
) -> None:
    """Verify PEM private-key assignments are masked beyond the first token."""
    pem_kind = "OPENSSH PRIVATE" + " KEY"
    pem_value = (
        f"-----BEGIN {pem_kind}-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ==\n"
        f"-----END {pem_kind}-----"
    )
    text = f"{prefix}{pem_value}{suffix}\nstatus=ready"
    match = compile_token_assignment_re().search(text)

    assert match is not None
    assert match.group("value") == pem_value
    runtime_redacted = redaction.redact_secrets(text)
    audit_redacted = audit.redact_audit_text(text, limit=5000)

    for redacted in (runtime_redacted, audit_redacted):
        assert "OPENSSH PRIVATE KEY" not in redacted
        assert "b3BlbnNzaC1rZXktdjE" not in redacted
        assert "status=ready" in redacted
    assert runtime_prefix in runtime_redacted
    assert audit_prefix in audit_redacted


@pytest.mark.unit
def test_shared_assignment_redactors_mask_quoted_pem_private_key_with_trailing_whitespace() -> None:
    """Verify quoted PEM assignments redact when whitespace precedes the closing quote."""
    pem_kind = "OPENSSH PRIVATE" + " KEY"
    pem_value = (
        f"-----BEGIN {pem_kind}-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ==\n"
        f"-----END {pem_kind}-----"
    )
    text = f'PRIVATE_KEY="{pem_value}\n  "\nstatus=ready'
    match = compile_token_assignment_re().search(text)

    assert match is not None
    assert match.group("value") == pem_value
    runtime_redacted = redaction.redact_secrets(text)
    audit_redacted = audit.redact_audit_text(text, limit=5000)

    for redacted in (runtime_redacted, audit_redacted):
        assert "OPENSSH PRIVATE KEY" not in redacted
        assert "b3BlbnNzaC1rZXktdjE" not in redacted
        assert "status=ready" in redacted
    assert 'PRIVATE_KEY="<redacted>' in runtime_redacted
    assert 'PRIVATE_KEY="[redacted]' in audit_redacted


@pytest.mark.unit
def test_token_assignment_pattern_guards_multiline_private_key_branch() -> None:
    """Verify PEM private-key matching is explicitly gated before scanning."""
    assignment_re = compile_token_assignment_re()
    private_key_suffix = " KEY-----"
    pem_header = "-----BEGIN OPENSSH PRIVATE" + private_key_suffix
    text = f"PRIVATE_KEY={pem_header}\n{'A' * 4096}\nstatus=ready"

    match = assignment_re.search(text)

    assert "(?=-----BEGIN [A-Z0-9 -]*PRIVATE KEY-----)" in assignment_re.pattern
    assert match is not None
    assert match.group("value") == "-----BEGIN"


@pytest.mark.unit
def test_service_log_pem_patterns_use_shared_assignment_key_pattern() -> None:
    """Keep service-log PEM assignment guards compiled from the shared key source."""
    assert hasattr(token_patterns, "TOKEN_ASSIGNMENT_KEY_PATTERN")
    assert service_logs._TOKEN_ASSIGNMENT_KEY_PATTERN == token_patterns.TOKEN_ASSIGNMENT_KEY_PATTERN  # noqa: SLF001
    assert (
        token_patterns.TOKEN_ASSIGNMENT_KEY_PATTERN
        in service_logs._MULTILINE_PEM_ASSIGNMENT_START_RE.pattern
    )  # noqa: SLF001
    assert (
        token_patterns.TOKEN_ASSIGNMENT_KEY_PATTERN
        in service_logs._PENDING_PEM_ASSIGNMENT_PREFIX_RE.pattern
    )  # noqa: SLF001


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
def test_shared_redactors_mask_atlassian_api_token_shapes() -> None:
    """Verify Atlassian API tokens (ATATT…) never leak through diagnostics.

    Defense-in-depth: the Bitbucket git-auth design never logs the token, but a
    stray token in a log line, an assignment, or a URL must still be masked.
    """
    raw_token = "ATATT3xFfGF0abcDEF_123-456=.789"

    bare = f"clone failed for token {raw_token} on bitbucket.org"
    assignment = f"BITBUCKET_API_TOKEN={raw_token}"
    url = f"https://agent@example.com:{raw_token}@bitbucket.org/ws/repo.git"

    for text in (bare, assignment, url):
        assert raw_token not in redaction.redact_secrets(text)
        assert raw_token not in audit.redact_audit_text(text)


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
