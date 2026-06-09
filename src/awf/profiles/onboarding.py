"""Project onboarding inspection and workspace profile drafting."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from awf.common.profile_paths import PROFILE_MARKER_PATHS
from awf.profiles.models import (
    DockerMode,
    EgressAllowlistTemplate,
    EgressMode,
    ProfileCommand,
    ProfileDocker,
    ProfileHealthCheck,
    ProfileResolution,
    ProfileRuntime,
    ProfileSecret,
    ProfileService,
    ProfileValidation,
    WorkspaceProfile,
)

_COMPOSE_FILENAMES = (
    "compose.yml",
    "compose.yaml",
    "docker-compose.yml",
    "docker-compose.yaml",
)
_SUPPORTED_TEMPLATES = frozenset(
    (
        "generic",
        "python",
        "node-nextjs",
        "docker-compose",
        "python-postgres",
        "node-playwright",
        "multi-service",
    )
)
_DEFAULT_RESTRICTED_ALLOWLIST_TEMPLATES = tuple(EgressAllowlistTemplate)
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)[^}]*\}")
_SECRET_NAME_RE = re.compile(
    r"(SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|CREDENTIAL|ACCESS_KEY)",
    re.IGNORECASE,
)
_HTTP_COMPOSE_PORTS = frozenset({80, 3000, 3001, 4173, 5000, 5173, 8000, 8080, 8081, 8888})
_HTTPS_COMPOSE_PORTS = frozenset({443, 8443})


@dataclass(frozen=True, slots=True)
class ComposeServiceInspection:
    """Small, safe subset of a Compose service used for onboarding diagnostics."""

    name: str
    has_healthcheck: bool
    ports: tuple[str, ...]
    secret_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectInspection:
    """Repository shape detected during project onboarding."""

    path: Path
    detected_template: str
    confidence: str
    signals: tuple[str, ...]
    package_manager: str | None = None
    package_scripts: Mapping[str, str] | None = None
    package_dependencies: frozenset[str] = frozenset()
    compose_file: str | None = None
    compose_services: tuple[ComposeServiceInspection, ...] = ()
    service_directories: tuple[str, ...] = ()

    def scripts(self) -> Mapping[str, str]:
        """Return project script mappings from package metadata."""
        return self.package_scripts or {}


@dataclass(frozen=True, slots=True)
class PreviewDiagnostics:
    """Missing project-profile sections that require human or agent review."""

    missing_services: tuple[str, ...]
    missing_secrets: tuple[str, ...]
    missing_ports: tuple[str, ...]
    missing_validation_commands: tuple[str, ...]
    missing_healthchecks: tuple[str, ...]

    def __post_init__(self) -> None:
        """Normalize diagnostic tuples during dataclass initialization."""
        for field_name in (
            "missing_services",
            "missing_secrets",
            "missing_ports",
            "missing_validation_commands",
            "missing_healthchecks",
        ):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))

    def to_dict(self) -> dict[str, list[str]]:
        """Export diagnostics to a JSON-serializable dictionary."""
        return {
            "missing_services": list(self.missing_services),
            "missing_secrets": list(self.missing_secrets),
            "missing_ports": list(self.missing_ports),
            "missing_validation_commands": list(self.missing_validation_commands),
            "missing_healthchecks": list(self.missing_healthchecks),
        }


@dataclass(frozen=True, slots=True)
class DraftProfile:
    """Draft workspace profile plus serialized repository profile YAML."""

    template: str
    profile: WorkspaceProfile
    yaml: str
    diagnostics: PreviewDiagnostics

    def to_dict(self) -> dict[str, object]:
        """Serialize draft profile payload for API and CLI output."""
        return {
            "template": self.template,
            "profile": self.profile.model_dump(mode="json", by_alias=True, exclude_none=True),
            "yaml": self.yaml,
        }


@dataclass(frozen=True, slots=True)
class ProjectOnboardingPreview:
    """Complete onboarding preview for CLI and documentation examples."""

    path: Path
    inspection: ProjectInspection
    draft: DraftProfile
    diagnostics: PreviewDiagnostics
    smoke_request: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize the full onboarding preview for consumers."""
        payload: dict[str, object] = {
            "path": str(self.path),
            "inspection": {
                "detected_template": self.inspection.detected_template,
                "confidence": self.inspection.confidence,
                "signals": list(self.inspection.signals),
                "package_manager": self.inspection.package_manager,
                "compose_file": self.inspection.compose_file,
                "compose_services": [service.name for service in self.inspection.compose_services],
                "service_directories": list(self.inspection.service_directories),
            },
            "draft": self.draft.to_dict(),
            "diagnostics": self.diagnostics.to_dict(),
        }
        if self.smoke_request is not None:
            payload["smoke_request"] = self.smoke_request
        return payload


