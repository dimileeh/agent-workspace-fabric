"""Typed workspace profile models.

The profile model is intentionally small for the first universalization pass:
it captures the execution surface AWF already needs (runtime env, optional
DinD, sidecar services, phase commands, health checks, and artifact hints)
without trying to encode every possible build-system nuance.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal
from urllib.parse import urlparse, urlsplit

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from awf.db.enums import ForgeKind
from awf.profiles.pricing import PricingMetadata


class DockerMode(StrEnum):
    """Docker availability inside the workspace."""

    none = "none"
    dind = "dind"


# Compose service name AWF prepends for the managed Docker-in-Docker daemon when
# ``docker.mode`` is ``dind`` (see ``ComposeManager._services_for``). It is also
# the host the agent reaches via ``DOCKER_HOST=tcp://docker:2375``, so a
# profile-declared service of the same name would shadow the managed daemon.
_MANAGED_DIND_SERVICE_NAME = "docker"


class EndpointVisibility(StrEnum):
    """Where a profile app endpoint should be exposed."""

    agent = "agent"
    validation = "validation"
    console = "console"
    internal = "internal"


# Toolchain declaration bounds (see ``ProfileRuntime.toolchains``). Language keys
# stay conservative safe identifiers; versions are dotted numeric strings such as
# ``17``, ``21``, ``1.8`` or ``11.0.2``. Counts are capped so a declaration stays
# bounded and cannot be used to inflate the schema.
_TOOLCHAIN_LANGUAGE_PATTERN = re.compile(r"^[a-z][a-z0-9+_.-]*$")
_TOOLCHAIN_VERSION_PATTERN = re.compile(r"^[0-9]+(\.[0-9]+){0,3}$")
_MAX_TOOLCHAIN_LANGUAGES = 16
_MAX_TOOLCHAIN_VERSIONS = 16


class ProfileRuntime(BaseModel):
    """Runtime-level settings for the agent container."""

    model_config = ConfigDict(extra="forbid")

    agent_image: str | None = Field(default=None, max_length=512)
    toolchain_image: str | None = Field(default=None, max_length=512)
    environment: dict[str, str] = Field(default_factory=dict)
    toolchains: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Language toolchains the workspace needs, mapping a language identifier "
            "to the versions that must be available in the runtime/toolchain image, "
            'e.g. ``{"java": ["17", "21"]}``. Absent (the default empty mapping) '
            "means no toolchain requirement is declared. The runtime/toolchain image "
            "is expected to provide each declared version side by side; for Java these "
            "are selected per-command via ``JAVA_HOME``/``update-alternatives``, while "
            "other runtimes may use different resolution mechanisms. A provision-time, "
            "in-container probe discovers the installed versions and the "
            "``runtime_toolchain_findings`` lint seam yields a non-blocking "
            "``RUNTIME_TOOLCHAIN_UNAVAILABLE`` warning when a declared version is "
            "missing from the runtime image."
        ),
    )

    @field_validator("toolchains", mode="before")
    @classmethod
    def _validate_toolchains(cls, value: object) -> object:
        """Normalize and bound the toolchain declaration.

        Lowercases language keys, validates each against a conservative identifier
        pattern, requires a non-empty list of dotted-numeric versions per language,
        de-duplicates versions while preserving declaration order, and caps the
        number of languages/versions. An absent or empty declaration is a no-op.
        """
        if value is None:
            return {}
        # Accept any Mapping (matching the declared contract / the in-scope ``Mapping`` import),
        # not just ``dict`` — a ``MappingProxyType``/``ChainMap`` passed by a caller is normalized
        # into a plain dict below rather than spuriously rejected. ``str``/list/tuple stay rejected.
        if not isinstance(value, Mapping):
            raise ValueError("runtime.toolchains must be a mapping of language to versions")
        if len(value) > _MAX_TOOLCHAIN_LANGUAGES:
            raise ValueError(
                f"runtime.toolchains declares too many languages (max {_MAX_TOOLCHAIN_LANGUAGES})"
            )
        normalized: dict[str, list[str]] = {}
        for raw_language, raw_versions in value.items():
            if not isinstance(raw_language, str):
                raise ValueError("runtime.toolchains language keys must be strings")
            language = raw_language.strip().lower()
            if not _TOOLCHAIN_LANGUAGE_PATTERN.fullmatch(language):
                raise ValueError(f"invalid toolchain language identifier: {raw_language!r}")
            if language in normalized:
                raise ValueError(f"duplicate toolchain language: {language}")
            if isinstance(raw_versions, str) or not isinstance(raw_versions, (list, tuple)):
                raise ValueError(
                    f"runtime.toolchains[{language!r}] must be a non-empty list of version strings"
                )
            if not raw_versions:
                raise ValueError(
                    f"runtime.toolchains[{language!r}] must declare at least one version"
                )
            if len(raw_versions) > _MAX_TOOLCHAIN_VERSIONS:
                raise ValueError(
                    f"runtime.toolchains[{language!r}] declares too many versions "
                    f"(max {_MAX_TOOLCHAIN_VERSIONS})"
                )
            versions: list[str] = []
            seen: set[str] = set()
            for raw_version in raw_versions:
                if not isinstance(raw_version, str):
                    raise ValueError(f"runtime.toolchains[{language!r}] versions must be strings")
                version = raw_version.strip()
                if not _TOOLCHAIN_VERSION_PATTERN.fullmatch(version):
                    raise ValueError(f"invalid toolchain version for {language!r}: {raw_version!r}")
                if version in seen:
                    continue
                versions.append(version)
                seen.add(version)
            normalized[language] = versions
        return normalized


class ProfileDocker(BaseModel):
    """Workspace Docker configuration."""

    model_config = ConfigDict(extra="forbid")

    mode: DockerMode = DockerMode.none
    compose_files: list[str] = Field(default_factory=list)
    project_directory: str = "."
    startup_timeout_seconds: int = Field(default=300, ge=1, le=7200)
    # Interpolated unescaped into image: "{{ s.image }}" in the compose Jinja
    # template (autoescape=False), so guard the canonical image-reference grammar
    # here to keep an embedded quote/newline from injecting compose YAML.
    dind_image: str = Field(
        default="docker:27-dind",
        min_length=1,
        max_length=512,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_./:@-]*$",
    )


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
    parallel_workers: StrictInt | None = Field(default=None, ge=1, le=64)
    parallel_worker_max: StrictInt | None = Field(default=None, ge=1, le=64)

    @field_validator("command", mode="before")
    @classmethod
    def _coerce_command(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            return ProfileCommand.from_shell(value)
        return value

    @model_validator(mode="after")
    def _validate_parallel_policy(self) -> ProfileCoverage:
        if self.command is None:
            if self.minimum_percent > 0:
                raise ValueError(
                    "validation.coverage.command is required when "
                    "validation.coverage.minimum_percent is greater than 0"
                )
            if self.parallel_workers is not None or self.parallel_worker_max is not None:
                raise ValueError(
                    "validation.coverage.command is required when coverage parallelism is set"
                )
        if self.parallel_worker_max is not None and self.parallel_workers is None:
            raise ValueError(
                "validation.coverage.parallel_worker_max requires "
                "validation.coverage.parallel_workers"
            )
        if (
            self.parallel_workers is not None
            and self.command is not None
            and _coverage_command_has_xdist_args(self.command.command)
        ):
            raise ValueError(
                "validation.coverage.command must not include pytest-xdist "
                "worker/distribution args when parallel_workers is set"
            )
        return self


class ProfileValidationStrategy(BaseModel):
    """Policy for when AWF runs or reuses expensive validation evidence."""

    model_config = ConfigDict(extra="forbid")

    baseline_coverage: Literal["always", "reuse_or_run_when_unknown", "skip"] = "always"
    edit_gate: Literal["full", "targeted"] = "full"
    final_gate: Literal["none", "coverage"] = "none"
    reuse_evidence: bool = False
    freshness_max_age_seconds: int = Field(default=86400, ge=1, le=2592000)
    full_gate_concurrency: int = Field(default=0, ge=0, le=64)


class ProfileAlembicValidation(BaseModel):
    """Opt-in Alembic migration-chain validation policy."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    config_path: Annotated[str, Field(min_length=1, max_length=512)] = "alembic.ini"
    script_location: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    fail_on_unconfigured: bool = True

    @model_validator(mode="after")
    def _validate_paths(self) -> ProfileAlembicValidation:
        _validate_workspace_relative_path("config_path", self.config_path)
        if self.script_location is not None:
            _validate_workspace_relative_path("script_location", self.script_location)
        return self


