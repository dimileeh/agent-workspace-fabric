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
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, BinaryIO

_SERVICE_AUTH_DIR = "service-auth"
_SNAPSHOTS_DIR = "gitconfig-snapshots"
_SNAPSHOT_HOME_DIR = "home"
_SERVICE_GITCONFIG_NAME = ".gitconfig"
_AGENT_GITCONFIG_NAME = "agent.gitconfig"
_EXTERNAL_INCLUDES_DIR = ".external-includes"
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
    host_root: Path | None = None,
    logical_host_home: Path | None = None,
    work_dir: Path,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> Path | None:
    """Create an immutable Git-config bundle readable by worker and agent.

    The bundle path is content-addressed. Repeated worker starts therefore reuse
    the same inode, while a changed config gets a new path and cannot invalidate
    a persistent agent container's existing Docker bind mount. Relative include
    files are mirrored into the bundle so relocating the global config does not
    change their origin semantics. External relative targets are mapped into a
    confined bundle directory rather than retaining parent traversal.
    """

    source_home = host_home.expanduser().absolute()
    condition_home = (
        logical_host_home.expanduser().absolute() if logical_host_home is not None else source_home
    )
    source_origin = source_home / _SERVICE_GITCONFIG_NAME
    source = _resolve_host_root_symlink(source_origin, host_root=host_root)
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
            source_origin=source_origin,
            source_home=source_home,
            host_root=host_root,
            condition_home=condition_home,
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


def _copy_config_graph(
    *,
    source: Path,
    source_origin: Path,
    source_home: Path,
    host_root: Path | None,
    condition_home: Path,
    snapshot_home: Path,
) -> None:
    pending: list[tuple[Path, Path, Path, int, frozenset[Path]]] = [
        (source, source_origin, Path(_SERVICE_GITCONFIG_NAME), 0, frozenset()),
    ]
    copied_destinations: set[Path] = set()
    while pending:
        current, current_origin, relative, depth, resolved_ancestors = pending.pop()
        resolved = current.resolve()
        if relative in copied_destinations or resolved in resolved_ancestors:
            continue
        if depth > _MAX_INCLUDE_DEPTH:
            raise RuntimeError("gitconfig relative include depth exceeds safe limit")
        copied_destinations.add(relative)
        child_ancestors = resolved_ancestors | {resolved}

        target = snapshot_home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(current.read_bytes())

        # Inspect the bytes just copied, not the live source again. Editors may
        # atomically replace the source between these steps; parsing ``target``
        # keeps the mirrored include graph consistent with the published main
        # config content.
        includes = _relative_includes(target)
        for key, include_path in includes:
            included_origin = current_origin.parent / include_path
            included = _resolve_host_root_symlink(included_origin, host_root=host_root)
            try:
                resolved_include = included.resolve()
            except (OSError, RuntimeError):
                continue
            if not resolved_include.is_file():
                continue
            included_relative = _snapshot_relative_path(
                included_origin,
                source_home=source_home,
            )
            snapshot_include = snapshot_home / included_relative
            current_destination = Path(
                os.path.abspath(target.parent / include_path),  # noqa: PTH100
            )
            if current_destination != snapshot_include:
                rewritten_path = Path(os.path.relpath(snapshot_include, target.parent))
                _rewrite_relative_include(
                    config_path=target,
                    key=key,
                    old_path=include_path,
                    new_path=rewritten_path,
                )
            pending.append(
                (included, included_origin, included_relative, depth + 1, child_ancestors),
            )
        _rewrite_relative_gitdir_conditions(
            config_path=target,
            source_dir=_logical_source_dir(
                current_origin.parent,
                source_home=source_home,
                logical_home=condition_home,
            ),
        )


def _resolve_host_root_symlink(path: Path, *, host_root: Path | None) -> Path:
    """Follow host symlinks at every component through an alternate root mount."""
    if host_root is None:
        return path

    root = Path(os.path.abspath(host_root))  # noqa: PTH100
    current = Path(os.path.abspath(path))  # noqa: PTH100
    visited: set[Path] = set()
    while True:
        relative = current.relative_to(root)
        candidate = root
        for index, component in enumerate(relative.parts):
            candidate /= component
            if not candidate.is_symlink():
                continue
            normalized = Path(os.path.abspath(candidate))  # noqa: PTH100
            remaining = Path(*relative.parts[index + 1 :])
            if normalized in visited:
                return candidate / remaining
            visited.add(normalized)
            target = candidate.readlink()
            if target.is_absolute():
                logical_target = Path(os.path.normpath(target))
            else:
                logical_parent = Path("/") / candidate.parent.relative_to(root)
                logical_target = Path(os.path.normpath(logical_parent / target))
            current = root / logical_target.relative_to(logical_target.anchor) / remaining
            break
        else:
            return current


