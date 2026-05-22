"""Automatic salvage of implementation diffs after conformance failures."""

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
from typing import Any, Literal, Protocol

from awf.common.git_identity import git_safe_directory_config_args
from awf.runtime.planning import build_conformance_retry_prompt

CONFORMANCE_SALVAGE_POLICY_KEY = "conformance_salvage"
INTERNAL_PLAN_ARTIFACT_PREFIX = "docs/awf-plans/"

SALVAGE_SOURCE_UNAVAILABLE = "SALVAGE_SOURCE_UNAVAILABLE"
SALVAGE_BASE_UNAVAILABLE = "SALVAGE_BASE_UNAVAILABLE"
SALVAGE_NO_IMPLEMENTATION_DIFF = "SALVAGE_NO_IMPLEMENTATION_DIFF"
SALVAGE_PATCH_UNAVAILABLE = "SALVAGE_PATCH_UNAVAILABLE"
SALVAGE_PATCH_DIGEST_MISMATCH = "SALVAGE_PATCH_DIGEST_MISMATCH"
SALVAGE_PATCH_APPLY_FAILED = "SALVAGE_PATCH_APPLY_FAILED"

CONFORMANCE_SALVAGE_APPLIED_EVENT_TYPE = "workspace.conformance_salvage_applied"
CONFORMANCE_SALVAGE_APPLIED_REASON = "CONFORMANCE_SALVAGE_APPLIED"
CONFORMANCE_SALVAGE_CONFLICT_EVENT_TYPE = "workspace.conformance_salvage_conflict"
CONFORMANCE_SALVAGE_CONFLICT_REASON = "CONFORMANCE_SALVAGE_CONFLICT"

_GIT_TIMEOUT_SECONDS = 30.0


class CompletedProcessLike(Protocol):
    """Protocol describing the small git subprocess result contract."""

    returncode: int
    stdout: str
    stderr: str


class SubprocessRun(Protocol):
    """Protocol for a subprocess runner used by salvage operations."""

    def __call__(  # pragma: no cover - Protocol method declaration only.
        self,
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: Literal[True],
        timeout: float,
        env: Mapping[str, str],
    ) -> CompletedProcessLike:
        """Execute subprocess command and return a captured result."""
        ...


@dataclass(frozen=True)
class ConformanceSalvageCapture:
    """Captures a salvage operation's metadata and artifact location."""

    source_workspace_id: str
    source_base_commit: str
    patch_path: Path
    patch_sha256: str
    patch_bytes: int
    implementation_paths: list[str]
    plan_artifact_paths: list[str]
    remaining_gaps: list[str]
    conformance_evidence_ref: dict[str, str] | None
    source_branch_name: str | None
    source_remote_push_branch: str | None
    created_at: str
    plan_path: str | None = None
    report_path: str | None = None

    def as_policy(self) -> dict[str, Any]:
        """Convert capture metadata into a plan-policy payload."""
        payload: dict[str, Any] = {
            "status": "captured",
            "source_workspace_id": self.source_workspace_id,
            "source_base_commit": self.source_base_commit,
            "patch_path": str(self.patch_path),
            "patch_sha256": self.patch_sha256,
            "patch_bytes": self.patch_bytes,
            "implementation_paths": self.implementation_paths,
            "plan_artifact_paths": self.plan_artifact_paths,
            "remaining_gaps": self.remaining_gaps,
            "conformance_evidence_ref": self.conformance_evidence_ref,
            "created_at": self.created_at,
        }
        if self.source_branch_name:
            payload["source_branch_name"] = self.source_branch_name
        if self.source_remote_push_branch:
            payload["source_remote_push_branch"] = self.source_remote_push_branch
        if self.plan_path:
            payload["plan_path"] = self.plan_path
        if self.report_path:
            payload["report_path"] = self.report_path
        return payload


class ConformanceSalvageError(Exception):
    """Raised when salvage capture or validation fails."""

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


