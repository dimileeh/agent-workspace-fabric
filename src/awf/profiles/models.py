"""Typed workspace profile models.

The profile model is intentionally small for the first universalization pass:
it captures the execution surface AWF already needs (runtime env, optional
DinD, sidecar services, phase commands, health checks, and artifact hints)
without trying to encode every possible build-system nuance.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


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


class ProfileDatabase(BaseModel):
    """Workspace-local project database lifecycle hooks."""

    model_config = ConfigDict(extra="forbid")

    generated_setup: list[ProfileCommand] = Field(default_factory=list)
    pre_validation_refresh: list[ProfileCommand] = Field(default_factory=list)

    @field_validator("generated_setup", "pre_validation_refresh", mode="before")
    @classmethod
    def _coerce_commands(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, list):
            return [ProfileCommand.from_shell(v) if isinstance(v, str) else v for v in value]
        return value


_HealthCheckCommand = Annotated[str, Field(min_length=1, max_length=4096)]
_HealthCheckUrl = Annotated[str, Field(min_length=1, max_length=2048)]


class ProfileHealthCheck(BaseModel):
    """A command that must pass before validation runs."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: Annotated[str, Field(min_length=1, max_length=128)]
    kind: Literal["command", "http"] | None = Field(
        default=None,
        validation_alias=AliasChoices("kind", "type"),
        serialization_alias="kind",
    )
    command: _HealthCheckCommand | None = None
    url: _HealthCheckUrl | None = None
    method: Literal["GET", "HEAD"] = "GET"
    expected_status: int = Field(default=200, ge=100, le=599)
    timeout_seconds: float = Field(default=60.0, gt=0, le=3600)
    interval_seconds: float = Field(default=1.0, gt=0, le=3600)
    attempt_timeout_seconds: float | None = Field(default=None, gt=0, le=3600)

    @field_validator("method", mode="before")
    @classmethod
    def _normalize_method(cls, value: object) -> object:
        if isinstance(value, str):
            return value.upper()
        return value

    @model_validator(mode="after")
    def _validate_mode(self) -> ProfileHealthCheck:
        has_command = self.command is not None
        has_url = self.url is not None
        if has_command == has_url:
            raise ValueError("healthcheck must set exactly one of command or url")

        inferred_kind: Literal["command", "http"] = "command" if has_command else "http"
        if self.kind is None:
            self.kind = inferred_kind
        elif self.kind != inferred_kind:
            raise ValueError("healthcheck kind must match command/url configuration")

        if self.url is not None:
            parsed = urlparse(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("healthcheck url must be an absolute http or https URL")

        return self

    def display_command(self) -> str:
        """Human-readable command/target used in validation provenance."""
        if self.command is not None:
            return self.command
        return f"{self.method} {self.target()} expected {self.expected_status}"

    def target(self) -> str:
        """Secret-free health-check target for logs and events."""
        if self.command is not None:
            return self.command
        return _redact_url_userinfo(self._require_url())

    def _require_url(self) -> str:
        if self.url is None:
            raise ValueError("healthcheck must set command or url")
        return self.url


def _redact_url_userinfo(url: str) -> str:
    parsed = urlparse(url)
    if "@" not in parsed.netloc:
        return url
    host_target = parsed.netloc.rsplit("@", 1)[1] or "<redacted>"
    return parsed._replace(netloc=host_target).geturl()


class ProfileCoverage(BaseModel):
    """Repository coverage policy expected from validation.

    The phase commands still decide the baseline validation surface. Coverage
    collection is explicit: when ``command`` is set, AWF runs it and parses the
    provider's output instead of inventing a number from test success.
    """

    model_config = ConfigDict(extra="forbid")

    minimum_percent: float = Field(default=0.0, ge=0.0, le=100.0)
    enforce: bool = True
    provider: Annotated[str, Field(min_length=1, max_length=64)] = "python"
    command: ProfileCommand | None = None

    @field_validator("command", mode="before")
    @classmethod
    def _coerce_command(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            return ProfileCommand.from_shell(value)
        return value


class OutOfScopeChangeMode(StrEnum):
    warn = "warn"
    block = "block"


class OutOfScopeChangePolicy(BaseModel):
    """Policy for changed files outside declared task scope."""

    model_config = ConfigDict(extra="forbid")

    mode: OutOfScopeChangeMode = OutOfScopeChangeMode.warn
    allowlist_patterns: list[str] = Field(default_factory=list, max_length=128)


class ProfileQuality(BaseModel):
    """Quality-control policies supplied by the workspace profile."""

    model_config = ConfigDict(extra="forbid")

    out_of_scope_changes: OutOfScopeChangePolicy = Field(default_factory=OutOfScopeChangePolicy)


class ProfileValidation(BaseModel):
    """Validation policy details beyond phase commands."""

    model_config = ConfigDict(extra="forbid")

    healthchecks: list[ProfileHealthCheck] = Field(default_factory=list)
    artifact_paths: list[str] = Field(default_factory=list)
    timeout_seconds: int | None = Field(default=None, ge=1, le=14400)
    requested_tier: int = Field(default=1, ge=1, le=3)
    coverage: ProfileCoverage = Field(default_factory=ProfileCoverage)
    retry_budget: int = Field(default=0, ge=0, le=10)


class ProfileMonitor(BaseModel):
    """PR monitor policy supplied by the workspace profile."""

    model_config = ConfigDict(extra="forbid")

    initial_review_grace_period_seconds: float = Field(default=900.0, ge=0, le=86400)
    non_check_reviewer_settle_seconds: float = Field(default=180.0, ge=0, le=86400)
    non_check_reviewer_logins: list[str] = Field(
        default_factory=lambda: ["greptile-apps"],
        max_length=64,
    )

    @field_validator("non_check_reviewer_logins")
    @classmethod
    def _normalize_non_check_reviewer_logins(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            login = _normalize_monitor_login(item)
            if not login or login in seen:
                continue
            normalized.append(login)
            seen.add(login)
        return normalized


def _normalize_monitor_login(value: str) -> str:
    login = value.strip().lower()
    if login.endswith("[bot]"):
        login = login[: -len("[bot]")]
    return re.sub(r"[^a-z0-9]+", "-", login).strip("-")


class ProfilePlanning(BaseModel):
    """Plan → execute → compare policy supplied by the workspace profile."""

    model_config = ConfigDict(extra="forbid")

    required: bool = False
    plan_path: str = Field(default="docs/awf-plans/{workspace_id}.md", max_length=512)
    conformance_report_path: str = Field(
        default="docs/awf-plans/{workspace_id}.conformance.json",
        max_length=512,
    )
    max_iterations: int = Field(default=3, ge=0, le=5)
    enforce_plan_only_changes: bool = True
    fail_on_unexplained_deviation: bool = True

    @model_validator(mode="after")
    def _validate_paths(self) -> ProfilePlanning:
        for field_name in ("plan_path", "conformance_report_path"):
            value = getattr(self, field_name)
            if not value or value.startswith("/") or ".." in value.split("/"):
                raise ValueError(f"{field_name} must be a workspace-relative path")
            if "{workspace_id}" not in value:
                raise ValueError(f"{field_name} must include '{{workspace_id}}'")
        return self


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


class ProfileLintSeverity(StrEnum):
    """Severity for structured profile lint findings."""

    warning = "warning"
    error = "error"


class ProfileLintFinding(BaseModel):
    """Structured profile validation finding for API/console consumers."""

    model_config = ConfigDict(extra="forbid")

    reason_code: Annotated[str, Field(min_length=1, max_length=128)]
    message: Annotated[str, Field(min_length=1, max_length=512)]
    path: str | None = Field(default=None, max_length=512)
    severity: ProfileLintSeverity = ProfileLintSeverity.error
    details: dict[str, Any] = Field(default_factory=dict)


class HostHomeAuthMountMode(StrEnum):
    """How profile-declared host-home auth mounts are treated."""

    block = "block"
    warn = "warn"


class HostHomeAuthMountPolicy(BaseModel):
    """Compatibility policy for local host-home credential mounts."""

    model_config = ConfigDict(extra="forbid")

    mode: HostHomeAuthMountMode = HostHomeAuthMountMode.block


class ProfileSecurity(BaseModel):
    """Security and policy declarations for the workspace."""

    model_config = ConfigDict(extra="forbid")

    egress: ProfileEgress = Field(default_factory=ProfileEgress)
    host_home_auth_mounts: HostHomeAuthMountPolicy = Field(
        default_factory=HostHomeAuthMountPolicy
    )


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
    database: ProfileDatabase = Field(default_factory=ProfileDatabase)
    validation: ProfileValidation = Field(default_factory=ProfileValidation)
    quality: ProfileQuality = Field(default_factory=ProfileQuality)
    monitor: ProfileMonitor = Field(default_factory=ProfileMonitor)
    planning: ProfilePlanning = Field(default_factory=ProfilePlanning)
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
    lint_findings: list[ProfileLintFinding] = Field(default_factory=list)
