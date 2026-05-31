"""Provider/model failure fingerprint classification tests."""

from __future__ import annotations

from awf.adapters.provider_failures import (
    AGENT_AUTH_FAILED,
    AGENT_IDLE_TIMEOUT,
    AGENT_PROVIDER_CAPACITY_EXHAUSTED,
    AGENT_TIMEOUT,
    classify_provider_failure,
    failure_fingerprint,
    infer_provider,
)


def test_classifies_gemini_auth_failure_and_redacts_secret_fingerprint() -> None:
    classification = classify_provider_failure(
        reason_code=None,
        stdout="",
        stderr=("GEMINI_API_KEY=AIzaSyProviderSecret was rejected: 401 unauthorized"),
        provider=None,
        model="gemini-1.5-pro",
    )

    assert classification is not None
    assert classification.reason_code == AGENT_AUTH_FAILED
    assert classification.failure_type == "auth"
    assert classification.provider == "google"
    assert classification.model == "gemini-1.5-pro"
    assert classification.retryable is True
    assert "AIzaSyProviderSecret" not in classification.failure_fingerprint
    assert "<redacted>" in classification.failure_fingerprint


def test_classifies_xai_grok_auth_failure_and_redacts_secret_fingerprint() -> None:
    classification = classify_provider_failure(
        reason_code=None,
        stdout="",
        stderr="XAI_API_KEY=xai-provider-secret was rejected: unauthorized",
        provider=None,
        model="grok-build-0.1",
    )

    assert classification is not None
    assert classification.reason_code == AGENT_AUTH_FAILED
    assert classification.failure_type == "auth"
    assert classification.provider == "xai"
    assert classification.model == "grok-build-0.1"
    assert classification.retryable is True
    assert "xai-provider-secret" not in classification.failure_fingerprint
    assert "<redacted>" in classification.failure_fingerprint


def test_classifies_xai_rate_limit_from_model_marker() -> None:
    classification = classify_provider_failure(
        reason_code=None,
        stdout="",
        stderr="Grok Build request failed: 429 too many requests. Retry-After: 60",
        provider=None,
        model="grok-build-0.1",
    )

    assert classification is not None
    assert classification.reason_code == AGENT_PROVIDER_CAPACITY_EXHAUSTED
    assert classification.failure_type == "quota"
    assert classification.provider == "xai"
    assert classification.retry_after_seconds == 60


def test_classifies_quota_capacity_with_retry_after() -> None:
    classification = classify_provider_failure(
        reason_code=None,
        stdout="",
        stderr="RESOURCE_EXHAUSTED RetryableQuotaError. Retry-After: 120",
        provider="google",
        model="gemini-2.5-pro",
    )

    assert classification is not None
    assert classification.reason_code == AGENT_PROVIDER_CAPACITY_EXHAUSTED
    assert classification.failure_type == "quota"
    assert classification.provider == "google"
    assert classification.retryable is True
    assert classification.retry_after_seconds == 120
    assert classification.fallback_allowed is True


def test_classifies_codex_spark_usage_limit_as_provider_retryable() -> None:
    classification = classify_provider_failure(
        reason_code=None,
        stdout="",
        stderr=(
            "You've hit your usage limit for GPT-5.3-Codex-Spark. "
            "Switch to another model now, or try again at 10:29 PM."
        ),
        provider=None,
        model="gpt-5.3-codex-spark",
    )

    assert classification is not None
    assert classification.reason_code == AGENT_PROVIDER_CAPACITY_EXHAUSTED
    assert classification.failure_type == "usage_limit"
    assert classification.provider == "openai"
    assert classification.model == "gpt-5.3-codex-spark"
    assert classification.retryable is True


def test_usage_limit_without_provider_hint_keeps_provider_unknown() -> None:
    classification = classify_provider_failure(
        reason_code=None,
        stdout="",
        stderr="You've hit your usage limit. Switch to another model.",
        provider=None,
        model=None,
    )

    assert classification is not None
    assert classification.reason_code == AGENT_PROVIDER_CAPACITY_EXHAUSTED
    assert classification.failure_type == "usage_limit"
    assert classification.provider is None
    assert classification.retryable is True


def test_known_timeout_reason_codes_become_provider_recovery_metadata() -> None:
    timeout = classify_provider_failure(
        reason_code=AGENT_TIMEOUT,
        stdout="",
        stderr="command timed out",
        provider="openai",
        model="gpt-5.3-codex",
    )
    idle_timeout = classify_provider_failure(
        reason_code=AGENT_IDLE_TIMEOUT,
        stdout="",
        stderr="idle timeout",
        provider="openai",
        model="gpt-5.3-codex",
    )

    assert timeout is not None
    assert timeout.reason_code == AGENT_TIMEOUT
    assert timeout.failure_type == "timeout"
    assert timeout.retryable is True
    assert idle_timeout is not None
    assert idle_timeout.reason_code == AGENT_IDLE_TIMEOUT
    assert idle_timeout.failure_type == "idle_timeout"
    assert idle_timeout.retryable is True


def test_timeout_without_provider_or_model_is_not_recovery_metadata() -> None:
    assert (
        classify_provider_failure(
            reason_code=AGENT_TIMEOUT,
            stdout="",
            stderr="command timed out",
            provider=None,
            model=None,
        )
        is None
    )


def test_provider_inference_covers_anthropic_and_ollama_markers() -> None:
    assert infer_provider(model="claude-sonnet-4.5", output=None) == "anthropic"
    assert infer_provider(model=None, output="ollama local model is unavailable") == "ollama"
    assert infer_provider(model="grok-build-0.1", output=None) == "xai"
    assert infer_provider(model=None, output="xAI Grok rate limit") == "xai"


def test_failure_fingerprint_truncates_long_output_after_redaction() -> None:
    fingerprint = failure_fingerprint(
        reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
        failure_type="capacity",
        provider="openai",
        model="gpt-5.5",
        output="x" * 50,
        limit=12,
    )

    assert fingerprint.endswith("|xxxxxxxxxxxx")
    assert len(fingerprint.rsplit("|", 1)[-1]) == 12


def test_deterministic_cli_failures_are_not_provider_failures() -> None:
    classification = classify_provider_failure(
        reason_code="AGENT_CLI_FAILED",
        stdout="SyntaxError: invalid syntax",
        stderr="ImportError: cannot import name missing",
        provider=None,
        model=None,
    )

    assert classification is None


def test_lint_rule_ids_containing_401_are_not_auth_failures() -> None:
    classification = classify_provider_failure(
        reason_code="AGENT_CLI_FAILED",
        stdout="",
        stderr="ruff failed: E401 multiple imports on one line",
        provider=None,
        model=None,
    )

    assert classification is None