def capture_conformance_salvage(
    *,
    work_dir: str | Path,
    source_workspace_id: str,
    source_base_commit: str | None,
    conformance_evidence: Mapping[str, Any] | None,
    conformance_evidence_ref: dict[str, str] | None,
    source_branch_name: str | None,
    source_remote_push_branch: str | None,
    run_subprocess: SubprocessRun | None = None,
) -> ConformanceSalvageCapture:
    """Capture a salvage patch and metadata from a failed workspace run."""
    if not source_base_commit:
        raise ConformanceSalvageError(
            reason_code=SALVAGE_BASE_UNAVAILABLE,
            message="Source workspace has no recorded base commit for salvage.",
        )

    root = Path(work_dir)
    source_worktree = root / "git" / "worktrees" / source_workspace_id
    if not source_worktree.is_dir():
        raise ConformanceSalvageError(
            reason_code=SALVAGE_SOURCE_UNAVAILABLE,
            message="Source workspace worktree is unavailable for salvage.",
            detail={"worktree_path": str(source_worktree)},
        )

    run = run_subprocess or subprocess.run
    env_base = dict(os.environ)

    _run_git(
        source_worktree,
        ["cat-file", "-e", f"{source_base_commit}^{{commit}}"],
        run=run,
        env=env_base,
        failure_reason=SALVAGE_BASE_UNAVAILABLE,
    )

    artifacts_dir = root / "artifacts" / "salvage"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="awf-salvage-index-") as tmp_dir:
        index_path = str(Path(tmp_dir) / "index")
        git_env = {**env_base, "GIT_INDEX_FILE": index_path}
        _run_git(source_worktree, ["read-tree", "HEAD"], run=run, env=git_env)
        _run_git(source_worktree, ["add", "-A", "--", "."], run=run, env=git_env)
        implementation_paths = _git_lines(
            _run_git(
                source_worktree,
                [
                    "diff",
                    "--cached",
                    "--name-only",
                    source_base_commit,
                    "--",
                    ".",
                    f":(exclude){INTERNAL_PLAN_ARTIFACT_PREFIX}**",
                ],
                run=run,
                env=git_env,
            ).stdout
        )
        plan_artifact_paths = _git_lines(
            _run_git(
                source_worktree,
                [
                    "diff",
                    "--cached",
                    "--name-only",
                    source_base_commit,
                    "--",
                    INTERNAL_PLAN_ARTIFACT_PREFIX,
                ],
                run=run,
                env=git_env,
            ).stdout
        )
        if not implementation_paths:
            raise ConformanceSalvageError(
                reason_code=SALVAGE_NO_IMPLEMENTATION_DIFF,
                message=(
                    "Conformance retry found no implementation diff to salvage; "
                    "only AWF plan/conformance artifacts changed."
                ),
                detail={"plan_artifact_paths": plan_artifact_paths},
            )
        patch = _run_git(
            source_worktree,
            [
                "diff",
                "--cached",
                "--binary",
                source_base_commit,
                "--",
                ".",
                f":(exclude){INTERNAL_PLAN_ARTIFACT_PREFIX}**",
            ],
            run=run,
            env=git_env,
        ).stdout.encode("utf-8")

    patch_sha256 = hashlib.sha256(patch).hexdigest()
    patch_path = artifacts_dir / f"{source_workspace_id}-{patch_sha256[:12]}.patch"
    patch_path.write_bytes(patch)

    evidence = conformance_evidence or {}
    capture = ConformanceSalvageCapture(
        source_workspace_id=source_workspace_id,
        source_base_commit=source_base_commit,
        patch_path=patch_path,
        patch_sha256=patch_sha256,
        patch_bytes=len(patch),
        implementation_paths=implementation_paths,
        plan_artifact_paths=plan_artifact_paths,
        remaining_gaps=_evidence_gaps(evidence),
        conformance_evidence_ref=conformance_evidence_ref,
        source_branch_name=source_branch_name,
        source_remote_push_branch=source_remote_push_branch,
        created_at=datetime.now(UTC).isoformat(),
        plan_path=_optional_str(evidence.get("plan_path")),
        report_path=_optional_str(evidence.get("report_path")),
    )
    metadata_path = patch_path.with_suffix(".json")
    metadata_path.write_text(
        json.dumps(capture.as_policy(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return capture


def build_conformance_salvage_retry_prompt(
    *,
    task_prompt: str,
    evidence: Mapping[str, Any],
    salvage: Mapping[str, Any],
) -> str:
    """Build a retry prompt that replays recovered conformance implementation diffs."""
    paths = _implementation_path_lines(salvage)
    return (
        "## Automatic AWF salvage\n\n"
        "AWF automatically captured the prior implementation diff from the failed "
        "conformance attempt. The retry workspace will restore that diff before "
        "the agent runs. Continue from the recovered implementation; do not "
        "restart from scratch unless the recovered code is unusable.\n\n"
        f"### Salvaged implementation paths\n{paths}\n\n"
        + build_conformance_retry_prompt(task_prompt=task_prompt, evidence=evidence)
    )


def build_agent_timeout_salvage_retry_prompt(
    *,
    task_prompt: str,
    evidence: Mapping[str, Any],
    salvage: Mapping[str, Any],
) -> str:
    """Build a retry prompt for timeout recovery with recovered implementation diff."""
    reason_code = _optional_str(evidence.get("reason_code")) or "AGENT_IDLE_TIMEOUT"
    message = _optional_str(evidence.get("message"))
    message_line = f"\n\n### Timeout message\n{message[:1000]}" if message else ""
    return (
        "## Automatic AWF timeout salvage\n\n"
        "AWF automatically captured the prior implementation diff from an agent "
        "run that timed out before it could finish. The retry workspace will "
        "restore that diff before the agent runs. Continue from the recovered "
        "implementation; do not restart from scratch unless the recovered code "
        "is unusable.\n\n"
        f"### Source reason\n`{reason_code}`{message_line}\n\n"
        f"### Salvaged implementation paths\n{_implementation_path_lines(salvage)}\n\n"
        f"### Original task\n{task_prompt}\n"
    )


def build_conformance_salvage_conflict_prompt(
    *,
    task_prompt: str,
    salvage: Mapping[str, Any],
    agent_patch_path: str,
    apply_error: str,
) -> str:
    """Build a prompt that guides a conflict-resolution retry attempt."""
    gaps = _string_list(salvage.get("remaining_gaps"))
    gap_lines = "\n".join(f"- {gap}" for gap in gaps) or "- Re-check conformance evidence."
    path_lines = _implementation_path_lines(salvage)
    return (
        "## Automatic AWF salvage conflict\n\n"
        "AWF captured the prior implementation diff, but it could not be applied "
        "cleanly to this fresh retry workspace. This is a self-recovery task: "
        "use the salvage patch as source material, resolve the conflict against "
        "the current base, and finish the original conformance gaps.\n\n"
        f"### Salvage patch\n`{agent_patch_path}`\n\n"
        f"### Salvaged implementation paths\n{path_lines}\n\n"
        f"### Remaining conformance gaps\n{gap_lines}\n\n"
        f"### Apply error\n{apply_error[:2000] or 'git apply --check failed.'}\n\n"
        f"### Original task\n{task_prompt}\n"
    )


def conformance_salvage_from_task_policy(
    task_policy: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Extract conformance-salvage metadata from a task policy blob."""
    if not isinstance(task_policy, Mapping):
        return None
    value = task_policy.get(CONFORMANCE_SALVAGE_POLICY_KEY)
    return dict(value) if isinstance(value, Mapping) else None


def _run_git(
    worktree: Path,
    args: list[str],
    *,
    run: SubprocessRun,
    env: Mapping[str, str],
    failure_reason: str = SALVAGE_SOURCE_UNAVAILABLE,
) -> CompletedProcessLike:
    """Run a git command for salvage with deterministic timeout and failure mapping."""
    result = run(
        ["git", *git_safe_directory_config_args(worktree), "-C", str(worktree), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        env=env,
    )
    if result.returncode != 0:
        raise ConformanceSalvageError(
            reason_code=failure_reason,
            message=f"git {' '.join(args)} failed during conformance salvage.",
            detail={
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-2000:],
            },
        )
    return result


def _git_lines(value: str) -> list[str]:
    """Split git output into a sorted list of non-empty changed paths."""
    return sorted(line.strip() for line in value.splitlines() if line.strip())


def _evidence_gaps(evidence: Mapping[str, Any]) -> list[str]:
    """Convert salvage evidence gaps into a stable list of non-empty strings."""
    value = evidence.get("gaps")
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _string_list(value: object) -> list[str]:
    """Return a filtered list of strings, dropping non-string and empty values."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _implementation_path_lines(salvage: Mapping[str, Any]) -> str:
    """Format captured implementation paths as prompt bullet lines with truncation."""
    implementation_paths = _string_list(salvage.get("implementation_paths"))
    paths = "\n".join(f"- `{path}`" for path in implementation_paths[:20])
    if len(implementation_paths) > 20:
        paths += f"\n- ... and {len(implementation_paths) - 20} more"
    return paths or "- No paths recorded."


def _optional_str(value: object) -> str | None:
    """Normalize an optional string value to ``str`` or ``None``."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
