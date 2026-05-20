# PRRT_kwDOSJAM6s6DWQI1 Rename-Aware Committed Guard Validation

## Plan Reference

- `plans/PRRT_kwDOSJAM6s6DWQI1_RENAME_AWARE_COMMITTED_GUARD_PLAN.md`

## Requirement Status

- Rename-aware committed-diff path loader includes rename and copy source and
  destination paths: Complete.
- `_fail_if_protected_quality_gate_committed_output` consumes the
  rename-aware path loader: Complete.
- Regression test blocks a committed rename from
  `.github/workflows/ci.yml` to `docs/ci.yml`: Complete.
- Existing staged-path and non-protected committed-path behavior remains
  intact: Complete.
- No branch switch or push: Complete.

## Evidence

Files changed:

- `src/awf/control/protected_file_diffs.py`
- `src/awf/control/executor.py`
- `tests/unit/control/test_executor_coverage_edges.py`
- `tests/unit/control/test_executor_validation_fix_cycle.py`
- `tests/unit/control/test_executor.py`
- `tests/unit/control/test_executor_monitor_recovery.py`
- `tests/unit/control/test_executor_error_paths.py`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges.py::test_committed_quality_gate_guard_blocks_protected_rename_source -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_validation_fix_cycle.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_monitor_recovery.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor.py src/awf/control/protected_file_diffs.py tests/unit/control/test_executor_coverage_edges.py tests/unit/control/test_executor_validation_fix_cycle.py tests/unit/control/test_executor.py tests/unit/control/test_executor_monitor_recovery.py tests/unit/control/test_executor_error_paths.py tests/unit/control/test_protected_file_diffs.py`
- `uv run --python 3.12 --extra dev mypy src/awf`

All listed commands passed after the implementation and fixture updates.
