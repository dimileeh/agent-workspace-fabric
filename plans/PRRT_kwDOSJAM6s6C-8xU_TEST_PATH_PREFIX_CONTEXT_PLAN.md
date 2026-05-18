# PRRT_kwDOSJAM6s6C-8xU Test Path Prefix Context Plan

## Problem Statement and Scope

The review thread reports that conformance gap classification can treat mixed
AWF-validation handoff plus test-file work as validation-only when the test path
contains a prefix before `tests`, such as `src/tests/unit/...` or `./tests/...`.
Scope is limited to the test-path work-context detection in
`src/awf/runtime/planning.py` and its regression coverage.

## Requirements Checklist

- Reproduce the reported gap with a failing unit regression.
- Keep validation command handoff paths classified as AWF-validation-only when
  they are command paths, not work requests.
- Classify verb-plus-prefixed-test-path gaps as deterministic agent work.
- Keep the code change narrowly scoped to the helper that detects test-path work
  context.

## Implementation Steps

1. Add regression cases to the existing conformance handoff test coverage for
   `src/tests/unit/...` and `./tests/...` work requests.
2. Run the targeted test to confirm the current implementation fails.
3. Update `_has_test_path_work_context` to allow path-token prefixes between
   the work verb/modifiers and the matched `tests` path segment.
4. Re-run the targeted regression and relevant runtime planning tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning.py::test_conformance_requires_awf_validation_rejects_mixed_named_command_test_path_work_gaps -q`
  passes after the implementation and fails before it.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning.py -q`
  passes after the implementation.
