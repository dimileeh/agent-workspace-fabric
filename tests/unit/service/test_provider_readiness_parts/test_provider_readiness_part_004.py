"""Provider credential readiness redaction checks for local service mode."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import awf.service.provider_readiness as provider_readiness
import awf.service.provider_readiness_helpers as provider_readiness_helpers
from awf.service.provider_readiness import collect_agent_readiness

from .test_provider_readiness_part_001 import (
    _completed,
    _ollama_ok,
    _settings,
    _unexpected_subprocess,
)


@pytest.mark.unit
@pytest.mark.parametrize("identifier", ["sk-live-abc12345", "sk-test-abc12345"])
def test_provider_readiness_preserves_non_secret_sk_identifiers(identifier: str) -> None:
    assert provider_readiness._redact(f"diagnostic {identifier}", frozenset()) == (
        f"diagnostic {identifier}"
    )


@pytest.mark.unit
def test_provider_readiness_redacts_secret_values_from_details(tmp_path: Path) -> None:
    github_secret = "ghp_super_secret"
    env = {
        "AWF_GITHUB_TOKEN": github_secret,
        "ANTHROPIC_API_KEY": "anthropic_secret",
        "GEMINI_API_KEY": "gemini_secret",
        "XAI_API_KEY": "xai_secret",
    }

    def _run(args: list[str], **_kwargs: object) -> Any:
        assert args == ["gh", "auth", "status", "--hostname", "github.com"]
        return _completed(
            returncode=1,
            stderr=(
                "failed cloning https://user:ghp_super_secret@github.com/org/repo "
                "with bearer anthropic_secret, gemini_secret, and xai_secret"
            ),
        )

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ=env,
        run_subprocess=_run,
    )

    github = payload["providers"]["github"]
    assert github["reason"] == "GITHUB_AUTH_UNUSABLE"
    serialized = json.dumps(payload, sort_keys=True)
    assert github_secret not in serialized
    assert "anthropic_secret" not in serialized
    assert "gemini_secret" not in serialized
    assert "xai_secret" not in serialized
    assert "<redacted>" in serialized


@pytest.mark.unit
def test_provider_readiness_preserves_long_diagnostic_ids_in_details(
    tmp_path: Path,
) -> None:
    github_secret = "ghp_diagnostic_secret"
    image_digest = "a" * 64
    container_id = "b" * 40
    error_payload_id = "payload_" + ("c" * 40)

    def _run(args: list[str], **_kwargs: object) -> Any:
        assert args == ["gh", "auth", "status", "--hostname", "github.com"]
        return _completed(
            returncode=1,
            stderr=(
                f"failed for token {github_secret}; "
                f"image sha256:{image_digest}; "
                f"container {container_id}; "
                f"payload {error_payload_id}"
            ),
        )

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"AWF_GITHUB_TOKEN": github_secret},
        run_subprocess=_run,
    )

    detail = payload["providers"]["github"]["detail"]
    assert github_secret not in detail
    assert "<redacted>" in detail
    assert image_digest in detail
    assert container_id in detail
    assert error_payload_id in detail


@pytest.mark.unit
def test_provider_readiness_redaction_parts_preserve_literal_context() -> None:
    rendered, parts = provider_readiness._redact_with_redaction_parts(
        (
            "prefix sk-proj-literal-secret "
            "https://user:ghp_url_secret@github.com/org/repo "
            "and token sk-ant-token-secret suffix"
        ),
        frozenset({"sk-proj-literal-secret"}),
    )

    assert rendered == (
        "prefix <redacted> https://<redacted>@github.com/org/repo and token <redacted> suffix"
    )
    assert parts == [
        "prefix ",
        " https://",
        "@github.com/org/repo and token ",
        " suffix",
    ]


@pytest.mark.unit
def test_provider_readiness_redaction_segment_helpers_cover_overlap_edges() -> None:
    segments = [
        ("literal", "alpha"),
        ("redaction", ""),
        ("literal", "beta"),
        ("literal", ""),
        ("literal", "gamma"),
    ]

    assert provider_readiness_helpers._replace_literal_redaction_spans(segments, "") is segments
    assert provider_readiness_helpers._slice_redaction_segments(segments, 8, 8) == []
    assert provider_readiness_helpers._slice_redaction_segments(segments, 0, 17) == [
        ("literal", "alpha"),
        ("redaction", ""),
        ("literal", "be"),
    ]
    assert provider_readiness_helpers._merge_literal_redaction_segments(segments) == [
        ("literal", "alpha"),
        ("redaction", ""),
        ("literal", "betagamma"),
    ]
    assert provider_readiness_helpers._replace_literal_redaction_spans(
        [("literal", "unchanged"), ("redaction", ""), ("literal", "alpha-alpha")],
        "alpha",
    ) == [
        ("literal", "unchanged"),
        ("redaction", ""),
        ("redaction", ""),
        ("literal", "-"),
        ("redaction", ""),
    ]
    assert provider_readiness_helpers._slice_redaction_segments(segments, 6, 14) == [
        ("literal", "redacted")
    ]


@pytest.mark.unit
def test_provider_readiness_helper_fallbacks_handle_unknown_shapes() -> None:
    assert (
        provider_readiness._primary_credential_scope([{"credential_scope": "custom_scope"}])
        == "not_observed"
    )
    assert provider_readiness._primary_isolation([{"isolation": "custom_isolation"}]) == "none"
    assert provider_readiness_helpers._provider_warning_values({"warnings": "not-a-list"}) == []

    summary = provider_readiness._security_summary(
        {
            "github": {"status": "warn", "reason": "GITHUB_TOKEN_ENV_MISSING"},
            "codex": {"status": "ok", "warnings": ["ignored"]},
            "docker": {
                "status": "ok",
                "warnings": [{"reason": "DOCKER_HOST_BROAD_CONTROL", "severity": "warning"}],
            },
        }
    )

    assert summary["status"] == "warning"
    assert summary["warning_count"] == 1
    assert summary["providers_with_warnings"] == ["github", "codex", "docker"]
    assert summary["reason_codes"] == [
        "DOCKER_HOST_BROAD_CONTROL",
        "GITHUB_TOKEN_ENV_MISSING",
    ]


@pytest.mark.unit
def test_provider_readiness_defensive_provider_dispatch_and_probe_fallbacks(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)

    with pytest.raises(AssertionError, match="unsupported provider"):
        provider_readiness._check_provider_readiness(
            "unknown",  # type: ignore[arg-type]
            settings,
            environ={},
            host_home=tmp_path,
            strict=True,
            run_subprocess=_unexpected_subprocess,
            http_get=_ollama_ok,
            secrets=frozenset(),
        )

    probe = provider_readiness._selected_launch_probe(
        "github",
        settings=settings,
        provider_result={"ok": True},
        model="gpt-test",
        environ={},
        run_subprocess=_unexpected_subprocess,
        http_get=_ollama_ok,
        secrets=frozenset(),
    )

    assert probe["status"] == "unavailable"
    assert probe["reason_code"] == "PROVIDER_PROBE_UNAVAILABLE"
