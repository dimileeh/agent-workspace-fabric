"""Salvage dirty CI-repair output before provider recovery or terminal failure."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from awf.runtime.validation_worktree import is_under_agent_runtime_root
from awf.service._git_salvage_utils import (
    CompletedProcessBytesLike,
    CompletedProcessLike,
    SubprocessRun,
    SubprocessRunBytes,
)
from awf.service._git_salvage_utils import (
    paths_from_ls_files_z as _paths_from_ls_files_z,
)
from awf.service._git_salvage_utils import (
    paths_from_name_status as _paths_from_name_status,
)
from awf.service._git_salvage_utils import (
    run_git as _run_git_shared,
)
from awf.service._git_salvage_utils import (
    run_git_bytes as _run_git_bytes_shared,
)
from awf.service._git_salvage_utils import (
    write_nul_delimited_pathspec_file as _write_nul_delimited_pathspec_file,
)
from awf.service.conformance_salvage import INTERNAL_PLAN_ARTIFACT_PREFIX

REPAIR_SALVAGE_BASE_UNAVAILABLE = "REPAIR_SALVAGE_BASE_UNAVAILABLE"
REPAIR_SALVAGE_SOURCE_UNAVAILABLE = "REPAIR_SALVAGE_SOURCE_UNAVAILABLE"
REPAIR_SALVAGE_NO_DIFF = "REPAIR_SALVAGE_NO_DIFF"
REPAIR_SALVAGE_UNEXPECTED = "REPAIR_SALVAGE_UNEXPECTED"

_SALVAGE_DIFF_BASE_UNSET: object = object()


@dataclass(frozen=True)
class RepairSalvageCapture:
    """Captures CI-repair salvage metadata and artifact location."""

    workspace_id: str
    operation_start_head: str
    salvage_diff_base: str
    patch_path: Path
    patch_sha256: str
    patch_bytes: int
    affected_paths: list[str]
    phase: str
    operation_type: str
    operation_id: str | None
    created_at: str


class RepairSalvageError(Exception):
    """Raised when CI-repair salvage capture fails."""

    def __init__(
        self,
        *,
        reason_code: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Initialise an error with machine-readable salvage metadata."""
        self.reason_code = reason_code
        self.detail = detail or {}
        super().__init__(message)


def as_repair_salvage_details(capture: RepairSalvageCapture) -> dict[str, Any]:
    """Return compact salvage metadata for monitor push-result details."""
    return {
        "patch_path": str(capture.patch_path),
        "patch_sha256": capture.patch_sha256,
        "patch_bytes": capture.patch_bytes,
        "affected_paths": list(capture.affected_paths),
        "phase": capture.phase,
        "operation_type": capture.operation_type,
        "operation_id": capture.operation_id,
        "operation_start_head": capture.operation_start_head,
        "salvage_diff_base": capture.salvage_diff_base,
        "created_at": capture.created_at,
    }


