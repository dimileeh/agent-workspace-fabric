"""Auth mount resolution for service-created workspace stacks."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
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
# A hard kill (OOM, SIGKILL) between ``mkdtemp`` and the ``finally`` cleanup in
# ``_ensure_shared_claude_base`` strands a ``.claude-base-*`` staging dir (a full
# ~1.7 GB copy of ``~/.claude``) under ``_shared``, which GC never reaps. The next
# provision sweeps staging dirs older than this; the bound is far above how long a
# live copy takes so a concurrent provision's in-progress staging dir is never hit.
_STALE_STAGING_MAX_AGE_SECONDS = 3600.0
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
    ``stat`` only, no file reads.

    Symlinks are followed (``followlinks=True`` + ``stat`` rather than ``lstat``)
    to mirror the copy, which uses ``copytree(symlinks=False)`` and so copies the
    *targets'* contents into the base. If an operator keeps skills/plugins/settings
    as symlinks into a dotfiles repo and updates a target without replacing the
    link, the link's own ``lstat`` is unchanged — signing on ``lstat`` would reuse a
    stale base while the copy would have refreshed it. Signing the resolved targets
    keeps the signature aligned with what actually gets copied.

    ``st_mode`` is part of the key too. ``copytree`` uses ``copy2``, which preserves
    permission bits, so an operator running ``chmod +x`` on a plugin/hook script
    inside ``~/.claude`` changes what the copy produces. ``chmod`` bumps ``ctime``
    but leaves ``st_size`` and ``st_mtime_ns`` untouched, so signing on size+mtime
    alone would reuse a stale base that lacks the new mode bits. Including the mode
    rebuilds when permissions change.

    ``followlinks=True`` does not detect symlink cycles, so a circular link in
    ``~/.claude`` (e.g. a skills dir linked back to a parent) would otherwise loop
    forever — and unlike the legacy per-workspace ``copytree`` this runs on every
    provision call, so one cycle would silently hang all future provisioning. A
    ``visited`` set of resolved ``(st_dev, st_ino)`` directory identities bounds
    the walk: a directory already seen is pruned before descent.
    """

    source = host_home / ".claude"
    excluded = frozenset(_CLAUDE_USAGE_HISTORY_DIRS)
    entries: list[str] = []
    visited: set[tuple[int, int]] = set()
    try:
        source_stat = source.stat()
        visited.add((source_stat.st_dev, source_stat.st_ino))
    except OSError:
        pass
    for root, dirs, files in os.walk(source, followlinks=True):
        root_path = Path(root)
        kept: list[str] = []
        for name in dirs:
            if name in excluded:
                continue
            try:
                dir_stat = (root_path / name).stat()
            except OSError:
                # A dangling/broken dir link: keep it so the entry loop below
                # records the ``missing`` marker, matching the prior behaviour.
                kept.append(name)
                continue
            identity = (dir_stat.st_dev, dir_stat.st_ino)
            if identity in visited:
                # Cycle (or a dir reachable via two links): pruning it bounds
                # the walk without dropping content already signed under its
                # first-seen path.
                continue
            visited.add(identity)
            kept.append(name)
        dirs[:] = kept
        for name in (*dirs, *files):
            entry = root_path / name
            rel = entry.relative_to(source).as_posix()
            try:
                stat = entry.stat()
            except OSError:
                entries.append(f"{rel}\0missing")
                continue
            entries.append(f"{rel}\0{stat.st_size}\0{stat.st_mtime_ns}\0{stat.st_mode}")
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


