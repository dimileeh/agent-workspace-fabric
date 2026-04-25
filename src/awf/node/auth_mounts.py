"""Auth mount resolution for service-created workspace stacks."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from awf.node.compose_manager import AuthMount

_CONTAINER_HOME = "/home/agent"


class WorkspaceAuthMountResolver(Protocol):
    """Resolves per-workspace auth mounts for an agent container."""

    def resolve(self, *, workspace_id: str) -> tuple[AuthMount, ...]: ...


@dataclass(frozen=True)
class ServiceAuthMountResolver:
    """Resolve host credentials for workspace stacks launched by the service worker."""

    host_home: Path
    work_dir: Path
    host_env: Mapping[str, str] | None = None

    def resolve(self, *, workspace_id: str) -> tuple[AuthMount, ...]:
        return resolve_service_auth_mounts(
            host_home=self.host_home,
            work_dir=self.work_dir,
            workspace_id=workspace_id,
            host_env=self.host_env,
        )


def resolve_service_auth_mounts(
    *,
    host_home: Path,
    work_dir: Path,
    workspace_id: str,
    host_env: Mapping[str, str] | None = None,
) -> tuple[AuthMount, ...]:
    """Return host auth mounts for one service-created workspace.

    The returned ``source`` paths are host-visible paths. In local Docker
    Compose service mode, ``docker/compose/local-service.yml`` mounts
    ``AWF_HOST_HOME`` at that same absolute path inside the worker container so
    the resolver can check and copy credential files while the host Docker daemon
    can later bind-mount the same sources into the agent container.
    """

    normalized_home = host_home.expanduser()
    base_mounts = _build_host_auth_mounts(normalized_home, host_env=host_env)
    return _workspace_auth_mounts(
        base_mounts,
        workspace_id=workspace_id,
        work_dir=work_dir.expanduser(),
        host_home=normalized_home,
    )


def _build_host_auth_mounts(
    host_home: Path,
    *,
    host_env: Mapping[str, str] | None = None,
) -> list[AuthMount]:
    rw_mounts = [
        (host_home / ".gemini", f"{_CONTAINER_HOME}/.gemini", "rw"),
    ]
    ro_mounts = [
        (host_home / ".config" / "gh", f"{_CONTAINER_HOME}/.config/gh", "ro"),
        (host_home / ".config" / "gcloud", f"{_CONTAINER_HOME}/.config/gcloud", "ro"),
        (host_home / ".gitconfig", f"{_CONTAINER_HOME}/.gitconfig", "ro"),
        (host_home / ".ssh", f"{_CONTAINER_HOME}/.ssh", "ro"),
    ]
    mounts = [
        AuthMount(source=str(src), target=target, mode=mode)
        for src, target, mode in [*rw_mounts, *ro_mounts]
        if src.exists()
    ]

    source_env = os.environ if host_env is None else host_env
    google_credentials = source_env.get("GOOGLE_APPLICATION_CREDENTIALS")
    if google_credentials:
        credentials_path = Path(google_credentials).expanduser()
        if credentials_path.exists():
            mounts.append(
                AuthMount(
                    source=str(credentials_path),
                    target=str(credentials_path),
                    mode="ro",
                )
            )

    return mounts


def _workspace_auth_mounts(
    base_mounts: Sequence[AuthMount],
    *,
    workspace_id: str,
    work_dir: Path,
    host_home: Path,
) -> tuple[AuthMount, ...]:
    auth_root = work_dir / "auth" / workspace_id
    mounts = []
    codex_home = _prepare_isolated_codex_home(
        host_home=host_home,
        target_root=auth_root / "codex",
    )
    if codex_home is not None:
        mounts.append(
            AuthMount(
                source=str(codex_home),
                target=f"{_CONTAINER_HOME}/.codex",
                mode="rw",
            ),
        )
    mounts.extend(
        _prepare_isolated_claude_auth(
            host_home=host_home,
            target_root=auth_root / "claude",
        )
    )
    mounts.extend(base_mounts)
    return tuple(mounts)


def _prepare_isolated_codex_home(*, host_home: Path, target_root: Path) -> Path | None:
    """Seed a per-workspace Codex home without sharing live runtime state."""

    source = host_home / ".codex"
    if not source.exists():
        return None

    target_root.mkdir(parents=True, exist_ok=True)

    for filename in ("auth.json", "config.toml", "installation_id"):
        src = source / filename
        dst = target_root / filename
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)

    rules = source / "rules"
    rules_target = target_root / "rules"
    if rules.is_dir() and not rules_target.exists():
        shutil.copytree(rules, target_root / "rules")

    return target_root


def _prepare_isolated_claude_auth(*, host_home: Path, target_root: Path) -> tuple[AuthMount, ...]:
    """Seed per-workspace Claude auth without sharing writable host files."""

    mounts: list[AuthMount] = []
    source_dir = host_home / ".claude"
    target_dir = target_root / ".claude"
    if source_dir.is_dir():
        target_root.mkdir(parents=True, exist_ok=True)
        if not target_dir.exists():
            shutil.copytree(source_dir, target_dir)
        mounts.append(
            AuthMount(
                source=str(target_dir),
                target=f"{_CONTAINER_HOME}/.claude",
                mode="rw",
            )
        )

    source_file = host_home / ".claude.json"
    target_file = target_root / ".claude.json"
    if source_file.is_file():
        target_root.mkdir(parents=True, exist_ok=True)
        if not target_file.exists():
            shutil.copy2(source_file, target_file)
        mounts.append(
            AuthMount(
                source=str(target_file),
                target=f"{_CONTAINER_HOME}/.claude.json",
                mode="rw",
            )
        )

    return tuple(mounts)
