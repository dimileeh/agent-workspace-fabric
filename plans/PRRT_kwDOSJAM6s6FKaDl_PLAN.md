# PRRT_kwDOSJAM6s6FKaDl Plan

## Problem Statement And Scope

The default terminal-workspace GC worktree remover currently wraps the full primary
and companion worktree removal loop in one exception handler. A failure removing
one worktree aborts remaining removals, which can leave companion worktrees
orphaned on disk. The fix is scoped to `src/awf/service/gc.py` and a focused
regression test for the default worktree remover.

## Requirements Checklist

- Add a regression test proving a failed worktree removal does not skip later
  companion worktree removals.
- Keep the overall GC result failed when any individual worktree removal fails.
- Preserve the existing successful, skipped, and single-target failure behavior.
- Do not run broad AWF/GitHub-owned validation; use focused checks only.

## Implementation Steps

1. Add a focused unit test in `tests/unit/service/test_gc_more2.py` covering
   companion worktree best-effort removal after an earlier failure.
2. Run the new focused test and confirm it fails against the current
   implementation when practical.
3. Update `_default_worktree_remover` to catch removal failures per target,
   record errors, and continue through all targets.
4. Return success only if all target removals succeed; otherwise return the
   existing failed status and reason code with an aggregated error summary.
5. Re-run targeted tests for the default worktree remover area.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_more2.py -q -k default_worktree_remover`
  - Passes after implementation.
- Full AWF/GitHub validation is intentionally not run during the agent phase;
  AWF owns broad validation, provenance, and merge gating after completion.
