# CI Validation Cleanup Fix Plan

## Problem Statement And Scope

PR #349 CI fails in the Python full-coverage job. The focused repro reports:

- stale validation cleanup tests now call the real stale-cleanup persistence
  helper instead of the monkeypatched helper exposed through
  `execution_validation`, causing missing `_session_factory` failures in unit
  doubles;
- stale-cleanup persistence tests cannot monkeypatch `WorkspaceRepository`
  through `execution_validation`;
- `tests/unit/runtime/test_validation_worktree.py` exceeds the first-party file
  line limit.

Scope is limited to restoring the intended stale validation cleanup behavior and
splitting oversized test coverage without weakening checks or running broad
AWF/GitHub-owned validation locally.

## Requirements Checklist

- Preserve primary validation failure causality when stale validation cleanup
  records secondary cleanup evidence.
- Keep unit-level monkeypatch seams compatible with the existing stale-cleanup
  regression tests or update the tests to the correct local module seams.
- Reduce first-party test files below the configured line limit without deleting
  behavioral coverage.
- Do not disable, skip, or weaken maintainability, coverage, or CI checks.
- Run only focused repro/verification commands locally; leave full AWF/GitHub
  validation to AWF after agent completion.
- Commit the fix locally on the current AWF-managed branch.

## Implementation Steps

1. Inspect the stale validation cleanup guard/module boundaries and the failing
   tests to identify the intended patching seam.
2. Update the cleanup guard or tests so stale callback cleanup failures record
   secondary evidence through the intended module-local dependency path.
3. Split the oversized validation worktree test file into smaller focused test
   modules while preserving existing test names and coverage.
4. Run the AWF-provided focused repro command.
5. Run any additional narrow tests for touched modules if the first repro does
   not cover the changed behavior.
6. Write `plans/CI_VALIDATION_CLEANUP_FIX_VALIDATION.md` with requirement
   status and focused evidence.
7. Commit the plan, code/test changes, and validation document locally.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest \
  'tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_execution_validation_fails_cleanup_when_callback_becomes_stale_after_cleanup_exception[callback-already-stale]' \
  'tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_execution_validation_fails_cleanup_when_callback_becomes_stale_after_cleanup_exception[callback-becomes-stale]' \
  tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_stale_validation_cleanup_failure_records_secondary_failure_evidence \
  tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit \
  tests/unit/control/test_executor_validation_stale_cleanup.py::test_stale_validation_cleanup_without_primary_keeps_failed_row_fields \
  -q
```

Pass criteria: all five focused CI repro node IDs pass.

Additional focused checks may be added for files moved or edited. Full coverage,
whole-repository pytest, frontend builds, and CI-equivalent validation are AWF
and GitHub responsibilities after this agent phase.
