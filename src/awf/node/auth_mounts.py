"""Auth mount resolution for service-created workspace stacks."""

from __future__ import annotations

import os
import shutil
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from awf.node.compose_manager import AuthMount

_CONTAINER_HOME = "/home/agent"
_GH_CONFIG_TARGET = f"{_CONTAINER_HOME}/.config/gh"
_GCLOUD_CONFIG_TARGET = f"{_CONTAINER_HOME}/.config/gcloud"
_GITCONFIG_TARGET = f"{_CONTAINER_HOME}/.gitconfig"
_SSH_TARGET = f"{_CONTAINER_HOME}/.ssh"
_CODEX_TARGET = f"{_CONTAINER_HOME}/.codex"
_CLAUDE_DIR_TARGET = f"{_CONTAINER_HOME}/.claude"
_CLAUDE_FILE_TARGET = f"{_CONTAINER_HOME}/.claude.json"
_GEMINI_TARGET = f"{_CONTAINER_HOME}/.gemini"
_OPENCODE_TARGET = f"{_CONTAINER_HOME}/.config/opencode"
_GROK_TARGET = f"{_CONTAINER_HOME}/.grok"
_OLLAMA_TARGET = f"{_CONTAINER_HOME}/.ollama"
_GROK_AUTH_FILES = ("auth.json", "config.toml")
_OLLAMA_AUTH_FILES = frozenset(("config.json", "id_ed25519", "id_ed25519.pub"))
# Per-workspace Claude/Gemini auth copies must exclude historical usage/transcript
# dirs. Otherwise ``ccusage`` (which reads these local files) would attribute the
# host's prior usage to the workspace run; baseline/delta still guards totals, but
# excluding the copy keeps the workspace from seeing unrelated host transcripts and
# avoids copying potentially large history trees.
_CLAUDE_USAGE_HISTORY_DIRS = ("projects", "todos", "shell-snapshots", "statsig")
_GEMINI_USAGE_HISTORY_DIRS = ("tmp",)
_LEGACY_PROVIDER_TARGETS: Mapping[str, frozenset[str]] = {
    "github": frozenset({_GH_CONFIG_TARGET}),
}


class WorkspaceAuthMountResolver(Protocol):
    """Resolves per-workspace auth mounts for an agent container."""

    def resolve(
        self,
        *,
        workspace_id: str,
        suppressed_targets: Collection[str] = (),
        suppressed_providers: Collection[str] = (),
    ) -> tuple[AuthMount, ...]: ...


@dataclass(frozen=True)
class ServiceAuthMountResolver:
    """Resolve host credentials for workspace stacks launched by the service worker."""

    host_home: Path
    work_dir: Path
    host_env: Mapping[str, str] | None = None
    workspace_owner_uid: int | None = None
    workspace_owner_gid: int | None = None

    def resolve(
        self,
        *,
        workspace_id: str,
        suppressed_targets: Collection[str] = (),
        suppressed_providers: Collection[str] = (),
    ) -> tuple[AuthMount, ...]:
        return resolve_service_auth_mounts(
            host_home=self.host_home,
            work_dir=self.work_dir,
            workspace_id=workspace_id,
            host_env=self.host_env,
            suppressed_targets=suppressed_targets,
            suppressed_providers=suppressed_providers,
            workspace_owner_uid=self.workspace_owner_uid,
            workspace_owner_gid=self.workspace_owner_gid,
        )


def resolve_service_auth_mounts(
    *,
    host_home: Path,
    work_dir: Path,
    workspace_id: str,
    host_env: Mapping[str, str] | None = None,
    suppressed_targets: Collection[str] = (),
    suppressed_providers: Collection[str] = (),
    workspace_owner_uid: int | None = None,
    workspace_owner_gid: int | None = None,
) -> tuple[AuthMount, ...]:
    """Return host auth mounts for one service-created workspace.

    The returned ``source`` paths are host-visible paths. In local Docker
    Compose service mode, ``docker/compose/local-service.yml`` mounts
    ``AWF_HOST_HOME`` at that same absolute path inside the worker container so
    the resolver can check and copy credential files while the host Docker daemon
    can later bind-mount the same sources into the agent container.
    """

    normalized_home = host_home.expanduser()
    suppressed_target_set = frozenset(suppressed_targets) | legacy_provider_targets(
        suppressed_providers
    )
    base_mounts = _build_host_auth_mounts(
        normalized_home,
        host_env=host_env,
        suppressed_targets=suppressed_target_set,
    )
    return _workspace_auth_mounts(
        base_mounts,
        workspace_id=workspace_id,
        work_dir=work_dir.expanduser(),
        host_home=normalized_home,
        suppressed_targets=suppressed_target_set,
        workspace_owner_uid=workspace_owner_uid,
        workspace_owner_gid=workspace_owner_gid,
    )


