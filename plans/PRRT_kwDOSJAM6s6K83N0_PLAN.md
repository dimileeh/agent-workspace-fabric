# PRRT_kwDOSJAM6s6K83N0 Plan

## Problem Statement and Scope

The dirty-worktree missing-HEAD recovery path validates recovery anchors through
the mirror object store when a mirror exists, but the no-mirror path can choose
`operation_start_head` or the merge candidate without proving that SHA exists in
the worktree object store.

Scope is limited to `src/awf/runtime/pr_monitor_runner/remote_repair.py` and a
focused regression test for PR review thread `PRRT_kwDOSJAM6s6K83N0`.

## Requirements Checklist

- Verify the no-mirror missing-HEAD recovery SHA with `git cat-file -e <sha>^{commit}` through the existing worktree object guard before filesystem recovery.
- Fall back safely when the preferred no-mirror recovery SHA is unavailable, and fail closed if no verified recovery SHA remains.
- Preserve existing mirror recovery behavior except where shared helper structure requires equivalent checks.
- Add a focused regression test for the reviewed no-mirror case.
- Run only focused validation for the changed behavior; leave broad AWF/GitHub validation to AWF after agent completion.

## Implementation Steps

1. Add a regression test that simulates missing HEAD with no mirror and a stale recovery anchor, asserting that unavailable SHAs are not passed to filesystem recovery.
2. Update `_commit_dirty_worktree` to validate selected no-mirror recovery heads through `_worktree_commit_object_exists`.
3. Run the targeted test or test file section needed to prove the change.
4. Record results in `plans/PRRT_kwDOSJAM6s6K83N0_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q`

Pass criteria: the focused test file passes and includes coverage of the no-mirror unavailable-anchor path.