def capture_ci_repair_salvage(
    *,
    worktrees_root: str | Path,
    artifacts_root: str | Path,
    workspace_id: str,
    operation_start_head: str | None,
    operation_id: str | None,
    operation_type: str,
    phase: str,
    salvage_diff_base: str | None | object = _SALVAGE_DIFF_BASE_UNSET,
    run_subprocess: SubprocessRun | None = None,
) -> RepairSalvageCapture:
    """Capture a binary patch and metadata for dirty CI-repair output."""
    if not operation_start_head:
        raise RepairSalvageError(
            reason_code=REPAIR_SALVAGE_BASE_UNAVAILABLE,
            message="CI repair salvage requires operation_start_head.",
        )
    if salvage_diff_base is _SALVAGE_DIFF_BASE_UNSET:
        diff_base = operation_start_head
    elif salvage_diff_base is None:
        raise RepairSalvageError(
            reason_code=REPAIR_SALVAGE_BASE_UNAVAILABLE,
            message=(
                "CI repair salvage requires salvage_diff_base when post-repair HEAD is unavailable."
            ),
        )
    else:
        diff_base = str(salvage_diff_base)

    worktree = Path(worktrees_root) / workspace_id
    if not worktree.is_dir():
        raise RepairSalvageError(
            reason_code=REPAIR_SALVAGE_SOURCE_UNAVAILABLE,
            message="CI repair worktree is unavailable for salvage.",
            detail={"worktree_path": str(worktree)},
        )

    run = run_subprocess or subprocess.run
    env_base = dict(os.environ)

    _run_git(
        worktree,
        ["cat-file", "-e", f"{diff_base}^{{commit}}"],
        run=run,
        env=env_base,
        failure_reason=REPAIR_SALVAGE_BASE_UNAVAILABLE,
    )

    artifacts_dir = Path(artifacts_root) / "salvage"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    untracked_paths = set(
        _paths_from_ls_files_z(
            _run_git(
                worktree,
                ["ls-files", "--others", "--exclude-standard", "-z"],
                run=run,
                env=env_base,
            ).stdout
        )
    )
    exclude_pathspecs = _salvage_exclude_pathspecs()

    with tempfile.TemporaryDirectory(prefix="awf-repair-salvage-index-") as tmp_dir:
        index_path = str(Path(tmp_dir) / "index")
        git_env = {**env_base, "GIT_INDEX_FILE": index_path}
        _run_git(worktree, ["read-tree", "HEAD"], run=run, env=git_env)
        _run_git(worktree, ["add", "-A", "--", "."], run=run, env=git_env)
        raw_paths = _paths_from_name_status(
            _run_git(
                worktree,
                [
                    "diff",
                    "--cached",
                    "--name-status",
                    "-z",
                    diff_base,
                    "--",
                    ".",
                    *exclude_pathspecs,
                ],
                run=run,
                env=git_env,
            ).stdout
        )
        affected_paths = sorted(
            path
            for path in raw_paths
            if not _is_untracked_runtime_artifact(path, untracked_paths=untracked_paths)
        )
        if not affected_paths:
            raise RepairSalvageError(
                reason_code=REPAIR_SALVAGE_NO_DIFF,
                message="CI repair salvage found no salvageable diff.",
            )
        pathspec_file = Path(tmp_dir) / "affected-pathspecs"
        _write_nul_delimited_pathspec_file(affected_paths, pathspec_file)
        _run_git(worktree, ["read-tree", diff_base], run=run, env=git_env)
        _run_git(
            worktree,
            [
                "--literal-pathspecs",
                "add",
                f"--pathspec-from-file={pathspec_file}",
                "--pathspec-file-nul",
            ],
            run=run,
            env=git_env,
        )
        patch = _run_git_bytes(
            worktree,
            ["diff", "--cached", "--binary", diff_base],
            run=run,
            env=git_env,
        ).stdout

    patch_sha256 = hashlib.sha256(patch).hexdigest()
    patch_path = artifacts_dir / f"{workspace_id}-{patch_sha256[:12]}.patch"
    patch_path.write_bytes(patch)

    created_at = datetime.now(UTC).isoformat()
    capture = RepairSalvageCapture(
        workspace_id=workspace_id,
        operation_start_head=operation_start_head,
        salvage_diff_base=diff_base,
        patch_path=patch_path,
        patch_sha256=patch_sha256,
        patch_bytes=len(patch),
        affected_paths=affected_paths,
        phase=phase,
        operation_type=operation_type,
        operation_id=operation_id,
        created_at=created_at,
    )
    metadata_path = patch_path.with_suffix(".json")
    metadata_path.write_text(
        json.dumps(as_repair_salvage_details(capture), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return capture


def _salvage_exclude_pathspecs() -> list[str]:
    return [f":(exclude){INTERNAL_PLAN_ARTIFACT_PREFIX}**"]


def _is_untracked_runtime_artifact(path: str, *, untracked_paths: set[str]) -> bool:
    """Return whether ``path`` is an untracked AWF-agent-runtime artifact."""
    return path in untracked_paths and is_under_agent_runtime_root(path)


def _run_git(
    worktree: Path,
    args: list[str],
    *,
    run: SubprocessRun,
    env: Mapping[str, str],
    failure_reason: str = REPAIR_SALVAGE_SOURCE_UNAVAILABLE,
) -> CompletedProcessLike:
    """Run a git command for salvage with deterministic timeout and failure mapping."""
    return _run_git_shared(
        worktree,
        args,
        run=run,
        env=env,
        raise_error=RepairSalvageError,
        failure_reason=failure_reason,
        failure_context="CI repair salvage",
    )


def _run_git_bytes(
    worktree: Path,
    args: list[str],
    *,
    run: SubprocessRun,
    env: Mapping[str, str],
    failure_reason: str = REPAIR_SALVAGE_SOURCE_UNAVAILABLE,
) -> CompletedProcessBytesLike:
    """Run a git command and return raw stdout bytes for binary patch capture."""
    return _run_git_bytes_shared(
        worktree,
        args,
        run=cast(SubprocessRunBytes, run),
        env=env,
        raise_error=RepairSalvageError,
        failure_reason=failure_reason,
        failure_context="CI repair salvage",
    )
