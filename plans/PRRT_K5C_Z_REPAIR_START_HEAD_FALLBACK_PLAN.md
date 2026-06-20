# PRRT K5C Z Repair Start Head Fallback Plan

## Problem Statement and Scope

Inline review thread `PRRT_kwDOSJAM6s6K5c-z` reports that
`_repair_operation_start_head_result` ignores a provided `fallback_head_sha`
when the repair worktree exists but `git rev-parse HEAD` fails. Sync-base and CI
repair pass the known PR head as that fallback, so the helper should still have
a stable repair baseline in this case.

Scope is limited to the shared repair-start head helper and focused regression
coverage for the fallback path.

## Requirements Checklist

- Verify the review claim against `remote_repair.py` and cited callers.
- Add a focused regression test before implementation.
- Use `fallback_head_sha` when worktree `rev-parse HEAD` fails.
- Preserve existing behavior that prefers a successful worktree HEAD.
- Keep changes scoped to the review feedback.
- Run only targeted validation for the changed behavior.

## Implementation Steps

1. Add a unit test for an existing worktree where `rev-parse HEAD` fails and a
   fallback head is supplied.
2. Run the targeted repair-start tests and confirm the new test fails.
3. Update `_repair_operation_start_head_result` to return the provided fallback
   when the worktree command cannot produce a head.
4. Re-run the targeted repair-start tests.
5. Record validation evidence in
   `plans/PRRT_K5C_Z_REPAIR_START_HEAD_FALLBACK_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py -q -k repair_operation_start_head`
  - Pass criteria: the new regression and existing repair-start helper tests
    pass.

Full AWF/GitHub validation is managed by AWF after agent completion and will not
be run in this agent phase.
