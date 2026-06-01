# Review Issue 4587587225 Validation Guard Validation

Plan reference:
`plans/review_issue_4587587225_validation_guard_PLAN.md`

## Requirement Status

- Import and use `_PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING_REASON` in the terminal
  push-outcome regression test: Complete.
- Add focused regression coverage for `_run_monitor_handoff_profile_setup` when
  `_validation` is `None`: Complete.
- Make the missing-validation path fail explicitly through the existing
  monitor-handoff setup failure persistence path: Complete.
- Avoid broad AWF/GitHub-owned validation; run only focused local checks:
  Complete.

## Evidence

Files changed:

- `src/awf/control/executor/monitor_handoff_setup.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
- `tests/unit/runtime/test_pr_monitor_remote_ops_toolchain_terminal.py`
- `plans/review_issue_4587587225_validation_guard_PLAN.md`
- `plans/review_issue_4587587225_validation_guard_VALIDATION.md`

Focused checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_handoff_setup_missing_validation_marks_clear_setup_failure tests/unit/runtime/test_pr_monitor_remote_ops_toolchain_terminal.py::test_toolchain_missing_pre_push_validation_failure_is_terminal -q
```

Result before implementation: failed as expected because `_validation = None`
was reported as a redacted `AttributeError`; the terminal push-outcome test
passed.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_handoff_setup_missing_validation_marks_clear_setup_failure tests/unit/runtime/test_pr_monitor_remote_ops_toolchain_terminal.py::test_toolchain_missing_pre_push_validation_failure_is_terminal -q
```

Result after implementation: passed, `2 passed in 0.86s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff_setup.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py tests/unit/runtime/test_pr_monitor_remote_ops_toolchain_terminal.py
```

Result after implementation: passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after completion.
