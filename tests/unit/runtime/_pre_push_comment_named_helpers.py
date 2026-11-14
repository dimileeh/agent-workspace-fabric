"""Shared probe builders for the comment-named target evidence tests (issue #1232).

Extracted from ``test_pre_push_validation_fix_pass_ancestry_comment_named.py`` so
that module and its statement-boundary sibling stay under the first-party file
line limit enforced by ``tests/unit/test_core_decomposition_maintainability.py``.
"""

from __future__ import annotations

from pathlib import Path

from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_comment_named as comment_named,
)
from tests.unit.runtime._pre_push_ancestry_cross_file_helpers import _LEFT, _RIGHT, _Probe

_DOC = "docs/runbooks/vm-core-update.md"
_ROUTES = "src/app/api/routes/team.py"
_INBOUND = "src/app/messaging/inbound_types.py"
_CHANNEL = "src/app/agents_management/channel_service.py"
_WEB = "src/app/web/team.ts"
_OTHER = "src/app/control/queue_sweeper.py"


def _diff(path: str, header: str, *body: str) -> str:
    return f"--- a/{path}\n+++ b/{path}\n{header}\n" + "".join(f"{line}\n" for line in body)


def _probe(
    *,
    texts: dict[str, str | None] | None = None,
    changed_paths: tuple[str, ...] | None,
    diffs: dict[str, str | None] | None = None,
) -> _Probe:
    return _Probe(
        texts={(_LEFT, path): text for path, text in (texts or {}).items()},
        changed_paths=changed_paths,
        diffs=dict(diffs or {}),
    )


async def _changes_named_target(runner: object, comment_text: str) -> bool:
    return await comment_named._commit_range_changes_comment_named_target(
        runner,
        worktree_path=Path("/tmp/ws"),
        left=_LEFT,
        right=_RIGHT,
        comment_text=comment_text,
    )
