# PR614 Shard 6 Recovered Head CI Plan

## Problem Statement and Scope

PR #614 CI fails in `python-coverage-shards (6)` on two focused unit tests
covering PR monitor pre-push validation after missing HEAD recovery. The fix is
limited to recovered-head result reporting and protected-scope failure cleanup
behavior in the PR monitor/pre-push validation path.

## Requirements Checklist

- Reproduce the two failing shard-6 tests locally with focused pytest commands.
- Preserve recovered HEAD identity in validation results after filesystem
  recovery, including protected-scope failures.
- Preserve the intentional cleanup reset in the protected commit-blocking path
  covered by the failing monitor helper test.
- Add or adjust focused regression coverage only where behavior changes.
- Do not run broad AWF/GitHub validation or full coverage locally; AWF owns that
  after agent completion.

## Implementation Steps

1. Inspect the failing tests and nearby implementation paths for recovered-head
   propagation and protected-scope cleanup.
2. Run the two failing tests locally to confirm the focused failure.
3. Patch the smallest implementation/test gap needed to make the expected
   behavior explicit and stable.
4. Run the same focused tests, plus a targeted neighboring test if the touched
   code path warrants it.
5. Record validation evidence in
   `plans/PR614_SHARD6_RECOVERED_HEAD_CI_VALIDATION.md`.

## Assumptions/Changes

- Existing plan history shows cleanup-to-`recovery_head` is intentional after
  recovered protected-scope rejection. The implementation change therefore
  preserves cleanup while reporting the recovered commit in result metadata, and
  the monitor helper regression is updated to assert the cleanup reset.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py::test_pre_push_validation_recovered_head_rename_includes_source_path tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py::TestMiscMonitorHelpers::test_commit_dirty_worktree_missing_head_recovery_blocks_protected_commit -q`
  - Passes.
- Any additional focused command chosen after code inspection must pass.
- Full AWF/GitHub validation is intentionally not run locally under the AWF
  workspace contract.
