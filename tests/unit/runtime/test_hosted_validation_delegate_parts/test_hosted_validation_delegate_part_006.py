"""Additional hosted validation delegate edge tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from awf.adapters.runtime_executor import AgentRuntimeExecRequest
from awf.db.enums import AgentRuntime
from awf.profiles.models import WorkspaceProfile
from awf.runtime.hosted_delegation import HostedValidationDelegate
from awf.runtime.hosted_delegation_payloads import (
    _HOSTED_KUBERNETES_LABEL_MAX_LENGTH,
    _HOSTED_VOLUME_HASH_LENGTHS,
    _agent_start_payload,
    _hosted_validation_disambiguated_compose_volume_name,
    _hosted_validation_normalized_compose_volume_name,
    _hosted_validation_profile_payload,
    _hosted_validation_rendered_stack_payload,
    _hosted_validation_sanitize_rendered_stack_volumes,
    _hosted_validation_secret_checked_fields,
    _hosted_validation_url_has_query_or_fragment_credentials,
)
from tests.unit.runtime.test_hosted_validation_delegate import _config


@pytest.mark.unit
async def test_hosted_profile_phases_preserve_unrequested_coverage_payload_status(
    tmp_path: Path,
) -> None:
    """Unexpected coverage metadata is recorded without applying profile policy."""

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            return httpx.Response(
                202,
                json={
                    "operation_id": "validate_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/validate_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/validate_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "validate_1",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "commands": [],
                    "coverage": {
                        "provider": "python",
                        "percent": 82.0,
                        "minimum_percent": 99.0,
                        "enforce": False,
                        "status": "error",
                        "reason_code": "COVERAGE_PROVIDER_FAILED",
                        "gaps": [{"file": "src/awf/runtime/hosted_delegation.py"}],
                    },
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path,
            client=client,
        )
        result = await delegate.run_profile_phases(
            workspace_id="ws_hosted",
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=WorkspaceProfile(name="hosted-test"),
            phase_names=("validate",),
            include_coverage=False,
        )

    assert result.commands == []
    assert result.coverage is not None
    assert result.coverage.status == "error"
    assert result.coverage.enforce is False
    assert result.coverage.reason_code == "COVERAGE_PROVIDER_FAILED"
    assert result.coverage.gaps == [{"file": "src/awf/runtime/hosted_delegation.py"}]


@pytest.mark.unit
def test_rendered_stack_prefixed_default_image_still_injects_postgres_trust(
    tmp_path: Path,
) -> None:
    """Expanded image candidates beyond operator arms must still detect Postgres."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: "docker.io/${IMAGE:-library/postgres:16}"
    environment:
      POSTGRES_PASSWORD: literal-postgres-secret
      POSTGRES_USER: awf
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload is not None
    assert payload["services"]["postgres"]["environment"] == {
        "POSTGRES_USER": "awf",
        "POSTGRES_HOST_AUTH_METHOD": "trust",
    }
    body = json.dumps(payload, sort_keys=True)
    assert "POSTGRES_PASSWORD" not in body
    assert "literal-postgres-secret" not in body


@pytest.mark.unit
def test_rendered_stack_env_file_skips_comments_and_blank_lines(
    tmp_path: Path,
) -> None:
    """Comment, blank, and non-password assignment lines must not hide POSTGRES_PASSWORD."""
    ignored = tmp_path / "ignore.env"
    ignored.write_text(
        "# ignored companion file\n\nPOSTGRES_USER=awf\nNOT_ASSIGNMENT\n",
        encoding="utf-8",
    )
    env_file = tmp_path / "postgres.env"
    env_file.write_text(
        "# local secrets\n\nPOSTGRES_USER=awf\nPOSTGRES_PASSWORD=literal-env-file-secret\n",
        encoding="utf-8",
    )
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: postgres:16
    env_file:
      - ignore.env
      - postgres.env
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload is not None
    assert payload["services"]["postgres"]["environment"] == {
        "POSTGRES_HOST_AUTH_METHOD": "trust",
    }
    body = json.dumps(payload, sort_keys=True)
    assert "POSTGRES_PASSWORD" not in body
    assert "literal-env-file-secret" not in body


