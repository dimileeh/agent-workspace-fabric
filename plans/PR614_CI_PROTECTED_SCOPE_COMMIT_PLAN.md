# PR614 CI Line-Limit Repair Plan

## Problem Statement And Scope

PR #614 CI has failed in Python coverage shard 8. The current actionable failure
from GitHub Actions run `27863885737` is
`tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit`:
`tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py` has 1513 lines,
exceeding the 1500-line first-party file limit.

Scope is limited to splitting the oversized test file into an existing related
part file without changing test behavior. Do not change workflow or quality-gate
configuration.

## Requirements Checklist

- [ ] Reproduce or inspect the focused failing line-limit guard.
- [ ] Move a cohesive recovered-HEAD edge test out of the oversized file into an
      existing related part file.
- [ ] Keep both affected files under the line limit.
- [ ] Preserve the moved test's behavior.
- [ ] Run only targeted local verification for touched behavior.
- [ ] Record validation evidence and note that broad AWF/GitHub validation is
      managed after agent completion.
- [ ] Commit the scoped fix locally without pushing.

## Implementation Steps

1. Inspect the current shard-8 failure log and confirm the oversized file.
2. Move one cohesive recovered-HEAD edge test from
   `test_pr_monitor_pre_push_validation_edges.py` to
   `test_pr_monitor_pre_push_validation_edges_part_002.py`.
3. Re-run the maintainability guard and the moved test.
4. Write `plans/PR614_CI_PROTECTED_SCOPE_COMMIT_VALIDATION.md` with evidence.
5. Commit the scoped line-limit fix locally.

## Assumptions/Changes

Initial investigation used the latest completed failed run (`27862959455`), which
showed a protected-scope commit test failure. That failure is stale at current
HEAD: the focused repro passes locally, and the current run (`27863885737`) moved
past that shard. The live current failure is the shard-8 line-limit guard above,
so this plan is narrowed to that actual failure.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  must pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges_part_002.py::test_pre_push_validation_recovered_head_ownership_repair_failure_blocks_validation -q`
  must pass.
- Do not run full coverage or full repository CI locally; AWF/GitHub own broad
  validation after this agent phase.