def _build_host_auth_mounts(
    host_home: Path,
    *,
    host_env: Mapping[str, str] | None = None,
    suppressed_targets: Collection[str] = (),
) -> list[AuthMount]:
    ro_mounts = [
        (host_home / ".config" / "gh", _GH_CONFIG_TARGET, "ro"),
        (host_home / ".config" / "gcloud", _GCLOUD_CONFIG_TARGET, "ro"),
        (host_home / ".gitconfig", _GITCONFIG_TARGET, "ro"),
        (host_home / ".ssh", _SSH_TARGET, "ro"),
    ]
    mounts = [
        AuthMount(source=str(src), target=target, mode=mode)
        for src, target, mode in ro_mounts
        if target not in suppressed_targets and src.exists()
    ]

    source_env = os.environ if host_env is None else host_env
    google_credentials = source_env.get("GOOGLE_APPLICATION_CREDENTIALS")
    if google_credentials:
        credentials_path = Path(google_credentials).expanduser()
        credentials_target = str(credentials_path)
        if credentials_target not in suppressed_targets and credentials_path.exists():
            mounts.append(
                AuthMount(
                    source=str(credentials_path),
                    target=credentials_target,
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
    suppressed_targets: Collection[str] = (),
    workspace_owner_uid: int | None = None,
    workspace_owner_gid: int | None = None,
) -> tuple[AuthMount, ...]:
    auth_root = work_dir / "auth" / workspace_id
    mounts = []
    if _CODEX_TARGET not in suppressed_targets:
        codex_home = _prepare_isolated_codex_home(
            host_home=host_home,
            target_root=auth_root / "codex",
        )
        if codex_home is not None:
            mounts.append(
                AuthMount(
                    source=str(codex_home),
                    target=_CODEX_TARGET,
                    mode="rw",
                ),
            )
    mounts.extend(
        _prepare_isolated_claude_auth(
            host_home=host_home,
            target_root=auth_root / "claude",
            suppressed_targets=suppressed_targets,
        )
    )
    if _GEMINI_TARGET not in suppressed_targets:
        mounts.extend(
            _prepare_isolated_gemini_auth(
                host_home=host_home,
                target_root=auth_root / "gemini",
            )
        )
    if _OPENCODE_TARGET not in suppressed_targets:
        mounts.extend(
            _prepare_isolated_opencode_auth(
                host_home=host_home,
                target_root=auth_root / "opencode",
            )
        )
    if _GROK_TARGET not in suppressed_targets:
        mounts.extend(
            _prepare_isolated_grok_auth(
                host_home=host_home,
                target_root=auth_root / "grok",
            )
        )
    if _OLLAMA_TARGET not in suppressed_targets:
        mounts.extend(
            _prepare_isolated_ollama_auth(
                host_home=host_home,
                target_root=auth_root / "ollama",
            )
        )
    _chown_workspace_auth_sources(
        mounts,
        uid=workspace_owner_uid,
        gid=workspace_owner_gid,
    )
    mounts.extend(base_mounts)
    return tuple(mounts)


def _chown_workspace_auth_sources(
    mounts: Sequence[AuthMount],
    *,
    uid: int | None,
    gid: int | None,
) -> None:
    if uid is None and gid is None:
        return
    if uid is None or gid is None:
        raise ValueError("workspace auth ownership requires both uid and gid")

    for mount in mounts:
        if mount.mode != "rw":
            continue
        _chown_tree(Path(mount.source), uid, gid)


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    if path.is_symlink():
        os.lchown(path, uid, gid)
        return

    os.chown(path, uid, gid)
    if not path.is_dir():
        return

    for root, dirs, files in os.walk(path, followlinks=False):
        for name in (*dirs, *files):
            child = Path(root) / name
            if child.is_symlink():
                os.lchown(child, uid, gid)
            else:
                os.chown(child, uid, gid)


def legacy_provider_targets(providers: Collection[str]) -> frozenset[str]:
    """Return legacy mount targets covered by provider-level suppression."""

    targets: set[str] = set()
    for provider in providers:
        targets.update(_LEGACY_PROVIDER_TARGETS.get(provider, ()))
    return frozenset(targets)


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


def _prepare_isolated_claude_auth(
    *,
    host_home: Path,
    target_root: Path,
    suppressed_targets: Collection[str] = (),
) -> tuple[AuthMount, ...]:
    """Seed per-workspace Claude auth without sharing writable host files."""

    mounts: list[AuthMount] = []
    source_dir = host_home / ".claude"
    target_dir = target_root / ".claude"
    if _CLAUDE_DIR_TARGET not in suppressed_targets and source_dir.is_dir():
        target_root.mkdir(parents=True, exist_ok=True)
        if not target_dir.exists():
            shutil.copytree(
                source_dir,
                target_dir,
                ignore=shutil.ignore_patterns(*_CLAUDE_USAGE_HISTORY_DIRS),
                ignore_dangling_symlinks=True,
            )
        mounts.append(
            AuthMount(
                source=str(target_dir),
                target=_CLAUDE_DIR_TARGET,
                mode="rw",
            )
        )

    source_file = host_home / ".claude.json"
    target_file = target_root / ".claude.json"
    if _CLAUDE_FILE_TARGET not in suppressed_targets and source_file.is_file():
        target_root.mkdir(parents=True, exist_ok=True)
        if not target_file.exists():
            shutil.copy2(source_file, target_file)
        mounts.append(
            AuthMount(
                source=str(target_file),
                target=_CLAUDE_FILE_TARGET,
                mode="rw",
            )
        )

    return tuple(mounts)


def _prepare_isolated_gemini_auth(*, host_home: Path, target_root: Path) -> tuple[AuthMount, ...]:
    """Seed per-workspace Gemini auth without sharing writable host files."""

    source_dir = host_home / ".gemini"
    target_dir = target_root / ".gemini"
    if not source_dir.is_dir():
        return ()

    target_root.mkdir(parents=True, exist_ok=True)
    if not target_dir.exists():
        shutil.copytree(
            source_dir,
            target_dir,
            ignore=shutil.ignore_patterns(*_GEMINI_USAGE_HISTORY_DIRS),
        )

    return (
        AuthMount(
            source=str(target_dir),
            target=_GEMINI_TARGET,
            mode="rw",
        ),
    )


def _prepare_isolated_opencode_auth(
    *,
    host_home: Path,
    target_root: Path,
) -> tuple[AuthMount, ...]:
    """Seed per-workspace OpenCode config without sharing writable host files."""

    source_dir = host_home / ".config" / "opencode"
    target_dir = target_root / ".config" / "opencode"
    if not source_dir.is_dir():
        return ()

    target_root.mkdir(parents=True, exist_ok=True)
    if not target_dir.exists():
        shutil.copytree(source_dir, target_dir)

    return (
        AuthMount(
            source=str(target_dir),
            target=_OPENCODE_TARGET,
            mode="rw",
        ),
    )


def _prepare_isolated_grok_auth(
    *,
    host_home: Path,
    target_root: Path,
) -> tuple[AuthMount, ...]:
    """Seed per-workspace Grok auth without copying host runtime binaries."""

    source_dir = host_home / ".grok"
    if not (source_dir / "auth.json").is_file():
        return ()

    target_dir = target_root / ".grok"
    target_dir.mkdir(parents=True, exist_ok=True)
    for filename in _GROK_AUTH_FILES:
        src = source_dir / filename
        dst = target_dir / filename
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)

    return (
        AuthMount(
            source=str(target_dir),
            target=_GROK_TARGET,
            mode="rw",
        ),
    )


def _prepare_isolated_ollama_auth(
    *,
    host_home: Path,
    target_root: Path,
) -> tuple[AuthMount, ...]:
    """Seed per-workspace Ollama auth used by OpenCode/Ollama integration.

    Do not copy ``~/.ollama/models``; those blobs can be tens of gigabytes and
    the workspace reaches them through the host Ollama daemon instead.
    """

    source_dir = host_home / ".ollama"
    target_dir = target_root / ".ollama"
    if not source_dir.is_dir():
        return ()

    target_dir.mkdir(parents=True, exist_ok=True)
    for filename in _OLLAMA_AUTH_FILES:
        src = source_dir / filename
        dst = target_dir / filename
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)

    return (
        AuthMount(
            source=str(target_dir),
            target=_OLLAMA_TARGET,
            mode="rw",
        ),
    )
