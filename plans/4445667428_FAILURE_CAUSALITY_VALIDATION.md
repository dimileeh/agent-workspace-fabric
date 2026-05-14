# Review Comment 4445667428 Failure Causality Validation

Plan reference: `plans/4445667428_FAILURE_CAUSALITY_PLAN.md`

## Requirement Status

- Complete: Bound `secondary_failures` to a small deterministic tail while preserving the latest `secondary_failure` field.
  - Evidence: `build_preserved_failure_payload` now caps retained secondary history at 20 entries and always writes the current secondary to `secondary_failure`.
- Complete: Preserve ordering of the retained secondary history.
  - Evidence: New tests assert that the retained history is the ordered tail of prior secondaries plus the current secondary.
- Complete: Restore `workspace.failure_reason` and `workspace.failure_message` from primary evidence in the controls cleanup failure path when cleanup failure is secondary.
  - Evidence: `WorkspaceControlService.destroy_workspace` now calls `_restore_primary_failure_row_fields` when `primary_failure` is present.
- Complete: Keep the existing epoch-reset behavior that ignores stale embedded primary failures after a resume.
  - Evidence: No changes were made to epoch reset predicates or primary bootstrapping rules; full `test_failure_causality.py` still passes.
- Complete: Add regression tests that fail without the implementation.
  - Evidence: The three focused regression tests failed before implementation and pass after it.
- Complete: Run the narrowest relevant test commands and record results in validation.
  - Evidence: Commands below.

## Files Changed

- `src/awf/service/failure_causality.py`
- `src/awf/service/controls.py`
- `tests/unit/service/test_failure_causality.py`
- `tests/unit/service/test_controls.py`
- `plans/4445667428_FAILURE_CAUSALITY_PLAN.md`
- `plans/4445667428_FAILURE_CAUSALITY_VALIDATION.md`

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py::test_preserved_failure_payload_caps_secondary_failure_history tests/unit/service/test_failure_causality.py::test_secondary_failure_history_reader_returns_bounded_tail tests/unit/service/test_controls.py::test_destroy_cleanup_failure_restores_primary_fields_from_embedded_payload -q`
  - Initial result before implementation: failed, confirming the regressions.
  - Final result after implementation: passed, `3 passed in 1.05s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py tests/unit/service/test_controls.py -q`
  - Passed, `55 passed in 14.29s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py src/awf/service/controls.py tests/unit/service/test_failure_causality.py tests/unit/service/test_controls.py`
  - Passed, `All checks passed!`.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed, `Success: no issues found in 154 source files`.

## Remaining Gaps

None.
