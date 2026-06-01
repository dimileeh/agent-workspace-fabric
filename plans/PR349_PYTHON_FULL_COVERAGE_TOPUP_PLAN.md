# PR349 Python Full Coverage Top-Up Plan

## Problem Statement and Scope

PR #349 fails the GitHub Actions `python-full-coverage` job after all tests pass because
total coverage is `98.91%`, below the configured `99%` threshold. The CI coverage
report shows the largest relevant gap in `src/awf/runtime/validation_worktree.py`,
especially around validation cleanup edge cases and defensive filesystem branches.

This fix is scoped to adding focused regression coverage for existing validation
worktree behavior. It must not disable, skip, or weaken the CI coverage gate, and it
must not run the full AWF/GitHub validation suite locally.

## Requirements Checklist

- Preserve the existing validation worktree behavior.
- Add focused unit tests for uncovered validation worktree cleanup/status branches.
- Keep changes limited to tests and plan/validation documentation unless a real code
  defect is found while writing the tests.
- Run focused local verification for the touched tests only.
- Do not run full coverage, whole-repository tests, frontend builds, push, rebase, or
  switch branches.

## Implementation Steps

1. Inspect the CI coverage report and local validation worktree tests to identify
   uncovered, meaningful branches.
2. Add focused unit tests in the validation worktree test module(s) for the missing
   edge cases.
3. Run narrow pytest commands for the touched validation worktree tests.
4. Write `plans/PR349_PYTHON_FULL_COVERAGE_TOPUP_VALIDATION.md` with requirement
   status and verification evidence.
5. Commit the fix locally with a conventional CI-fix message.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py tests/unit/runtime/test_validation_worktree_head_cleanup.py -q`
  - Passes with only the touched validation worktree unit tests.
- Optional targeted diagnostic only if needed:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py tests/unit/runtime/test_validation_worktree_head_cleanup.py --cov=awf.runtime.validation_worktree --cov-report=term-missing --cov-fail-under=0 -q`
  - Used only to confirm local coverage movement for the touched module; this is not
    the broad repository coverage gate.

Full AWF/GitHub validation and the repository-wide coverage gate are managed by AWF
after agent completion.
