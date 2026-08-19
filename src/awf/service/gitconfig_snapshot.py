"""Stable service-owned snapshots of the host Git configuration."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, BinaryIO

_SERVICE_AUTH_DIR = "service-auth"
_SNAPSHOTS_DIR = "gitconfig-snapshots"
_SNAPSHOT_HOME_DIR = "home"
_SERVICE_GITCONFIG_NAME = ".gitconfig"
_AGENT_GITCONFIG_NAME = "agent.gitconfig"
_MAX_INCLUDE_DEPTH = 16
_MAX_SNAPSHOT_BUNDLES = 8
_MAX_SNAPSHOT_AGE_SECONDS = 30 * 24 * 60 * 60
_SNAPSHOT_FORMAT_VERSION = b"awf-gitconfig-snapshot-v2\0"
_SNAPSHOT_COORDINATION_LOCK_NAME = ".snapshot.lock"
_WORKER_LEASE_NAME = "worker.lock"
_ACTIVE_BUNDLE_LEASES: dict[Path, BinaryIO] = {}


def materialize_service_gitconfig(
    *,
    host_home: Path,
    work_dir: Path,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> Path | None:
    """Create an immutable Git-config bundle readable by worker and agent.

    The bundle path is content-addressed. Repeated worker starts therefore reuse
    the same inode, while a changed config gets a new path and cannot invalidate
    a persistent agent container's existing Docker bind mount. Relative include
    files below ``host_home`` are mirrored into the bundle so relocating the
    global config does not change their origin semantics.
    """

    source_home = host_home.expanduser().absolute()
    source = source_home / _SERVICE_GITCONFIG_NAME
    if not source.is_file():
        return None
    if (owner_uid is None) != (owner_gid is None):
        raise ValueError("gitconfig snapshot ownership requires both uid and gid")

    snapshots_root = work_dir / _SERVICE_AUTH_DIR / _SNAPSHOTS_DIR
    snapshots_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    snapshots_root.chmod(0o700)
    staging_root = Path(tempfile.mkdtemp(prefix=".gitconfig-", dir=snapshots_root))
    try:
        staging_home = staging_root / _SNAPSHOT_HOME_DIR
        _copy_config_graph(
            source=source,
            source_home=source_home,
            snapshot_home=staging_home,
        )
        digest = _snapshot_digest(staging_home)
        final_root = snapshots_root / digest
        final_config = final_root / _SNAPSHOT_HOME_DIR / _SERVICE_GITCONFIG_NAME
        with _snapshot_coordination_lock(snapshots_root):
            if not final_root.exists():
                # The worker-only copy retains its confined relative include graph.
                # Agents receive only the immutable top-level config, preserving the
                # pre-snapshot visibility boundary instead of gaining access to every
                # conditional include mirrored for the worker.
                agent_config = staging_root / _AGENT_GITCONFIG_NAME
                agent_config.write_bytes(
                    (staging_home / _SERVICE_GITCONFIG_NAME).read_bytes(),
                )
                (staging_root / _WORKER_LEASE_NAME).touch()
                _make_bundle_readable(
                    staging_root,
                    owner_uid=owner_uid,
                    owner_gid=owner_gid,
                )
                with suppress(FileExistsError):
                    staging_root.rename(final_root)
            _hold_worker_bundle_lease(final_root)
        _reap_stale_gitconfig_bundles(
            snapshots_root=snapshots_root,
            work_dir=work_dir,
            protected_root=final_root,
        )
        return final_config
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root)


def _copy_config_graph(*, source: Path, source_home: Path, snapshot_home: Path) -> None:
    pending: list[tuple[Path, int]] = [(source, 0)]
    copied: set[Path] = set()
    resolved_home = source_home.resolve()
    while pending:
        current, depth = pending.pop()
        resolved = current.resolve()
        if resolved in copied:
            continue
        if depth > _MAX_INCLUDE_DEPTH:
            raise RuntimeError("gitconfig relative include depth exceeds safe limit")
        copied.add(resolved)

        relative = _relative_to_home(current, source_home=source_home)
        target = snapshot_home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(current.read_bytes())

        # Inspect the bytes just copied, not the live source again. Editors may
        # atomically replace the source between these steps; parsing ``target``
        # keeps the mirrored include graph consistent with the published main
        # config content.
        include_paths = _relative_include_paths(target)
        _rewrite_relative_gitdir_conditions(
            config_path=target,
            source_dir=current.parent,
        )
        for include_path in include_paths:
            included = current.parent / include_path
            resolved_include = included.resolve()
            if resolved_include.is_file() and resolved_include.is_relative_to(resolved_home):
                pending.append((included, depth + 1))


def _relative_to_home(path: Path, *, source_home: Path) -> Path:
    if path == source_home / _SERVICE_GITCONFIG_NAME:
        return Path(_SERVICE_GITCONFIG_NAME)
    # Normalize ``..`` without resolving symlinks: the parent config continues
    # to reference this lexical path after it is copied into the snapshot.
    lexical_path = Path(os.path.abspath(path))  # noqa: PTH100 - preserve symlink alias
    lexical_home = Path(os.path.abspath(source_home))  # noqa: PTH100 - same path basis
    return lexical_path.relative_to(lexical_home)


def _relative_include_paths(config_path: Path) -> tuple[Path, ...]:
    result = subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(config_path),
            "--null",
            "--get-regexp",
            r"^include(If\..*)?\.path$",
        ],
        check=False,
        capture_output=True,
        timeout=10,
    )
    if result.returncode == 1 and not result.stderr:
        return ()
    if result.returncode != 0:
        message = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"could not inspect gitconfig includes: {message or result.returncode}")

    paths: list[Path] = []
    for entry in result.stdout.split(b"\0"):
        if not entry:
            continue
        _key, separator, raw_value = entry.partition(b"\n")
        if not separator:
            continue
        value = raw_value.decode(errors="surrogateescape")
        candidate = Path(value)
        if value.startswith("~") or candidate.is_absolute():
            continue
        paths.append(candidate)
    return tuple(paths)


def _rewrite_relative_gitdir_conditions(*, config_path: Path, source_dir: Path) -> None:
    """Keep ``gitdir:./`` conditions based at their original config directory."""

    result = subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(config_path),
            "--null",
            "--name-only",
            "--get-regexp",
            r"^includeIf\..*\.path$",
        ],
        check=False,
        capture_output=True,
        timeout=10,
    )
    if result.returncode == 1 and not result.stderr:
        return
    if result.returncode != 0:
        message = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(
            f"could not inspect gitconfig conditions: {message or result.returncode}",
        )

    conditions: set[str] = set()
    for raw_key in result.stdout.split(b"\0"):
        key = raw_key.decode(errors="surrogateescape")
        if not key:
            continue
        condition = key[len("includeif.") : -len(".path")]
        if condition.startswith("gitdir:./") or condition.startswith("gitdir/i:./"):
            conditions.add(condition)

    source_prefix = source_dir.as_posix().rstrip("/")
    for condition in conditions:
        kind, relative_pattern = condition.split(":./", maxsplit=1)
        rewritten = f"{kind}:{source_prefix}/{relative_pattern}"
        rename = subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(config_path),
                "--rename-section",
                f"includeIf.{condition}",
                f"includeIf.{rewritten}",
            ],
            check=False,
            capture_output=True,
            timeout=10,
        )
        if rename.returncode != 0:
            message = rename.stderr.decode(errors="replace").strip()
            raise RuntimeError(
                f"could not rewrite relative gitdir condition: {message or rename.returncode}",
            )


def _snapshot_digest(snapshot_home: Path) -> str:
    digest = hashlib.sha256()
    digest.update(_SNAPSHOT_FORMAT_VERSION)
    for path in sorted(item for item in snapshot_home.rglob("*") if item.is_file()):
        digest.update(path.relative_to(snapshot_home).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _reap_stale_gitconfig_bundles(
    *,
    snapshots_root: Path,
    work_dir: Path,
    protected_root: Path,
    now: float | None = None,
) -> None:
    """Remove stale bundles only when all live-reference checks succeed."""

    try:
        with _snapshot_coordination_lock(snapshots_root):
            _reap_stale_gitconfig_bundles_from_known_state(
                snapshots_root=snapshots_root,
                work_dir=work_dir,
                protected_root=protected_root,
                now=now,
            )
    except OSError:
        # Retention is best-effort. A concurrent worker may publish or reap a
        # bundle between directory enumeration and stat/open; startup must win.
        return


def _reap_stale_gitconfig_bundles_from_known_state(
    *,
    snapshots_root: Path,
    work_dir: Path,
    protected_root: Path,
    now: float | None,
) -> None:
    bundle_roots = tuple(
        path
        for path in snapshots_root.iterdir()
        if path.is_dir() and len(path.name) == 64 and _is_lower_hex(path.name)
    )
    if not bundle_roots:
        return

    compose_contents = _compose_config_contents(work_dir)
    container_sources = _running_container_mount_sources()
    if compose_contents is None or container_sources is None:
        # Cleanup is a storage optimization, never a reason to risk invalidating
        # an existing container bind. Unknown Docker/filesystem state fails safe.
        return

    timestamp = time.time() if now is None else now
    newest = frozenset(
        sorted(bundle_roots, key=lambda path: (path.stat().st_mtime, path.name), reverse=True)[
            :_MAX_SNAPSHOT_BUNDLES
        ]
    )
    for bundle_root in bundle_roots:
        if bundle_root == protected_root:
            continue
        if any(str(bundle_root) in content for content in compose_contents):
            continue
        if any(_path_is_within(source, bundle_root) for source in container_sources):
            continue
        if bundle_root in _ACTIVE_BUNDLE_LEASES:
            continue
        is_expired = timestamp - bundle_root.stat().st_mtime > _MAX_SNAPSHOT_AGE_SECONDS
        if bundle_root not in newest or is_expired:
            with _exclusive_bundle_cleanup_lease(bundle_root) as acquired:
                if acquired:
                    with suppress(OSError):
                        shutil.rmtree(bundle_root)


def _hold_worker_bundle_lease(bundle_root: Path) -> None:
    if bundle_root in _ACTIVE_BUNDLE_LEASES:
        return
    # ``a+b`` upgrades bundles published by the immediately preceding AWF
    # build, before lifetime leases existed. The lock is metadata only; config
    # content and the agent-mounted inode remain immutable.
    lease = (bundle_root / _WORKER_LEASE_NAME).open("a+b")
    os.fchmod(lease.fileno(), 0o600)
    fcntl.flock(lease.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    _ACTIVE_BUNDLE_LEASES[bundle_root] = lease


@contextmanager
def _snapshot_coordination_lock(snapshots_root: Path) -> Iterator[None]:
    lock_path = snapshots_root / _SNAPSHOT_COORDINATION_LOCK_NAME
    with lock_path.open("a+b") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            with suppress(OSError):
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def _exclusive_bundle_cleanup_lease(bundle_root: Path) -> Iterator[bool]:
    try:
        lease = (bundle_root / _WORKER_LEASE_NAME).open("r+b")
    except OSError:
        # Pre-lease bundles may still belong to an overlapping old worker.
        yield False
        return
    try:
        try:
            fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            with suppress(OSError):
                fcntl.flock(lease.fileno(), fcntl.LOCK_UN)
    finally:
        lease.close()


def _compose_config_contents(work_dir: Path) -> tuple[str, ...] | None:
    compose_root = work_dir / "compose"
    try:
        if not compose_root.exists():
            return ()
        return tuple(
            path.read_text(errors="surrogateescape") for path in compose_root.glob("*/compose.yml")
        )
    except OSError:
        return None


def _running_container_mount_sources() -> frozenset[Path] | None:
    try:
        listed = subprocess.run(
            ["docker", "container", "ls", "--all", "--quiet"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if listed.returncode != 0:
            return None
        container_ids = tuple(line for line in listed.stdout.splitlines() if line)
        if not container_ids:
            return frozenset()
        inspected = subprocess.run(
            ["docker", "container", "inspect", *container_ids],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if inspected.returncode != 0:
        return None
    try:
        containers: Any = json.loads(inspected.stdout)
        if not isinstance(containers, list):
            return None
        sources: set[Path] = set()
        for container in containers:
            if not isinstance(container, dict):
                return None
            mounts = container.get("Mounts", [])
            if not isinstance(mounts, list):
                return None
            for mount in mounts:
                if not isinstance(mount, dict):
                    return None
                source = mount.get("Source")
                if isinstance(source, str) and source:
                    sources.add(Path(source))
        return frozenset(sources)
    except (json.JSONDecodeError, TypeError):
        return None


def _path_is_within(path: Path, directory: Path) -> bool:
    return path == directory or path.is_relative_to(directory)


def _is_lower_hex(value: str) -> bool:
    return all(character in "0123456789abcdef" for character in value)


def _make_bundle_readable(
    bundle_root: Path,
    *,
    owner_uid: int | None,
    owner_gid: int | None,
) -> None:
    effective_uid = os.geteuid()
    use_agent_ownership = owner_uid is not None and owner_gid is not None and effective_uid == 0
    needs_shared_read = owner_uid is not None and effective_uid not in {0, owner_uid}
    for directory in (bundle_root, *(path for path in bundle_root.rglob("*") if path.is_dir())):
        directory.chmod(0o755)
    for path in (item for item in bundle_root.rglob("*") if item.is_file()):
        with path.open("r+b") as file_obj:
            if use_agent_ownership:
                assert owner_uid is not None and owner_gid is not None
                os.fchown(file_obj.fileno(), owner_uid, owner_gid)
            os.fchmod(file_obj.fileno(), 0o644 if needs_shared_read else 0o600)
            os.fsync(file_obj.fileno())


__all__ = ["materialize_service_gitconfig"]
