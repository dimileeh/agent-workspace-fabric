"""Validation side-effect failure helpers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from awf.runtime.validation import ValidationCommandResult, ValidationResult
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED,
    ValidationWorktreeCleanup,
)


def _safe_validation_artifact_name(value: str) -> str:
    """Return a filesystem-safe artifact name for validation evidence."""
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
    return safe or "validation"


def _cleaned_side_effect_paths(cleanup_result: ValidationWorktreeCleanup) -> tuple[str, ...]:
    """Return stable path evidence for cleaned validation side effects."""
    return cleanup_result.side_effect_paths


def _side_effect_failure_result(
    *,
    val_result: ValidationResult,
    cleanup_result: ValidationWorktreeCleanup,
    workspace_id: str,
    validation_run_id: str,
    artifacts_root: Path,
) -> ValidationResult:
    """Add a synthetic validation failure for cleaned successful side effects."""
    side_effect_paths = _cleaned_side_effect_paths(cleanup_result)
    paths_text = ", ".join(side_effect_paths) if side_effect_paths else "<unknown>"
    artifacts_dir = artifacts_root / workspace_id / "validation_worktree"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    safe_validation_run_id = _safe_validation_artifact_name(validation_run_id)
    stdout_path = artifacts_dir / f"{safe_validation_run_id}.side_effects.stdout"
    stderr_path = artifacts_dir / f"{safe_validation_run_id}.side_effects.stderr"
    stdout_path.write_text(
        (
            "AWF validation commands passed only before validation worktree cleanup "
            "restored or deleted side effects. The restored commit state was not "
            f"validated. Cleaned paths: {paths_text}."
        ),
        encoding="utf-8",
    )
    stderr_path.write_text("", encoding="utf-8")
    command = ValidationCommandResult(
        command="validation worktree side-effect guard",
        returncode=1,
        duration_seconds=0.0,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        reason_code=VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED,
        policy_failed=True,
        metadata={
            "cleaned_paths": list(side_effect_paths),
            "restore_ref": cleanup_result.restore_ref,
        },
    )
    return replace(val_result, commands=[*val_result.commands, command])
