# PR614 Shard 8 Part 005 Line Limit Plan

## Problem Statement and Scope

PR #614 currently fails GitHub Actions `python-coverage-shards (8)` in
`tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit`.
The failed job reports
`tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py`
at 1504 lines, above the first-party file line limit.

Scope is limited to splitting a small coherent set of existing tests from the
oversized file into a sibling shard file. No behavior should change.

## Requirements Checklist

- [ ] Do not switch branches, push, rebase, or run broad AWF/GitHub-owned
      validation.
- [ ] Preserve all moved test behavior and assertions.
- [ ] Reduce `test_pr_monitor_runner_part_005.py` below the line limit.
- [ ] Keep new test file below the line limit.
- [ ] Run focused checks only: the line-limit test and the moved tests.
- [ ] Record focused evidence in a validation document and note that full
      AWF/GitHub validation remains owned by AWF after agent completion.
- [ ] Commit the scoped fix locally with a conventional `fix(ci): ...` message.

## Implementation Steps

1. Inspect the failed shard 8 log and confirm the exact oversized file.
2. Move the related dirty-worktree commit-subject tests from
   `test_pr_monitor_runner_part_005.py` into a new sibling part file.
3. Import only the fixtures and helpers needed by the new sibling file.
4. Run the focused line-limit test.
5. Run the moved tests in their new location.
6. Create `plans/PR614_SHARD8_PART005_LINE_LIMIT_VALIDATION.md` with command
   evidence and residual risk.
7. Commit the scoped changes locally.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  must pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_010.py -q`
  must pass.
- Do not run full coverage, all unit tests, frontend builds, or CI-equivalent
  validation locally.