def _coverage_command_has_xdist_args(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    return any(_is_xdist_arg(token) for token in tokens)


def _is_xdist_arg(token: str) -> bool:
    return (
        token == "-n"
        or (token.startswith("-n") and len(token) > 2)
        or token == "--numprocesses"
        or token.startswith("--numprocesses=")
        or token == "--dist"
        or token.startswith("--dist=")
    )


def _validate_workspace_relative_path(field_name: str, value: str) -> None:
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        posix_path.is_absolute()
        or windows_path.drive
        or windows_path.root
        or ".." in posix_path.parts
        or ".." in windows_path.parts
    ):
        raise ValueError(f"{field_name} must be a workspace-relative path")


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
    strategy: ProfileValidationStrategy = Field(default_factory=ProfileValidationStrategy)
    alembic: ProfileAlembicValidation = Field(default_factory=ProfileAlembicValidation)
    retry_budget: int = Field(default=0, ge=0, le=10)


class ProfileMonitor(BaseModel):
    """PR monitor policy supplied by the workspace profile."""

    model_config = ConfigDict(extra="forbid")

    initial_review_grace_period_seconds: float = Field(default=900.0, ge=0, le=86400)
    non_check_reviewer_settle_seconds: float = Field(default=900.0, ge=0, le=86400)
    require_ci: bool = Field(default=True)
    """Whether the PR monitor must observe at least one check/status before
    auto-merge. Default ``True`` is today's behavior; set ``false`` only for
    repos that intentionally run NO CI so the monitor merges a clean PR once the
    forge confirms an empty check set (paired with ``PRStatus.no_checks_observed``)."""
    non_check_reviewer_logins: list[str] = Field(
        default_factory=lambda: ["greptile-apps", "chatgpt-codex-connector"],
        max_length=64,
    )
    awaiting_required_checks_grace_seconds: float = Field(default=600.0, le=86400)
    """Grace window for required CI that is expected but absent on the current
    head before the monitor escalates ``NotifyHuman`` (#655 / #662).

    Right after a monitor push the forge has not started CI on the new head yet,
    so the head shows an empty check set while branch protection reports
    ``BLOCKED`` (the required context is absent). Within this window the monitor
    defers to a bounded ``WaitForCI`` instead of a premature ``NotifyHuman``; once
    it expires a head that genuinely never gets CI escalates as before. The
    default (``600.0``) preserves today's hard-coded behavior and covers the
    observed ≈5.5-min CI-start lag with margin. Set ``<= 0`` to disable the grace
    (escalate immediately, pre-#655 behavior). The upper bound (``86400``) mirrors
    the other monitor knobs; there is deliberately no lower bound so the
    documented ``<= 0`` disable escape hatch is reachable from operator
    configuration."""

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


