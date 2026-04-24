from __future__ import annotations

import pytest

from awf.profiles.lint import lint_profile
from awf.profiles.models import (
    DockerMode,
    ProfileCommand,
    ProfileSecret,
    ProfileService,
    WorkspaceProfile,
)


@pytest.mark.unit
def test_lint_dind_missing_docker_host() -> None:
    profile = WorkspaceProfile(
        name="test",
        docker={"mode": DockerMode.dind},
        runtime={"environment": {}},
    )
    issues = lint_profile(profile)
    assert any(
        i.code == "dind-docker-host-mismatch"
        and i.severity == "error"
        and i.field_path == "runtime.environment"
        for i in issues
    )


@pytest.mark.unit
def test_lint_dind_wrong_docker_host() -> None:
    profile = WorkspaceProfile(
        name="test",
        docker={"mode": DockerMode.dind},
        runtime={"environment": {"DOCKER_HOST": "localhost"}},
    )
    issues = lint_profile(profile)
    assert any(i.code == "dind-docker-host-mismatch" for i in issues)


@pytest.mark.unit
def test_lint_reserved_service_names() -> None:
    profile = WorkspaceProfile(
        name="test",
        services=[
            ProfileService(name="agent", image="busybox"),
            ProfileService(name="docker", image="busybox"),
            ProfileService(name="Agent", image="busybox"),
            ProfileService(name="DOCKER", image="busybox"),
            ProfileService(name="valid", image="busybox"),
        ],
    )
    issues = lint_profile(profile)
    reserved_issues = [i for i in issues if i.code == "reserved-service-name"]
    assert len(reserved_issues) == 4
    assert any(i.field_path == "services.0.name" for i in reserved_issues)
    assert any(i.field_path == "services.1.name" for i in reserved_issues)
    assert any(i.field_path == "services.2.name" for i in reserved_issues)
    assert any(i.field_path == "services.3.name" for i in reserved_issues)


@pytest.mark.unit
def test_lint_missing_timeout_warning() -> None:
    profile = WorkspaceProfile(
        name="test",
        phases={
            "setup": [ProfileCommand(command="echo hi", timeout_seconds=None)],
            "validate": [ProfileCommand(command="test", timeout_seconds=60)],
        },
    )
    issues = lint_profile(profile)
    assert any(
        i.code == "missing-command-timeout"
        and i.severity == "warning"
        and i.field_path == "phases.setup.0.timeout_seconds"
        for i in issues
    )


@pytest.mark.unit
def test_lint_dangerous_secret_target() -> None:
    profile = WorkspaceProfile(
        name="test",
        secrets=[
            ProfileSecret(name="s1", target="/home/agent/.ssh"),
            ProfileSecret(name="s2", target="/root/secret"),
            ProfileSecret(name="s3", target="/workspace/config"),
            ProfileSecret(name="s4", target="/etc/safe"),
        ],
    )
    issues = lint_profile(profile)
    dangerous_issues = [i for i in issues if i.code == "dangerous-secret-target"]
    assert len(dangerous_issues) == 3
    assert any(i.field_path == "secrets.0.target" for i in dangerous_issues)
    assert any(i.field_path == "secrets.1.target" for i in dangerous_issues)
    assert any(i.field_path == "secrets.2.target" for i in dangerous_issues)


@pytest.mark.unit
def test_lint_unresolved_service_dependency() -> None:
    profile = WorkspaceProfile(
        name="test",
        services=[
            ProfileService(name="s1", image="busybox", depends_on=["db", "redis"]),
            ProfileService(name="db", image="postgres"),
        ],
    )
    issues = lint_profile(profile)
    unresolved = [i for i in issues if i.code == "unresolved-service-dependency"]
    assert len(unresolved) == 1
    assert unresolved[0].field_path == "services.0.depends_on"
    assert "redis" in unresolved[0].message


@pytest.mark.unit
def test_lint_valid_profile_returns_no_issues() -> None:
    profile = WorkspaceProfile(
        name="valid",
        docker={"mode": DockerMode.dind},
        runtime={"environment": {"DOCKER_HOST": "tcp://docker:2375"}},
        services=[
            ProfileService(name="db", image="postgres"),
            ProfileService(name="app", image="node", depends_on=["db"]),
        ],
        phases={
            "setup": [ProfileCommand(command="init", timeout_seconds=30)],
        },
        secrets=[
            ProfileSecret(name="cert", target="/etc/certs/cert.pem"),
        ],
    )
    issues = lint_profile(profile)
    assert issues == []
