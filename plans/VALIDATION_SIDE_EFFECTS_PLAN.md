# Validation Side Effects Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6GOY2y` reports that AWF can record a successful validation after validation commands write files into the worktree and cleanup restores or deletes those files. In that case, the restored commit state was not the state that passed validation.

Scope is limited to executor validation success handling after `cleanup_validation_worktree_side_effects`. Existing cleanup failure behavior, stale callback handling, and broad validation policy are out of scope.

## Requirements Checklist

- Reject a successful validation when post-validation cleanup had to restore or delete tracked/untracked worktree side effects.
- Keep true no-op cleanup success as a valid successful validation.
- Preserve cleanup failure guard behavior for failed cleanup results.
- Record a clear validation failure reason and artifact evidence so fix-cycle/error reporting has an anchor.
- Add a focused regression test for a passing validation command whose side effects are cleaned.
- Run only targeted tests for the touched behavior; full AWF/GitHub validation remains owned by AWF after agent completion.

## Implementation Steps

1. Add or reuse a validation-worktree reason code for “validation passed with cleaned side effects.”
2. Convert successful validation results into a synthetic failed validation result when cleanup succeeds but reports cleaned side effects.
3. Write concise artifact evidence for that synthetic failure under the executor artifact root.
4. Add a unit test covering the executor path where validation passes, cleanup cleans side effects, and the run is recorded as failed.
5. Run the targeted unit test file or selected test node.

## Assumptions/Changes

- The cleanup result also needs explicit cleaned-path evidence because ignored artifact cleanup can succeed even when the cleanup check itself is considered clean. The executor guard therefore treats either `cleanup_result.side_effect_paths` or a dirty cleanup check as invalid successful validation evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py -q`

Pass criteria: the new regression and existing focused executor cleanup tests pass. Full repository validation and coverage gates are intentionally not run in-agent per the AWF workspace contract.