class ProfileConformanceStallPolicy(BaseModel):
    """Thresholds and recovery action for the conformance stall detector."""

    model_config = ConfigDict(extra="forbid")

    no_output_seconds: int = Field(default=600, gt=0, le=86400)
    over_duration_seconds: int = Field(default=1800, gt=0, le=86400)
    repeated_output_threshold: int = Field(default=3, gt=1, le=20)
    recovery_action: Literal["retry_conformance", "proceed_to_validation", "fail"] = (
        "retry_conformance"
    )


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
    conformance_stall: ProfileConformanceStallPolicy = Field(
        default_factory=ProfileConformanceStallPolicy
    )

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
    # Each value is emitted as a raw YAML key in the compose template, so it
    # must be a bare service name. Mirror ``name``'s pattern to reject strings
    # carrying YAML syntax (newlines, colons) that could otherwise inject
    # service-level settings or invalidate the generated compose file.
    depends_on: list[
        Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")]
    ] = Field(default_factory=list)
    healthcheck_cmd: str | None = Field(default=None, max_length=4096)
    ports: list[tuple[int, int]] = Field(default_factory=list)
    command: str | None = Field(default=None, max_length=4096)
    volumes: list[tuple[str, str]] = Field(default_factory=list)
    privileged: bool = False
    required: bool = True

    @model_validator(mode="after")
    def _has_image_or_build(self) -> ProfileService:
        if not self.image and not self.build_context:
            raise ValueError("service must set either image or build_context")
        if self.image and self.build_context:
            raise ValueError("service cannot set both image and build_context")
        return self


class ProfileAppEndpointHealth(BaseModel):
    """Health metadata for a profile-defined app endpoint."""

    model_config = ConfigDict(extra="forbid")

    path: Annotated[str, Field(min_length=1, max_length=2048)]
    method: Literal["GET", "HEAD"] = "GET"
    expected_status: int = Field(default=200, ge=100, le=599)

    @field_validator("method", mode="before")
    @classmethod
    def _normalize_method(cls, value: object) -> object:
        if isinstance(value, str):
            return value.upper()
        return value

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return _validate_endpoint_url_path(value, field_name="health path")


