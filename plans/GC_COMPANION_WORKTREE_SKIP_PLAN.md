# GC Companion Worktree Skip Plan

## Problem Statement and Scope

PR thread `PRRT_kwDOSJAM6s6FKlzW` reports that when the Git worktree-removal step fails during workspace GC execution, the result records a skipped outcome only for the primary worktree. Companion worktrees remain in the serialized candidate payload with their default planned status, which loses the explicit failure context.

Scope is limited to `src/awf/service/gc.py`, a focused regression test for the reported behavior, and this plan/validation record.

## Requirements Checklist

- Add a regression test showing companion worktrees are marked `skipped` when worktree removal fails.
- Preserve existing partial-cleanup behavior: compose/auth paths are still deleted after worktree-removal failure.
- Apply the same worktree-removal failure metadata to primary and companion worktree outcomes.
- Avoid broad AWF/GitHub-owned validation; run only focused local checks.

## Implementation Steps

1. Add a failing test covering a terminal workspace with a companion worktree and a failing `worktree_remover`.
2. Update the worktree-removal failure branch to append skipped outcomes for the primary and companion worktrees.
3. Run the focused regression test, then the nearest focused GC test file if practical.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_more2.py::test_gc_partial_worktree_remove_failure_marks_companion_worktrees_skipped -q`
  - Passes after implementation and fails before production change.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_more2.py -q`
  - Passes after implementation.

Full AWF/GitHub validation is managed by AWF after agent completion.
