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

## Iteration 2: Exact Coverage Failure

### Requirement Status

- Add focused tests that cover real operator-hint runner behavior: Complete.
- Avoid protected workflow, quality-gate, and broad configuration edits:
  Complete.
- Keep tests below first-party line-count guardrails: Complete.
- Run narrow pytest/ruff checks for touched files only: Complete.
- Leave full AWF/GitHub coverage validation to AWF after agent completion:
  Complete.

### Evidence

- Added `tests/unit/runtime/test_pr_monitor_operator_hint_coverage_edges.py`
  covering operator hint early returns, default terminal reasons, non-terminal
  push failures, processed pushed hints without a resolved head SHA, concurrent
  hint/freeze merge helpers, activity-freeze parsing, and manual-ready helper
  branches.
- Updated `plans/PR_329_CI_FIX_PLAN.md` with this coverage-focused iteration.

### Focused Checks Run

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hint_coverage_edges.py -q`
  - Result: passed, `24 passed in 8.38s`.
- `uv run --python 3.12 --extra dev ruff check tests/unit/runtime/test_pr_monitor_operator_hint_coverage_edges.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev ruff format --check tests/unit/runtime/test_pr_monitor_operator_hint_coverage_edges.py`
  - Result: passed, `1 file already formatted`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hint_coverage_edges.py tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Result: passed, `25 passed in 7.80s`.
- `git diff --check`
  - Result: passed.

Full `python-full-coverage` was not run locally per the AWF workspace contract;
AWF/GitHub own the broad coverage gate after this local fix is captured.
