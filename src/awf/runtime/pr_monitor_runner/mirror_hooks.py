"""Mirror hooks-path repair diagnostics for PR monitor operations."""

from __future__ import annotations

from pathlib import Path

from awf.node.git_manager import GitOperationError

_EVIDENCE_TEXT_LIMIT = 1000


def mirror_hooks_repair_failure_details(
    exc: BaseException,
    *,
    repair_stage: str,
    mirror_path: Path | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return structured, bounded evidence for a failed mirror hooks repair."""
    details: dict[str, object] = {
        "mirror_hooks_repair_failed": True,
        "repair_stage": repair_stage,
        "error_type": exc.__class__.__name__,
    }
    if mirror_path is not None:
        details["mirror_path"] = str(mirror_path)
    if isinstance(exc, GitOperationError):
        details.update(
            {
                "repair_reason_code": exc.reason_code,
                "git_operation": exc.operation,
                "git_returncode": exc.returncode,
                "stderr": exc.stderr[:_EVIDENCE_TEXT_LIMIT],
                "stdout": exc.stdout[:_EVIDENCE_TEXT_LIMIT],
            }
        )
    else:
        details["error"] = str(exc)[:_EVIDENCE_TEXT_LIMIT]
    if extra:
        details.update(extra)
    return details
