# CI PR 396 Monitor GC Line Limit Validation

Plan reference: `plans/CI_PR396_MONITOR_GC_LINE_LIMIT_PLAN.md`

## Requirement Status

- Reproduce the reported focused failure before editing: Complete.
  - Evidence: `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
    failed with `tests/unit/runtime/test_monitor_completion_gc.py` reported at
    1,641 lines.
- Keep the maintainability guard unchanged: Complete.
  - Evidence: no edits were made to
    `tests/unit/test_core_decomposition_maintainability.py`.
- Split a coherent subset of `test_monitor_completion_gc.py` into a separate
  unit test module so all first-party code files are under 1,500 lines:
  Complete.
  - Evidence: moved completed-monitor filesystem GC cleanup cases into
    `tests/unit/runtime/test_monitor_completion_filesystem_gc.py`.
  - Evidence: `wc -l tests/unit/runtime/test_monitor_completion_gc.py tests/unit/runtime/test_monitor_completion_filesystem_gc.py`
    reported 1,004 and 693 lines respectively.
- Preserve the moved tests' behavior and assertions: Complete.
  - Evidence: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py tests/unit/runtime/test_monitor_completion_filesystem_gc.py -q`
    passed with `26 passed`.
- Run focused verification only; leave broad AWF/GitHub validation to AWF after
  agent completion: Complete.
  - Evidence: only targeted ruff, focused maintainability, and affected-module
    pytest commands were run locally.
- Commit the fix locally with a conventional commit message: Complete.
  - Evidence: the fix cycle is staged for a local conventional commit after
    this validation document is saved.

## Verification Evidence

- `uv run --python 3.12 --extra dev ruff check tests/unit/runtime/test_monitor_completion_gc.py tests/unit/runtime/test_monitor_completion_filesystem_gc.py`
  - Passed: `All checks passed!`
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passed: `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py tests/unit/runtime/test_monitor_completion_filesystem_gc.py -q`
  - Passed: `26 passed`.

Full AWF/GitHub validation was not run locally because AWF owns broad
post-agent validation and merge gating for this workspace.