def inspect_project(path: Path) -> ProjectInspection:
    """Inspect a repository and select the best onboarding template."""
    root = path.expanduser().resolve()
    if not root.exists():
        raise ValueError(f"project path does not exist: {root}")
    if not root.is_dir():
        raise ValueError(f"project path is not a directory: {root}")

    package_data = _read_package_json(root / "package.json")
    package_scripts = _string_mapping(package_data.get("scripts"))
    package_dependencies = frozenset(_package_dependency_names(package_data))
    package_manager = _detect_package_manager(root) if package_data else None
    compose_file = _detect_compose_file(root)
    compose_services = _inspect_compose_services(root / compose_file) if compose_file else ()
    service_directories = tuple(
        directory
        for directory in ("apps", "services", "backend", "frontend")
        if (root / directory).is_dir()
    )

    has_python = _has_python_signal(root)
    has_node = bool(package_data)
    has_next = has_node and (
        "next" in package_dependencies or any((root / name).is_file() for name in _next_configs())
    )
    has_playwright = has_node and (
        "@playwright/test" in package_dependencies
        or any((root / name).is_file() for name in _playwright_configs())
    )
    has_postgres = has_python and _has_postgres_signal(root)
    has_multi_service = len(compose_services) > 1 or len(service_directories) >= 2

    signals: list[str] = []
    if has_python:
        signals.append("python")
    if has_node:
        signals.append("node")
    if has_next:
        signals.append("nextjs")
    if has_playwright:
        signals.append("playwright")
    if has_postgres:
        signals.append("postgres")
    if compose_file:
        signals.append("docker-compose")
    if has_multi_service:
        signals.append("multi-service")

    if has_multi_service:
        detected_template = "multi-service"
        confidence = "medium"
    elif compose_file:
        detected_template = "docker-compose"
        confidence = "medium"
    elif has_postgres:
        detected_template = "python-postgres"
        confidence = "medium"
    elif has_playwright:
        detected_template = "node-playwright"
        confidence = "medium"
    elif has_next:
        detected_template = "node-nextjs"
        confidence = "medium"
    elif has_python:
        detected_template = "python"
        confidence = "medium"
    else:
        detected_template = "generic"
        confidence = "low"

    return ProjectInspection(
        path=root,
        detected_template=detected_template,
        confidence=confidence,
        signals=tuple(signals),
        package_manager=package_manager,
        package_scripts=package_scripts,
        package_dependencies=package_dependencies,
        compose_file=compose_file,
        compose_services=compose_services,
        service_directories=service_directories,
    )


def draft_workspace_profile(
    inspection: ProjectInspection,
    template: str = "auto",
) -> DraftProfile:
    """Generate a draft workspace profile from a project inspection."""
    selected = inspection.detected_template if template == "auto" else template
    if selected not in _SUPPORTED_TEMPLATES:
        raise ValueError(f"unsupported onboarding template: {selected}")

    profile = _profile_for_template(inspection, selected)
    diagnostics = _diagnostics_for(inspection, profile=profile, template=selected)
    return DraftProfile(
        template=selected,
        profile=profile,
        yaml=_profile_yaml(profile),
        diagnostics=diagnostics,
    )


def preview_project_onboarding(
    path: Path,
    template: str = "auto",
    include_smoke_request: bool = False,
) -> ProjectOnboardingPreview:
    """Return an onboarding preview without writing files or launching workspaces."""
    inspection = inspect_project(path)
    draft = draft_workspace_profile(inspection, template=template)
    smoke_request = (
        _smoke_request(inspection.path, draft.profile) if include_smoke_request else None
    )
    return ProjectOnboardingPreview(
        path=inspection.path,
        inspection=inspection,
        draft=draft,
        diagnostics=draft.diagnostics,
        smoke_request=smoke_request,
    )


