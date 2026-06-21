# PR614 Current Coverage Threshold Repair Plan

## Problem Statement and Scope

PR #614 currently fails only `python-full-coverage` because the combined GitHub Actions report is 98.99%, just below the 99.00% threshold. The `ci-required` failure is a cascade from that coverage job.

Scope is limited to behavior-focused tests for live uncovered paths in the latest baseline-cleanup missing-HEAD recovery change. Do not edit protected workflow, coverage, or quality-gate configuration.

## Requirements Checklist

- Diagnose the current coverage failure from CI logs/artifacts before editing.
- Add focused regression coverage for real behavior in the latest changed code.
- Do not disable, skip, weaken, or reconfigure the coverage gate.
- Run only narrow local checks for the touched tests/code.
- Record validation evidence and note that broad AWF/GitHub validation is deferred to AWF after agent completion.
- Commit the local fix on the current AWF-managed branch without pushing.

## Implementation Steps

1. Use the current CI run artifact to identify uncovered lines tied to the latest change.
2. Extend `tests/unit/control/test_executor_baseline_cleanup_recovery.py` with behavior assertions for:
   - baseline cleanup failure when the HEAD object is already present, so missing-HEAD recovery is not invoked;
   - failed mirror-hook repair after baseline cleanup, so missing-HEAD recovery is not attempted.
   - unavailable missing-HEAD recovery hook, so baseline cleanup failure remains the terminal failure.
   - adjacent agent-cleanup recovery edges with HEAD already present or recovery unavailable.
   - setup-cleanup recovery edge with HEAD already present.
3. Run the focused test files and focused lint for touched files.
4. Create `plans/PR614_CURRENT_COVERAGE_THRESHOLD_REPAIR_VALIDATION.md` with evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_baseline_cleanup_recovery.py tests/unit/control/test_executor_setup_cleanup_recovery.py -q`
  - Passes.
- `uv run --python 3.12 --extra dev ruff check tests/unit/control/test_executor_baseline_cleanup_recovery.py tests/unit/control/test_executor_setup_cleanup_recovery.py`
  - Passes.
- Full sharded coverage and required CI status are not run locally; AWF/GitHub CI owns that validation after agent completion.