def _overlay_upper_has_data(upper: Path) -> bool:
    """True when a surviving overlay ``upper`` carries agent mutations.

    The first overlay attempt creates an empty ``upper`` before mounting; a failed
    mount leaves that empty dir on disk while provisioning falls back to the legacy
    full copy (which the agent then mutates). An empty leftover therefore must not
    masquerade as a real overlay and shadow the mutated legacy copy. Only an
    ``upper`` with at least one entry — the writable layer of a previously mounted
    overlay — counts as real overlay data.
    """

    try:
        return any(upper.iterdir())
    except OSError:
        # Missing dir (a pure legacy/no-overlay workspace) or an unreadable one:
        # treat as no overlay data so the legacy-copy guard stays in force.
        return False


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
    legacy_claude_copy = target_root / ".claude"
    if _CLAUDE_DIR_TARGET not in suppressed_targets and source_dir.is_dir():
        # A prior legacy/pre-upgrade provision (overlay unsupported then) may have
        # left a per-workspace ``.claude`` copy carrying the agent's auth/config
        # mutations. The legacy branch deliberately reuses such a copy (``if not
        # target_dir.exists()``) to preserve retry state; mounting a fresh
        # shared-base overlay over it would silently drop those mutations (empty
        # ``upper``, lower seeded from the current host). So normally only reach
        # for the overlay when no legacy copy exists — otherwise keep using that
        # copy.
        #
        # Exception: a surviving *non-empty* overlay ``upper`` overrides that guard.
        # A transient remount failure preserves ``upper``/``work`` on disk but still
        # degrades this provision to a *fresh* legacy copy (no mutations). Without
        # this override the next retry would see that fresh copy, skip the overlay
        # forever, and strand the agent's real mutations in the unremounted
        # ``upper``. A non-empty ``upper`` exists only for overlay-backed workspaces
        # (a pure legacy copy never has one), so its presence safely distinguishes
        # "remount the surviving overlay" from "preserve a real legacy copy". The
        # overlay mount, when it succeeds, takes precedence over the stale legacy
        # copy.
        #
        # The ``upper`` must be *non-empty* to override: the first overlay attempt
        # creates an empty ``upper`` before the mount, and if that mount fails the
        # empty dir is left behind while the fallback legacy copy is what the agent
        # actually mutates. Treating that empty leftover as a real overlay would
        # mount it over a fresh shared base and silently shadow the mutated legacy
        # copy. So only a ``upper`` carrying real overlay data bypasses the guard.
        overlay_upper = target_root / "upper"
        overlay = (
            None
            if legacy_claude_copy.exists() and not _overlay_upper_has_data(overlay_upper)
            else _prepare_claude_overlay_mount(
                host_home=host_home,
                claude_root=target_root,
                work_dir=work_dir,
                overlay_mounter=overlay_mounter,
                workspace_owner_uid=workspace_owner_uid,
                workspace_owner_gid=workspace_owner_gid,
            )
        )
        if overlay is not None:
            mount, upper, work = overlay
            mounts.append(mount)
            chown_exempt.add(mount.source)
            extra_chown.extend((upper, work))
        else:
            target_dir = legacy_claude_copy
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


