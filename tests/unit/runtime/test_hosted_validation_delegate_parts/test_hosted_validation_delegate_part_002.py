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