def supported_onboarding_templates() -> tuple[str, ...]:
    """Return supported project onboarding template names."""
    return tuple(sorted(_SUPPORTED_TEMPLATES))


def customize_project_onboarding_preview(
    preview: ProjectOnboardingPreview,
    *,
    egress_mode: str | EgressMode | None = None,
    open_explanation: str | None = None,
    validation_commands: Sequence[str] | None = None,
) -> ProjectOnboardingPreview:
    """Return a preview with safe first-run profile choices applied."""
    profile = preview.draft.profile.model_copy(deep=True)

    if egress_mode is not None:
        selected_mode = EgressMode(egress_mode)
        if selected_mode == EgressMode.restricted:
            egress = profile.security.egress.model_copy(
                update={
                    "mode": selected_mode,
                    "allowlist_templates": list(_DEFAULT_RESTRICTED_ALLOWLIST_TEMPLATES),
                    "open_explanation": None,
                }
            )
        else:
            egress = profile.security.egress.model_copy(
                update={
                    "mode": selected_mode,
                    "allowlist_templates": [],
                    "open_explanation": open_explanation
                    if selected_mode == EgressMode.open
                    else None,
                }
            )
        profile = profile.model_copy(
            update={"security": profile.security.model_copy(update={"egress": egress})}
        )

    if validation_commands is not None:
        clean_commands = [command.strip() for command in validation_commands if command.strip()]
        if clean_commands:
            profile = profile.with_validation_commands(clean_commands)
        else:
            profile = profile.clear_validation_commands()

    diagnostics = _diagnostics_for(
        preview.inspection,
        profile=profile,
        template=preview.draft.template,
    )
    draft = DraftProfile(
        template=preview.draft.template,
        profile=profile,
        yaml=_profile_yaml(profile),
        diagnostics=diagnostics,
    )
    smoke_request = (
        _smoke_request(preview.path, profile) if preview.smoke_request is not None else None
    )
    return ProjectOnboardingPreview(
        path=preview.path,
        inspection=preview.inspection,
        draft=draft,
        diagnostics=diagnostics,
        smoke_request=smoke_request,
    )


def preview_workspace_profile(
    project: Path,
    resolution: ProfileResolution,
) -> ProjectOnboardingPreview:
    """Construct an onboarding preview from a resolved on-disk workspace profile."""
    existing = inspect_project(project)
    inspection = ProjectInspection(
        path=project,
        detected_template=resolution.profile.name,
        confidence=resolution.profile.confidence,
        signals=existing.signals,
        package_manager=existing.package_manager,
        package_scripts=existing.package_scripts,
        package_dependencies=existing.package_dependencies,
        compose_file=existing.compose_file,
        compose_services=existing.compose_services,
        service_directories=existing.service_directories,
    )
    source = resolution.profile.source or ""
    if source.startswith("repo:"):
        rel = source[len("repo:") :]
        project_root = project.resolve()
        candidate = (project_root / rel).resolve()
        if (
            rel in PROFILE_MARKER_PATHS
            and candidate.is_file()
            and candidate.is_relative_to(project_root)
        ):
            profile_path: Path | None = candidate
        else:
            profile_path = None
    else:
        profile_path = None
    # Keep a separate read for the raw YAML preview payload that smoke reports
    # should emit. This intentionally duplicates I/O from resolver parsing; if
    # the file changes between reads, the parsed model and raw YAML can diverge.
    draft_yaml = profile_path.read_text(encoding="utf-8") if profile_path is not None else ""
    if not draft_yaml:
        draft_yaml = _profile_yaml(resolution.profile)

    diagnostics = _diagnostics_for(
        inspection,
        profile=resolution.profile,
        template=resolution.profile.name,
    )
    draft = DraftProfile(
        template=resolution.profile.name,
        profile=resolution.profile,
        yaml=draft_yaml,
        diagnostics=diagnostics,
    )
    return ProjectOnboardingPreview(
        path=project,
        inspection=inspection,
        draft=draft,
        diagnostics=draft.diagnostics,
    )


def write_workspace_profile(preview: ProjectOnboardingPreview, force: bool = False) -> Path:
    """Write the previewed draft to ``.awf/workspace.yml``."""
    profile_path = preview.path / ".awf" / "workspace.yml"
    existing_profile_path = _existing_workspace_profile_path(preview.path)
    if existing_profile_path is None and profile_path.exists():
        existing_profile_path = profile_path
    if existing_profile_path is not None and not force:
        raise FileExistsError(f"{existing_profile_path} already exists; pass --force to overwrite")
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(preview.draft.yaml, encoding="utf-8")
    return profile_path


