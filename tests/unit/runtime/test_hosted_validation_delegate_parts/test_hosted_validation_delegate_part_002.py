"""Hosted validation delegate profile sanitization tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from awf.profiles.models import WorkspaceProfile
from awf.runtime.hosted_delegation import HostedValidationDelegate
from awf.runtime.hosted_delegation_payloads import (
    _hosted_validation_profile_payload,
    _hosted_validation_rendered_stack_payload,
)
from tests.unit.runtime.test_hosted_validation_delegate import (
    _config,
    _profile_with_runtime_secret,
    _profile_with_secret_ref,
    _profile_with_service_secret,
    _profile_with_service_without_environment,
)


@pytest.mark.unit
async def test_hosted_validation_strips_credentials_from_pr_identity_urls(
    tmp_path: Path,
) -> None:
    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                202,
                json={
                    "operation_id": "val_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/val_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/val_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "val_1",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "commands": [
                        {
                            "command": "uv run pytest tests/unit/foo -q",
                            "returncode": 0,
                            "duration_seconds": 1.25,
                            "stdout": "passed\n",
                            "stderr": "",
                            "phase": "validate",
                            "reason_code": "COMMAND_FAILED",
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path,
            client=client,
        )
        await delegate.run_profile_phases(
            workspace_id="ws_hosted",
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=WorkspaceProfile(name="hosted-test"),
            phase_names=("validate",),
            pr_identity={
                "repo_url": "https://base-secret-123@github.com/base/repo.git",
                "head_repo_url": "ssh://git:fork-secret-456@github.com/fork/repo.git",
                "pr_number": 277,
            },
        )

    pr_identity = seen["body"]["pr_identity"]
    assert pr_identity["repo_url"] == "https://github.com/base/repo.git"
    assert pr_identity["head_repo_url"] == "ssh://git@github.com/fork/repo.git"
    body_blob = json.dumps(seen["body"], sort_keys=True)
    assert "base-secret-123" not in body_blob
    assert "fork-secret-456" not in body_blob


@pytest.mark.unit
async def test_hosted_validation_preserves_passwordless_ssh_pr_identity_userinfo(
    tmp_path: Path,
) -> None:
    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                202,
                json={
                    "operation_id": "val_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/val_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/val_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "val_1",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "commands": [
                        {
                            "command": "uv run pytest tests/unit/foo -q",
                            "returncode": 0,
                            "duration_seconds": 1.25,
                            "stdout": "passed\n",
                            "stderr": "",
                            "phase": "validate",
                            "reason_code": "COMMAND_FAILED",
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path,
            client=client,
        )
        await delegate.run_profile_phases(
            workspace_id="ws_hosted",
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=WorkspaceProfile(name="hosted-test"),
            phase_names=("validate",),
            pr_identity={
                "repo_url": "ssh://git@github.com/base/repo.git",
                "head_repo_url": "ssh://git@github.com/fork/repo.git",
                "pr_number": 277,
            },
        )

    pr_identity = seen["body"]["pr_identity"]
    assert pr_identity["repo_url"] == "ssh://git@github.com/base/repo.git"
    assert pr_identity["head_repo_url"] == "ssh://git@github.com/fork/repo.git"


@pytest.mark.unit
async def test_hosted_validation_profile_without_service_environment_passes_through(
    tmp_path: Path,
) -> None:
    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                202,
                json={
                    "operation_id": "val_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/val_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/val_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "val_1",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "commands": [],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path,
            client=client,
        )
        await delegate.run_profile_phases(
            workspace_id="ws_hosted",
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=_profile_with_service_without_environment(),
            phase_names=("validate",),
        )

    service = seen["body"]["profile"]["services"][0]
    assert service["name"] == "redis"
    assert service["image"] == "redis:7"
    assert service["environment"] == {}


@pytest.mark.unit
async def test_hosted_validation_sanitizes_literal_runtime_environment_secrets(
    tmp_path: Path,
) -> None:
    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                202,
                json={
                    "operation_id": "val_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/val_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/val_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "val_1",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "commands": [],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path,
            client=client,
        )
        await delegate.run_profile_phases(
            workspace_id="ws_hosted",
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=_profile_with_runtime_secret(),
            phase_names=("validate",),
        )

    body_blob = json.dumps(seen["body"], sort_keys=True)
    assert "npm-profile-secret" not in body_blob
    assert "pkg-token" not in body_blob
    assert seen["body"]["profile"]["runtime"]["environment"] == {
        "NPM_TOKEN": "${NPM_TOKEN}",
        "OLLAMA_HOST": "http://ollama.profile:11434",
        "PIP_INDEX_URL": "${PIP_INDEX_URL}",
    }


@pytest.mark.unit
async def test_hosted_validation_sanitizes_literal_service_environment_secrets(
    tmp_path: Path,
) -> None:
    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                202,
                json={
                    "operation_id": "val_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/val_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/val_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "val_1",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "commands": [],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path,
            client=client,
        )
        await delegate.run_profile_phases(
            workspace_id="ws_hosted",
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=_profile_with_service_secret(),
            phase_names=("validate",),
        )

    body_blob = json.dumps(seen["body"], sort_keys=True)
    assert "literal-service-secret" not in body_blob
    assert seen["body"]["profile"]["services"][0]["environment"] == {
        "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD}",
        "POSTGRES_USER": "awf",
        "EXTERNAL_API_KEY": "${SERVICE_API_KEY}",
    }


@pytest.mark.unit
async def test_hosted_validation_strips_profile_secret_refs(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                202,
                json={
                    "operation_id": "val_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/val_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/val_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "val_1",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "commands": [],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path,
            client=client,
        )
        await delegate.run_profile_phases(
            workspace_id="ws_hosted",
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=_profile_with_secret_ref(),
            phase_names=("validate",),
        )

    body_blob = json.dumps(seen["body"], sort_keys=True)
    assert seen["body"]["profile"]["secrets"] == []
    assert "codex-default" not in body_blob
    assert "/run/awf/secrets/codex-default" not in body_blob
    assert "local-file" not in body_blob
    assert "local-file:///home/user/.awf/secrets/codex.default" not in body_blob
    assert "/home/user/.awf/secrets/codex.default" not in body_blob


def _profile_with_postgres_password_secret() -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "hosted-pg-secret-declaration-test",
            "secrets": [
                {
                    "name": "postgres-password",
                    "target": "AWF_POSTGRES_PASSWORD",
                    "kind": "env",
                    "mode": "ro",
                    "required": True,
                    "provider": "env",
                    "ref": "env/AWF_POSTGRES_PASSWORD_TOKEN_ghp_exampleTokenMaterial12",
                }
            ],
            "runtime": {
                "environment": {
                    "POSTGRES_USER": "awf",
                    "EXTERNAL_API_KEY": "${SERVICE_API_KEY}",
                    "POSTGRES_PASSWORD": "literal-postgres-password-secret",
                }
            },
            "services": [
                {
                    "name": "postgres",
                    "image": "postgres:16",
                    "environment": {
                        "POSTGRES_USER": "awf",
                        "EXTERNAL_API_KEY": "${SERVICE_API_KEY}",
                        "POSTGRES_PASSWORD": "literal-service-password-secret",
                    },
                }
            ],
        }
    )


@pytest.mark.unit
async def test_hosted_validation_omits_postgres_password_secret_declarations(
    tmp_path: Path,
) -> None:
    seen: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/validation-runs":
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                202,
                json={
                    "operation_id": "val_1",
                    "workspace_id": "ws_hosted",
                    "operation_url": "/v1/operations/val_1",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/operations/val_1":
            return httpx.Response(
                200,
                json={
                    "operation_id": "val_1",
                    "workspace_id": "ws_hosted",
                    "state": "succeeded",
                    "commands": [],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        delegate = HostedValidationDelegate(
            _config(),
            artifacts_dir=tmp_path,
            client=client,
        )
        await delegate.run_profile_phases(
            workspace_id="ws_hosted",
            compose_project="unused",
            compose_file=tmp_path / "missing-compose.yml",
            profile=_profile_with_postgres_password_secret(),
            phase_names=("validate",),
        )

    profile = seen["body"]["profile"]
    assert profile["secrets"] == []
    body_blob = json.dumps(seen["body"], sort_keys=True)
    assert "postgres-password" not in body_blob
    assert "AWF_POSTGRES_PASSWORD" not in body_blob
    assert "AWF_POSTGRES_PASSWORD_TOKEN_ghp_exampleTokenMaterial12" not in body_blob
    assert "ghp_exampleTokenMaterial12" not in body_blob
    assert "literal-postgres-password-secret" not in body_blob
    assert "literal-service-password-secret" not in body_blob
    assert profile["runtime"]["environment"] == {
        "POSTGRES_USER": "awf",
        "EXTERNAL_API_KEY": "${SERVICE_API_KEY}",
        "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD}",
    }
    assert profile["services"][0]["environment"] == {
        "POSTGRES_USER": "awf",
        "EXTERNAL_API_KEY": "${SERVICE_API_KEY}",
        "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD}",
    }


@pytest.mark.unit
def test_hosted_validation_profile_payload_clears_secret_declarations() -> None:
    payload = _hosted_validation_profile_payload(
        WorkspaceProfile.model_validate(
            {
                "name": "hosted-helper-secrets-cleared",
                "secrets": [
                    {
                        "name": "postgres-password",
                        "target": "AWF_POSTGRES_PASSWORD",
                        "kind": "env",
                        "provider": "env",
                        "ref": "env/AWF_POSTGRES_PASSWORD",
                    },
                    {
                        "name": "codex-default",
                        "target": "/run/awf/secrets/codex-default",
                        "kind": "mount",
                        "provider": "local-file",
                        "ref": "local-file:///home/user/.awf/secrets/codex.default",
                    },
                ],
                "runtime": {
                    "environment": {
                        "POSTGRES_PASSWORD": "literal-helper-password",
                        "POSTGRES_USER": "awf",
                    }
                },
            }
        )
    )

    assert payload["secrets"] == []
    assert payload["runtime"]["environment"] == {
        "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD}",
        "POSTGRES_USER": "awf",
    }


@pytest.mark.unit
def test_rendered_stack_required_image_interpolation_does_not_abort(
    tmp_path: Path,
) -> None:
    """Required ``:?`` image forms must not abort omit-credential sanitization."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  app:
    image: "${APP_IMAGE:?must set APP_IMAGE}"
    environment:
      PUBLIC_URL: http://app:8000
      API_TOKEN: literal-api-token
  postgres:
    image: "${POSTGRES_IMAGE:?must set POSTGRES_IMAGE}"
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
    assert payload["services"]["app"]["image"] == "${APP_IMAGE:?must set APP_IMAGE}"
    assert payload["services"]["app"]["environment"] == {
        "PUBLIC_URL": "http://app:8000",
    }
    # Unexpanded required image is not treated as postgres-like, so trust is
    # not injected; credential keys are still omitted.
    assert payload["services"]["postgres"]["environment"] == {
        "POSTGRES_USER": "awf",
    }
    body = json.dumps(payload, sort_keys=True)
    assert "API_TOKEN" not in body
    assert "POSTGRES_PASSWORD" not in body
    assert "literal-api-token" not in body
    assert "literal-postgres-secret" not in body
    assert "POSTGRES_HOST_AUTH_METHOD" not in body