class ProfileAppEndpoint(BaseModel):
    """A named internal service endpoint for agents, validation, or the console."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")]
    service: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")]
    scheme: Literal["http", "https"] = "http"
    port: int = Field(ge=1, le=65535)
    path: Annotated[str, Field(default="/", min_length=1, max_length=2048)] = "/"
    health: ProfileAppEndpointHealth | None = None
    visibility: EndpointVisibility = EndpointVisibility.agent

    @field_validator("scheme", mode="before")
    @classmethod
    def _normalize_scheme(cls, value: object) -> object:
        if isinstance(value, str):
            return value.lower()
        return value

    @field_validator("visibility", mode="before")
    @classmethod
    def _normalize_visibility(cls, value: object) -> object:
        if isinstance(value, str):
            return value.lower()
        return value

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return _validate_endpoint_url_path(value, field_name="endpoint path")


def _validate_endpoint_url_path(value: str, *, field_name: str) -> str:
    if value.strip() != value:
        raise ValueError(f"{field_name} must be a URL path")
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{field_name} must be a URL path")
    return value


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


class EgressAllowlistTemplate(StrEnum):
    """Reusable restricted egress allowlist categories for common destinations."""

    github = "github"
    model_providers = "model_providers"
    package_registries = "package_registries"
    os_mirrors = "os_mirrors"
    documentation = "documentation"


class EgressMode(StrEnum):
    open = "open"
    restricted = "restricted"
    offline = "offline"


class ProfileEgress(BaseModel):
    """Network egress policy for the workspace."""

    model_config = ConfigDict(extra="forbid")

    mode: EgressMode = EgressMode.restricted
    allowlist_templates: list[EgressAllowlistTemplate] = Field(default_factory=list)
    open_explanation: str | None = Field(default=None, max_length=512)


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


class SupplyChainGuardrailMode(StrEnum):
    """How a supply-chain guardrail finding affects workspace progress."""

    warn = "warn"
    block = "block"


class SupplyChainGuardrailPolicy(BaseModel):
    """Warn/block mode for one supply-chain guardrail."""

    model_config = ConfigDict(extra="forbid")

    mode: SupplyChainGuardrailMode = SupplyChainGuardrailMode.warn


class SupplyChainRegistryPolicy(SupplyChainGuardrailPolicy):
    """Registry-host allowlist for package-manager commands."""

    allowed_hosts: list[str] = Field(default_factory=list, max_length=128)

    @field_validator("allowed_hosts")
    @classmethod
    def _normalize_allowed_hosts(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            host = _normalize_registry_host(item)
            if host in seen:
                continue
            normalized.append(host)
            seen.add(host)
        return normalized


class ProfileSupplyChainPolicy(BaseModel):
    """Profile-selectable supply-chain guardrails for agent-authored work."""

    model_config = ConfigDict(extra="forbid")

    unpinned_dependency_installs: SupplyChainGuardrailPolicy = Field(
        default_factory=SupplyChainGuardrailPolicy
    )
    remote_script_execution: SupplyChainGuardrailPolicy = Field(
        default_factory=SupplyChainGuardrailPolicy
    )
    unexpected_registry_hosts: SupplyChainRegistryPolicy = Field(
        default_factory=SupplyChainRegistryPolicy
    )
    lockfile_changes_outside_owned_paths: SupplyChainGuardrailPolicy = Field(
        default_factory=SupplyChainGuardrailPolicy
    )


def _normalize_registry_host(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("registry host must not be empty")
    if "://" in raw:
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("registry host URL must be absolute http or https")
        if parsed.username or parsed.password:
            raise ValueError("registry host URL must not include credentials")
        host = parsed.hostname
    else:
        if any(char in raw for char in "/?#@"):
            raise ValueError("registry host must be a hostname or URL without credentials")
        host = raw.rsplit(":", 1)[0] if ":" in raw else raw
    if host is None:
        raise ValueError("registry host must include a hostname")
    normalized = host.lower().rstrip(".")
    if normalized == "localhost":
        return normalized
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if not re.fullmatch(rf"(?=^.{{1,253}}$){label}(?:\.{label})*", normalized):
        raise ValueError("registry host must be a valid hostname")
    return normalized


class ProfileSecurity(BaseModel):
    """Security policy settings for a workspace."""

    model_config = ConfigDict(extra="forbid")

    egress: ProfileEgress = Field(default_factory=ProfileEgress)
    host_home_auth_mounts: HostHomeAuthMountPolicy = Field(default_factory=HostHomeAuthMountPolicy)
    supply_chain: ProfileSupplyChainPolicy = Field(default_factory=ProfileSupplyChainPolicy)


class ProfilePricing(BaseModel):
    """Optional pricing metadata in a workspace profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pricing: PricingMetadata


