"""Typed workspace profile models.

The profile model is intentionally small for the first universalization pass:
it captures the execution surface AWF already needs (runtime env, optional
DinD, sidecar services, phase commands, health checks, and artifact hints)
without trying to encode every possible build-system nuance.
"""

from __future__ import annotations

import re
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
    provider: str | None = Field(default=None, max_length=128)
    ref: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _validate_secret(self) -> ProfileSecret:
        # Broad targets check
        if self.kind == "mount":
            target_norm = self.target.rstrip("/")
            if target_norm in ("", "/tmp", "/var", "/etc", "/root", "/home", "/dev", "/proc", "/sys"):
                raise ValueError(f"secret target '{self.target}' is too broad")
        elif self.kind == "env":
            if self.target in ("PATH", "HOME", "USER", ""):
                raise ValueError(f"secret target '{self.target}' is too broad or invalid")

        # Missing provider/ref on required secrets check
        if self.provider and not self.ref:
            raise ValueError("secret 'ref' must be provided if 'provider' is specified")
        if self.ref and not self.provider:
            raise ValueError("secret 'provider' must be provided if 'ref' is specified")
        
        # If required and it's not a legacy 'secrets' declaration, we might enforce both,
        # but for compatibility, we only require they be symmetric right now.
        
        # Raw looking values
        def _looks_like_raw_secret(value: str | None) -> bool:
            if not value:
                return False
            # Check for generic high-entropy strings or common token prefixes
            if value.startswith(("sk-", "xoxb-", "xoxp-", "AIza")):
                return True
            # Check length to prevent embedding large JWTs or keys in 'ref'
            # Overly long strings not looking like a simple path or ARN
            return len(value) > 128 and not bool(re.match(r"^[a-zA-Z0-9_\-./:@]+$", value))

        if _looks_like_raw_secret(self.ref):
            raise ValueError("secret 'ref' appears to contain a raw secret value")

        return self


class EgressMode(StrEnum):
    open = "open"
    allowlist = "allowlist"
    offline = "offline"
    mirrored = "mirrored"


class ProfileEgress(BaseModel):
    """Network egress policy for the workspace."""

    model_config = ConfigDict(extra="forbid")

    mode: EgressMode = EgressMode.open
    allowlist: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_egress(self) -> ProfileEgress:
        if self.mode not in (EgressMode.allowlist, EgressMode.mirrored) and self.allowlist:
            raise ValueError(f"allowlist cannot be populated when egress mode is {self.mode}")
        if self.mode == EgressMode.allowlist and not self.allowlist:
            raise ValueError("allowlist must be populated when egress mode is allowlist")
        if self.mode in (EgressMode.allowlist, EgressMode.mirrored):
            for item in self.allowlist:
                if not item or item.startswith("*") or "/" in item:
                    raise ValueError(f"invalid allowlist entry: '{item}'")
        return self


class ProfileSecurity(BaseModel):
    """Security and policy declarations for the workspace."""

    model_config = ConfigDict(extra="forbid")

    egress: ProfileEgress = Field(default_factory=ProfileEgress)


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
    security: ProfileSecurity = Field(default_factory=ProfileSecurity)
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