@pytest.mark.unit
def test_rendered_stack_preserves_credential_placeholders_by_default(
    tmp_path: Path,
) -> None:
    """Agent-run path keeps safe ${NAME} placeholders for sidecar credentials."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  backend:
    image: backend:latest
    environment:
      PUBLIC_URL: http://backend:8000
      API_TOKEN: literal-service-secret
  postgres:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: literal-postgres-secret
      POSTGRES_USER: awf
  agent:
    image: awf-agent-runtime:latest
    environment:
      NPM_TOKEN: literal-agent-secret
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
    )

    assert payload is not None
    assert payload["services"]["backend"]["environment"] == {
        "PUBLIC_URL": "http://backend:8000",
        "API_TOKEN": "${API_TOKEN}",
    }
    assert payload["services"]["postgres"]["environment"] == {
        "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD}",
        "POSTGRES_USER": "awf",
    }
    assert "POSTGRES_HOST_AUTH_METHOD" not in payload["services"]["postgres"]["environment"]
    body = json.dumps(payload, sort_keys=True)
    assert "literal-service-secret" not in body
    assert "literal-postgres-secret" not in body
    assert "literal-agent-secret" not in body


@pytest.mark.unit
@pytest.mark.parametrize(
    "image",
    [
        "postgres:16",
        "postgres-bis:tag",
        "ghcr.io/org/postgres-custom:16",
        "library/postgres:16",
        "${POSTGRES_IMAGE:-postgres:16}",
        "${POSTGRES_IMAGE:-${FALLBACK_IMAGE:-ghcr.io/org/postgres-custom:16}}",
    ],
)
def test_rendered_stack_omits_postgres_password_and_sets_trust(
    tmp_path: Path,
    image: str,
) -> None:
    """Hosted rendered Postgres env omits password keys and uses trust auth."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        f"""
