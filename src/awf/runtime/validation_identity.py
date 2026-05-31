"""Stable validation provenance identity helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from awf.profiles.compose import profile_app_endpoint_environment, resolve_app_endpoints
from awf.profiles.models import (
    ProfileCommand,
    ProfileCoverage,
    ProfileHealthCheck,
    ProfileSecret,
    ProfileService,
    WorkspaceProfile,
)

_IDENTITY_SCHEMA_VERSION = 1


def resolved_profile_digest(profile: WorkspaceProfile) -> str:
    """Digest the full resolved profile snapshot with canonical JSON ordering."""

    return _digest_json(
        {
            "schema_version": _IDENTITY_SCHEMA_VERSION,
            "profile": profile.model_dump(mode="json", by_alias=True),
        }
    )


def environment_identity_digest(profile: WorkspaceProfile) -> str:
    """Digest the validation-relevant runtime/toolchain identity inputs."""

    return _digest_json(environment_identity_inputs(profile))


def environment_identity_inputs(profile: WorkspaceProfile) -> dict[str, Any]:
    """Return sanitized inputs used for the environment identity digest.

    Raw environment values and secret references are represented by stable
    hashes so the API can explain identity inputs without leaking credentials.
    """

    app_endpoints = resolve_app_endpoints(profile)

    return {
        "schema_version": _IDENTITY_SCHEMA_VERSION,
        "runtime": {
            "agent_image": profile.runtime.agent_image,
            "toolchain_image": profile.runtime.toolchain_image,
            "environment": _environment_entries(profile.runtime.environment),
        },
        "docker": {
            "mode": str(profile.docker.mode),
            "compose_files": sorted(profile.docker.compose_files),
            "project_directory": profile.docker.project_directory,
            "dind_image": profile.docker.dind_image,
            "startup_timeout_seconds": profile.docker.startup_timeout_seconds,
        },
        "services": [_service_identity(service) for service in _sorted_services(profile.services)],
        "app_endpoints": list(app_endpoints),
        "generated_endpoint_environment": _environment_entries(
            dict(profile_app_endpoint_environment(profile, resolved_endpoints=app_endpoints))
        ),
        "database": {
            "generated_setup": [
                _command_identity(command) for command in profile.database.generated_setup
            ],
            "pre_validation_refresh": [
                _command_identity(command) for command in profile.database.pre_validation_refresh
            ],
        },
        "validation": {
            "healthchecks": [
                _healthcheck_identity(healthcheck)
                for healthcheck in sorted(
                    profile.validation.healthchecks,
                    key=lambda item: (
                        item.name,
                        item.kind or "",
                        item.command or "",
                        item.url or "",
                        item.timeout_seconds,
                    ),
                )
            ],
            "artifact_paths": sorted(profile.validation.artifact_paths),
            "timeout_seconds": profile.validation.timeout_seconds,
            "requested_tier": profile.validation.requested_tier,
            "coverage": _coverage_identity(profile.validation.coverage),
            "alembic": {
                "enabled": profile.validation.alembic.enabled,
                "config_path": profile.validation.alembic.config_path,
                "script_location": profile.validation.alembic.script_location,
                "fail_on_unconfigured": profile.validation.alembic.fail_on_unconfigured,
            },
            "retry_budget": profile.validation.retry_budget,
        },
        "security": {
            "network_posture": profile.security.egress.mode.value,
        },
        "secrets": [_secret_identity(secret) for secret in _sorted_secrets(profile.secrets)],
        "ports": [
            {"name": key, "value_sha256": _value_digest(profile.ports[key])}
            for key in sorted(profile.ports)
        ],
    }


def _digest_json(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _value_digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _environment_entries(environment: dict[str, str]) -> list[dict[str, str]]:
    return [
        {"name": key, "value_sha256": _value_digest(environment[key])}
        for key in sorted(environment)
    ]


def _sorted_services(services: list[ProfileService]) -> list[ProfileService]:
    return sorted(
        services,
        key=lambda item: (
            item.name,
            item.image or "",
            item.build_context or "",
            item.dockerfile or "",
        ),
    )


def _service_identity(service: ProfileService) -> dict[str, Any]:
    return {
        "name": service.name,
        "image": service.image,
        "build_context": service.build_context,
        "dockerfile": service.dockerfile,
        "env_file": service.env_file,
        "environment": _environment_entries(service.environment),
        "depends_on": sorted(service.depends_on),
        "healthcheck_cmd": service.healthcheck_cmd,
        "ports": [list(port) for port in sorted(service.ports)],
        "command": service.command,
        "volumes": [list(volume) for volume in sorted(service.volumes)],
        "privileged": service.privileged,
    }


def _healthcheck_identity(healthcheck: ProfileHealthCheck) -> dict[str, Any]:
    return {
        "name": healthcheck.name,
        "kind": healthcheck.kind,
        "command": healthcheck.command,
        "url": healthcheck.url,
        "method": healthcheck.method,
        "expected_status": healthcheck.expected_status,
        "timeout_seconds": healthcheck.timeout_seconds,
        "interval_seconds": healthcheck.interval_seconds,
        "attempt_timeout_seconds": healthcheck.attempt_timeout_seconds,
    }


def _coverage_identity(coverage: ProfileCoverage) -> dict[str, Any]:
    return {
        "minimum_percent": coverage.minimum_percent,
        "enforce": coverage.enforce,
        "provider": coverage.provider,
        "command": _command_identity(coverage.command) if coverage.command is not None else None,
        "parallel_workers": coverage.parallel_workers,
        "parallel_worker_max": coverage.parallel_worker_max,
    }


def _command_identity(command: ProfileCommand) -> dict[str, Any]:
    return {
        "command": command.command,
        "timeout_seconds": command.timeout_seconds,
        "required": command.required,
    }


def _sorted_secrets(secrets: list[ProfileSecret]) -> list[ProfileSecret]:
    return sorted(
        secrets,
        key=lambda item: (
            item.name,
            item.target,
            item.kind,
            item.provider or "",
            item.ref or "",
        ),
    )


def _secret_identity(secret: ProfileSecret) -> dict[str, Any]:
    return {
        "name": secret.name,
        "target": secret.target,
        "kind": secret.kind,
        "mode": secret.mode,
        "required": secret.required,
        "provider": secret.provider,
        "ref_sha256": _value_digest(secret.ref) if secret.ref is not None else None,
    }
