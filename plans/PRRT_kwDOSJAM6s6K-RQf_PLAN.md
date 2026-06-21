# PRRT_kwDOSJAM6s6K-RQf Plan

## Problem Statement and Scope

The missing-HEAD recovery path in `remote_repair.py` returns early when a
worktree still has `MERGE_HEAD`. The review reports that this can leave the
shared mirror branch ref pointing at a missing commit after branch verification,
poisoning the next monitor/workspace.

Scope is limited to restoring the verified branch ref to `operation_start_head`
before abandoning missing-HEAD recovery for an in-progress merge, plus focused
regression coverage.

## Requirements Checklist

- Verify `operation_start_head` exists before attempting branch-ref restoration.
- Only restore the branch ref after the worktree branch ref matches the expected
  workspace branch ref.
- When `MERGE_HEAD` is present, attempt to update the verified branch ref back
  to `operation_start_head` before returning `None`.
- Preserve fail-closed recovery behavior: do not reset the index, commit, or
  proceed with filesystem recovery while a merge is in progress.
- Add/update a focused regression test for the review scenario.

## Implementation Steps

1. Update the existing merge-in-progress missing-HEAD recovery test to expect a
   branch `update-ref` reset before the early return.
2. Confirm the focused test fails against the current implementation.
3. Move or add the `update-ref` reset before the `MERGE_HEAD` early return,
   keeping branch verification before the reset.
4. Run the focused test file or individual test that covers the changed branch.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_fails_closed_during_merge -q`

Pass criteria: the targeted regression test passes and shows that merge-state
recovery restores the verified branch ref while still returning `None` without
index reset or commit.

Full AWF/GitHub validation is intentionally not run in the agent phase; AWF owns
the broad validation and merge-gating suite after completion.