def _pinned_overlay_base(work_dir: Path, sig_marker: Path) -> Path | None:
    """Return the base an existing overlay ``upper``/``work`` were built against.

    Reads the ``base.signature`` marker written when the overlay was first
    created and resolves it back to the shared base dir. Returns ``None`` when no
    marker exists (a pre-pin overlay, or none at all) or the recorded base is no
    longer on disk — the caller then rebuilds from the current host. The recorded
    base normally persists because shared bases are immutable and old signatures
    are left intact for exactly this case (a still-relevant lowerdir).
    """

    try:
        signature = sig_marker.read_text().strip()
    except OSError:
        return None
    base = _shared_claude_base_dir(work_dir, signature)
    return base if base.is_dir() else None


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

    upper = claude_root / "upper"
    work = claude_root / "work"
    merged = claude_root / "merged"
    sig_marker = claude_root / "base.signature"

    # Pin the lowerdir for retries over an existing overlay. If ``upper``/``work``
    # survived a torn-down mount (e.g. a host reboot) they must be remounted over
    # the *original* base they were created against. Recomputing the base from a
    # since-changed host ``~/.claude`` would either expose the changed host config
    # through the surviving upper or trip an overlayfs upper/work mismatch — and
    # that failure path ``rmtree``s upper/work, destroying the agent's Claude
    # mutations. ``base.signature`` records which base each upper belongs to.
    base = _pinned_overlay_base(work_dir, sig_marker) if upper.exists() else None
    fresh_signature: str | None = None
    if base is None:
        fresh_signature = _host_claude_signature(host_home)
        try:
            base = _ensure_shared_claude_base(
                host_home=host_home,
                work_dir=work_dir,
                signature=fresh_signature,
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
    try:
        for directory in (upper, work, merged):
            directory.mkdir(parents=True, exist_ok=True)

        if fresh_signature is not None:
            # Record the base signature so a later retry over this upper/work pins
            # to this exact base instead of recomputing it from a changed host.
            # (Pinned retries leave ``fresh_signature`` None — the marker exists.)
            sig_marker.write_text(fresh_signature)
    except OSError as exc:
        # Creating the per-workspace scratch dirs or writing the signature marker
        # can fail with OSError (disk full after the base copytree just consumed
        # space, permissions). Degrade to the legacy full copy rather than
        # hard-failing provisioning — the same resilience contract honored when
        # the base build or the mount itself fails. ``upper``/``work`` are left
        # intact: a retry path may carry the agent's overlay mutations there,
        # which a teardown would destroy.
        _log.warning(
            "claude_auth_overlay_unavailable",
            reason_code=_CLAUDE_AUTH_OVERLAY_UNAVAILABLE,
            workspace_auth_root=str(claude_root),
            error=str(exc),
        )
        return None

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
        if overlay_mounter.is_mounted(merged):
            # A concurrent provision won the mount race in the window between the
            # ``is_mounted`` pre-check above (then false) and this attempt, so our
            # ``mount`` failed (EBUSY) onto a now-live overlay. Tearing down
            # ``upper``/``work`` here would destroy the winner's writable layer
            # while the overlay stays mounted; reuse the live mount instead,
            # exactly as the idempotent-retry pre-check does.
            return (
                AuthMount(source=str(merged), target=_CLAUDE_DIR_TARGET, mode="rw"),
                upper,
                work,
            )
        _log.warning(
            "claude_auth_overlay_unavailable",
            reason_code=_CLAUDE_AUTH_OVERLAY_UNAVAILABLE,
            workspace_auth_root=str(claude_root),
            error=str(exc),
        )
        # Drop the signature marker if we wrote one this call (``fresh_signature``
        # set). The mount never validated the surviving ``upper`` against that
        # base, and for a pre-pin upper (no prior marker) the recorded signature
        # is only a guess from the current host hash — the upper may have been
        # built against an older base. Persisting it would pin every later retry
        # to the wrong lowerdir, so the surviving upper could never remount even
        # after the host reverts. Removing it lets retries recompute the base from
        # the host. A pinned retry leaves ``fresh_signature`` None and keeps its
        # (already validated) marker so a later transient-failure retry can remount.
        if fresh_signature is not None:
            sig_marker.unlink(missing_ok=True)
        # Remove only the unused ``merged`` mountpoint. ``upper``/``work`` are left
        # intact: a normal teardown leaves the agent's overlay mutations in
        # ``upper`` on disk, and a retry that fails to remount here (a transient
        # error — the pinned lowerdir already rules out the upper/base mismatch)
        # must not wipe them. We degrade to the legacy full copy for now; a later
        # provision can pin the surviving ``upper`` and remount it, recovering the
        # mutations. This matches the scratch-dir OSError path above.
        shutil.rmtree(merged, ignore_errors=True)
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
    signature: str,
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

    Known limitation (superseded-base accumulation): each distinct host
    ``~/.claude`` signature builds a new ``<sig>/.claude`` base (~1.7 GB) and
    leaves prior-signature bases in place. ``_reap_stale_claude_base_staging``
    only reclaims crash-orphaned ``.claude-base-*`` *staging* dirs, and GC only
    enumerates ``auth/<workspace_id>`` rows (never ``_shared``), so completed
    but superseded base dirs are never reclaimed today. With N host-content
    revisions this strands up to N×1.7 GB under ``_shared``. Reaping them safely
    is deferred to a follow-up (GC-B): a superseded base may still back a live
    overlay as its ``lowerdir`` (overlayfs forbids mutating/removing a live
    lower), so a reaper must skip the current signature and any base referenced
    by an ``lowerdir=`` in ``/proc/mounts`` before removing it.
    """

    base = _shared_claude_base_dir(work_dir, signature)
    # Sweep crash-orphaned staging on *every* provision — before the existing-base
    # early return below, not only on the build path. Once the base for a
    # signature exists every later call returns early, so an orphan that outlived
    # its build window (too young to reap when the base was built, then later
    # stale) — or one stranded under a now-superseded signature on a host whose
    # ``~/.claude`` stopped changing — would never be revisited, and GC never
    # enters ``_shared``, permanently stranding the ~1.7 GB copy. Sweep from the
    # signature-root (``base.parent.parent``, parent of every ``<signature>``
    # dir): a crash strands an orphan under whatever signature was current then,
    # so a per-signature sweep would never revisit an old signature's orphan.
    _reap_stale_claude_base_staging(base.parent.parent)
    if base.is_dir():
        return base

    shared_root = base.parent
    shared_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".claude-base-", dir=shared_root))
    staged_base = staging / ".claude"
    # ``finally`` guarantees the staging dir is reclaimed on every exit: a
    # copytree/chown OSError (disk full, permissions) that propagates to the
    # caller's legacy-copy fallback, a lost build race, or the success path
    # (where ``replace`` has already moved ``staged_base`` out). Without it a
    # failed copy/chown would orphan the staging tree under the shared root.
    try:
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
        except OSError as exc:
            if base.is_dir():
                # Lost the build race against a concurrent provision; reuse its
                # base. The winner's ``replace`` populated ``base``, so renaming
                # onto the now non-empty directory fails — that is the race, and
                # ``base.is_dir()`` distinguishes it from a genuine failure.
                return base
            # ``base`` does not exist, so this is not a lost race but a real
            # failure (e.g. permissions). Surface it with log evidence and
            # re-raise so the caller degrades to the legacy copy visibly rather
            # than silently returning a non-existent base on every provision.
            _log.warning(
                "claude_auth_shared_base_replace_failed",
                reason_code=_CLAUDE_AUTH_SHARED_BASE_FAILED,
                staging=str(staging),
                base=str(base),
                error=str(exc),
            )
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return base


def _reap_stale_claude_base_staging(base_root: Path) -> None:
    """Remove crash-orphaned ``.claude-base-*`` staging dirs under ``base_root``.

    ``_ensure_shared_claude_base`` builds the base in a ``mkdtemp`` staging dir
    (under the current ``<signature>`` dir) and its ``finally`` reclaims it on
    every normal exit, but a hard kill (OOM, SIGKILL, crash) before that cleanup
    strands the staging tree — a full ~1.7 GB copy of ``~/.claude`` — under
    ``_shared``, which GC deliberately never reaps. Sweeping here on the next
    provision keeps those orphans from accumulating indefinitely.

    ``base_root`` is the parent of every ``<signature>`` dir, so the sweep covers
    *all* signatures (``*/.claude-base-*``), not just the one current now. A crash
    orphans staging under whatever signature was current at the time; if the host
    ``~/.claude`` changes before the next provision the current signature moves,
    and a sweep scoped to only the new signature's dir would never revisit — and
    so never reap — the old signature's orphan.

    Only staging dirs older than ``_STALE_STAGING_MAX_AGE_SECONDS`` are removed so a
    concurrent provision's in-progress staging dir (mtime ~build start, well under
    the bound) is never clobbered. A staging dir that vanishes from under us (a
    concurrent reap or its winning ``replace``) is skipped rather than fatal.
    """

    now = time.time()
    for staging in base_root.glob("*/.claude-base-*"):
        try:
            age = now - staging.stat().st_mtime
        except OSError:
            continue
        if age >= _STALE_STAGING_MAX_AGE_SECONDS:
            shutil.rmtree(staging, ignore_errors=True)


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
