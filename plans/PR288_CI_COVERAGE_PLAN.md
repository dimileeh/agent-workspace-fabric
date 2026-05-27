# PR288 CI Coverage Plan

## Context

GitHub Actions run `26505881006` failed in `python-full-coverage`. The concrete
test failure was:

- `tests/unit/runtime/test_pr_monitor_pre_push_validation.py::test_pre_push_validation_failure_does_not_push`
- expected `PRE_PUSH_VALIDATION_FAILED`, observed `PRE_PUSH_VALIDATION_FIX_FAILED`

Current `HEAD` already contains follow-up commits that make that exact focused
test pass locally. The failed run also reported total coverage below the 99%
gate, with the largest changed-file gap in
`src/awf/runtime/pr_monitor_runner/pre_push_validation.py`.

## Plan

1. Use the failed CI logs and coverage artifact as evidence; do not run the
   repository-wide coverage suite locally because AWF/GitHub own that gate.
2. Add focused regression coverage for pre-push validation branches that affect
   push safety:
   - missing workspace / missing HEAD infrastructure failures block push;
   - cleanup and unexpected validation exceptions persist failed validation
     runs with useful reason codes;
   - profile coverage runs only after successful phase validation and persists
     coverage metadata;
   - fix-pass exception paths return `PRE_PUSH_VALIDATION_FIX_FAILED` without
     pushing.
3. Run only targeted unit tests covering the changed test module.
4. Record focused validation evidence in
   `plans/PR288_CI_COVERAGE_VALIDATION.md`, including that full CI coverage is
   left to AWF/GitHub after agent completion.
