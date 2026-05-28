# PR301 Full Coverage Gap Plan

## Problem Statement and Scope

PR #301 failed the GitHub Actions `python-full-coverage` job because the full
test suite passed but coverage reported `98.99%`, below the required `99.00%`
threshold. The aggregate `ci-required` job failed only because it rolls up that
coverage job.

The fix is limited to restoring coverage through focused worker admission,
capacity-claim, and execution-task regression tests. Workflow files, coverage
thresholds, and broad validation policy are out of scope.

## Requirements Checklist

- Preserve the existing `99%` coverage requirement; do not skip, disable, or
  weaken CI.
- Do not edit protected GitHub workflow or quality-gate configuration files.
- Add focused regression coverage for uncovered worker branches introduced or
  exposed by this PR.
- Keep validation local and narrow; AWF/GitHub owns full coverage and broad CI
  after this agent phase.
- Commit the fix locally on the current AWF-managed branch.

## Implementation Steps

1. Use the CI `coverage.xml` artifact to identify uncovered worker lines without
   running the full coverage suite locally.
2. Add focused tests for non-Postgres admission locking and zero execution-slot
   admission rows.
3. Add focused tests for capacity claiming when admission rows are exhausted,
   zero effective capacity claim limits, and empty capacity candidate batches.
4. Add focused tests for execution task draining when tasks are cancelled or
   raise exceptions.
5. Run targeted tests for the changed test file, plus a focused coverage report
   for the touched worker modules.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q`
  - Passes all tests in the focused worker admission regression file.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py --cov=awf.control.worker.admission --cov=awf.control.worker.claims --cov=awf.control.worker.manager --cov-report=term-missing --cov-fail-under=0 -q`
  - Passes and shows the targeted previously uncovered worker lines are covered
    by the focused suite.

Full repository coverage and required CI checks are intentionally left to
AWF/GitHub after agent completion per the workspace contract.
