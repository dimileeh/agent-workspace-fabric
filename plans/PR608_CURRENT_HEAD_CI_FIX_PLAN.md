# PR608 Current Head CI Fix Plan

## Problem Statement and Scope

PR #608 has recent failed CI history, with the latest completed failed run at
commit `90f499431` failing `python-full-coverage` because combined branch-aware
coverage reported `98.97%`, below the `99.00%` threshold. The current workspace
head is `b9e0ecbcc`, and its CI run was still in progress during investigation.

Scope is limited to fixing failures that still apply to the current PR head.
Do not change workflow, quality-gate, or broad validation configuration files.
Do not run full AWF/GitHub-owned validation locally.

## Requirements Checklist

- [ ] Inspect GitHub Actions status and logs for PR #608 current head.
- [ ] If the current head fails, identify the concrete failing check and root cause.
- [ ] Prefer focused behavior tests for uncovered or failing code touched by this PR.
- [ ] Avoid coverage-theater tests, weakened assertions, skipped checks, or protected configuration edits.
- [ ] Run only focused local verification for changed tests/files.
- [ ] Record validation evidence and note that full AWF/GitHub validation is managed after agent completion.
- [ ] Commit the local fix with a conventional `fix(ci): ...` message if code or test changes are needed.

## Implementation Steps

1. Use `gh pr checks 608` and `gh run view` to determine whether the current
   head still has a failing check.
2. If coverage still fails, use the combined coverage artifact or failed shard
   log to identify missing behavior in changed source files.
3. Add the smallest focused regression tests that assert real behavior for the
   uncovered or failing path.
4. Run the targeted test file or specific tests touched by the change.
5. Re-check PR #608 checks for updated status where available.
6. Write `plans/PR608_CURRENT_HEAD_CI_FIX_VALIDATION.md`.

## Assumptions/Changes

- Current-head CI later reported `python-coverage-shards (8)` failed in
  `tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit`.
- Root cause: `tests/unit/runtime/test_planning_parts/test_planning_part_001.py`
  had grown to 1504 lines, exceeding the 1500-line first-party file limit.
- Implementation scope is therefore a test-only move of one conformance-stall
  test into `test_planning_part_002.py`, which already owns related
  `classify_conformance_stall` coverage helpers.

## Verification Commands and Pass Criteria

- `gh pr checks 608 --json name,state,bucket,link,startedAt,completedAt,workflow`
  - Pass: current-head failing checks are identified, or the current run is
    confirmed clean/pending with no local code change needed.
- Focused `uv run --python 3.12 --extra dev pytest ... -q` for changed tests.
  - Pass: targeted tests pass locally.

Full coverage, whole-repository tests, full frontend builds, and GitHub merge
gates remain AWF/GitHub-owned validation after this agent phase.