services:
  postgres:
    image: "{image}"
    environment:
      POSTGRES_PASSWORD: literal-postgres-secret
      POSTGRES_USER: awf
      POSTGRES_DB: awf
      PUBLIC_URL: http://postgres:5432
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload is not None
    environment = payload["services"]["postgres"]["environment"]
    assert environment == {
        "POSTGRES_USER": "awf",
        "POSTGRES_DB": "awf",
        "PUBLIC_URL": "http://postgres:5432",
        "POSTGRES_HOST_AUTH_METHOD": "trust",
    }
    body = json.dumps(payload, sort_keys=True)
    assert "POSTGRES_PASSWORD" not in body
    assert "${POSTGRES_PASSWORD}" not in body
    assert "literal-postgres-secret" not in body


@pytest.mark.unit
def test_rendered_stack_list_env_omits_credential_keys_postgres_trust(
    tmp_path: Path,
) -> None:
    """List-shaped Postgres env omits credential keys and injects trust."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: postgres:16
    environment:
      - POSTGRES_PASSWORD=literal-postgres-secret
      - API_TOKEN=literal-api-token
      - POSTGRES_USER=awf
      - POSTGRES_DB=awf
      - PUBLIC_URL=http://postgres:5432
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload is not None
    environment = payload["services"]["postgres"]["environment"]
    assert isinstance(environment, list)
    assert environment == [
        "POSTGRES_USER=awf",
        "POSTGRES_DB=awf",
        "PUBLIC_URL=http://postgres:5432",
        "POSTGRES_HOST_AUTH_METHOD=trust",
    ]
    body = json.dumps(payload, sort_keys=True)
    assert "POSTGRES_PASSWORD" not in body
    assert "API_TOKEN" not in body
    assert "literal-postgres-secret" not in body
    assert "literal-api-token" not in body


@pytest.mark.unit
def test_rendered_stack_non_postgres_omits_password_without_trust(
    tmp_path: Path,
) -> None:
    """Non-Postgres services omit credential keys without trust injection."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  redis:
    image: redis:7
    environment:
      POSTGRES_PASSWORD: should-not-leak
      WORKER_PASSWORD: worker-secret
      API_TOKEN: api-secret
      REDIS_URL: redis://redis:6379
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload is not None
    environment = payload["services"]["redis"]["environment"]
    assert environment == {"REDIS_URL": "redis://redis:6379"}
    assert "POSTGRES_HOST_AUTH_METHOD" not in environment
    body = json.dumps(payload, sort_keys=True)
    assert "POSTGRES_PASSWORD" not in body
    assert "WORKER_PASSWORD" not in body
    assert "API_TOKEN" not in body
    assert "should-not-leak" not in body
    assert "worker-secret" not in body
    assert "api-secret" not in body


