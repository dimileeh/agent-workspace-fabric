"""Secret redaction helpers for workspace logs."""

from __future__ import annotations

import pytest

from awf.common.redaction import REDACTION_MARKER, redact_secrets


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
    ("text", "expected"),
    [
        ("GH_TOKEN='gho_fakeGitHubOauthToken123456'", "GH_TOKEN='<redacted>'"),
        ("GITHUB_TOKEN='ghp_fakeGitHubToken123456'", "GITHUB_TOKEN='<redacted>'"),
        ("AWF_GITHUB_TOKEN='ghp_fakeAwfGitHubToken123456'", "AWF_GITHUB_TOKEN='<redacted>'"),
        ('AWF_AUTH_TOKEN="awf-auth-value-123456"', 'AWF_AUTH_TOKEN="<redacted>"'),
        ("CUSTOM_API_TOKEN: custom-api-token-123456", "CUSTOM_API_TOKEN: <redacted>"),
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
