"""Secret redaction helpers for workspace logs."""

from __future__ import annotations

import pytest

from awf.common.redaction import (
    REDACTION_MARKER,
    redact_secrets,
    redact_secrets_byte_slice,
    redact_secrets_slice,
)


@pytest.mark.unit
def test_redact_secrets_preserves_context_while_removing_known_secret_bodies() -> None:
    raw_values = (
        "ghp_FAKEgithubTokenValue123456",
        "github_pat_FAKEgithubPatTokenValue_123456",
        "glpat-fakeGitLabTokenValue123456",
        "sk-proj-fakeOpenAIProjectKey123456789",
        "sk-ant-fakeAnthropicKey123456789",
        "AIzaFakeGeminiApiKey1234567890ABCD",
        "xoxb-fakeSlackTokenValue123456",
        "opaqueBearerToken123456",
        "basicHeaderValue123456",
        "url-password-value",
        "awf-token-value-123456",
    )
    text = (
        "push token ghp_FAKEgithubTokenValue123456 "
        "pat github_pat_FAKEgithubPatTokenValue_123456 "
        "gitlab glpat-fakeGitLabTokenValue123456 "
        "openai sk-proj-fakeOpenAIProjectKey123456789 "
        "anthropic sk-ant-fakeAnthropicKey123456789 "
        "gemini AIzaFakeGeminiApiKey1234567890ABCD "
        "slack xoxb-fakeSlackTokenValue123456 "
        "Authorization: Bearer opaqueBearerToken123456 "
        "Authorization: Basic basicHeaderValue123456 "
        "repo https://user:url-password-value@github.com/example/repo.git "
        "AWF_API_TOKEN=awf-token-value-123456"
    )

    redacted = redact_secrets(text)

    for raw_value in raw_values:
        assert raw_value not in redacted
    assert redacted.count(REDACTION_MARKER) == 11
    assert "push token" in redacted
    assert "Authorization: Bearer <redacted>" in redacted
    assert "Authorization: Basic <redacted>" in redacted
    assert "https://<redacted>@github.com/example/repo.git" in redacted
    assert "AWF_API_TOKEN=<redacted>" in redacted


@pytest.mark.unit
@pytest.mark.parametrize(
    "token",
    [
        "ghp_ABCDEF",
        "gho_ABCDEFG",
        "github_pat_ABCDEF",
        "github_pat_ABCDEFG",
    ],
)
def test_redact_secrets_catches_truncated_github_tokens(token: str) -> None:
    redacted = redact_secrets(f"clone failed with token {token} in stderr")

    assert token not in redacted
    assert f"token {REDACTION_MARKER} in stderr" in redacted


@pytest.mark.unit
def test_redact_secrets_masks_exact_secret_before_pattern_substrings() -> None:
    """Mask exact secrets as wholes even when they contain regex-redacted parts."""
    url_secret = "https://svc-user:svc-password@example.test/private/project"
    header_secret = "Authorization: Bearer exactHeaderToken123456 team=platform"
    text = f"setup url={url_secret} header={header_secret} done"

    redacted = redact_secrets(text, extra_secrets=(url_secret, header_secret))

    assert redacted == f"setup url={REDACTION_MARKER} header={REDACTION_MARKER} done"
    for leaked_fragment in (
        "svc-user",
        "svc-password",
        "example.test",
        "/private/project",
        "exactHeaderToken123456",
        "team=platform",
    ):
        assert leaked_fragment not in redacted


@pytest.mark.unit
def test_redact_secrets_slice_masks_overlapping_exact_secret() -> None:
    """Mask a requested slice that starts inside an exact configured secret."""
    secret = "opaque-nonpattern-workspace-secret-value"
    text = f"before AWF_GITHUB_TOKEN={secret} after"
    offset = text.index("workspace")
    limit = len("workspace")

    redacted = redact_secrets_slice(
        text,
        offset,
        offset + limit,
        extra_secrets=(secret,),
    )

    assert redacted == REDACTION_MARKER
    assert "workspace" not in redacted


@pytest.mark.unit
def test_redact_secrets_slice_preserves_nonsecret_requested_text() -> None:
    """Return unmasked text when the requested slice does not overlap a secret."""
    secret = "opaque-nonpattern-workspace-secret-value"
    text = f"before AWF_GITHUB_TOKEN={secret} after"
    offset = text.index("before")
    limit = len("before")

    assert (
        redact_secrets_slice(
            text,
            offset,
            offset + limit,
            extra_secrets=(secret,),
        )
        == "before"
    )


@pytest.mark.unit
def test_redact_secrets_slice_preserves_text_after_secret_span() -> None:
    """Return plain text for a slice that begins after a secret span."""
    secret = "opaque-nonpattern-workspace-secret-value"
    text = f"before AWF_GITHUB_TOKEN={secret} after"
    offset = text.index("after")
    limit = len("after")

    assert (
        redact_secrets_slice(
            text,
            offset,
            offset + limit,
            extra_secrets=(secret,),
        )
        == "after"
    )


