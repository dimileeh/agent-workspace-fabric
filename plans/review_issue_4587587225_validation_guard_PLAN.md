# Review Issue 4587587225 Validation Guard Plan

## Problem Statement and Scope

The review-level comment identifies two small regressions in the PR monitor
handoff/pre-push validation work:

- `tests/unit/runtime/test_pr_monitor_remote_ops_toolchain_terminal.py` hard-codes
  the toolchain-missing reason code instead of importing the shared constant.
- `_run_monitor_handoff_profile_setup` calls `self._validation.run_profile_phases`
  without first handling a missing validation runner, so `_validation = None`
  produces a generic redacted `AttributeError` failure message.

Scope is limited to synchronizing the test with the shared constant and adding
an explicit monitor-handoff setup diagnostic for missing validation.

## Requirements Checklist

- Import and use `_PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING_REASON` in the terminal
  push-outcome regression test.
- Add focused regression coverage for `_run_monitor_handoff_profile_setup` when
  `_validation` is `None`.
- Make the missing-validation path fail explicitly through the existing
  monitor-handoff setup failure persistence path.
- Avoid broad AWF/GitHub-owned validation; run only focused local checks.

## Implementation Steps

1. Update the runtime test to use the shared pre-push validation constant.
2. Add a failing unit test covering `_validation = None` in
   `_run_monitor_handoff_profile_setup`, asserting the named setup reason code
   and a clear missing-validation message.
3. Add the explicit validation-runner guard in
   `src/awf/control/executor/monitor_handoff_setup.py`.
4. Run the focused tests before and after implementation, then run targeted
   lint for only the changed files.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_handoff_setup_missing_validation_marks_clear_setup_failure tests/unit/runtime/test_pr_monitor_remote_ops_toolchain_terminal.py::test_toolchain_missing_pre_push_validation_failure_is_terminal -q
uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff_setup.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py tests/unit/runtime/test_pr_monitor_remote_ops_toolchain_terminal.py
```

Pass criteria: the missing-validation test fails before the guard, passes after
the guard, the terminal push-outcome test still passes using the shared
constant, and targeted lint passes. Full AWF/GitHub validation remains owned by
AWF after agent completion.
