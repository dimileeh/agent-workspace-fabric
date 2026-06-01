# PRRT_kwDOSJAM6s6GEhxU Plan

## Problem Statement And Scope

The review thread reports that validation worktree cleanup can ignore tracked
modifications under caller-provided ignored roots. The scope is limited to
`src/awf/runtime/validation_worktree.py` and focused unit coverage in
`tests/unit/runtime/test_validation_worktree.py`.

## Requirements Checklist

- Preserve the ability to ignore pre-existing ignored/untracked roots during
  validation checks and cleanup.
- Ensure tracked modifications under ignored roots are still reported as dirty.
- Ensure cleanup restores tracked modifications under ignored roots when a
  restore reference is available.
- Keep validation focused to the targeted unit tests; broad AWF/GitHub
  validation remains managed after agent completion.

## Implementation Steps

1. Add a regression test showing `check_validation_worktree_clean` reports a
   tracked file under an ignored root even when ignored roots are ignored.
2. Add a cleanup regression test showing tracked files under ignored roots are
   restored instead of being treated as clean.
3. Adjust path filtering so ignored-root filtering only suppresses untracked or
   ignored entries, not tracked changes.
4. Run the targeted regression tests and the focused validation worktree unit
   test module if needed.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`

Pass criteria: the targeted validation worktree unit tests pass. Full
AWF/GitHub validation is intentionally not run during the agent phase.
