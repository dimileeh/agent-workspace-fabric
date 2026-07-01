"""Shared helpers for post-operation residue cleanup regression tests."""

from __future__ import annotations

from pathlib import Path

from awf.common.commands import FakeCommandRunner


def seed_oneline_capture_residue(
    worktree: Path,
    path: str,
    *,
    content: str = "",
) -> None:
    """Create on-disk content matching a provable ``git log --oneline`` accident."""
    file_path = worktree / path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


def queue_post_operation_residue_proof_commands(
    cmd: FakeCommandRunner,
    *,
    worktree: Path,
    residue_path: str = "--oneline",
    residue_content: str = "",
) -> None:
    """Queue git commands proving ``--oneline`` is safe post-operation residue."""
    seed_oneline_capture_residue(worktree, residue_path, content=residue_content)
    cmd.queue_result(returncode=0, stdout="")  # unstaged delta: staged-only residue
    cmd.queue_result(returncode=128, stdout="")  # cat-file: path absent at HEAD


def queue_residue_cleanup_anchor_and_delta(
    cmd: FakeCommandRunner,
    *,
    head_sha: str,
    owned_delta_z: str,
) -> None:
    """Queue pinned cleanup-anchor HEAD and committed-delta diff for residue cleanup."""
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # pinned cleanup anchor HEAD
    cmd.queue_result(returncode=0, stdout=owned_delta_z)  # residue gate committed delta


def queue_residue_cleanup_execution(
    cmd: FakeCommandRunner,
    *,
    head_sha: str,
    head_after: str | None = None,
    restore_returncode: int = 0,
    restore_stderr: str = "",
) -> None:
    """Queue pre-cleanup HEAD verify, scoped restore/clean, and post-cleanup HEAD check."""
    cmd.queue_result(returncode=0, stdout=f"{head_sha}\n")  # pre-cleanup HEAD verify
    cmd.queue_result(
        returncode=restore_returncode,
        stdout="",
        stderr=restore_stderr,
    )
    cmd.queue_result(
        returncode=0,
        stdout=f"{(head_after if head_after is not None else head_sha)}\n",
    )  # head_after
