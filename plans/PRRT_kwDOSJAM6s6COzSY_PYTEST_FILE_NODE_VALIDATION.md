# PRRT_kwDOSJAM6s6COzSY Pytest File Node Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6COzSY_PYTEST_FILE_NODE_PLAN.md`

## Requirement Status

- Complete: Capture file-level pytest node IDs for short-summary `ERROR` lines.
  `tests/unit/runtime/test_validation.py` adds a regression for
  `ERROR tests/unit/test_imports.py - ...`.
- Complete: Preserve class, function, and parametrized node ID behavior. The
  existing parser tests for xdist/class and parametrized IDs pass.
- Complete: Preserve the existing boundary that non-`ERROR` file-only summaries
  do not become node IDs. The existing
  `test_pytest_failure_parser_does_not_scan_error_details_for_node_ids` passes.
- Complete: Keep fallback evidence collection unchanged. The existing fallback
  parser regression passes.

## Evidence

Files changed:

- `src/awf/runtime/validation.py`
- `tests/unit/runtime/test_validation.py`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::TestCoverageEnforcement::test_pytest_failure_parser_captures_file_level_error_node_ids -q`
  failed before implementation with no parsed node ID.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::TestCoverageEnforcement::test_pytest_failure_parser_captures_file_level_error_node_ids tests/unit/runtime/test_validation.py::TestCoverageEnforcement::test_pytest_failure_parser_preserves_class_style_xdist_node_ids tests/unit/runtime/test_validation.py::TestCoverageEnforcement::test_pytest_failure_parser_preserves_param_ids_with_spaces tests/unit/runtime/test_validation.py::TestCoverageEnforcement::test_pytest_failure_parser_does_not_scan_error_details_for_node_ids tests/unit/runtime/test_validation.py::TestCoverageEnforcement::test_pytest_failure_parser_falls_back_to_best_evidence_without_node_ids -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passed with 139 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  passed.

No gaps remain.