class WorkspaceProfile(BaseModel):
    """Workspace profile shape, used both as create/adoption **input** and as the
    resolved profile persisted on a workspace.

    As submitted (the inline ``profile`` on a workspace-create or PR-adoption
    request) some fields may still be unresolved — notably ``forge`` may be the
    ``"auto"`` sentinel. At provision time the resolver stamps concrete values
    (``forge`` becomes ``github``/``bitbucket``) and that snapshot is what gets
    stored on the workspace, so a *resolved* profile never carries ``"auto"``."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=128)]
    version: int = Field(default=1, ge=1)
    description: str | None = Field(default=None, max_length=1024)
    source: str = Field(default="inline", max_length=256)
    confidence: Literal["low", "medium", "high"] = "high"
    forge: ForgeKind | Literal["auto"] = Field(
        default="auto",
        description=(
            'Code forge the workspace repo lives on (issue #345). "auto" is the '
            "unresolved input sentinel: it defers to URL-host detection at resolve "
            "time. The resolver always persists a concrete github/bitbucket value "
            "(precedence: explicit forge > URL host > github), so a resolved "
            'profile never carries "auto".'
        ),
    )
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
    pricing: ProfilePricing | None = None
    ports: dict[str, str] = Field(default_factory=dict)
    app_endpoints: list[ProfileAppEndpoint] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_service_names(self) -> WorkspaceProfile:
        """Reject ambiguous service names before they reach Compose rendering.

        Each service becomes a top-level key in the generated Compose file, so two
        entries sharing a name would emit duplicate keys (Compose rejects the file
        or one definition silently shadows the other). In ``dind`` mode AWF also
        prepends its own managed ``docker`` daemon; a profile-declared ``docker``
        service would collide with it and leave the agent's
        ``DOCKER_HOST=tcp://docker:2375`` pointing at the wrong container.
        """
        seen: set[str] = set()
        for service in self.services:
            if service.name in seen:
                raise ValueError(f"duplicate service name: {service.name}")
            seen.add(service.name)
        if self.docker.mode == DockerMode.dind and _MANAGED_DIND_SERVICE_NAME in seen:
            raise ValueError(
                f"service name {_MANAGED_DIND_SERVICE_NAME!r} is reserved for the "
                "managed Docker-in-Docker daemon when docker.mode is 'dind'"
            )
        return self

    @model_validator(mode="after")
    def _validate_app_endpoints(self) -> WorkspaceProfile:
        if not self.app_endpoints:
            return self

        service_names = {service.name for service in self.services}
        seen_names: set[str] = set()
        seen_env_names: set[str] = set()
        for endpoint in self.app_endpoints:
            normalized_name = endpoint.name.lower()
            if normalized_name in seen_names:
                raise ValueError(f"duplicate app endpoint name: {endpoint.name}")
            seen_names.add(normalized_name)

            env_name = _normalized_endpoint_env_name(endpoint.name)
            if not env_name:
                raise ValueError(f"app endpoint environment name cannot be empty: {endpoint.name}")
            if env_name in seen_env_names:
                raise ValueError(f"duplicate app endpoint environment name: {endpoint.name}")
            seen_env_names.add(env_name)

            if endpoint.service not in service_names:
                raise ValueError(f"unknown app endpoint service: {endpoint.service}")
        return self

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

    def clear_validation_commands(self) -> WorkspaceProfile:
        """Return a copy with validation commands removed."""
        return self.model_copy(
            deep=True,
            update={"phases": self.phases.model_copy(update={"validate_commands": []})},
        )


# Derive the backfill default from the field itself so retuning the field default
# (e.g. the CI-start lag observation window) keeps legacy inline-profile replays
# idempotent automatically, instead of silently diverging from a hardcoded copy.
_MONITOR_AWAITING_REQUIRED_CHECKS_GRACE_SECONDS_DEFAULT: float = ProfileMonitor.model_fields[
    "awaiting_required_checks_grace_seconds"
].default


def normalize_inline_profile_snapshot(
    profile: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Normalize a stored/requested inline-profile snapshot for idempotent-replay comparison.

    Create idempotency (``workspace_create_payload_matches``) and PR-adoption
    policy checks (``_raise_if_policy_conflicts``) compare the persisted
    ``requested_profile`` JSON against a freshly dumped request profile
    byte-for-byte. The ``forge`` field (issue #345) was added after some
    workspaces were persisted, so their stored snapshot has no ``forge`` key while
    an otherwise-identical replay now dumps ``"forge": "auto"`` (the unresolved
    input default). Default a missing ``forge`` to ``"auto"`` so replays of
    pre-forge inline profiles stay idempotent instead of raising a spurious
    conflict.

    Likewise ``monitor.awaiting_required_checks_grace_seconds`` (#655 / #662) was
    added after some inline-profile workspaces were persisted, so their stored
    ``monitor`` sub-dict lacks the key while an otherwise-identical replay now
    dumps the ``600.0`` default. Backfill a missing value there too so legacy
    inline-profile replays stay idempotent. Returns a shallow copy; the input is
    never mutated (the ``monitor`` sub-dict is copied only when backfilled).
    """
    if profile is None:
        return None
    result = dict(profile)
    if "forge" not in result:
        result["forge"] = "auto"
    monitor = result.get("monitor")
    if isinstance(monitor, dict) and "awaiting_required_checks_grace_seconds" not in monitor:
        result["monitor"] = {
            **monitor,
            "awaiting_required_checks_grace_seconds": (
                _MONITOR_AWAITING_REQUIRED_CHECKS_GRACE_SECONDS_DEFAULT
            ),
        }
    return result


