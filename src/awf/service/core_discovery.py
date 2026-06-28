"""Secret-free AWF Core capability discovery payload."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from pydantic import BaseModel

from awf import __version__
from awf.runtime.driver import WORKSPACE_EXECUTION_V1

PACKAGE_NAME = "agent-workspace-fabric"
UNKNOWN_GIT_COMMIT = "unknown"
CORE_DISCOVERY_STATE_ATTR = "core_discovery_payload"


class CoreDiscoveryResponse(BaseModel):
    """Public, secret-free Core discovery response."""

    package_name: str
    package_version: str
    git_commit: str
    capabilities: list[str]


@dataclass(frozen=True)
class CoreDiscoveryPayload:
    """Internal immutable discovery payload before API serialization."""

    package_name: str = PACKAGE_NAME
    package_version: str = __version__
    git_commit: str = UNKNOWN_GIT_COMMIT
    capabilities: tuple[str, ...] = (WORKSPACE_EXECUTION_V1,)

    def to_response(self) -> CoreDiscoveryResponse:
        return CoreDiscoveryResponse(
            package_name=self.package_name,
            package_version=self.package_version,
            git_commit=self.git_commit,
            capabilities=list(self.capabilities),
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
    env_commit = os.environ.get("AWF_GIT_COMMIT")
    if env_commit is not None and env_commit.strip():
        return env_commit.strip()
    return _git_rev_parse_head() or UNKNOWN_GIT_COMMIT


def _git_rev_parse_head() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
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
