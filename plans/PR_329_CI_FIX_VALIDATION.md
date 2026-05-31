# PR 329 CI Fix Validation

## Focused Checks Run

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_controls_idempotency_parts/test_workspace_controls_idempotency_part_001.py::test_remonitor_resets_only_claims_and_records_audit_rows tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/test_core_decomposition_maintainability.py::test_public_facades_do_not_dynamic_scan_private_modules -q`
  - Result: passed, `3 passed in 1.83s`.
- `uv run --python 3.12 --extra dev ruff check ...`
  - Result: passed for the touched source and test files.
- `uv run --python 3.12 --extra dev mypy src/awf/service/controls.py src/awf/service/workspaces.py`
  - Result: passed, `Success: no issues found in 2 source files`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_controls_idempotency_parts/test_workspace_controls_idempotency_part_003.py -q`
  - Result: passed, `5 passed in 7.12s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_004.py -q`
  - Result: passed, `12 passed in 11.45s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints_merge_recheck.py -q`
  - Result: passed, `10 passed in 14.61s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py -q`
  - Result: passed, `9 passed in 2.87s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q`
  - Result: passed, `25 passed in 25.58s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q`
  - Result: passed, `25 passed in 23.95s`.
- `git diff --check`
  - Result: passed.

## Notes

The CI failure was limited to `python-full-coverage`. Local validation stayed
focused per the AWF workspace contract; full AWF/GitHub validation and coverage
provenance remain owned by AWF after agent completion.