def _existing_workspace_profile_path(project: Path) -> Path | None:
    """Return the first supported workspace profile marker path on disk."""
    for relative_path in PROFILE_MARKER_PATHS:
        candidate = project / relative_path
        if candidate.is_file():
            return candidate
    return None


def _profile_for_template(inspection: ProjectInspection, template: str) -> WorkspaceProfile:
    """Build a draft workspace profile for the selected onboarding template."""
    if template == "generic":
        profile = WorkspaceProfile(
            name="generic",
            source="onboarding:generic",
            confidence="low",
            description="Draft generic AWF profile. Add project setup and validation commands.",
        )
    elif template == "python":
        profile = WorkspaceProfile(
            name="python",
            source="onboarding:python",
            confidence="medium",
            description="Draft Python project profile.",
            phases={
                "setup": [_python_setup_command(inspection.path)],
                "validate": ["pytest -q"],
            },
        )
    elif template == "node-nextjs":
        profile = _node_profile(
            inspection,
            name="node-nextjs",
            description="Draft Node/Next.js project profile.",
            validation_scripts=("lint", "test", "build"),
            fallback_validation=("build",),
        )
    elif template == "node-playwright":
        profile = _node_profile(
            inspection,
            name="node-playwright",
            description="Draft Node/Playwright browser-test project profile.",
            validation_scripts=_playwright_validation_scripts(inspection),
            fallback_validation=(),
            fallback_commands=(_playwright_command(inspection.package_manager or "npm"),),
        )
    elif template == "python-postgres":
        database_url = "postgresql+psycopg://awf:${POSTGRES_PASSWORD}@postgres:5432/awf"
        setup = [_python_setup_command(inspection.path)]
        if (inspection.path / "alembic.ini").is_file():
            setup.append(ProfileCommand(command="alembic upgrade head"))
        profile = WorkspaceProfile(
            name="python-postgres",
            source="onboarding:python-postgres",
            confidence="medium",
            description="Draft Python profile with a Postgres sidecar.",
            runtime=ProfileRuntime(environment={"DATABASE_URL": database_url}),
            services=[
                ProfileService(
                    name="postgres",
                    image="postgres:16-alpine",
                    environment={
                        "POSTGRES_DB": "awf",
                        "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD}",
                        "POSTGRES_USER": "awf",
                    },
                    healthcheck_cmd="pg_isready -U awf -d awf",
                    volumes=[("postgres_data", "/var/lib/postgresql/data")],
                )
            ],
            phases={"setup": setup, "validate": ["pytest -q"]},
            validation=ProfileValidation(
                healthchecks=[
                    ProfileHealthCheck(
                        name="postgres",
                        command="pg_isready -h postgres -U awf -d awf",
                    )
                ],
                requested_tier=1,
            ),
            secrets=[
                ProfileSecret(
                    name="postgres-password",
                    target="POSTGRES_PASSWORD",
                    kind="env",
                )
            ],
        )
    elif template in {"docker-compose", "multi-service"}:
        profile = WorkspaceProfile(
            name=template,
            source=f"onboarding:{template}",
            confidence="medium",
            description="Draft Docker Compose profile running project services inside DinD.",
            docker=ProfileDocker(
                mode=DockerMode.dind,
                compose_files=[inspection.compose_file] if inspection.compose_file else [],
            ),
            runtime=ProfileRuntime(environment={"DOCKER_HOST": "tcp://docker:2375"}),
            phases={
                "setup": ["docker compose up -d --wait"],
                "cleanup": ["docker compose down -v --remove-orphans"],
            },
            ports=_compose_ports(inspection),
        )
    else:
        raise ValueError(f"unsupported onboarding template: {template}")

    return _ensure_restricted_egress(profile)


def _ensure_restricted_egress(profile: WorkspaceProfile) -> WorkspaceProfile:
    """Enforce restricted egress defaults on profiles that opt into egress control."""
    if profile.security.egress.mode == EgressMode.restricted:
        profile.security.egress.mode = EgressMode.restricted
        profile.security.egress.allowlist_templates = list(_DEFAULT_RESTRICTED_ALLOWLIST_TEMPLATES)
    return profile


