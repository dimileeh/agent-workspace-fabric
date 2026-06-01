# Validation Fix-Pass Worktree Guard Plan

## Problem Statement And Scope

An unresolved review thread reports that the post-fix worktree guard in
`src/awf/control/executor/execution_validation.py` can pass the current
`validation_run_id` to `_fail_validation_worktree_guard` after that run has
already been finished. The scope is limited to preventing a double finish for
the fix-pass worktree cleanliness failure path while preserving the workspace
and pending-operation failure behavior.

## Requirements Checklist

- Add or update a focused regression test that fails under the current double-finish behavior.
- Update only the post-fix dirty worktree guard so it does not re-close an already-finished validation run.
- Preserve earlier pre-validation guard behavior that still closes the just-started validation run.
- Run targeted tests only; broad AWF/GitHub validation remains managed after agent completion.

## Implementation Steps

1. Add a focused unit test for a validation failure followed by a fix pass that leaves the worktree dirty.
2. Confirm the new regression fails before the implementation change.
3. Pass `validation_run_id=None` to `_fail_validation_worktree_guard` from the post-fix dirty-worktree branch.
4. Run the targeted regression test and the nearby validation-guard tests.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py -q -k "fix_pass_dirty_worktree or fix_pass_ignored_artifacts or ignored_signature_drift or new_ignored_paths_after_initial_validation_pass"`

Pass criteria: the focused tests pass, and the validation document records that full AWF/GitHub validation was not run inside the agent phase.
