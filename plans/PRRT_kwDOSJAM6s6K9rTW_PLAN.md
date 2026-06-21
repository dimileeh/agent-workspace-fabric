# PRRT_kwDOSJAM6s6K9rTW Plan

## Problem Statement And Scope

An unresolved PR review thread reports that validate-only recovery with an
existing PR can take the recovery skip-push return before repairing a poisoned
shared mirror `core.hooksPath` after successful post-agent/validate phases. The
fix is scoped to the recovery skip-push branch in
`src/awf/control/executor/execution_flow.py` and focused regression coverage.

## Requirements Checklist

- Verify the skip-push branch currently returns before the existing pre-push
  mirror hooks repair.
- Add a focused regression proving validate-only recovery repairs mirror hooks
  before transitioning out of `validating` via `recovery_skip_push`.
- Move or add the same fail-closed repair before the skip-push
  transition/return.
- Keep validation focused; do not run AWF/GitHub-owned broad suites.
- Record implementation validation in the matching validation document.

## Implementation Steps

1. Extend an existing recovery skip-push unit test or add a nearby focused test
   that fails when the repair is skipped.
2. Add the minimal repair call before the `recovery_skip_push` status recheck
   and transition.
3. Run the targeted unit test file or single test that covers the changed path.
4. Commit the scoped changes locally with the review-thread id in the message.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_004.py -q`

Pass criteria: the focused recovery tests pass, including the new fail-closed
mirror hooks repair regression. Full AWF/GitHub validation is managed by AWF
after agent completion.
