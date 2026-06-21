# PR614 Coverage Threshold Plan

## Problem Statement and Scope

PR #614 current HEAD passes all Python coverage shards, lint/type, console, and release jobs, but the aggregate `python-full-coverage` job fails because combined line+branch coverage is 98.86%, below the required 99.00%.

Scope is limited to adding meaningful focused regression/behavior tests for reachable branches introduced or affected by this PR. Do not lower thresholds, skip checks, edit CI/workflow configuration, or run local full coverage gates.

## Requirements Checklist

- Inspect the combined `coverage.xml` missing opportunity report from the failed GitHub Actions run.
- Target changed AWF runtime files with real uncovered behavior, prioritizing the largest gaps.
- Add focused tests that assert observable behavior or side effects.
- Keep changes minimal and avoid unrelated refactors.
- Run narrow pytest commands for the tests changed or added.
- Record validation evidence and note that broad AWF/GitHub validation remains owned by AWF after completion.

## Implementation Steps

1. Use the downloaded `coverage.xml` from run `27836507550` to identify missing line and branch opportunities in changed files.
2. Inspect the relevant code and nearby existing tests for `remote_repair.py`, `pre_push_validation_fix_pass.py`, and `fix_cycle.py`.
3. Add focused unit tests to existing test modules where possible, preferring local fakes and assertions over broad integration flows.
4. Run targeted pytest commands for only the modified test files or specific tests.
5. Create `plans/PR614_COVERAGE_THRESHOLD_VALIDATION.md` with requirement status and evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest <focused tests> -q`
  - Passes for every added/changed test.
- `gh pr checks 614 --json name,state,bucket,link,startedAt,completedAt,workflow`
  - Used only to confirm CI failure source and avoid duplicating broad validation locally.
