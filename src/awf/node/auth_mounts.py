"""Auth mount resolution for service-created workspace stacks."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from awf.common.logging import get_logger
from awf.node.compose_manager import AuthMount

_log = get_logger(__name__)

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
_OLLAMA_TARGET = f"{_CONTAINER_HOME}/.ollama"
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

# Shared, read-only overlay base for ``~/.claude``. The bulk of ``~/.claude``
# (skills, plugins, static config) is identical across workspaces and read-only
# at runtime, so a single host-wide base snapshot is reused as the overlay
# ``lowerdir`` instead of copying ~1.7 GB per workspace. The base lives outside
# any ``auth/<workspace_id>`` dir so GC (which enumerates candidates from DB
# workspace rows) can never reap it.
_SHARED_AUTH_DIRNAME = "_shared"
_CLAUDE_BASE_DIRNAME = "claude-base"
_CLAUDE_AUTH_OVERLAY_UNAVAILABLE = "CLAUDE_AUTH_OVERLAY_UNAVAILABLE"
_CLAUDE_AUTH_SHARED_BASE_FAILED = "CLAUDE_AUTH_SHARED_BASE_FAILED"
_PROC_FILESYSTEMS = Path("/proc/filesystems")
_PROC_SELF_STATUS = Path("/proc/self/status")
_CAP_SYS_ADMIN_BIT = 21
_ISOLATION_OVERLAY = "per_workspace_overlay"
_ISOLATION_COPY = "per_workspace_copy"


def _overlay_filesystem_available(proc_filesystems: Path = _PROC_FILESYSTEMS) -> bool:
    """Return whether the kernel advertises overlayfs in ``/proc/filesystems``."""

    try:
        contents = proc_filesystems.read_text()
    except OSError:
        return False
    return any("overlay" in line.split() for line in contents.splitlines())


def _has_cap_sys_admin(proc_status: Path = _PROC_SELF_STATUS) -> bool:
    """Return whether the current process holds ``CAP_SYS_ADMIN`` (needed to mount)."""

    try:
        contents = proc_status.read_text()
    except OSError:
        return False
    for line in contents.splitlines():
        if not line.startswith("CapEff:"):
            continue
        try:
            caps = int(line.split(":", 1)[1].strip(), 16)
        except ValueError:
            return False
        return bool(caps & (1 << _CAP_SYS_ADMIN_BIT))
    return False


class OverlayMounter(Protocol):
    """Set up and tear down the per-workspace ``~/.claude`` overlay mount.

    Injected so unit tests can exercise the overlay/fallback/teardown branches
    without root or a real overlayfs kernel mount.
    """

    def supported(self) -> bool:
        """Return whether overlayfs can be mounted on this host."""
        ...  # pragma: no cover - Protocol method declaration only.

    def mount(self, *, lowerdir: Path, upperdir: Path, workdir: Path, merged: Path) -> None:
        """Mount the overlay at ``merged``; raise on failure."""
        ...  # pragma: no cover - Protocol method declaration only.

    def unmount(self, target: Path) -> None:
        """Unmount the overlay at ``target``; raise on failure."""
        ...  # pragma: no cover - Protocol method declaration only.

    def is_mounted(self, target: Path) -> bool:
        """Return whether ``target`` is currently an overlay mountpoint."""
        ...  # pragma: no cover - Protocol method declaration only.


@dataclass(frozen=True)
class _SubprocessOverlayMounter:
    """Default :class:`OverlayMounter` backed by ``mount(8)``/``umount(8)``."""

    timeout_seconds: float = 30.0

    def supported(self) -> bool:
        return _overlay_filesystem_available() and _has_cap_sys_admin()

    def mount(self, *, lowerdir: Path, upperdir: Path, workdir: Path, merged: Path) -> None:
        options = f"lowerdir={lowerdir},upperdir={upperdir},workdir={workdir}"
        subprocess.run(
            ["mount", "-t", "overlay", "overlay", "-o", options, str(merged)],
            check=True,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )

    def unmount(self, target: Path) -> None:
        subprocess.run(
            ["umount", str(target)],
            check=True,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )

    def is_mounted(self, target: Path) -> bool:
        return os.path.ismount(target)


def default_overlay_mounter() -> OverlayMounter:
    """Return the production overlay mounter (real ``mount``/``umount``)."""

    return _SubprocessOverlayMounter()


def claude_auth_isolation_label(
    *, overlay_filesystem_available: Callable[[], bool] | None = None
) -> str:
    """Return the isolation posture label for Claude file auth on this host.

    ``per_workspace_overlay`` when overlayfs is usable (shared read-only base +
    per-workspace writable upper), else ``per_workspace_copy`` (full per-workspace
    copy fallback).

    This describes the **worker's** posture, not the calling process's. The worker
    is the only service that provisions workspaces and mounts the overlay, and it
    alone is granted ``CAP_SYS_ADMIN`` (see ``docker/compose/local-service.yml``);
    readiness, however, is usually collected from the API/status/MCP path, which
    deliberately runs without that capability. Probing the *caller's* own
    ``CAP_SYS_ADMIN`` there would surface ``per_workspace_copy`` even though the
    worker uses ``per_workspace_overlay``, so the label keys off kernel overlayfs
    availability — the one host fact shared across services — and treats the
    worker's ``CAP_SYS_ADMIN`` as the deployment invariant it is. Best effort: an
    individual mount can still fall back to copy, which keeps provisioning correct
    either way.
    """

    probe = overlay_filesystem_available or _overlay_filesystem_available
    return _ISOLATION_OVERLAY if probe() else _ISOLATION_COPY


def _shared_claude_base_dir(work_dir: Path, signature: str) -> Path:
    """Return the host-wide shared read-only ``~/.claude`` base for ``signature``.

    The host-content ``signature`` names the base dir so a changed host
    ``~/.claude`` (operator added/updated skills, plugins, settings, tokens, …)
    builds a *fresh* base instead of reusing a stale one. Bases are immutable
    once built: a new signature gets a new dir and old signature dirs are left
    untouched, so a workspace that still has an old base mounted as its overlay
    lowerdir keeps a consistent view (overlayfs forbids mutating a live lower).
    """

    return work_dir / "auth" / _SHARED_AUTH_DIRNAME / _CLAUDE_BASE_DIRNAME / signature / ".claude"


def _host_claude_signature(host_home: Path) -> str:
    """Return a content signature of the host ``~/.claude`` overlay source.

    Lets a new workspace notice that an operator changed ``~/.claude`` since the
    shared base was last built, so it rebuilds under a fresh signature rather
    than mounting a stale lowerdir (the legacy per-workspace copy always
    reflected the current host; the shared base must not silently fall behind).
    The usage-history dirs excluded from the copied base are excluded here too,
    so churn in those transcript trees never forces a needless rebuild. Cheap:
    ``lstat`` only, no file reads.
    """

    source = host_home / ".claude"
    excluded = frozenset(_CLAUDE_USAGE_HISTORY_DIRS)
    entries: list[str] = []
    for root, dirs, files in os.walk(source):
        dirs[:] = [name for name in dirs if name not in excluded]
        root_path = Path(root)
        for name in (*dirs, *files):
            entry = root_path / name
            rel = entry.relative_to(source).as_posix()
            try:
                stat = entry.lstat()
            except OSError:
                entries.append(f"{rel}\0missing")
                continue
            entries.append(f"{rel}\0{stat.st_size}\0{stat.st_mtime_ns}")
    digest = hashlib.sha256("\n".join(sorted(entries)).encode("utf-8")).hexdigest()
    return digest[:16]


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
    overlay_mounter: OverlayMounter | None = None

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
            overlay_mounter=self.overlay_mounter,
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
    overlay_mounter: OverlayMounter | None = None,
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
        overlay_mounter=overlay_mounter or default_overlay_mounter(),
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
    overlay_mounter: OverlayMounter,
) -> tuple[AuthMount, ...]:
    auth_root = work_dir / "auth" / workspace_id
    mounts = []
    chown_exempt_sources: set[str] = set()
    extra_chown_paths: list[Path] = []
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
    claude_auth = _prepare_isolated_claude_auth(
        host_home=host_home,
        target_root=auth_root / "claude",
        work_dir=work_dir,
        suppressed_targets=suppressed_targets,
        overlay_mounter=overlay_mounter,
        workspace_owner_uid=workspace_owner_uid,
        workspace_owner_gid=workspace_owner_gid,
    )
    mounts.extend(claude_auth.mounts)
    chown_exempt_sources.update(claude_auth.chown_exempt_sources)
    extra_chown_paths.extend(claude_auth.extra_chown_paths)
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
        exempt_sources=frozenset(chown_exempt_sources),
        extra_paths=tuple(extra_chown_paths),
    )
    mounts.extend(base_mounts)
    return tuple(mounts)


def _chown_workspace_auth_sources(
    mounts: Sequence[AuthMount],
    *,
    uid: int | None,
    gid: int | None,
    exempt_sources: frozenset[str] = frozenset(),
    extra_paths: Sequence[Path] = (),
) -> None:
    if uid is None and gid is None:
        return
    if uid is None or gid is None:
        raise ValueError("workspace auth ownership requires both uid and gid")

    for mount in mounts:
        if mount.mode != "rw":
            continue
        # The Claude overlay's merged mount is excluded: chowning through a live
        # overlay would write into the shared lower's inodes. Its writable
        # ``upper``/``work`` dirs are chowned explicitly via ``extra_paths``.
        if mount.source in exempt_sources:
            continue
        _chown_tree(Path(mount.source), uid, gid)
    for path in extra_paths:
        _chown_tree(path, uid, gid)


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


@dataclass(frozen=True)
class _ClaudeAuthResult:
    """Claude auth mounts plus chown routing for the overlay branch.

    ``chown_exempt_sources`` lists mount sources the generic rw-chown walk must
    skip (the live overlay ``merged`` mount). ``extra_chown_paths`` lists the
    writable overlay scratch dirs (``upper``/``work``) chowned in their place.
    """

    mounts: tuple[AuthMount, ...]
    chown_exempt_sources: frozenset[str] = frozenset()
    extra_chown_paths: tuple[Path, ...] = ()


def _prepare_isolated_claude_auth(
    *,
    host_home: Path,
    target_root: Path,
    work_dir: Path,
    overlay_mounter: OverlayMounter,
    suppressed_targets: Collection[str] = (),
    workspace_owner_uid: int | None = None,
    workspace_owner_gid: int | None = None,
) -> _ClaudeAuthResult:
    """Seed per-workspace Claude auth without sharing writable host files.

    Prefers a shared read-only base + per-workspace writable overlay (no
    ~1.7 GB per-workspace copy); falls back to the legacy full copy when
    overlayfs is unavailable so provisioning never hard-fails.
    """

    mounts: list[AuthMount] = []
    chown_exempt: set[str] = set()
    extra_chown: list[Path] = []
    source_dir = host_home / ".claude"
    if _CLAUDE_DIR_TARGET not in suppressed_targets and source_dir.is_dir():
        overlay = _prepare_claude_overlay_mount(
            host_home=host_home,
            claude_root=target_root,
            work_dir=work_dir,
            overlay_mounter=overlay_mounter,
            workspace_owner_uid=workspace_owner_uid,
            workspace_owner_gid=workspace_owner_gid,
        )
        if overlay is not None:
            mount, upper, work = overlay
            mounts.append(mount)
            chown_exempt.add(mount.source)
            extra_chown.extend((upper, work))
        else:
            target_dir = target_root / ".claude"
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

    return _ClaudeAuthResult(
        mounts=tuple(mounts),
        chown_exempt_sources=frozenset(chown_exempt),
        extra_chown_paths=tuple(extra_chown),
    )


def _prepare_claude_overlay_mount(
    *,
    host_home: Path,
    claude_root: Path,
    work_dir: Path,
    overlay_mounter: OverlayMounter,
    workspace_owner_uid: int | None,
    workspace_owner_gid: int | None,
) -> tuple[AuthMount, Path, Path] | None:
    """Mount a shared-base + per-workspace overlay at ``<claude_root>/merged``.

    Returns ``(merged_mount, upper, work)`` on success, or ``None`` to signal the
    caller should fall back to the legacy full copy (overlay unsupported or the
    mount failed). The shared base is built once per host and reused.
    """

    if not overlay_mounter.supported():
        _log.info(
            "claude_auth_overlay_unavailable",
            reason_code=_CLAUDE_AUTH_OVERLAY_UNAVAILABLE,
            workspace_auth_root=str(claude_root),
            reason="overlayfs_unsupported",
        )
        return None

    try:
        base = _ensure_shared_claude_base(
            host_home=host_home,
            work_dir=work_dir,
            workspace_owner_uid=workspace_owner_uid,
            workspace_owner_gid=workspace_owner_gid,
        )
    except OSError as exc:
        # Building the shared base (copytree/chown) can fail with OSError (disk
        # full, permissions). Degrade to the legacy full copy rather than
        # hard-failing provisioning — the same resilience contract honored when
        # overlayfs is unsupported or the mount itself fails.
        _log.warning(
            "claude_auth_shared_base_failed",
            reason_code=_CLAUDE_AUTH_SHARED_BASE_FAILED,
            workspace_auth_root=str(claude_root),
            error=str(exc),
        )
        return None
    upper = claude_root / "upper"
    work = claude_root / "work"
    merged = claude_root / "merged"
    for directory in (upper, work, merged):
        directory.mkdir(parents=True, exist_ok=True)

    if overlay_mounter.is_mounted(merged):
        # Idempotent retry: a prior provision already mounted this overlay (e.g.
        # the auth dir survived a failed stack launch). Reuse the live mount
        # instead of remounting onto a busy mountpoint — a second mount would
        # raise EBUSY and the cleanup below would ``rmtree`` the writable
        # ``upper``/``work`` layers, destroying the agent's overlay data and
        # forcing a needless full-copy fallback.
        return (
            AuthMount(source=str(merged), target=_CLAUDE_DIR_TARGET, mode="rw"),
            upper,
            work,
        )

    try:
        overlay_mounter.mount(lowerdir=base, upperdir=upper, workdir=work, merged=merged)
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning(
            "claude_auth_overlay_unavailable",
            reason_code=_CLAUDE_AUTH_OVERLAY_UNAVAILABLE,
            workspace_auth_root=str(claude_root),
            error=str(exc),
        )
        for directory in (merged, work, upper):
            shutil.rmtree(directory, ignore_errors=True)
        return None

    return (
        AuthMount(source=str(merged), target=_CLAUDE_DIR_TARGET, mode="rw"),
        upper,
        work,
    )


def _ensure_shared_claude_base(
    *,
    host_home: Path,
    work_dir: Path,
    workspace_owner_uid: int | None,
    workspace_owner_gid: int | None,
) -> Path:
    """Build (once per host-content signature) the shared ``~/.claude`` base.

    The base dir is keyed by a signature of the current host ``~/.claude`` so a
    later workspace whose host changed gets a freshly built base instead of a
    stale lowerdir; an unchanged host reuses the existing one. Built into a temp
    dir and moved into place atomically so a concurrent provision either sees a
    complete base or loses the rename race and reuses the winner's. Chowned once
    to the agent uid/gid (all workspace agents share uid 1000) so the read-only
    lower is readable through the overlay.
    """

    base = _shared_claude_base_dir(work_dir, _host_claude_signature(host_home))
    if base.is_dir():
        return base

    shared_root = base.parent
    shared_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".claude-base-", dir=shared_root))
    staged_base = staging / ".claude"
    shutil.copytree(
        host_home / ".claude",
        staged_base,
        ignore=shutil.ignore_patterns(*_CLAUDE_USAGE_HISTORY_DIRS),
        ignore_dangling_symlinks=True,
    )
    if workspace_owner_uid is not None and workspace_owner_gid is not None:
        _chown_tree(staged_base, workspace_owner_uid, workspace_owner_gid)
    try:
        staged_base.replace(base)
    except OSError:
        # Lost the build race against a concurrent provision; reuse its base.
        shutil.rmtree(staging, ignore_errors=True)
        return base
    shutil.rmtree(staging, ignore_errors=True)
    return base


def teardown_workspace_auth_overlay(
    *,
    work_dir: Path,
    workspace_id: str,
    overlay_mounter: OverlayMounter | None = None,
) -> None:
    """Unmount a workspace's Claude overlay before its auth dir is removed.

    Unmount-before-remove: a busy overlay mount makes ``rmtree`` fail with
    ``EBUSY``, which is exactly the class of leak GC cannot clean up. This only
    unmounts (GC owns removal) and is idempotent — a no-op when nothing is
    mounted, and it surfaces (logs and re-raises) a genuine busy/umount error.
    """

    mounter = overlay_mounter or default_overlay_mounter()
    merged = work_dir.expanduser() / "auth" / workspace_id / "claude" / "merged"
    if not mounter.is_mounted(merged):
        return
    try:
        mounter.unmount(merged)
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning(
            "claude_auth_overlay_unmount_failed",
            reason_code="CLAUDE_AUTH_OVERLAY_UNMOUNT_FAILED",
            workspace_id=workspace_id,
            merged=str(merged),
            error=str(exc),
        )
        raise


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
