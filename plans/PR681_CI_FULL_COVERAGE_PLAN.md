# PR681 CI Full Coverage Plan

## Problem Statement And Scope

PR #681 fails the GitHub Actions `python-full-coverage` check because combined
coverage is 98.96%, below the required 99.00%. The downloaded CI coverage
artifact shows the largest PR-touched gap in `src/awf/service/status.py`,
especially the worker reaper heartbeat readiness path and orphan cleanup
readiness helper branches.

Scope is limited to focused regression/unit tests for reachable status behavior.
Do not weaken or skip coverage checks, and do not run the broad AWF/GitHub
coverage gate locally.

## Requirements Checklist

- Add behavior assertions for worker reaper readiness outcomes:
  - fresh heartbeat reports ready.
  - missing heartbeat reports `WORKER_HEARTBEAT_MISSING`.
  - stale heartbeat reports `WORKER_HEARTBEAT_STALE` with age/threshold detail.
  - heartbeat lookup timeout reports `WORKER_HEARTBEAT_UNAVAILABLE`.
  - heartbeat repository errors are redacted and reported unavailable.
- Add focused assertions for orphan cleanup readiness helper branches that CI
  marks uncovered and that affect operator-facing cleanup guidance.
- Keep production behavior unchanged unless tests reveal a real logic bug.
- Run only targeted tests covering the changed test file(s).
- Document validation evidence in `plans/PR681_CI_FULL_COVERAGE_VALIDATION.md`.

## Implementation Steps

1. Add failing tests in `tests/unit/service/test_status_parts/test_status_part_001.py`
   for the uncovered status behaviors.
2. Run the targeted status test file to confirm the new tests expose any issues.
3. Make the smallest implementation/test adjustments needed for focused tests to pass.
4. Re-run the same targeted test file.
5. Create the validation document with changed files and focused command evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_status_parts/test_status_part_001.py -q`
  - Passes locally.

Full AWF/GitHub coverage validation is intentionally not run in the agent phase;
AWF owns broad validation, coverage provenance, and merge gating after agent
completion.
