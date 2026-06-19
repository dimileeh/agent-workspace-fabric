# PR608 Current Coverage Fix Plan

## Problem Statement And Scope

PR #608 has a completed CI failure in `python-full-coverage` from run
`27817076948`: total coverage was `98.97`, below the `99.00` threshold.
The root failing job is coverage; `ci-required` failed only because
`python-full-coverage` failed. The downloaded `coverage.xml` from that run
shows uncovered PR-owned lines in `src/awf/control/executor/planning_conformance.py`.

This pass is scoped to fixing any remaining coverage gap on the current branch
without changing GitHub workflow/configuration files or running broad AWF-owned
validation locally.

## Requirements Checklist

- Inspect GitHub Actions logs and coverage artifact before editing.
- Use the saved coverage report to target real behavior, not line-padding tests.
- Add or adjust focused tests only if the current branch still leaves uncovered
  behavior in the affected executor planning/conformance code.
- Do not edit protected workflow, quality-gate, or configuration files.
- Run focused local validation only for the changed behavior.
- Record that full AWF/GitHub validation remains owned by AWF/CI.
- Commit the final fix locally if new changes are required.

## Implementation Steps

1. Parse the failed run's `coverage.xml` and identify uncovered changed
   production paths.
2. Run a focused local coverage check against the current branch for
   `awf.control.executor.planning_conformance`.
3. If current coverage still misses behavior, add targeted tests that assert
   the cleanup/deposit side effects or logging behavior.
4. Re-run the focused test command.
5. Create `plans/PR608_CURRENT_COVERAGE_FIX_VALIDATION.md` with evidence.
6. Commit the scoped plan, validation, and code/test changes when applicable.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_planning_ops_branch_edges.py -q --cov=awf.control.executor.planning_conformance --cov-report=term-missing`
  - Pass criteria: tests pass and the target module no longer shows the
    failed-run missing lines as uncovered.
- `gh run view 27819759334 --json conclusion,status,jobs,url,headSha`
  - Pass criteria: use as remote status evidence only; do not block local fix
    on broad CI completion unless it has already reached a terminal state.

Full repository coverage and broad CI validation are managed by AWF/GitHub
after the agent completes.
