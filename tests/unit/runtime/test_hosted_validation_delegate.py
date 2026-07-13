"""Shared helpers for hosted validation delegate tests."""

from __future__ import annotations

from awf.profiles.models import WorkspaceProfile
from awf.runtime.hosted_delegation import HostedDelegationConfig


def _config(**overrides: object) -> HostedDelegationConfig:
    values: dict[str, object] = {
        "base_url": "https://hosted.example.test",
        "bearer_token": "secret-token",
        "poll_interval_seconds": 0.001,
        "operation_timeout_seconds": 1.0,
        "request_timeout_seconds": 1.0,
        "cancel_timeout_seconds": 1.0,
        "max_output_bytes": 100_000,
    }
    values.update(overrides)
    return HostedDelegationConfig(**values)  # type: ignore[arg-type]


def _profile_with_runtime_secret() -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "hosted-secret-test",
            "runtime": {
                "environment": {
                    "NPM_TOKEN": "npm-profile-secret",
                    "OLLAMA_HOST": "http://ollama.profile:11434",
                    "PIP_INDEX_URL": "https://pkg-token@packages.example/simple",
                }
            },
        }
    )


def _profile_with_service_secret() -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "hosted-service-secret-test",
            "services": [
                {
                    "name": "postgres",
                    "image": "postgres:16",
                    "environment": {
                        "POSTGRES_PASSWORD": "literal-service-secret",
                        "POSTGRES_USER": "awf",
                        "EXTERNAL_API_KEY": "${SERVICE_API_KEY}",
                    },
                }
            ],
        }
    )


def _profile_with_secret_ref() -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "hosted-secret-ref-test",
            "secrets": [
                {
                    "name": "codex-default",
                    "target": "/run/awf/secrets/codex-default",
                    "kind": "mount",
                    "mode": "ro",
                    "required": True,
                    "provider": "local-file",
                    "ref": "local-file:///home/user/.awf/secrets/codex.default",
                }
            ],
        }
    )


def _profile_with_service_without_environment() -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "hosted-service-no-env-test",
            "services": [
                {
                    "name": "redis",
                    "image": "redis:7",
                }
            ],
        }
    )