def _node_profile(
    inspection: ProjectInspection,
    *,
    name: str,
    description: str,
    validation_scripts: tuple[str, ...],
    fallback_validation: tuple[str, ...],
    fallback_commands: tuple[str, ...] = (),
) -> WorkspaceProfile:
    """Build a node-based profile including install and validation command derivation."""
    package_manager = inspection.package_manager or "npm"
    scripts = inspection.scripts()
    validate_commands = [
        _script_command(package_manager, script)
        for script in validation_scripts
        if script in scripts
    ]
    if not validate_commands:
        validate_commands.extend(
            _script_command(package_manager, script)
            for script in fallback_validation
            if script in {"lint", "test", "build"}
        )
    if not validate_commands:
        validate_commands.extend(fallback_commands)
    return WorkspaceProfile(
        name=name,
        source=f"onboarding:{name}",
        confidence="medium",
        description=description,
        phases={
            "setup": [_node_install_command(inspection.path, package_manager)],
            "validate": validate_commands,
        },
    )


def _diagnostics_for(
    inspection: ProjectInspection,
    *,
    profile: WorkspaceProfile,
    template: str,
) -> PreviewDiagnostics:
    """Compute missing onboarding signal diagnostics from inspection and template."""
    declared_secrets = {
        item for secret in profile.secrets for item in (secret.name, secret.target) if item
    }
    missing_services: list[str] = []
    if template == "node-playwright":
        missing_services.append("browser")
    if template == "multi-service" and not inspection.compose_file:
        missing_services.extend(inspection.service_directories)

    missing_secrets: list[str] = []
    missing_ports: list[str] = []
    missing_healthchecks: list[str] = []
    if profile.docker.mode == DockerMode.dind:
        missing_secrets = sorted(
            {
                secret_name
                for service in inspection.compose_services
                for secret_name in service.secret_names
                if secret_name not in declared_secrets
            }
        )
        missing_ports = [
            service.name
            for service in inspection.compose_services
            if _compose_service_endpoint(service) is None
        ]
        missing_healthchecks = [
            service.name for service in inspection.compose_services if not service.has_healthcheck
        ]

    if template == "node-playwright":
        if not profile.ports:
            missing_ports.append("app")
        if not profile.validation.healthchecks:
            missing_healthchecks.append("app")

    missing_validation_commands = []
    if not profile.phases.validate_commands:
        missing_validation_commands.append(
            "No validation command was detected; add lint, test, or build commands."
        )

    return PreviewDiagnostics(
        missing_services=_dedupe_sorted(missing_services),
        missing_secrets=tuple(missing_secrets),
        missing_ports=_dedupe_sorted(missing_ports),
        missing_validation_commands=tuple(missing_validation_commands),
        missing_healthchecks=_dedupe_sorted(missing_healthchecks),
    )


def _profile_yaml(profile: WorkspaceProfile) -> str:
    """Render a workspace profile to the canonical on-disk YAML layout."""
    payload = {
        "awf": profile.model_dump(mode="json", by_alias=True, exclude_none=True),
    }
    return yaml.safe_dump(payload, sort_keys=False)


def _smoke_request(path: Path, profile: WorkspaceProfile) -> dict[str, object]:
    """Build a smoke test request payload from a workspace profile."""
    validation_commands = [command.command for command in profile.phases.validate_commands]
    return {
        "repo": {"url": path.as_uri(), "base_branch": "main"},
        "task": {
            "title": "Smoke AWF project onboarding profile",
            "prompt": "Run a no-op smoke pass that validates the drafted AWF project profile.",
            "agent": "codex",
            "kind": "feature_branch_pr",
            "auto_merge": False,
        },
        "workspace": {
            "profile_ref": None,
            "profile": profile.model_dump(mode="json", by_alias=True, exclude_none=True),
        },
        "validation": {"commands": validation_commands, "requested_tier": 1},
        "resources": {},
    }


def _read_package_json(path: Path) -> Mapping[str, object]:
    """Read `package.json` as a JSON mapping when available."""
    if not path.is_file():
        return {}
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if isinstance(raw, Mapping):
        return {str(key): value for key, value in raw.items()}
    return {}


def _string_mapping(value: object) -> dict[str, str]:
    """Normalize mapping-like values to a string-string dictionary."""
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }


