# ADDRESS_THREAD_PRRT_kwDOSJAM6s6GOJ1p Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6GOJ1p` reports that a successful pre-push validation fix-pass commit can later fail to capture `HEAD`, but `_run_pre_push_validation_fix_pass` reports `VALIDATION_WORKTREE_CLEANUP_FAILED`. The caller then labels the outcome as cleanup failure even though validation-worktree cleanup did not run. Scope is limited to reason/message handling for that post-commit `HEAD` capture failure path and focused regression coverage.

## Requirements Checklist

- Return a non-cleanup reason when post-commit `HEAD` capture fails after a fix-pass commit.
- Ensure the fix-pass wrapper does not label that path as cleanup failure.
- Preserve existing cleanup failure behavior for actual cleanup failures.
- Add focused regression tests for the helper and wrapper behavior.
- Run only targeted tests for the changed behavior; leave broad AWF/GitHub validation to AWF after agent completion.

## Implementation Steps

1. Add a unit test proving post-commit `HEAD` capture failure returns an infrastructure reason and does not call cleanup.
2. Add or update a unit test proving the wrapper message labels infrastructure failure distinctly from cleanup failure.
3. Change `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` to return `PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON` for post-commit `HEAD` capture failure.
4. Adjust fix-pass failure label selection so committed infrastructure failures do not read as cleanup failures.
5. Run the targeted pre-push validation fix-pass tests that cover the new paths.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass.py -q`
- Pass criteria: targeted tests pass, including the new regressions, with no broad suite or coverage gate executed locally.
