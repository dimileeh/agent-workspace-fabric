"""Secret-free AWF Core capability discovery payload."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field, fields
from pathlib import Path

from pydantic import BaseModel

from awf import __version__
from awf.profiles.capabilities import ProfileCapabilities
from awf.runtime.driver import WORKSPACE_EXECUTION_V1

PACKAGE_NAME = "agent-workspace-fabric"
UNKNOWN_GIT_COMMIT = "unknown"
CORE_DISCOVERY_STATE_ATTR = "core_discovery_payload"

# Public-safe profile capability flag names exposed through Core discovery.
# Derived from ``ProfileCapabilities`` field declarations so the tuple cannot
# drift from the classifier's dataclass. Ordering is the stable wire contract
# consumed by `awf-cloud`; do not reorder the dataclass fields.
PROFILE_CAPABILITY_FLAGS: tuple[str, ...] = tuple(f.name for f in fields(ProfileCapabilities))

# Cloud-neutral rule summary describing the classification contract. Core keeps
# this text public-safe and free of cloud product policy; `awf-cloud` interprets
# per-profile capability payloads against this contract.
PROFILE_CAPABILITY_RULE_SUMMARY = (
    "autopilot_supported is false when a profile requires a host Docker daemon "
    "(docker.mode != none), declares any privileged service, binds any host "
    "ports, declares any service with a build_context (a Compose build: stanza "
    "requires a Docker builder/daemon), or relies on Compose-only orchestration "
    "(compose_files non-empty) or Docker-in-Docker (docker.mode == dind). "
    "Otherwise the profile is Autopilot-supported."
)


class ProfileCapabilitySchema(BaseModel):
    """Public-safe description of the profile capability classification contract.

    Surfaced through Core discovery so a cloud substrate can interpret
    per-profile capability payloads without Core embedding cloud product
    policy. Carries no secrets.
    """

    flags: list[str]
    rule_summary: str


def profile_capability_schema() -> ProfileCapabilitySchema:
    """Return the stable, cloud-neutral profile capability classification schema."""
    return ProfileCapabilitySchema(
        flags=list(PROFILE_CAPABILITY_FLAGS),
        rule_summary=PROFILE_CAPABILITY_RULE_SUMMARY,
    )


class CoreDiscoveryResponse(BaseModel):
    """Public, secret-free Core discovery response."""

    package_name: str
    package_version: str
    git_commit: str
    capabilities: list[str]
    profile_capability_schema: ProfileCapabilitySchema


@dataclass(frozen=True)
class CoreDiscoveryPayload:
    """Internal immutable discovery payload before API serialization."""

    package_name: str = PACKAGE_NAME
    package_version: str = __version__
    git_commit: str = UNKNOWN_GIT_COMMIT
    capabilities: tuple[str, ...] = (WORKSPACE_EXECUTION_V1,)
    profile_capability_schema: ProfileCapabilitySchema = field(
        default_factory=profile_capability_schema
    )

    def to_response(self) -> CoreDiscoveryResponse:
        """Serialize this payload into the public API response model."""
        return CoreDiscoveryResponse(
            package_name=self.package_name,
            package_version=self.package_version,
            git_commit=self.git_commit,
            capabilities=list(self.capabilities),
            profile_capability_schema=self.profile_capability_schema,
        )


def build_core_discovery_payload() -> CoreDiscoveryPayload:
    """Build the stable public Core discovery payload without reading secrets."""

    return CoreDiscoveryPayload(git_commit=_resolve_git_commit())


def core_discovery_payload_from_state(app_state: object) -> CoreDiscoveryPayload:
    """Return the cached discovery payload without resolving git during requests."""

    payload = getattr(app_state, CORE_DISCOVERY_STATE_ATTR, None)
    if isinstance(payload, CoreDiscoveryPayload):
        return payload
    return CoreDiscoveryPayload()


def _resolve_git_commit() -> str:
    """Resolve the Core git commit from env override or the checkout HEAD."""
    env_commit = os.environ.get("AWF_GIT_COMMIT")
    if env_commit is not None and env_commit.strip():
        return env_commit.strip()
    return _git_rev_parse_head() or UNKNOWN_GIT_COMMIT


def _core_source_checkout_root() -> Path | None:
    """Return the AWF Core checkout root when running from a source tree."""
    module_file = Path(__file__).resolve()
    try:
        package_dir = module_file.parents[1]
        checkout_root = module_file.parents[3]
    except IndexError:  # pragma: no cover - supported module paths are nested under awf/service.
        return None
    if package_dir != (checkout_root / "src" / "awf").resolve():
        return None
    if not (checkout_root / ".git").exists():
        return None
    return checkout_root


def _git_rev_parse_head() -> str | None:
    """Return ``git rev-parse HEAD`` for the Core checkout when available."""
    checkout_root = _core_source_checkout_root()
    if checkout_root is None:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout_root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    commit = result.stdout.strip()
    return commit or None