@pytest.mark.unit
def test_redact_secrets_slice_returns_plain_text_when_no_secret_matches() -> None:
    """Return the requested slice unchanged when no secret patterns match."""
    text = "before ordinary output after"
    offset = text.index("ordinary")
    limit = len("ordinary")

    assert redact_secrets_slice(text, offset, offset + limit) == "ordinary"


@pytest.mark.unit
def test_redact_secrets_byte_slice_uses_utf8_byte_offsets() -> None:
    """Return the requested byte window when earlier text is multi-byte."""
    prefix = "\U0001f525alpha\n"
    text = f"{prefix}beta\n"
    offset = len(prefix.encode())
    limit = len(b"beta")

    assert redact_secrets_byte_slice(text, offset, offset + limit) == "beta"


@pytest.mark.unit
def test_redact_secrets_byte_slice_masks_secret_after_multibyte_prefix() -> None:
    """Mask a byte slice overlapping a secret after earlier multi-byte text."""
    secret = "opaque-nonpattern-workspace-secret-value"
    prefix = "\u00e9\u0905\U0001f525before "
    text = f"{prefix}AWF_GITHUB_TOKEN={secret} after"
    offset = len(f"{prefix}AWF_GITHUB_TOKEN=opaque-nonpattern-".encode())
    limit = len(b"workspace")

    redacted = redact_secrets_byte_slice(
        text,
        offset,
        offset + limit,
        extra_secrets=(secret,),
    )

    assert redacted == REDACTION_MARKER
    assert "workspace" not in redacted


@pytest.mark.unit
def test_redact_secrets_byte_slice_masks_secret_starting_at_byte_zero() -> None:
    """Mask a byte slice overlapping a secret at the start of the text."""
    secret = "opaque-nonpattern-workspace-secret-value"
    text = f"{secret} after"

    redacted = redact_secrets_byte_slice(
        text,
        0,
        len(b"opaque"),
        extra_secrets=(secret,),
    )

    assert redacted == REDACTION_MARKER
    assert "opaque" not in redacted


@pytest.mark.unit
def test_redact_secrets_byte_slice_masks_overlapping_exact_secret_self_match() -> None:
    """Mask a byte slice covered only by a later overlapping configured secret."""
    secret = "abcabc"
    text = "abcabcabc"

    redacted = redact_secrets_byte_slice(
        text,
        6,
        9,
        extra_secrets=(secret,),
    )

    assert redacted == REDACTION_MARKER
    assert "abc" not in redacted


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("GH_TOKEN='gho_fakeGitHubOauthToken123456'", "GH_TOKEN='<redacted>'"),
        ("GITHUB_TOKEN='ghp_fakeGitHubToken123456'", "GITHUB_TOKEN='<redacted>'"),
        ("AWF_GITHUB_TOKEN='ghp_fakeAwfGitHubToken123456'", "AWF_GITHUB_TOKEN='<redacted>'"),
        ('AWF_AUTH_TOKEN="awf-auth-value-123456"', 'AWF_AUTH_TOKEN="<redacted>"'),
        ("CUSTOM_API_TOKEN: custom-api-token-123456", "CUSTOM_API_TOKEN: <redacted>"),
        ("SERVICE_TOKEN=generic-token-value-123456", "SERVICE_TOKEN=<redacted>"),
        ("PASSWORD='database-password-123456'", "PASSWORD='<redacted>'"),
        ("PASSWD=database-passwd-value-123456", "PASSWD=<redacted>"),
        ("SECRET=shared-secret-value-123456", "SECRET=<redacted>"),
        ("PROJECT_API_KEY: project-api-key-123456", "PROJECT_API_KEY: <redacted>"),
        ("ACCESS_KEY=access-key-value-123456", "ACCESS_KEY=<redacted>"),
        ("curl -H 'Authorization: Bearer bearerToken123456'", "Authorization: Bearer <redacted>"),
        ("using Bearer looseBearerValue123456 now", "using Bearer <redacted> now"),
    ],
)
def test_redact_secrets_handles_token_assignments_and_bearer_values(
    text: str,
    expected: str,
) -> None:
    redacted = redact_secrets(text)

    assert expected in redacted
    assert "123456" not in redacted


@pytest.mark.unit
def test_redact_secrets_redacts_provider_refs_and_plain_file_paths() -> None:
    """Redact setup credential references and their plain-file paths."""
    raw_refs = (
        "keyring://awf/github/default",
        "env://OPENAI_API_KEY",
        "plain-file:///home/user/.awf/secrets/codex.default",
    )
    raw_token = "ghp_providerRefToken123456"
    text = (
        f"github={raw_refs[0]} "
        f"codex credential_ref={raw_refs[1]} "
        f"plain ref {raw_refs[2]} "
        f"token={raw_token} "
        "repo https://user:plain-password@github.com/example/repo.git"
    )

    redacted = redact_secrets(text)

    for raw_ref in raw_refs:
        assert raw_ref not in redacted
    assert "/home/user/.awf/secrets/codex.default" not in redacted
    assert raw_token not in redacted
    assert "plain-password" not in redacted
    assert redacted.count(REDACTION_MARKER) >= 5
    assert "github=" in redacted
    assert "codex credential_ref=" in redacted
    assert "plain ref" in redacted
