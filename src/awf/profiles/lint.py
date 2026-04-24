from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from .models import DockerMode, WorkspaceProfile


class LintSeverity(StrEnum):
    error = "error"
    warning = "warning"


class LintIssue(BaseModel):
    code: str
    severity: LintSeverity
    message: str
    field_path: str


def lint_profile(profile: WorkspaceProfile) -> list[LintIssue]:
    """Validate a WorkspaceProfile and return structured issues."""
    issues: list[LintIssue] = []

    # 1. DinD profiles should expose DOCKER_HOST=tcp://docker:2375 in runtime.environment
    if profile.docker.mode == DockerMode.dind:
        docker_host = profile.runtime.environment.get("DOCKER_HOST")
        if docker_host != "tcp://docker:2375":
            issues.append(
                LintIssue(
                    code="dind-docker-host-mismatch",
                    severity=LintSeverity.error,
                    message="DinD profiles must set DOCKER_HOST=tcp://docker:2375 in runtime.environment",
                    field_path="runtime.environment",
                )
            )

    # 2. Service names must not include reserved names agent or docker
    reserved_names = {"agent", "docker"}
    for i, service in enumerate(profile.services):
        if service.name in reserved_names:
            issues.append(
                LintIssue(
                    code="reserved-service-name",
                    severity=LintSeverity.error,
                    message=f"Service name '{service.name}' is reserved and cannot be used",
                    field_path=f"services.{i}.name",
                )
            )

    # 3. Profile phase commands should have explicit timeout_seconds and should warn when missing
    for phase_name in ["setup", "pre_agent", "post_agent", "validate_commands", "cleanup"]:
        commands = getattr(profile.phases, phase_name)
        # validate_commands is aliased to 'validate' in some places,
        # but the attribute on ProfilePhaseSet is validate_commands.
        field_alias = "validate" if phase_name == "validate_commands" else phase_name
        for i, cmd in enumerate(commands):
            if cmd.timeout_seconds is None:
                issues.append(
                    LintIssue(
                        code="missing-command-timeout",
                        severity=LintSeverity.warning,
                        message=f"Command in phase '{field_alias}' is missing explicit timeout_seconds",
                        field_path=f"phases.{field_alias}.{i}.timeout_seconds",
                    )
                )

    # 4. Secret declarations must not target broad host-home/workspace paths
    dangerous_paths = ["/home/agent", "/root", "/workspace"]
    for i, secret in enumerate(profile.secrets):
        target = secret.target
        if any(target == p or target.startswith(f"{p}/") for p in dangerous_paths):
            issues.append(
                LintIssue(
                    code="dangerous-secret-target",
                    severity=LintSeverity.error,
                    message=f"Secret target '{target}' uses a protected path prefix",
                    field_path=f"secrets.{i}.target",
                )
            )

    # 5. Services with depends_on should depend only on declared services
    service_names = {s.name for s in profile.services}
    for i, service in enumerate(profile.services):
        for dep in service.depends_on:
            if dep not in service_names:
                issues.append(
                    LintIssue(
                        code="unresolved-service-dependency",
                        severity=LintSeverity.error,
                        message=f"Service '{service.name}' depends on unknown service '{dep}'",
                        field_path=f"services.{i}.depends_on",
                    )
                )

    return issues
