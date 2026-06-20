# Pre-Push Fix Mirror Repair Exception Validation

Plan reference: `plans/PRE_PUSH_FIX_MIRROR_REPAIR_EXCEPTION_PLAN.md`

## Requirement Status

- Complete: Added regression coverage proving generic fix-agent exceptions
  repair the mirror after rollback.
- Complete: Preserved rollback failure precedence; the handler still returns a
  rollback failure before attempting post-exception mirror repair.
- Complete: Added coverage proving post-exception mirror repair failure fails
  closed with `MIRROR_HOOKS_PATH_POISONED`.
- Complete: Kept validation focused to touched behavior and narrow lint.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`
- `plans/PRE_PUSH_FIX_MIRROR_REPAIR_EXCEPTION_PLAN.md`
- `plans/PRE_PUSH_FIX_MIRROR_REPAIR_EXCEPTION_VALIDATION.md`

Checks run:

- Failing pre-fix regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py -q -k generic_exception_repairs_hooks_path`
  failed because events were `["repair", "agent"]` instead of
  `["repair", "agent", "repair"]`.
- Passing focused tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py -q -k "generic_exception or cleanup_error_repairs_hooks_path"`
  passed with 5 tests.
- Passing narrow lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`

Full AWF/GitHub validation is managed by AWF after agent completion.
