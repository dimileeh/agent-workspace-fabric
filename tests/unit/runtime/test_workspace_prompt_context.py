"""Workspace runtime context prompt rendering tests."""

from __future__ import annotations

import pytest

from awf.profiles.models import (
    DockerMode,
    EndpointVisibility,
    ProfileAppEndpoint,
    ProfileAppEndpointHealth,
    ProfileDocker,
    ProfileRuntime,
    ProfileService,
    WorkspaceProfile,
)
from awf.runtime.workspace_prompt_context import (
    render_workspace_runtime_context,
    render_workspace_runtime_context_section,
)


@pytest.mark.unit
def test_workspace_runtime_context_describes_sidecar_database_env_without_secrets() -> None:
    profile = WorkspaceProfile(
        name="awf-self",
        docker=ProfileDocker(mode=DockerMode.none),
        runtime=ProfileRuntime(
            environment={
                "AWF_DATABASE_URL": (
                    "postgresql+asyncpg://awf:super-secret-password@postgres:5432/awf"
                ),
                "AWF_TEST_DATABASE_URL": (
                    "postgresql+asyncpg://awf:super-secret-password@postgres:5432/awf"
                ),
                "PYTHONUNBUFFERED": "1",
            }
        ),
        services=[
            ProfileService(
                name="postgres",
                image="postgres:16-alpine",
                environment={
                    "POSTGRES_DB": "awf",
                    "POSTGRES_PASSWORD": "super-secret-password",
                    "POSTGRES_USER": "awf",
                },
                healthcheck_cmd="pg_isready -U awf -d awf",
            )
        ],
    )

    context = render_workspace_runtime_context(profile)

    assert "Workspace runtime context" in context
    assert "postgres" in context
    assert "postgres:5432" in context
    assert "$AWF_TEST_DATABASE_URL" in context
    assert "postgresql+asyncpg://[redacted]@postgres:5432/awf" in context
    assert "localhost is the agent container" in context
    assert "AWF already started" in context
    assert "Docker may be unavailable" in context
    assert "super-secret-password" not in context


@pytest.mark.unit
def test_workspace_runtime_context_keeps_generic_non_database_sidecars() -> None:
    profile = WorkspaceProfile(
        name="redis-app",
        runtime=ProfileRuntime(
            environment={
                "APP_BASE_URL": "http://app:8080",
                "CACHE_URL": "redis://redis:6379/0",
            }
        ),
        services=[
            ProfileService(name="app", image="example/app:latest", ports=[(8080, 18080)]),
            ProfileService(name="redis", image="redis:7-alpine"),
        ],
    )

    context = render_workspace_runtime_context(profile)

    assert "app" in context
    assert "app:8080" in context
    assert "redis" in context
    assert "redis:6379" in context
    assert "$APP_BASE_URL" in context
    assert "$CACHE_URL" in context


@pytest.mark.unit
def test_workspace_runtime_context_omits_connection_env_for_external_hosts() -> None:
    profile = WorkspaceProfile(
        name="external-db",
        runtime=ProfileRuntime(
            environment={
                "DATABASE_URL": "postgresql://awf:secret@postgres:5432/awf",
                "EXTERNAL_POSTGRES_URL": (
                    "postgresql://awf:external-secret@external.example.com:5432/awf"
                ),
                "REDIS_URL": "redis://cache.example.com:6379/0",
            }
        ),
        services=[
            ProfileService(name="postgres", image="postgres:16-alpine"),
        ],
    )

    context = render_workspace_runtime_context(profile)

    assert "$DATABASE_URL" in context
    assert "postgresql://[redacted]@postgres:5432/awf" in context
    assert "$EXTERNAL_POSTGRES_URL" not in context
    assert "external.example.com" not in context
    assert "$REDIS_URL" not in context
    assert "cache.example.com" not in context
    assert "external-secret" not in context


@pytest.mark.unit
def test_workspace_runtime_context_includes_generated_app_endpoint_env() -> None:
    profile = WorkspaceProfile(
        name="node-browser",
        services=[
            ProfileService(name="app", image="node:20-alpine"),
            ProfileService(name="browser", image="mcr.microsoft.com/playwright:v1"),
        ],
        app_endpoints=[
            ProfileAppEndpoint(name="app", service="app", port=3000),
            ProfileAppEndpoint(
                name="browser_validation",
                service="browser",
                port=9323,
                path="/validate",
                health=ProfileAppEndpointHealth(path="/healthz"),
                visibility=EndpointVisibility.validation,
            ),
            ProfileAppEndpoint(
                name="operator_notes",
                service="app",
                port=3000,
                path="/operator",
                visibility=EndpointVisibility.console,
            ),
        ],
    )

    context = render_workspace_runtime_context(profile)

    assert "Service `app` is already started by AWF; internal endpoints: `app:3000`." in context
    assert (
        "Service `browser` is already started by AWF; internal endpoints: `browser:9323`."
        in context
    )
    assert "$AWF_APP_ENDPOINTS_JSON" in context
    assert "$AWF_APP_ENDPOINT_APP_URL" in context
    assert "http://app:3000/" in context
    assert "$AWF_APP_ENDPOINT_BROWSER_VALIDATION_URL" in context
    assert "http://browser:9323/validate" in context
    assert "operator_notes" not in context


@pytest.mark.unit
def test_workspace_runtime_context_redacts_sensitive_generated_connection_env() -> None:
    profile = WorkspaceProfile(
        name="tokenized-app",
        runtime=ProfileRuntime(
            environment={
                "SERVICE_TOKEN_URL": "https://token-service:8443/api?token=secret-token-value",
                "QUEUE_URL": "redis://queue",
                "BROKEN_DATABASE_URL": "postgresql://db.example.com:bad/awf",
                "INVALID_DATABASE_URL": "postgresql://[bad",
            }
        ),
        services=[
            ProfileService(name="token-service", image="example/token-service:latest"),
            ProfileService(name="queue", image="redis:7-alpine"),
        ],
    )

    context = render_workspace_runtime_context(profile)

    assert "$SERVICE_TOKEN_URL" in context
    assert "$SERVICE_TOKEN_URL` is set." in context
    assert "secret-token-value" not in context
    assert "token-service" in context
    assert "queue" in context
    assert "$QUEUE_URL" in context
    assert "`redis://queue`" in context
    assert "$BROKEN_DATABASE_URL" not in context
    assert "$INVALID_DATABASE_URL" not in context


@pytest.mark.unit
def test_workspace_runtime_context_describes_default_docker_mode_without_ports() -> None:
    profile = WorkspaceProfile(
        name="sidecar-only",
        docker=ProfileDocker(mode=DockerMode.dind),
        services=[ProfileService(name="queue", image="redis:7-alpine")],
    )

    context = render_workspace_runtime_context(profile)

    assert "Service `queue` is already started by AWF; Compose DNS: `queue`." in context
    assert "do not start duplicate copies" in context
    assert "Docker may be unavailable" not in context


@pytest.mark.unit
def test_workspace_runtime_context_is_empty_without_services_or_runtime_env() -> None:
    assert render_workspace_runtime_context(WorkspaceProfile(name="plain")) == ""


@pytest.mark.unit
def test_workspace_runtime_context_section_trims_and_preserves_prompt_gap() -> None:
    assert (
        render_workspace_runtime_context_section(
            "\n Workspace runtime context\n- Use `$DATABASE_URL`. \n"
        )
        == "Workspace runtime context\n- Use `$DATABASE_URL`.\n\n"
    )
    assert render_workspace_runtime_context_section(" \n\t ") == ""