@pytest.mark.unit
def test_rendered_stack_postgres_without_password_does_not_add_trust(
    tmp_path: Path,
) -> None:
    """Postgres without a declared password must not get trust mode."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: awf
      POSTGRES_DB: awf
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
    )

    assert payload is not None
    environment = payload["services"]["postgres"]["environment"]
    assert environment == {
        "POSTGRES_USER": "awf",
        "POSTGRES_DB": "awf",
    }
    assert "POSTGRES_HOST_AUTH_METHOD" not in environment


@pytest.mark.unit
@pytest.mark.parametrize(
    "image_yaml",
    [
        'image: ""',
        'image: "   "',
        'image: "${MISSING_IMAGE}"',
        'image: "${EMPTY_DEFAULT:-}"',
        "image: null",
        "",  # missing image field
    ],
)
def test_rendered_stack_blank_or_unresolved_image_omits_password_without_trust(
    tmp_path: Path,
    image_yaml: str,
) -> None:
    """Blank, null, missing, or unresolved images are not postgres-like."""
    compose_file = tmp_path / "compose.yml"
    image_line = f"    {image_yaml}\n" if image_yaml else ""
    compose_file.write_text(
        f"""
services:
  db:
{image_line}    environment:
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
    environment = payload["services"]["db"]["environment"]
    assert environment == {"POSTGRES_USER": "awf"}
    assert "POSTGRES_HOST_AUTH_METHOD" not in environment
    body = json.dumps(payload, sort_keys=True)
    assert "POSTGRES_PASSWORD" not in body
    assert "literal-postgres-secret" not in body


@pytest.mark.unit
@pytest.mark.parametrize(
    "image",
    [
        "postgres",
        "localhost:5000/postgres",
        "myregistry.com:5000/postgres-custom",
    ],
)
def test_rendered_stack_untagged_and_port_registry_postgres_gets_trust(
    tmp_path: Path,
    image: str,
) -> None:
    """Detect postgres via untagged and host:port/repo image forms."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        f"""