def _package_dependency_names(package_data: Mapping[str, object]) -> set[str]:
    """Collect unique dependency names from standard package metadata sections."""
    dependency_names: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        value = package_data.get(key)
        if isinstance(value, Mapping):
            dependency_names.update(str(name) for name in value if isinstance(name, str))
    return dependency_names


def _detect_package_manager(root: Path) -> str:
    """Detect the most specific JavaScript package manager from lock files."""
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    if (root / "bun.lockb").is_file() or (root / "bun.lock").is_file():
        return "bun"
    return "npm"


def _detect_compose_file(root: Path) -> str | None:
    """Return the first matching docker compose filename for this project."""
    for name in _COMPOSE_FILENAMES:
        if (root / name).is_file():
            return name
    return None


def _inspect_compose_services(path: Path) -> tuple[ComposeServiceInspection, ...]:
    """Parse compose services safely and return inspected metadata for discovered services."""
    try:
        raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return ()
    if not isinstance(raw, Mapping):
        return ()
    services = raw.get("services")
    if not isinstance(services, Mapping):
        return ()

    inspected: list[ComposeServiceInspection] = []
    for name, value in services.items():
        if not isinstance(name, str):
            continue
        service = value if isinstance(value, Mapping) else {}
        inspected.append(
            ComposeServiceInspection(
                name=name,
                has_healthcheck=_compose_service_has_healthcheck(service),
                ports=_compose_service_ports(service),
                secret_names=_compose_service_secret_names(service),
            )
        )
    return tuple(inspected)


def _compose_service_has_healthcheck(service: Mapping[object, object]) -> bool:
    """Return True when a compose service defines an active healthcheck block."""
    healthcheck = service.get("healthcheck")
    return healthcheck is not None and not (
        isinstance(healthcheck, Mapping) and healthcheck.get("disable") is True
    )


def _compose_service_ports(service: Mapping[object, object]) -> tuple[str, ...]:
    """Extract rendered compose port entries from service configuration."""
    ports = service.get("ports")
    if not isinstance(ports, Sequence) or isinstance(ports, str):
        return ()
    rendered: list[str] = []
    for port in ports:
        if isinstance(port, Mapping):
            target = port.get("target")
            published = port.get("published")
            if target is None:
                continue
            rendered.append(f"{published}:{target}" if published is not None else str(target))
        else:
            rendered.append(str(port))
    return tuple(rendered)


def _compose_service_secret_names(service: Mapping[object, object]) -> tuple[str, ...]:
    """Collect secret-like variable names referenced by a compose service."""
    names: set[str] = set()
    environment = service.get("environment")
    if isinstance(environment, Mapping):
        for key, value in environment.items():
            if isinstance(key, str) and value in (None, "") and _looks_like_secret_name(key):
                names.add(key)
            if isinstance(value, str):
                names.update(_secret_placeholders(value))
    elif isinstance(environment, Sequence) and not isinstance(environment, str):
        for item in environment:
            if not isinstance(item, str):
                continue
            names.update(_secret_placeholders(item))
            key = item.split("=", 1)[0]
            if "=" not in item and _looks_like_secret_name(key):
                names.add(key)

    for key in ("env_file", "secrets"):
        value = service.get(key)
        if isinstance(value, str):
            names.update(_secret_placeholders(value))
        elif isinstance(value, Sequence) and not isinstance(value, str):
            for item in value:
                if isinstance(item, str):
                    names.update(_secret_placeholders(item))
    return tuple(sorted(names))


def _secret_placeholders(value: str) -> set[str]:
    """Extract `${VAR}`-style placeholder names from an environment token."""
    return {name for name in _PLACEHOLDER_RE.findall(value) if _looks_like_secret_name(name)}


def _looks_like_secret_name(name: str) -> bool:
    """Heuristically classify a token as likely representing a secret."""
    return bool(_SECRET_NAME_RE.search(name))


def _has_python_signal(root: Path) -> bool:
    """Check for common Python project signals in a repository root."""
    return any(
        (root / name).is_file()
        for name in ("pyproject.toml", "requirements.txt", "uv.lock", "pytest.ini")
    )