def _logical_source_dir(
    source_dir: Path,
    *,
    source_home: Path,
    logical_home: Path,
) -> Path:
    """Map helper-mounted config directories to their worker-visible paths."""

    lexical_source_dir = Path(os.path.abspath(source_dir))  # noqa: PTH100
    lexical_source_home = Path(os.path.abspath(source_home))  # noqa: PTH100
    relative = Path(os.path.relpath(lexical_source_dir, lexical_source_home))
    return Path(os.path.abspath(logical_home / relative))  # noqa: PTH100


def _relative_to_home(path: Path, *, source_home: Path) -> Path:
    if path == source_home / _SERVICE_GITCONFIG_NAME:
        return Path(_SERVICE_GITCONFIG_NAME)
    # Normalize ``..`` without resolving symlinks: the parent config continues
    # to reference this lexical path after it is copied into the snapshot.
    lexical_path = Path(os.path.abspath(path))  # noqa: PTH100 - preserve symlink alias
    lexical_home = Path(os.path.abspath(source_home))  # noqa: PTH100 - same path basis
    return lexical_path.relative_to(lexical_home)


def _snapshot_relative_path(path: Path, *, source_home: Path) -> Path:
    resolved = path.resolve()
    try:
        relative = _relative_to_home(path, source_home=source_home)
    except ValueError:
        relative = None
    if relative is not None and resolved.is_relative_to(source_home.resolve()):
        return relative
    # Distinct external symlink aliases need distinct destinations because Git
    # resolves their nested relative includes from each alias's lexical parent.
    lexical_path = Path(os.path.abspath(path))  # noqa: PTH100 - preserve symlink alias
    digest = hashlib.sha256(os.fsencode(lexical_path)).hexdigest()
    return Path(_EXTERNAL_INCLUDES_DIR) / digest


def _relative_includes(config_path: Path) -> tuple[tuple[str, Path], ...]:
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

    includes: list[tuple[str, Path]] = []
    for entry in result.stdout.split(b"\0"):
        if not entry:
            continue
        raw_key, separator, raw_value = entry.partition(b"\n")
        if not separator:
            continue
        value = raw_value.decode(errors="surrogateescape")
        candidate = Path(value)
        if value.startswith("~") or candidate.is_absolute():
            continue
        key = raw_key.decode(errors="surrogateescape")
        includes.append((key, candidate))
    return tuple(includes)


def _rewrite_relative_include(
    *,
    config_path: Path,
    key: str,
    old_path: Path,
    new_path: Path,
) -> None:
    result = subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(config_path),
            "--fixed-value",
            "--replace-all",
            key,
            new_path.as_posix(),
            old_path.as_posix(),
        ],
        check=False,
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        message = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(
            f"could not rewrite relative gitconfig include: {message or result.returncode}",
        )


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
    lease_path = bundle_root / _WORKER_LEASE_NAME
    lease_existed = lease_path.exists()
    lease = lease_path.open("a+b")
    if not lease_existed:
        os.fchmod(lease.fileno(), 0o600)
    fcntl.flock(lease.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    _ACTIVE_BUNDLE_LEASES[bundle_root] = lease


def release_superseded_service_gitconfig_leases(
    *,
    snapshots_root: Path,
    protected_configs: Iterable[Path | None],
) -> None:
    """Release worker leases not needed by current or in-flight consumers."""

    protected_roots = {config.parent.parent for config in protected_configs if config is not None}
    for bundle_root, lease in tuple(_ACTIVE_BUNDLE_LEASES.items()):
        if bundle_root.parent != snapshots_root:
            continue
        if bundle_root in protected_roots:
            continue
        if _ACTIVE_BUNDLE_LEASES.get(bundle_root) is not lease:
            continue  # pragma: no cover - defensive against future concurrent callers
        del _ACTIVE_BUNDLE_LEASES[bundle_root]
        try:
            with suppress(OSError):
                fcntl.flock(lease.fileno(), fcntl.LOCK_UN)
        finally:
            with suppress(OSError):
                lease.close()


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
            os.fchmod(file_obj.fileno(), 0o644 if needs_shared_read else 0o600)
            if use_agent_ownership:
                assert owner_uid is not None and owner_gid is not None
                os.fchown(file_obj.fileno(), owner_uid, owner_gid)
            os.fsync(file_obj.fileno())


__all__ = [
    "materialize_service_gitconfig",
    "release_superseded_service_gitconfig_leases",
]
