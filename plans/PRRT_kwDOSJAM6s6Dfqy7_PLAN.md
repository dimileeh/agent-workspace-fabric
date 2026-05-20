# PRRT_kwDOSJAM6s6Dfqy7 Plan

## Problem Statement And Scope

The preserved-active PR monitor salvage path in `src/awf/control/worker.py`
can transition a workspace to `monitoring_pr` when `pr_url` is present but
`pr_number` is still `NULL`. The PR monitor runner treats that state as an
invariant violation and fails the workspace.

Scope is limited to preserved-active PR monitor attachment and focused worker
regression coverage.

## Requirements Checklist

- Do not transition preserved-active salvage to `monitoring_pr` unless a PR
  number is available.
- Recover legacy rows with a parseable GitHub PR URL by deriving and persisting
  `pr_number` before monitor attachment.
- Leave branch-based open PR lookup behavior intact when no attachable PR URL
  exists.
- Add regression coverage that fails against the current implementation.
- Run the narrowest relevant test proving the fix.

## Implementation Steps

1. Add a focused worker regression for a preserved active workspace with
   `pr_url` populated and `pr_number` missing.
2. Confirm the regression fails before the code change when practical.
3. Update preserved-active PR monitor attachment to derive `pr_number` from a
   parseable PR URL and guard the transition when it remains unavailable.
4. Re-run the focused regression and nearby preserved-active PR handoff tests.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_pr_handoff'`
  passes.
