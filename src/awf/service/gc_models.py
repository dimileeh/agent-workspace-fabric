"""Inspectable data structures for terminal service-workspace GC.

The pure-data status vocabularies and models (candidates, plan, result) live in
:mod:`awf.service.gc_classify` (statuses) and :mod:`awf.service.gc_results`
(models) so the orchestration module stays under the first-party line limit.
This module re-exports them alongside the GC callback type aliases, so
``awf.service.gc.<name>`` (which imports from here) stays the stable public
surface for callers and tests.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from awf.service.gc_classify import (
    PROTECTED_WORKSPACE_GC_STATUSES as PROTECTED_WORKSPACE_GC_STATUSES,
)
from awf.service.gc_classify import (
    TERMINAL_WORKSPACE_GC_STATUSES as TERMINAL_WORKSPACE_GC_STATUSES,
)
from awf.service.gc_results import WorkspaceGCCandidate as WorkspaceGCCandidate
from awf.service.gc_results import WorkspaceGCComposeTeardownResult
from awf.service.gc_results import WorkspaceGCPlan as WorkspaceGCPlan
from awf.service.gc_results import WorkspaceGCResult as WorkspaceGCResult
from awf.service.gc_worktrees import WorkspaceGCWorktreeRemoveResult

WorkspaceGCComposeTeardown = Callable[
    [WorkspaceGCCandidate],
    WorkspaceGCComposeTeardownResult | Awaitable[WorkspaceGCComposeTeardownResult],
]

WorkspaceGCWorktreeRemove = Callable[
    [WorkspaceGCCandidate],
    WorkspaceGCWorktreeRemoveResult | Awaitable[WorkspaceGCWorktreeRemoveResult],
]

# Prunes stale cached companion images once per GC run (independent of the
# per-workspace candidates). Returns a small report dict for the GC payload.
CompanionImagePrune = Callable[[], Awaitable[dict[str, object]]]

# Reaps superseded shared ``~/.claude`` overlay bases once per GC run (GC-B, #389),
# independent of the per-workspace candidates. The argument is the set of candidate
# auth dirs to treat as already pruned (their ``base.signature`` pins ignored) so a
# dry-run preview matches what the same candidate set frees on execute. Returns an
# inspectable report dict.
ClaudeBaseReap = Callable[[frozenset[Path]], Awaitable[dict[str, object]]]
