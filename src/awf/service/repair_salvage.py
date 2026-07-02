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
from typing import Any

from awf.runtime.validation_worktree import is_under_agent_runtime_root
from awf.runtime.validation_worktree_constants import AWF_AGENT_RUNTIME_IGNORED_ROOTS
from awf.service._git_salvage_utils import (
    CompletedProcessLike,
    SubprocessRun,
)
from awf.service._git_salvage_utils import (
    git_lines as _git_lines,
)
from awf.service._git_salvage_utils import (
    run_git as _run_git_shared,
)
from awf.service.conformance_salvage import INTERNAL_PLAN_ARTIFACT_PREFIX

REPAIR_SALVAGE_BASE_UNAVAILABLE = "REPAIR_SALVAGE_BASE_UNAVAILABLE"
REPAIR_SALVAGE_SOURCE_UNAVAILABLE = "REPAIR_SALVAGE_SOURCE_UNAVAILABLE"
REPAIR_SALVAGE_NO_DIFF = "REPAIR_SALVAGE_NO_DIFF"
REPAIR_SALVAGE_UNEXPECTED = "REPAIR_SALVAGE_UNEXPECTED"


@dataclass(frozen=True)
class RepairSalvageCapture:
    """Captures CI-repair salvage metadata and artifact location."""

    workspace_id: str
    operation_start_head: str
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
    run_subprocess: SubprocessRun | None = None,
) -> RepairSalvageCapture:
    """Capture a binary patch and metadata for dirty CI-repair output."""
    if not operation_start_head:
        raise RepairSalvageError(
            reason_code=REPAIR_SALVAGE_BASE_UNAVAILABLE,
            message="CI repair salvage requires operation_start_head.",
        )

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
        ["cat-file", "-e", f"{operation_start_head}^{{commit}}"],
        run=run,
        env=env_base,
        failure_reason=REPAIR_SALVAGE_BASE_UNAVAILABLE,
    )

    artifacts_dir = Path(artifacts_root) / "salvage"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    exclude_pathspecs = _salvage_exclude_pathspecs()

    with tempfile.TemporaryDirectory(prefix="awf-repair-salvage-index-") as tmp_dir:
        index_path = str(Path(tmp_dir) / "index")
        git_env = {**env_base, "GIT_INDEX_FILE": index_path}
        _run_git(worktree, ["read-tree", "HEAD"], run=run, env=git_env)
        _run_git(worktree, ["add", "-A", "--", "."], run=run, env=git_env)
        raw_paths = _git_lines(
            _run_git(
                worktree,
                [
                    "diff",
                    "--cached",
                    "--name-only",
                    operation_start_head,
                    "--",
                    ".",
                    *exclude_pathspecs,
                ],
                run=run,
                env=git_env,
            ).stdout
        )
        affected_paths = sorted(path for path in raw_paths if not is_under_agent_runtime_root(path))
        if not affected_paths:
            raise RepairSalvageError(
                reason_code=REPAIR_SALVAGE_NO_DIFF,
                message="CI repair salvage found no salvageable diff.",
            )
        patch = _run_git(
            worktree,
            [
                "diff",
                "--cached",
                "--binary",
                operation_start_head,
                "--",
                ".",
                *exclude_pathspecs,
            ],
            run=run,
            env=git_env,
        ).stdout.encode("utf-8")

    patch_sha256 = hashlib.sha256(patch).hexdigest()
    patch_path = artifacts_dir / f"{workspace_id}-{patch_sha256[:12]}.patch"
    patch_path.write_bytes(patch)

    created_at = datetime.now(UTC).isoformat()
    capture = RepairSalvageCapture(
        workspace_id=workspace_id,
        operation_start_head=operation_start_head,
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
    excludes = [f":(exclude){INTERNAL_PLAN_ARTIFACT_PREFIX}**"]
    for root in AWF_AGENT_RUNTIME_IGNORED_ROOTS:
        normalized = root.rstrip("/")
        # Directory-only: mirror ``is_under_agent_runtime_root`` — suppress the
        # ignored root directory and descendants, not a regular file spelled
        # exactly ``.claude/agent-memory`` (no trailing slash).
        excludes.append(f":(exclude){normalized}/")
        excludes.append(f":(exclude){normalized}/**")
    return excludes


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
