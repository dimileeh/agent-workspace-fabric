# Review 4790062198 Orphan Reaper Test Name Plan

## Problem Statement and Scope

PR review comment `issue:4790062198` flags that
`test_reaper_flag_on_reaps_terminal_volume_and_worktree_after_retention` implies
the test exercises the missing-orphan age gate, while its setup classifies the
records as `terminal`, which are reaped without the age guard.

Scope is limited to addressing this review comment and verifying the affected
test. Broader review-summary concerns are checked against the current branch
but not expanded unless still present.

## Requirements Checklist

- Verify the cited code before changing it.
- Rename the terminal reaper test to describe the behavior it actually asserts.
- Preserve existing assertions and regression coverage.
- Record any stale broader review-summary claims in validation evidence.
- Run a focused test for the changed test only.
- Do not run broad AWF/GitHub-owned validation.

## Implementation Steps

1. Inspect the cited test and orphan reaper behavior around terminal vs missing
   classifications.
2. Confirm whether current branch already addresses the double-apply and
   scanner-precondition-comment review-summary concerns.
3. Rename the misleading test function without weakening assertions.
4. Run the renamed test directly.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_orphan_resources_parts/test_orphan_resources_part_002.py::test_reaper_flag_on_reaps_terminal_volume_and_worktree -q`

Pass criteria: the focused test passes. Full AWF/GitHub validation remains
owned by AWF after agent completion.
