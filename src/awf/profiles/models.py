"""Typed workspace profile models.

The profile model is intentionally small for the first universalization pass:
it captures the execution surface AWF already needs (runtime env, optional
DinD, sidecar services, phase commands, health checks, and artifact hints)
without trying to encode every possible build-system nuance.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DockerMode(StrEnum):
    """Docker availability inside the workspace."""

    none = "none"
    dind = "dind"


class ProfileRuntime(BaseModel):
    """Runtime-level settings for the agent container."""

    model_config = ConfigDict(extra="forbid")

    agent_image: str | None = Field(default=None, max_length=512)
    toolchain_image: str | None = Field(default=None, max_length=512)
    environment: dict[str, str] = Field(default_factory=dict)


class ProfileDocker(BaseModel):
    """Workspace Docker configuration."""

    model_config = ConfigDict(extra="forbid")

    mode: DockerMode = DockerMode.none
    compose_files: list[str] = Field(default_factory=list)
    project_directory: str = "."
    startup_timeout_seconds: int = Field(default=300, ge=1, le=7200)


class ProfileCommand(BaseModel):
    """A shell command executed inside the agent container."""

    model_config = ConfigDict(extra="forbid")

    command: Annotated[str, Field(min_length=1, max_length=4096)]
    timeout_seconds: int | None = Field(default=None, ge=1, le=14400)
    required: bool = True

    @classmethod
    def from_shell(cls, value: str | ProfileCommand) -> ProfileCommand:
        if isinstance(value, ProfileCommand):
            return value
        return cls(command=value)


class ProfilePhaseSet(BaseModel):
    """Lifecycle phases AWF can execute around the agent run."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    setup: list[ProfileCommand] = Field(default_factory=list)
    pre_agent: list[ProfileCommand] = Field(default_factory=list)
    post_agent: list[ProfileCommand] = Field(default_factory=list)
    validate_commands: list[ProfileCommand] = Field(default_factory=list, alias="validate")
    cleanup: list[ProfileCommand] = Field(default_factory=list)

    @field_validator(
        "setup", "pre_agent", "post_agent", "validate_commands", "cleanup", mode="before"
    )
    @classmethod
    def _coerce_commands(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, list):
            return [ProfileCommand.from_shell(v) if isinstance(v, str) else v for v in value]
        return value

    def commands_for(
        self, phase_names: list[str] | tuple[str, ...]
    ) -> list[tuple[str, ProfileCommand]]:
        commands: list[tuple[str, ProfileCommand]] = []
        for phase in phase_names:
            attr_name = "validate_commands" if phase == "validate" else phase
            for command in getattr(self, attr_name):
                commands.append((phase, command))
        return commands


class ProfileHealthCheck(BaseModel):
    """A command that must pass before validation runs."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=128)]
    command: Annotated[str, Field(min_length=1, max_length=4096)]
    timeout_seconds: int = Field(default=60, ge=1, le=3600)


class ProfileCoverage(BaseModel):
    """Repository coverage policy expected from validation.

    The phase commands still decide how coverage is measured. This profile
    field records the required threshold so schedulers, prompts, and future
    merge policy can treat coverage as an explicit contract instead of prose.
    """

    model_config = ConfigDict(extra="forbid")

    minimum_percent: float = Field(default=0.0, ge=0.0, le=100.0)
    enforce: bool = True


class ProfileValidation(BaseModel):
    """Validation policy details beyond phase commands."""

    model_config = ConfigDict(extra="forbid")

    healthchecks: list[ProfileHealthCheck] = Field(default_factory=list)
    artifact_paths: list[str] = Field(default_factory=list)
    timeout_seconds: int | None = Field(default=None, ge=1, le=14400)
    requested_tier: int = Field(default=1, ge=1, le=3)
    coverage: ProfileCoverage = Field(default_factory=ProfileCoverage)


class ProfileMonitor(BaseModel):
    """PR monitor policy supplied by the workspace profile."""

    model_config = ConfigDict(extra="forbid")

    initial_review_grace_period_seconds: float = Field(default=900.0, ge=0, le=86400)


class ProfileService(BaseModel):
    """A service in the outer AWF compose stack.

    This deliberately mirrors a small, safe subset of Compose. More exotic
    project-specific orchestration should run inside DinD via profile phases.
    """

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")]
    image: str | None = Field(default=None, max_length=512)
    build_context: str | None = Field(default=None, max_length=1024)
    dockerfile: str = "Dockerfile"
    env_file: str | None = Field(default=None, max_length=1024)
    environment: dict[str, str] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    healthcheck_cmd: str | None = Field(default=None, max_length=4096)
    ports: list[tuple[int, int]] = Field(default_factory=list)
    command: str | None = Field(default=None, max_length=4096)
    volumes: list[tuple[str, str]] = Field(default_factory=list)
    privileged: bool = False

    @model_validator(mode="after")
    def _has_image_or_build(self) -> ProfileService:
        if not self.image and not self.build_context:
            raise ValueError("service must set either image or build_context")
        if self.image and self.build_context:
            raise ValueError("service cannot set both image and build_context")
        return self


class ProfileSecret(BaseModel):
    """A named secret mount or env lease the profile expects."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=128)]
    target: Annotated[str, Field(min_length=1, max_length=512)]
    kind: Literal["mount", "env"] = "mount"
    mode: Literal["ro", "rw"] = "ro"
    required: bool = True


class WorkspaceProfile(BaseModel):
    """Resolved project profile stored on each workspace."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=128)]
    version: int = Field(default=1, ge=1)
    description: str | None = Field(default=None, max_length=1024)
    source: str = Field(default="inline", max_length=256)
    confidence: Literal["low", "medium", "high"] = "high"
    runtime: ProfileRuntime = Field(default_factory=ProfileRuntime)
    docker: ProfileDocker = Field(default_factory=ProfileDocker)
    services: list[ProfileService] = Field(default_factory=list)
    phases: ProfilePhaseSet = Field(default_factory=ProfilePhaseSet)
    validation: ProfileValidation = Field(default_factory=ProfileValidation)
    monitor: ProfileMonitor = Field(default_factory=ProfileMonitor)
    secrets: list[ProfileSecret] = Field(default_factory=list)
    ports: dict[str, str] = Field(default_factory=dict)

    def with_validation_commands(self, commands: list[str]) -> WorkspaceProfile:
        """Return a copy with request-supplied validation commands appended."""
        if not commands:
            return self
        phase_commands = [
            *self.phases.validate_commands,
            *(ProfileCommand(command=c) for c in commands),
        ]
        return self.model_copy(
            deep=True,
            update={"phases": self.phases.model_copy(update={"validate_commands": phase_commands})},
        )


class ProfileResolution(BaseModel):
    """Result of resolving a profile for one workspace."""

    model_config = ConfigDict(extra="forbid")

    profile: WorkspaceProfile
    reason: str
    candidates_considered: list[str] = Field(default_factory=list)
