# PRRT_kwDOSJAM6s6C-8xU Test Path Prefix Context Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6C-8xU_TEST_PATH_PREFIX_CONTEXT_PLAN.md`

## Requirement Status

- Reproduce the reported gap with a failing unit regression: Complete.
  The targeted test failed before the implementation for `src/tests/unit/...`
  and `./tests/...` work-request gaps.
- Keep validation command handoff paths classified as AWF-validation-only when
  they are command paths, not work requests: Complete. Existing command handoff
  tests remain covered by the full planning test module run.
- Classify verb-plus-prefixed-test-path gaps as deterministic agent work:
  Complete. Regression cases now assert these mixed gaps are rejected as
  AWF-validation-only handoffs.
- Keep the code change narrowly scoped to the helper that detects test-path
  work context: Complete. Implementation changed only
  `_has_test_path_work_context`.

## Evidence

Files changed:

- `src/awf/runtime/planning.py`
- `tests/unit/runtime/test_planning.py`
- `plans/PRRT_kwDOSJAM6s6C-8xU_TEST_PATH_PREFIX_CONTEXT_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6C-8xU_TEST_PATH_PREFIX_CONTEXT_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning.py::test_conformance_requires_awf_validation_rejects_mixed_named_command_test_path_work_gaps -q`
  failed before the implementation with 2 failing prefixed-path cases.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning.py::test_conformance_requires_awf_validation_rejects_mixed_named_command_test_path_work_gaps -q`
  passed after the implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning.py -q`
  passed with 102 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/planning.py tests/unit/runtime/test_planning.py`
  passed.

## Remaining Gaps

None.
