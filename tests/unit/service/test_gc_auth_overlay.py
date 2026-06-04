from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from awf.service.gc import WorkspaceGCCandidate
from awf.service.gc_auth_overlay import _auth_unmount_skipped_outcome
from awf.service.gc_classify import WorkspaceGCPath


def _gc_path(kind: str) -> WorkspaceGCPath:
    return WorkspaceGCPath(kind=kind, path=Path(f"/work/{kind}"), exists=True, estimated_bytes=1)


def _candidate() -> WorkspaceGCCandidate:
    return WorkspaceGCCandidate(
        workspace_id="ws-1",
        status="destroyed",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        age_hours=1,
        reason_code="GC_ELIGIBLE",
        worktree=_gc_path("worktree"),
        compose=_gc_path("compose"),
        auth=_gc_path("auth"),
    )


def test_auth_unmount_skipped_outcome_carries_failure_reason() -> None:
    outcome = _auth_unmount_skipped_outcome(
        _candidate(),
        _gc_path("auth"),
        ("CLAUDE_AUTH_OVERLAY_UNMOUNT_FAILED", "target is busy"),
    )

    assert outcome.status == "skipped"
    assert outcome.reason_code == "CLAUDE_AUTH_OVERLAY_UNMOUNT_FAILED"
    assert outcome.error == "target is busy"


def test_auth_unmount_skipped_outcome_rejects_missing_failure() -> None:
    # The production guard (``_auth_overlay_unmount_skips_target``) never lets a
    # ``None`` failure reach here; the explicit raise replaces an ``assert`` that
    # ``python -O`` would strip, so a bypassed guard fails loudly rather than with
    # an opaque tuple-unpack ``TypeError``.
    with pytest.raises(ValueError, match="without an unmount failure"):
        _auth_unmount_skipped_outcome(_candidate(), _gc_path("auth"), None)
