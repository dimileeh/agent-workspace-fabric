# Comment 3331028609 Monitor Handoff Mark Failed Validation

## Plan Conformance

- Added a focused regression for monitor handoff setup command failure
  persistence fallback.
- Confirmed the regression failed before the production change: only one
  `_mark_failed` attempt was made.
- Updated `monitor_handoff_setup.py` so a failed detailed setup failure
  persistence attempt is followed by a generic `PR_MONITOR_SETUP_FAILED`
  transition without dependency details.
- Left fallback transition errors unswallowed so handoff callers do not exit
  quietly if persistence is still unavailable.

## Focused Checks

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_handoff_setup_mark_failed_error_after_command_failure_falls_back -q
```

Result before implementation: failed with `assert len(mark_failed_calls) == 2`
because only one `_mark_failed` call occurred.

Result after implementation: passed.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup -q
```

Result: passed, `14 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff_setup.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py
```

Result: passed.

## Deferred Validation

Full AWF/GitHub validation, full unit suites, and coverage gates are managed by
AWF after agent completion per the workspace contract.
