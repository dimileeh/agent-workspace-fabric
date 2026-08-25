"""Detached worktree materialization for trusted-base profile snapshots."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from awf.common.profile_paths import PROFILE_MARKER_PATHS
from awf.node.git_manager_ownership import (
    TRUSTED_BASE_GIT_CONFIG_ARGS,
    git_env_for_trusted_base_materialization,
)

if TYPE_CHECKING:
    from awf.node.git_manager import GitManager, WorktreeLayout

_TRUSTED_PROFILE_MISMATCH_REASON = "GIT_TRUSTED_BASE_PROFILE_MISMATCH"
_GIT_TREE_ENTRY_OID_LEN = 20

# Paths ``detect_profile`` / Java builtin selection inspect on disk. Published as
# raw commit blobs after ``--no-checkout`` so auto-detection still works without
# running checkout filters.
_TRUSTED_BASE_AUTODETECT_PROBE_PATHS: tuple[str, ...] = (
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "bun.lock",
    "go.mod",
    "Cargo.toml",
    "CMakeLists.txt",
    "compose.yml",
    "compose.yaml",
    "docker-compose.yml",
    "docker-compose.yaml",
    "mvnw",
    "gradlew",
    "pom.xml",
    "build.gradle",
)


def _trusted_git_args(mirror_path: Path, *tail: str) -> list[str]:
    """Build a git argv that disables checkout attributes/hooks for the mirror."""
    return [
        "git",
        *TRUSTED_BASE_GIT_CONFIG_ARGS,
        "--git-dir",
        str(mirror_path),
        *tail,
    ]


async def add_detached_worktree_at_commit(
    manager: GitManager,
    *,
    workspace_id: str,
    repo_url: str,
    commit_sha: str,
) -> WorktreeLayout:
    """Materialize a detached read-only worktree at an immutable commit SHA.

    Used for trusted-base profile resolution during adopted ``sync_feature_pr``
    provisioning: the durable workspace worktree remains the PR head, while
    this ephemeral snapshot exposes the adopted target-base tree. The caller
    must always reclaim via ``remove_worktree`` (success and failure).

    Rev-parse / fetch / ``worktree add --detach --no-checkout`` run with replace
    refs, grafts, object/config overrides, and external attributes/hooks
    disabled. Profile markers and auto-detect probe files are then published
    from hash-verified raw commit blobs (commit → tree → blob OID check) so
    committed ``.gitattributes`` filter drivers and poisoned loose objects on a
    shared mirror cannot execute or authorize attacker profile bytes under an
    unchanged SHA.

    Raises ``GitOperationError`` with:
    - ``GIT_WORKTREE_ALREADY_EXISTS`` when the path is already present
    - ``GIT_BASE_BRANCH_MISSING`` when the commit cannot be resolved in the mirror
    - ``GIT_TRUSTED_BASE_PROFILE_MISMATCH`` when a profile marker appears on disk
      (including as a leaf symlink) without a matching blob in the raw commit
    """
    # Late import: ``git_manager`` loads this module while defining ``GitManager``.
    from awf.node.git_manager import GitOperationError, WorktreeLayout

    cleaned_sha = (commit_sha or "").strip()
    if not (
        len(cleaned_sha) == 40 and all(char in "0123456789abcdefABCDEF" for char in cleaned_sha)
    ):
        raise GitOperationError(
            operation="worktree.add_detached",
            returncode=1,
            stdout="",
            stderr=(
                "exact immutable full commit SHA (40 hex) is required for "
                "detached worktree materialization"
            ),
            reason_code="GIT_BASE_BRANCH_MISSING",
        )

    worktree_path = manager._worktree_path_for(workspace_id)
    mirror_path = await manager.ensure_mirror(repo_url)
    manager._worktrees_dir.mkdir(parents=True, exist_ok=True)

    if worktree_path.exists():
        raise GitOperationError(
            operation="worktree.add_detached",
            returncode=1,
            stdout="",
            stderr=f"worktree path already exists: {worktree_path}",
            reason_code="GIT_WORKTREE_ALREADY_EXISTS",
        )

    trusted_env = git_env_for_trusted_base_materialization(manager._effective_env())

    lock = manager._lock_for_mirror(mirror_path)
    async with lock:
        try:
            await manager._run(
                _trusted_git_args(
                    mirror_path,
                    "rev-parse",
                    "--verify",
                    f"{cleaned_sha}^{{commit}}",
                ),
                operation="mirror.rev-parse_commit",
                env=trusted_env,
            )
        except GitOperationError:
            # Commit may exist only on a recently updated remote tip that
            # ``ensure_mirror`` has not yet advertised as a peelable object
            # under some shallow/partial mirrors — try a targeted fetch.
            try:
                await manager._run(
                    _trusted_git_args(
                        mirror_path,
                        "fetch",
                        "--no-tags",
                        "origin",
                        cleaned_sha,
                    ),
                    operation="mirror.fetch_commit",
                    env=trusted_env,
                )
                await manager._run(
                    _trusted_git_args(
                        mirror_path,
                        "rev-parse",
                        "--verify",
                        f"{cleaned_sha}^{{commit}}",
                    ),
                    operation="mirror.rev-parse_commit",
                    env=trusted_env,
                )
            except GitOperationError as exc:
                raise GitOperationError(
                    operation="worktree.add_detached",
                    returncode=exc.returncode,
                    stdout=exc.stdout,
                    stderr=exc.stderr,
                    reason_code="GIT_BASE_BRANCH_MISSING",
                ) from exc

        # ``--no-checkout``: committed ``.gitattributes`` filter smudge commands
        # must never run; profile / detector files are published from raw blobs.
        await manager._run(
            _trusted_git_args(
                mirror_path,
                "worktree",
                "add",
                "--detach",
                "--no-checkout",
                str(worktree_path),
                cleaned_sha,
            ),
            operation="worktree.add_detached",
            env=trusted_env,
        )
        try:
            await _verify_and_materialize_trusted_profile_markers(
                manager,
                mirror_path=mirror_path,
                worktree_path=worktree_path,
                commit_sha=cleaned_sha,
                env=trusted_env,
            )
            await _materialize_trusted_base_autodetect_probes(
                manager,
                mirror_path=mirror_path,
                worktree_path=worktree_path,
                commit_sha=cleaned_sha,
                env=trusted_env,
            )
        except BaseException:
            # Leave no half-trusted snapshot path behind on verify failure.
            with contextlib.suppress(GitOperationError):
                await manager._run(
                    _trusted_git_args(
                        mirror_path,
                        "worktree",
                        "remove",
                        "--force",
                        str(worktree_path),
                    ),
                    operation="worktree.remove_trusted_base_verify_failed",
                    env=trusted_env,
                )
            if worktree_path.exists():
                # Best-effort under the mirror lock: avoid stranding a dir that
                # would trip GIT_WORKTREE_ALREADY_EXISTS on retry.
                shutil.rmtree(worktree_path, ignore_errors=True)
            raise

    # Ephemeral profile snapshot — no agent runtime will write here.
    return WorktreeLayout(
        mirror_path=mirror_path,
        worktree_path=worktree_path,
        branch_name="",
    )


async def _verify_and_materialize_trusted_profile_markers(
    manager: GitManager,
    *,
    mirror_path: Path,
    worktree_path: Path,
    commit_sha: str,
    env: dict[str, str],
) -> None:
    """Ensure profile markers match the raw commit blob (no smudge / replace).

    ``git cat-file blob <sha>:<path>`` returns object-store bytes without checkout
    filters. Disk files that exist without a blob fail closed. When the blob
    exists, the worktree file is rewritten to those raw bytes so filter poison
    cannot reach profile resolve under an unchanged commit SHA.

    Leaf symlinks from checkout are unlinked before the rewrite: ``Path.write_bytes``
    follows links, which would corrupt a relative target or overwrite an absolute
    host path under the provisioner's privileges.
    """
    from awf.node.git_manager import GitOperationError

    for relative in PROFILE_MARKER_PATHS:
        disk_path = worktree_path / relative
        raw = await _raw_commit_blob_bytes(
            manager,
            mirror_path=mirror_path,
            commit_sha=commit_sha,
            relative_path=relative,
            env=env,
        )
        # ``is_file()`` follows links; include the leaf symlink itself so a dangling
        # Git symlink marker without a blob still fails closed.
        disk_exists = disk_path.is_symlink() or disk_path.is_file()
        if raw is None:
            if disk_exists:
                raise GitOperationError(
                    operation="worktree.verify_trusted_base_profile",
                    returncode=1,
                    stdout="",
                    stderr=(
                        f"trusted-base profile marker {relative!r} present on disk "
                        "but absent from the raw commit; refusing poisoned snapshot"
                    ),
                    reason_code=_TRUSTED_PROFILE_MISMATCH_REASON,
                )
            continue
        _write_trusted_base_file(disk_path, raw)


async def _materialize_trusted_base_autodetect_probes(
    manager: GitManager,
    *,
    mirror_path: Path,
    worktree_path: Path,
    commit_sha: str,
    env: dict[str, str],
) -> None:
    """Publish detector probe files from raw blobs after ``--no-checkout``."""
    for relative in _TRUSTED_BASE_AUTODETECT_PROBE_PATHS:
        raw = await _raw_commit_blob_bytes(
            manager,
            mirror_path=mirror_path,
            commit_sha=commit_sha,
            relative_path=relative,
            env=env,
        )
        if raw is None:
            continue
        _write_trusted_base_file(worktree_path / relative, raw)


def _write_trusted_base_file(disk_path: Path, raw: bytes) -> None:
    """Write verified blob bytes, replacing a leaf symlink rather than following it."""
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    if disk_path.is_symlink():
        disk_path.unlink()
    disk_path.write_bytes(raw)


async def _raw_commit_blob_bytes(
    manager: GitManager,
    *,
    mirror_path: Path,
    commit_sha: str,
    relative_path: str,
    env: dict[str, str],
) -> bytes | None:
    """Return hash-verified raw blob bytes for ``commit:path``, or None if absent.

    Walks the commit → tree → blob chain and recomputes each object's Git OID so
    a poisoned loose object under an agent-writable shared mirror cannot supply
    attacker profile bytes under an unchanged commit SHA. Blob payloads are never
    decoded through ``GitManager._run``'s UTF-8 ``errors=replace`` path.
    """
    del manager  # interface parity with call sites; argv/env are fully explicit

    cleaned_sha = commit_sha.strip().lower()
    commit_payload = await _read_verified_git_object(
        mirror_path=mirror_path,
        env=env,
        oid=cleaned_sha,
        expected_type="commit",
    )
    tree_oid = _tree_oid_from_commit_payload(commit_payload)
    parts = [part for part in relative_path.split("/") if part]
    if not parts:
        return None
    current_tree_oid = tree_oid
    for index, part in enumerate(parts):
        tree_payload = await _read_verified_git_object(
            mirror_path=mirror_path,
            env=env,
            oid=current_tree_oid,
            expected_type="tree",
        )
        entry = _tree_entry_by_name(tree_payload, part)
        if entry is None:
            return None
        mode, entry_oid = entry
        is_last = index == len(parts) - 1
        if is_last:
            if mode.startswith("120") or mode.startswith("160"):
                # Symlink / gitlink markers are not trusted profile blobs.
                return None
            if not mode.startswith("100"):
                return None
            return await _read_verified_git_object(
                mirror_path=mirror_path,
                env=env,
                oid=entry_oid,
                expected_type="blob",
            )
        # Git tree directory mode is ``40000`` (no leading zero in the object).
        if mode != "40000" and not mode.startswith("040"):
            return None
        current_tree_oid = entry_oid
    return None


def _git_object_oid(object_type: str, payload: bytes) -> str:
    """Return the Git SHA-1 OID for an object type + payload."""
    header = f"{object_type} {len(payload)}\0".encode()
    return hashlib.sha1(header + payload).hexdigest()


def _tree_oid_from_commit_payload(payload: bytes) -> str:
    """Extract the root tree OID from a verified commit object payload."""
    from awf.node.git_manager import GitOperationError

    for line in payload.split(b"\n"):
        if line.startswith(b"tree "):
            tree_oid = line[5:].decode("ascii", errors="strict").strip().lower()
            if len(tree_oid) == 40 and all(char in "0123456789abcdef" for char in tree_oid):
                return tree_oid
            break
    raise GitOperationError(
        operation="mirror.verify_trusted_base_commit",
        returncode=1,
        stdout="",
        stderr="trusted-base commit object lacks a valid tree OID",
        reason_code=_TRUSTED_PROFILE_MISMATCH_REASON,
    )


def _tree_entry_by_name(tree_payload: bytes, name: str) -> tuple[str, str] | None:
    """Return ``(mode, oid_hex)`` for ``name`` in a verified tree payload."""
    name_bytes = name.encode("utf-8")
    offset = 0
    length = len(tree_payload)
    while offset < length:
        space = tree_payload.find(b" ", offset)
        if space < 0:
            return None
        mode = tree_payload[offset:space].decode("ascii", errors="strict")
        nul = tree_payload.find(b"\0", space + 1)
        if nul < 0:
            return None
        entry_name = tree_payload[space + 1 : nul]
        oid_start = nul + 1
        oid_end = oid_start + _GIT_TREE_ENTRY_OID_LEN
        if oid_end > length:
            return None
        entry_oid = tree_payload[oid_start:oid_end].hex()
        if entry_name == name_bytes:
            return mode, entry_oid
        offset = oid_end
    return None


async def _read_verified_git_object(
    *,
    mirror_path: Path,
    env: dict[str, str],
    oid: str,
    expected_type: str,
) -> bytes:
    """Read a Git object by OID and fail closed unless its content hashes to OID."""
    from awf.node.git_manager import GitOperationError

    args = _trusted_git_args(mirror_path, "cat-file", expected_type, oid)
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    assert proc.returncode is not None
    if proc.returncode != 0:
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        raise GitOperationError(
            operation="mirror.verify_trusted_base_object",
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            reason_code=_TRUSTED_PROFILE_MISMATCH_REASON,
        )
    actual_oid = _git_object_oid(expected_type, stdout_bytes)
    if actual_oid != oid.lower():
        raise GitOperationError(
            operation="mirror.verify_trusted_base_object",
            returncode=1,
            stdout="",
            stderr=(
                f"trusted-base {expected_type} object at {oid} failed content hash "
                f"verification (got {actual_oid})"
            ),
            reason_code=_TRUSTED_PROFILE_MISMATCH_REASON,
        )
    return stdout_bytes
