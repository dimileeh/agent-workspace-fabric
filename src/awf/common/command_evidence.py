"""Helpers for collecting bounded command-result evidence."""

from __future__ import annotations


def append_command_evidence(
    command_evidence: list[str] | None,
    *,
    stdout: str,
    stderr: str,
) -> None:
    """Append non-empty command output streams to an optional evidence sink."""
    if command_evidence is None:
        return
    if stdout:
        command_evidence.append(stdout)
    if stderr:
        command_evidence.append(stderr)
