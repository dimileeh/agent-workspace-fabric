# PRRT_kwDOSJAM6s6K9JMk Plan

## Problem Statement and Scope

The PR review thread reports that `_commit_dirty_worktree` does not verify a
merge-candidate recovery anchor against the mirror when the operation-start
anchor is missing from the mirror. Scope is limited to that missing-HEAD
recovery branch in `src/awf/runtime/pr_monitor_runner/remote_repair.py` and the
focused regression test that covers it.

## Requirements Checklist

- Confirm whether the mirror-backed fallback candidate is currently unchecked.
- Add a focused regression test that fails when the candidate is not
  mirror-checked.
- Verify the candidate recovery head on the mirror before filesystem recovery
  uses it.
- Preserve the existing no-mirror worktree verification behavior.
- Run only focused local checks for the changed behavior; broad AWF/GitHub
  validation remains managed by AWF after agent completion.

## Implementation Steps

1. Update the existing stale-anchor unit test to require mirror checks for both
   the stale operation-start SHA and the candidate SHA.
2. Run that focused test to confirm the current implementation fails.
3. Update `_commit_dirty_worktree` so the mirror-backed candidate fallback is
   mirror-checked and rejected if unavailable.
4. Re-run the focused test and, if useful, a narrow neighboring test covering
   the no-mirror candidate rejection path.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q -k 'missing_head_falls_back_from_stale_start_head'`
  - First run should fail after the test update and before implementation.
  - Final run should pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q -k 'missing_head_falls_back_from_stale_start_head or no_mirror_rejects_unverified_candidate_head'`
  - Final run should pass.