@pytest.mark.unit
def test_rendered_stack_omit_mode_operator_arms_with_trailing_or_non_operator_text(
    tmp_path: Path,
) -> None:
    """Operator arms that are not pure ``${NAME}`` refs fall through to secret scans."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  app:
    image: app:latest
    environment:
      KEEP_TRAILING_ARM: ${OUTER:-${PUBLIC}suffix}
      KEEP_EQUALS_ARM: ${OUTER:-${PUBLIC=plain}}
      KEEP_INVALID_NAME_ARM: ${OUTER:-${!!!}}
      OMIT_PASSWORD_ARM: ${OUTER:-password=secret}
      SAFE_URL: http://app:8000
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload is not None
    environment = payload["services"]["app"]["environment"]
    assert environment == {
        "KEEP_TRAILING_ARM": "${OUTER:-${PUBLIC}suffix}",
        "KEEP_EQUALS_ARM": "${OUTER:-${PUBLIC=plain}}",
        "KEEP_INVALID_NAME_ARM": "${OUTER:-${!!!}}",
        "SAFE_URL": "http://app:8000",
    }
    body = json.dumps(payload, sort_keys=True)
    assert "password=secret" not in body
    assert "OMIT_PASSWORD_ARM" not in body


@pytest.mark.unit
def test_rendered_stack_omit_mode_handles_bare_dollar_and_malformed_interpolations(
    tmp_path: Path,
) -> None:
    """Bare $CREDENTIAL refs are omitted; malformed braces must not abort sanitization."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  app:
    image: app:latest
    environment:
      PUBLIC_HEADER: Bearer $API_TOKEN
      CACHE_KEY: prefix-$POSTGRES_PASSWORD-suffix
      KEEP_BARE: Bearer $PUBLIC_HOST
      KEEP_EMPTY_DEFAULT: ${PUBLIC_HOST:-}
      KEEP_UNCLOSED: prefix-${UNCLOSED
      KEEP_NESTED_UNCLOSED: ${OUTER:-${INNER
      KEEP_TRAILING: ${PUBLIC_HOST}suffix
      KEEP_INVALID_BRACE: ${!!!}
      KEEP_INVALID_BARE: $123
      KEEP_BARE_SUFFIX: $PUBLIC_HOST-extra
      KEEP_DOLLAR_DIGIT: costs $5 today
      SAFE_URL: http://app:8000
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload is not None
    environment = payload["services"]["app"]["environment"]
    assert environment == {
        "KEEP_BARE": "Bearer $PUBLIC_HOST",
        "KEEP_EMPTY_DEFAULT": "${PUBLIC_HOST:-}",
        "KEEP_UNCLOSED": "prefix-${UNCLOSED",
        "KEEP_NESTED_UNCLOSED": "${OUTER:-${INNER",
        "KEEP_TRAILING": "${PUBLIC_HOST}suffix",
        "KEEP_INVALID_BRACE": "${!!!}",
        "KEEP_INVALID_BARE": "$123",
        "KEEP_BARE_SUFFIX": "$PUBLIC_HOST-extra",
        "KEEP_DOLLAR_DIGIT": "costs $5 today",
        "SAFE_URL": "http://app:8000",
    }
    body = json.dumps(payload, sort_keys=True)
    assert "API_TOKEN" not in body
    assert "POSTGRES_PASSWORD" not in body
    assert "PUBLIC_HEADER" not in body
    assert "CACHE_KEY" not in body


@pytest.mark.unit
def test_hosted_profile_keeps_invalid_ipv6_postgres_urls_untouched() -> None:
    """urlsplit ValueError on invalid IPv6 authority must not abort profile sanitize."""
    invalid = "postgresql://[::1/db"
    profile = WorkspaceProfile.model_validate(
        {
            "name": "hosted-invalid-ipv6",
            "runtime": {
                "environment": {
                    "DATABASE_URL": invalid,
                    "KEEP": "ok",
                }
            },
        }
    )

    payload = _hosted_validation_profile_payload(profile)

    assert payload["runtime"]["environment"] == {
        "DATABASE_URL": invalid,
        "KEEP": "ok",
    }
    assert _hosted_validation_url_has_query_or_fragment_credentials(invalid) is False
    assert _hosted_validation_url_has_query_or_fragment_credentials("http://[bad") is False


@pytest.mark.unit
def test_rendered_stack_omits_bare_dollar_safe_named_credential_refs(
    tmp_path: Path,
) -> None:
    """Safe-named URL/DSN keys that are pure bare ``$NAME`` refs must be omitted."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  app:
    image: app:latest
    environment:
      DATABASE_URL: $PUBLIC_HOST
      APP_DSN: $PUBLIC_HOST
      KEEP: http://app:8000
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload is not None
    assert payload["services"]["app"]["environment"] == {"KEEP": "http://app:8000"}
    body = json.dumps(payload, sort_keys=True)
    assert "DATABASE_URL" not in body
    assert "APP_DSN" not in body
    assert "$PUBLIC_HOST" not in body


@pytest.mark.unit
def test_agent_start_payload_forwards_owned_paths() -> None:
    """PR identity owned_paths must reach the hosted agent-start payload."""
    request = AgentRuntimeExecRequest(
        workspace_id="ws_hosted",
        agent_runtime=AgentRuntime.codex,
        cli_args=("codex", "exec", "-"),
        prompt_stdin=b"repair prompt",
        log_source="monitor.repair",
        model="gpt-5",
        effort="high",
        owned_paths=("src/awf/runtime/**", "tests/unit/runtime/**"),
        base_ref="development",
        head_ref="awf/ws_hosted",
    )

    payload = _agent_start_payload(request)

    assert payload["pr_identity"]["owned_paths"] == [
        "src/awf/runtime/**",
        "tests/unit/runtime/**",
    ]
    assert payload["pr_identity"]["base_ref"] == "development"
    assert payload["pr_identity"]["head_ref"] == "awf/ws_hosted"


@pytest.mark.unit
def test_hosted_volume_helpers_reject_aliasing_collisions() -> None:
    """Volume helpers must refuse translations that alias distinct Compose names."""
    with pytest.raises(ValueError, match="volume declaration collision"):
        _hosted_validation_sanitize_rendered_stack_volumes(
            {"alpha": {}, "bravo": {}},
            volume_translations={"alpha": "shared", "bravo": "shared"},
        )

    original_name = "bad_volume!"
    digest = hashlib.sha256(original_name.encode("utf-8")).hexdigest()
    normalized = _hosted_validation_normalized_compose_volume_name(original_name)
    used_names: dict[str, str] = {}
    for hash_length in _HOSTED_VOLUME_HASH_LENGTHS:
        suffix = f"-{digest[:hash_length]}"
        max_prefix_length = _HOSTED_KUBERNETES_LABEL_MAX_LENGTH - len(suffix)
        prefix = normalized[:max_prefix_length].rstrip("-") or "volume"
        used_names[f"{prefix}{suffix}"] = "other"
    with pytest.raises(ValueError, match="volume name collision"):
        _hosted_validation_disambiguated_compose_volume_name(
            normalized_base=normalized,
            original_name=original_name,
            used_names=used_names,
        )


@pytest.mark.unit
def test_hosted_secret_field_scan_skips_non_container_optional_sections() -> None:
    """Non-mapping/list optional sections are skipped without yielding command fields."""
    assert (
        list(
            _hosted_validation_secret_checked_fields(
                {
                    "phases": "not-a-mapping",
                    "database": ["not-a-mapping"],
                    "validation": "not-a-mapping",
                    "services": {"name": "not-a-list"},
                }
            )
        )
        == []
    )
    assert (
        list(
            _hosted_validation_secret_checked_fields(
                {
                    "validation": {
                        "coverage": "not-a-mapping",
                        "healthchecks": {"name": "not-a-list"},
                    }
                }
            )
        )
        == []
    )


@pytest.mark.unit
def test_hosted_profile_payload_skips_non_list_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive skip when model_dump yields a non-list services container."""
    profile = WorkspaceProfile(name="hosted-non-list-services")
    original_dump = WorkspaceProfile.model_dump

    def _dump_with_dict_services(self: WorkspaceProfile, *args: object, **kwargs: object) -> dict:
        payload = original_dump(self, *args, **kwargs)
        payload["services"] = {"broken": True}
        return payload

    monkeypatch.setattr(WorkspaceProfile, "model_dump", _dump_with_dict_services)

    payload = _hosted_validation_profile_payload(profile)

    assert payload["name"] == "hosted-non-list-services"
    assert payload["services"] == {"broken": True}