def _normalized_endpoint_env_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").upper()


# Reason code emitted when a profile declares a language toolchain version that the
# runtime/toolchain image does not provide. Profile-lint reason codes are free-form
# strings consumed by preflight/console, not part of the generated doctor catalog.
RUNTIME_TOOLCHAIN_UNAVAILABLE = "RUNTIME_TOOLCHAIN_UNAVAILABLE"


def runtime_toolchain_findings(
    profile: WorkspaceProfile,
    available: Mapping[str, set[str]] | None,
) -> tuple[ProfileLintFinding, ...]:
    """Warn about declared language toolchains missing from the runtime image.

    Pure, I/O-free seam for a (sibling-owned) preflight: ``available`` maps each
    language to the set of versions actually discovered in the runtime/toolchain
    image. ``available is None`` means the image contents are unknown, so this layer
    stays silent (it never fails resolution on unknown availability). For every
    declared ``(language, version)`` absent from ``available`` — including a language
    not present at all — a ``warning``-severity :class:`ProfileLintFinding` carrying
    the ``RUNTIME_TOOLCHAIN_UNAVAILABLE`` reason code is returned.
    """
    if available is None:
        return ()
    # Profile toolchain keys are normalized to lowercase (see ProfileRuntime._validate_toolchains),
    # but ``available`` keys come from external image discovery with no case guarantee. Fold the
    # availability keys to lowercase so a mixed-case ``"Java"`` does not produce a false-positive
    # RUNTIME_TOOLCHAIN_UNAVAILABLE warning against a declared ``"java"`` toolchain. Merge (rather
    # than overwrite) sets so two casings of the same language — e.g. ``"JAVA"`` and ``"Java"`` —
    # union their versions instead of silently dropping all but the last.
    available_lower: dict[str, set[str]] = {}
    for key, value in available.items():
        k = key.lower()
        if k in available_lower:
            available_lower[k] = available_lower[k] | value
        else:
            available_lower[k] = value
    findings: list[ProfileLintFinding] = []
    for language, versions in profile.runtime.toolchains.items():
        available_versions = available_lower.get(language, set())
        for version in versions:
            if version in available_versions:
                continue
            findings.append(
                ProfileLintFinding(
                    reason_code=RUNTIME_TOOLCHAIN_UNAVAILABLE,
                    message=(
                        f"runtime image does not provide {language} toolchain version {version}"
                    ),
                    path=f"runtime.toolchains.{language}",
                    severity=ProfileLintSeverity.warning,
                    details={
                        "language": language,
                        "version": version,
                        "available_versions": sorted(available_versions),
                    },
                )
            )
    return tuple(findings)


class ProfileResolution(BaseModel):
    """Result of resolving a profile for one workspace."""

    model_config = ConfigDict(extra="forbid")

    profile: WorkspaceProfile
    network_posture: Literal["offline", "restricted", "open"]
    reason: str
    candidates_considered: list[str] = Field(default_factory=list)
    lint_findings: list[ProfileLintFinding] = Field(default_factory=list)
