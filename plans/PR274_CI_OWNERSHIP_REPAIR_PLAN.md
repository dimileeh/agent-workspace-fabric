# PR #274 CI Ownership Repair Plan

## Problem Statement And Scope

PR #274 fails the `python-full-coverage` CI job in focused monitor and executor
unit tests. The focused repro shows the runtime ownership repair wrapper
validates linked-worktree metadata and returns a hard failure while tests run as
the non-root `agent` user. The lower-level ownership repair helper is already
root-only, so non-root control-plane processes should not block workspace
execution merely because no ownership change can be attempted.

Scope is limited to restoring correct root-gated ownership repair behavior and
keeping the existing root-mode safety validations covered.

## Requirements Checklist

- Reproduce the listed real pytest node IDs before changing code.
- Add a regression proving non-root ownership repair returns success without
  requiring linked `.git` metadata.
- Preserve root-mode validation of linked-worktree metadata before any repair
  target is passed to `repair_agent_writable_worktree`.
- Keep monitor and executor ownership-repair failure handling intact when the
  repair helper reports failure.
- Do not disable, skip, or weaken the failing CI check.
- Commit the local fix with a conventional `fix(ci): ...` message and do not
  push.

## Implementation Steps

1. Add a unit regression in `tests/unit/runtime/test_ownership.py` for non-root
   no-op behavior.
2. Update existing ownership safety tests to simulate root where they assert
   validation and repair target handling.
3. Gate `repair_agent_runtime_ownership` in `src/awf/runtime/ownership.py` so
   non-root callers return success before metadata validation.
4. Re-run the focused PR #274 pytest node IDs plus the ownership regression
   tests.
5. Create `plans/PR274_CI_OWNERSHIP_REPAIR_VALIDATION.md` with
   requirement-by-requirement evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ownership.py -q`
  must pass.
- The focused PR #274 pytest node IDs from the CI evidence must pass.