def _has_postgres_signal(root: Path) -> bool:
    """Check for Postgres or database hints from common project files."""
    if (root / "alembic.ini").is_file():
        return True
    for name in ("pyproject.toml", "requirements.txt", ".env", ".env.example"):
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8").lower()
        except (OSError, UnicodeDecodeError):
            continue
        if any(signal in text for signal in ("psycopg", "asyncpg", "postgres", "database_url")):
            return True
    return False


def _python_setup_command(root: Path) -> ProfileCommand:
    """Choose a default Python setup command for onboarding profile generation."""
    if (root / "requirements.txt").is_file() and not (root / "pyproject.toml").is_file():
        return ProfileCommand(command="python -m pip install -r requirements.txt")
    if (root / "uv.lock").is_file():
        return ProfileCommand(command="uv sync --extra dev", timeout_seconds=900)
    return ProfileCommand(command='uv pip install -e ".[dev]"', timeout_seconds=900)


def _node_install_command(root: Path, package_manager: str) -> str:
    """Resolve the preferred install command for Node toolchains."""
    if package_manager == "pnpm":
        return "pnpm install --frozen-lockfile"
    if package_manager == "yarn":
        return "yarn install --frozen-lockfile"
    if package_manager == "bun":
        return "bun install --frozen-lockfile"
    if (root / "package-lock.json").is_file():
        return "npm ci"
    return "npm install"


def _script_command(package_manager: str, script: str) -> str:
    """Translate npm-style scripts to package-manager-specific command invocations."""
    if package_manager == "pnpm":
        return "pnpm test" if script == "test" else f"pnpm run {script}"
    if package_manager == "yarn":
        return f"yarn {script}"
    if package_manager == "bun":
        return "bun test" if script == "test" else f"bun run {script}"
    return "npm test" if script == "test" else f"npm run {script}"


def _playwright_validation_scripts(inspection: ProjectInspection) -> tuple[str, ...]:
    """Detect package scripts suitable for playwright-driven validation."""
    scripts = inspection.scripts()
    for name in ("test:e2e", "e2e", "playwright", "test"):
        command = scripts.get(name)
        if command is not None and "playwright" in command:
            return (name,)
    return ()


def _playwright_command(package_manager: str) -> str:
    """Build the package-manager-specific Playwright command for one-shot validation."""
    if package_manager == "pnpm":
        return "pnpm exec playwright test"
    if package_manager == "yarn":
        return "yarn playwright test"
    if package_manager == "bun":
        return "bunx playwright test"
    return "npx playwright test"


def _compose_ports(inspection: ProjectInspection) -> dict[str, str]:
    """Compute expected service endpoint URLs for discovered compose services."""
    ports: dict[str, str] = {}
    for service in inspection.compose_services:
        endpoint = _compose_service_endpoint(service)
        if endpoint is not None:
            ports[service.name] = endpoint
    return ports


def _compose_service_endpoint(service: ComposeServiceInspection) -> str | None:
    """Build an endpoint URL for a compose service based on exposed port and protocol."""
    port = _first_container_port(service.ports)
    if port is None:
        return None
    scheme = _compose_web_port_scheme(port)
    if scheme is None:
        return None
    return f"{scheme}://{service.name}:{port}"


def _compose_web_port_scheme(port: str) -> str | None:
    """Map common container ports to an inferred web scheme."""
    try:
        container_port = int(port)
    except ValueError:
        return None
    if container_port in _HTTPS_COMPOSE_PORTS:
        return "https"
    if container_port in _HTTP_COMPOSE_PORTS:
        return "http"
    return None


def _first_container_port(ports: tuple[str, ...]) -> str | None:
    """Return the first numeric container port found in service port declarations."""
    for port in ports:
        numbers: list[str] = re.findall(r"\d+", port)
        if numbers:
            return numbers[-1]
    return None


def _next_configs() -> tuple[str, ...]:
    """Return candidate Next.js config filenames."""
    return (
        "next.config.js",
        "next.config.mjs",
        "next.config.cjs",
        "next.config.ts",
    )


def _playwright_configs() -> tuple[str, ...]:
    """Return candidate Playwright config filenames."""
    return (
        "playwright.config.js",
        "playwright.config.mjs",
        "playwright.config.cjs",
        "playwright.config.ts",
    )


def _dedupe_sorted(values: list[str]) -> tuple[str, ...]:
    """Return sorted unique values from a list."""
    return tuple(sorted(set(values)))
