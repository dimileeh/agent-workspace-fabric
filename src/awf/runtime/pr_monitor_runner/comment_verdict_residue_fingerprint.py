"""Correction-attempt residue fingerprint API for verdict protocol retries.

Kept separate so ``comment_verdict_residue`` stays under the first-party line budget.
Hashing / nested-probe helpers remain in ``comment_verdict_residue``; this module owns
porcelain decode and the correction fingerprint / mutation predicates.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from awf.common.commands import AsyncioSubprocessRunner, CommandResult
from awf.common.logging import get_logger
from awf.common.redaction import redact_secrets
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command

if TYPE_CHECKING:
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)

# Item-start local Git config snapshots keyed by resolved worktree path so
# protocol-retry rollback can restore config-only mutations
# (PRRT_kwDOSJAM6s6e0Xdl) without threading the blob through every call site.
_ITEM_START_LOCAL_GIT_CONFIGS: dict[str, dict[str, dict[str, str]]] = {}

_LOCAL_GIT_CONFIG_NAMES: tuple[str, ...] = ("config", "config.worktree")


def _decode_porcelain_status_stdout(
    *,
    stdout: str,
    stdout_bytes: bytes | None,
) -> tuple[str, bool]:
    """Return decoded porcelain and whether NUL-delimited ``-z`` records are present."""
    if stdout_bytes is not None:
        return stdout_bytes.decode("utf-8", errors="surrogateescape"), True
    if "\0" in stdout:
        return stdout, True
    return stdout, False


def _format_porcelain_z_line(status: str, path: str, original_path: str | None) -> str:
    if original_path:
        return f"{status} {original_path} -> {path}"
    return f"{status} {path}"


def _snapshot_worktree_local_git_configs(
    worktree_path: Path,
) -> dict[str, dict[str, str]] | None:
    """Return ``{resolved_git_dir: {config_name: text}}`` or ``None`` to fail closed."""
    from awf.node.git_manager_ownership import (
        _nested_repository_git_dirs_for_include_scan,
        _snapshot_git_dir_local_configs,
    )
    from awf.runtime.pr_monitor_runner.comment_verdict_residue_nested import (
        _approved_git_metadata_roots,
    )

    roots = _approved_git_metadata_roots(worktree_path)
    git_dirs = _nested_repository_git_dirs_for_include_scan(
        worktree_path,
        containment_roots=roots if roots else None,
    )
    if git_dirs is None:
        return None
    out: dict[str, dict[str, str]] = {}
    for git_dir in git_dirs:
        snap = _snapshot_git_dir_local_configs(git_dir)
        if snap is None:
            return None
        try:
            key = str(git_dir.resolve())
        except OSError:
            return None
        out[key] = dict(snap)
    return out


def _hash_local_git_config_snapshot(snapshot: dict[str, dict[str, str]]) -> str:
    digest = hashlib.sha256()
    for git_dir in sorted(snapshot):
        configs = snapshot[git_dir]
        for name in sorted(configs):
            digest.update(git_dir.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(configs[name].encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
    return digest.hexdigest()


def _fingerprint_with_git_metadata(
    worktree_path: Path,
    path_fingerprint: str,
) -> str | None:
    """Append ``git-meta:<sha256>`` so config-only mutations are visible."""
    snapshot = _snapshot_worktree_local_git_configs(worktree_path)
    if snapshot is None:
        return None
    meta_line = f"git-meta:{_hash_local_git_config_snapshot(snapshot)}"
    if not path_fingerprint:
        return meta_line
    return f"{path_fingerprint}\n{meta_line}"


def _fingerprint_has_pr_worthy_path_residue(fingerprint: str) -> bool:
    """True when fingerprint lines include porcelain/path residue (not git-meta)."""
    return any(
        line.strip() and not line.startswith("git-meta:") for line in fingerprint.splitlines()
    )


def remember_item_start_local_git_configs(worktree_path: Path) -> bool:
    """Snapshot worktree-local Git configs for later protocol-retry restore."""
    if not worktree_path.exists():
        return True
    snapshot = _snapshot_worktree_local_git_configs(worktree_path)
    if snapshot is None:
        return False
    try:
        key = str(worktree_path.resolve())
    except OSError:
        return False
    _ITEM_START_LOCAL_GIT_CONFIGS[key] = snapshot
    return True


def _write_local_git_config_file(path: Path, text: str) -> bool:
    """Replace a local config file without following symlinks."""
    encoded = text.encode("utf-8", errors="surrogateescape")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o644)
    except OSError:
        return False
    try:
        try:
            st = os.fstat(fd)
        except OSError:
            return False
        if not stat.S_ISREG(st.st_mode):
            return False
        remaining = memoryview(encoded)
        while remaining:
            try:
                written = os.write(fd, remaining)
            except OSError:
                return False
            if written <= 0:  # pragma: no cover - defensive
                return False
            remaining = remaining[written:]
    finally:
        os.close(fd)
    return True


def _restore_worktree_local_git_configs(snapshot: dict[str, dict[str, str]]) -> bool:
    """Rewrite snapshotted local configs and remove agent-created extras."""
    for git_dir_key, configs in snapshot.items():
        git_dir = Path(git_dir_key)
        for name in _LOCAL_GIT_CONFIG_NAMES:
            path = git_dir / name
            if name in configs:
                if not _write_local_git_config_file(path, configs[name]):
                    return False
                continue
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                continue
            except OSError:
                return False
            if stat.S_ISLNK(mode) or stat.S_ISREG(mode):
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                except OSError:
                    return False
            else:
                return False
    return True


def restore_item_start_local_git_configs(worktree_path: Path) -> bool:
    """Restore the remembered item-start local Git config snapshot, if any."""
    if not worktree_path.exists():
        return True
    try:
        key = str(worktree_path.resolve())
    except OSError:
        return False
    snapshot = _ITEM_START_LOCAL_GIT_CONFIGS.get(key)
    if snapshot is None:
        return True
    return _restore_worktree_local_git_configs(snapshot)


def _porcelain_status_bytes_from_nul_records(records: tuple[bytes, ...]) -> bytes:
    if not records:
        return b""
    return b"\0".join(records) + b"\0"


async def _read_ordinary_porcelain_status(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    git_env: dict[str, str],
) -> CommandResult | None:
    """Read ordinary ``git status --porcelain -z`` without unbounded communicate().

    ``AsyncioSubprocessRunner.run`` materializes stdout via ``communicate()``
    before any caller-side byte check, so a path-name flood can pin hundreds of
    megabytes per concurrent monitor. Stream through the same capped NUL reader
    nested probes already use (PRRT_kwDOSJAM6s6eutWq). Test doubles still inject
    porcelain via ``runner.run``.
    """
    from awf.node.git_manager import (
        DISABLE_LOCAL_FSMONITOR_GIT_CONFIG_ARGS,
        FORCE_CASE_SENSITIVE_PATHS_GIT_CONFIG_ARGS,
        FORCE_FILE_MODE_TRACKING_GIT_CONFIG_ARGS,
        FORCE_SYMLINK_TRACKING_GIT_CONFIG_ARGS,
    )
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as _residue

    # Agent-set ``core.ignoreCase=true`` on a case-sensitive worker hides
    # ``FOO`` beside tracked ``foo`` from porcelain status (PRRT_kwDOSJAM6s6ex8lZ).
    # Agent-set ``core.fileMode=false`` hides executable-bit flips the same way
    # (PRRT_kwDOSJAM6s6ey_47); nested probes already force ``core.fileMode=true``.
    # Agent-set ``core.symlinks=false`` hides symlink→file typechanges the same
    # way (PRRT_kwDOSJAM6s6ezrHU); nested probes already force ``core.symlinks=true``.
    # Agent-set ``core.fsmonitor`` can prime then omit tracked edits from status
    # (PRRT_kwDOSJAM6s6e0BJS); nested probes already clear ``core.fsmonitor``.
    command = git_worktree_command(
        worktree_path,
        *FORCE_CASE_SENSITIVE_PATHS_GIT_CONFIG_ARGS,
        *FORCE_FILE_MODE_TRACKING_GIT_CONFIG_ARGS,
        *FORCE_SYMLINK_TRACKING_GIT_CONFIG_ARGS,
        *DISABLE_LOCAL_FSMONITOR_GIT_CONFIG_ARGS,
        "status",
        "--porcelain",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    runner_impl = runner._deps.runner
    if isinstance(runner_impl, AsyncioSubprocessRunner):
        records = await _residue.asyncio.to_thread(
            _residue._run_ordinary_porcelain_status_capped,
            command,
            git_env=git_env,
        )
        if records is None:
            return None
        raw = _porcelain_status_bytes_from_nul_records(records)
        return CommandResult(
            returncode=0,
            stdout=raw.decode("utf-8", errors="surrogateescape"),
            stderr="",
            stdout_bytes=raw,
        )
    return await runner_impl.run(
        command,
        env=git_env,
        timeout_seconds=_residue._RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
    )


async def _read_correction_pr_worthy_residue_fingerprint(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
) -> str | None:
    """Return a fingerprint of PR-worthy dirty porcelain plus local Git metadata.

    Empty string means missing worktree. A ``git-meta:<sha256>``-only value means
    clean path residue. ``None`` means the status/metadata probe failed and
    callers must fail closed. Untracked AWF-agent-runtime paths are excluded,
    matching the commit sink's dirtiness filter.

    Path names alone are not enough: when attempt 0 leaves ``src/x.py`` dirty and
    the correction edits that same file, a path-only fingerprint collides and
    attribution treats the mutation as pre-existing residue
    (PRRT_kwDOSJAM6s6eKj9D). Include staged/unstaged diff hashes and untracked
    file content identity while retaining the runtime-path exclusion.

    Local Git config is included so config-only mutations (for example
    ``url.*.insteadOf``) cannot collide with a clean fingerprint
    (PRRT_kwDOSJAM6s6e0Xdl).
    """
    # Resolve helpers via the residue module object so monkeypatches on
    # ``comment_verdict_residue`` (asyncio.to_thread, hash callees, scan budget)
    # continue to apply after this split.
    from awf.runtime.pr_monitor_runner import comment_verdict_residue as _residue

    if not worktree_path.exists():
        return ""

    from awf.node.git_manager import git_env_without_object_lookup_overrides
    from awf.runtime.pr_monitor_runner.path_parsing import (
        _changed_paths_from_porcelain,
        _changed_paths_from_porcelain_z,
        _porcelain_z_records,
        _untracked_paths_from_porcelain,
        _untracked_paths_from_porcelain_z,
    )
    from awf.runtime.validation_worktree import is_under_agent_runtime_root

    git_env = git_env_without_object_lookup_overrides()

    try:
        status = await _read_ordinary_porcelain_status(
            runner,
            worktree_path=worktree_path,
            git_env=git_env,
        )
    except OSError as exc:
        # Spawn failures (e.g. OSError from create_subprocess_exec) must fail
        # closed like a non-ok status so the correction mutation path rolls back
        # unaccepted dirty edits (PRRT_kwDOSJAM6s6eJi5X). Do not swallow
        # programming errors such as TypeError (review 5096023656).
        _log.warning(
            "monitor.agent_verdict_correction_residue_status_failed",
            workspace_id=workspace_id,
            exc_type=type(exc).__name__,
            error=redact_secrets(str(exc))[:400],
        )
        return None
    if status is None or not status.ok:
        _log.warning(
            "monitor.agent_verdict_correction_residue_status_failed",
            workspace_id=workspace_id,
            returncode=None if status is None else status.returncode,
            stderr="" if status is None else redact_secrets(status.stderr or "")[:400],
        )
        return None

    status_stdout, is_z = _decode_porcelain_status_stdout(
        stdout=status.stdout or "",
        stdout_bytes=status.stdout_bytes,
    )
    raw_status = (
        status.stdout_bytes
        if status.stdout_bytes is not None
        else status_stdout.encode("utf-8", errors="surrogateescape")
    )
    if len(raw_status) > _residue._RESIDUE_ORDINARY_GIT_MAX_STDOUT_BYTES:
        _log.warning(
            "monitor.agent_verdict_correction_residue_status_failed",
            workspace_id=workspace_id,
            stdout_bytes=len(raw_status),
        )
        return None
    if is_z:
        if status.stdout_bytes is not None and not status.stdout_bytes.strip(b"\0"):
            return _fingerprint_with_git_metadata(worktree_path, "")
        if (
            status.stdout_bytes is None and not status_stdout.strip()
        ):  # pragma: no cover - NUL survives strip
            return _fingerprint_with_git_metadata(worktree_path, "")
    elif not status_stdout.strip():
        return _fingerprint_with_git_metadata(worktree_path, "")

    if is_z:
        untracked = set(_untracked_paths_from_porcelain_z(status_stdout))
        paths = sorted(
            path
            for path in _changed_paths_from_porcelain_z(status_stdout)
            if not (path in untracked and is_under_agent_runtime_root(path))
        )
    else:
        untracked = set(_untracked_paths_from_porcelain(status_stdout))
        paths = sorted(
            path
            for path in _changed_paths_from_porcelain(status_stdout)
            if not (path in untracked and is_under_agent_runtime_root(path))
        )
    if not paths:
        return _fingerprint_with_git_metadata(worktree_path, "")

    tracked_paths = [path for path in paths if path not in untracked]

    # Status identity: keep XY codes for PR-worthy paths (not path names alone).
    path_set = set(paths)
    if is_z:
        status_lines = sorted(
            _format_porcelain_z_line(status_code, path, original_path)
            for status_code, path, original_path in _porcelain_z_records(status_stdout)
            if path in path_set or (original_path is not None and original_path in path_set)
        )
    else:
        status_lines = sorted(
            line
            for line in status_stdout.splitlines()
            if line
            and any(
                candidate in path_set for candidate in _changed_paths_from_porcelain(f"{line}\n")
            )
        )

    try:
        with _residue._residue_fingerprint_nested_scan_budget():
            if tracked_paths:
                staged_digest, unstaged_digest = await _residue.asyncio.to_thread(
                    _residue._hash_tracked_residue_staged_and_unstaged,
                    worktree_path=worktree_path,
                    git_env=git_env,
                )
            else:
                empty_digest = hashlib.sha256().hexdigest()
                staged_digest = unstaged_digest = empty_digest
            if staged_digest is None or unstaged_digest is None:
                _log.warning(
                    "monitor.agent_verdict_correction_residue_diff_failed",
                    workspace_id=workspace_id,
                    staged_digest=staged_digest,
                    unstaged_digest=unstaged_digest,
                )
                return None

            try:
                untracked_digest = await _residue.asyncio.to_thread(
                    _residue._hash_untracked_residue_paths,
                    worktree_path=worktree_path,
                    paths=paths,
                    untracked=untracked,
                    git_env=git_env,
                )
            except OSError as exc:
                # Hash helpers raise OSError on spawn/IO failure; programming
                # errors must propagate (review 5096023656).
                _log.warning(
                    "monitor.agent_verdict_correction_residue_untracked_failed",
                    workspace_id=workspace_id,
                    exc_type=type(exc).__name__,
                    error=redact_secrets(str(exc))[:400],
                )
                return None
    except OSError as exc:
        _log.warning(
            "monitor.agent_verdict_correction_residue_diff_failed",
            workspace_id=workspace_id,
            exc_type=type(exc).__name__,
            error=redact_secrets(str(exc))[:400],
        )
        return None
    if untracked_digest is None:
        _log.warning(
            "monitor.agent_verdict_correction_residue_untracked_unreadable",
            workspace_id=workspace_id,
        )
        return None

    path_fingerprint = "\n".join(
        [
            *status_lines,
            f"staged:{staged_digest}",
            f"unstaged:{unstaged_digest}",
            f"untracked:{untracked_digest}",
        ]
    )
    return _fingerprint_with_git_metadata(worktree_path, path_fingerprint)


def _correction_authored_mutation_vs_start(
    *,
    attempt_start_head: str | None,
    pre_sink_head: str | None,
    correction_start_residue_fp: str | None,
    pre_sink_residue_fp: str | None,
) -> bool:
    """True when the correction agent mutated HEAD or dirt before the commit sink."""
    if pre_sink_head is None:
        # Cannot observe pre-sink HEAD — fail closed (PRRT_kwDOSJAM6s6eKoIe).
        return True
    if attempt_start_head is not None and pre_sink_head.lower() != attempt_start_head.lower():
        return True
    if pre_sink_residue_fp is None:
        # Cannot observe post-agent dirt — fail closed.
        return True
    if correction_start_residue_fp is None:
        # Unreadable baseline: dirty-to-clean correction mutations are
        # unverifiable (PRRT_kwDOSJAM6s6eU900).
        return True
    return pre_sink_residue_fp != correction_start_residue_fp


def _stranded_residue_is_correction_mutation(
    *,
    correction_start_residue_fp: str | None,
    post_residue_fp: str | None,
) -> bool:
    """True when post-sink stranded dirt is not attributable to correction-start."""
    if post_residue_fp is None:
        return True
    if correction_start_residue_fp is None:
        # Unreadable baseline: empty post-sink residue cannot prove no correction
        # mutation (PRRT_kwDOSJAM6s6eU900).
        return True
    return post_residue_fp != correction_start_residue_fp


async def _correction_attempt_left_pr_worthy_residue(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
) -> bool:
    """True when uncommitted PR-worthy dirt remains after the commit sink.

    ``_commit_dirty_worktree`` may return False after status/add/commit failure
    while leaving correction edits dirty. HEAD can stay at attempt-start with
    ``dirty_changes_committed`` False, so mutation detection must probe porcelain
    before rollback accepts a non-FIXED correction verdict. Status inspection
    failure fails closed. Untracked AWF-agent-runtime paths are excluded, matching
    the commit sink's dirtiness filter.
    """
    fingerprint = await _read_correction_pr_worthy_residue_fingerprint(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
    )
    if fingerprint is None:
        return True
    return _fingerprint_has_pr_worthy_path_residue(fingerprint)