services:
  postgres:
    image: "{image}"
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
    environment = payload["services"]["postgres"]["environment"]
    assert environment == {
        "POSTGRES_USER": "awf",
        "POSTGRES_HOST_AUTH_METHOD": "trust",
    }
    body = json.dumps(payload, sort_keys=True)
    assert "POSTGRES_PASSWORD" not in body
    assert "literal-postgres-secret" not in body


@pytest.mark.unit
def test_rendered_stack_list_env_replaces_auth_method_and_omits_bare_credentials(
    tmp_path: Path,
) -> None:
    """List env drops bare credential keys and replaces auth-method entries with trust."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: postgres:16
    environment:
      - POSTGRES_PASSWORD
      - API_TOKEN
      - POSTGRES_HOST_AUTH_METHOD=md5
      - POSTGRES_HOST_AUTH_METHOD
      - POSTGRES_USER=awf
      - PUBLIC_URL=http://postgres:5432
""".lstrip(),
        encoding="utf-8",
    )

    payload = _hosted_validation_rendered_stack_payload(
        compose_project="awf_ws_hosted",
        compose_file=compose_file,
        omit_credential_env_keys=True,
    )

    assert payload is not None
    environment = payload["services"]["postgres"]["environment"]
    assert isinstance(environment, list)
    assert environment == [
        "POSTGRES_USER=awf",
        "PUBLIC_URL=http://postgres:5432",
        "POSTGRES_HOST_AUTH_METHOD=trust",
    ]
    body = json.dumps(payload, sort_keys=True)
    assert "POSTGRES_PASSWORD" not in body
    assert "API_TOKEN" not in body
    assert "md5" not in body


@pytest.mark.unit
def test_rendered_stack_env_file_postgres_password_injects_trust(
    tmp_path: Path,
) -> None:
    """Postgres password declared only via env_file still gets trust in omit mode."""
    env_file = tmp_path / "postgres.env"
    env_file.write_text(
        "POSTGRES_PASSWORD=literal-env-file-secret\nPOSTGRES_USER=awf\n",
        encoding="utf-8",
    )
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: postgres:16
    env_file:
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
    environment = payload["services"]["postgres"]["environment"]
    assert environment == {"POSTGRES_HOST_AUTH_METHOD": "trust"}
    body = json.dumps(payload, sort_keys=True)
    assert "POSTGRES_PASSWORD" not in body
    assert "literal-env-file-secret" not in body


@pytest.mark.unit
def test_rendered_stack_env_file_null_environment_still_injects_trust(
    tmp_path: Path,
) -> None:
    """Explicit environment: null must not block env_file-driven trust injection."""
    env_file = tmp_path / "postgres.env"
    env_file.write_text(
        "POSTGRES_PASSWORD=null-env-file-secret\nPOSTGRES_USER=awf\n",
        encoding="utf-8",
    )
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: postgres:16
    env_file:
      - postgres.env
    environment: null
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
    assert "null-env-file-secret" not in body


@pytest.mark.unit
def test_rendered_stack_env_file_list_mapping_postgres_password_injects_trust(
    tmp_path: Path,
) -> None:
    """Mapping-shaped env_file entries declaring POSTGRES_PASSWORD get trust."""
    env_file = tmp_path / "db.env"
    env_file.write_text("POSTGRES_PASSWORD=mapping-env-file-secret\n", encoding="utf-8")
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: postgres:16
    env_file:
      - path: db.env
        required: true
    environment:
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
    assert "mapping-env-file-secret" not in body


@pytest.mark.unit
def test_rendered_stack_unreadable_env_file_does_not_inject_trust(
    tmp_path: Path,
) -> None:
    """Unreadable env_file must not abort sanitization or invent trust mode."""
    env_file = tmp_path / "postgres.env"
    env_file.write_text("POSTGRES_PASSWORD=secret\n", encoding="utf-8")
    env_file.chmod(0o000)
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """
services:
  postgres:
    image: postgres:16
    env_file: postgres.env
""".lstrip(),
        encoding="utf-8",
    )

    try:
        payload = _hosted_validation_rendered_stack_payload(
            compose_project="awf_ws_hosted",
            compose_file=compose_file,
            omit_credential_env_keys=True,
        )
    finally:
        env_file.chmod(0o600)

    assert payload is not None
    assert "environment" not in payload["services"]["postgres"]
