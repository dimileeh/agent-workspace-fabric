# Monitor Handoff Fallback Persistence Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6F_WcL` reports that monitor-handoff setup
failure handling logs and returns when both the normal `_mark_failed` path and
the direct `transition_if_current` fallback fail. That can leave a workspace in
`running` with no monitor and no terminal failure persisted.

Scope is limited to the last-resort monitor-handoff setup failure persistence
path in `src/awf/control/executor/monitor_handoff.py` and a focused regression
test.

## Requirements Checklist

- Add a regression test showing direct fallback persistence failures propagate.
- Preserve successful direct fallback behavior.
- Preserve existing behavior when no direct persistence fallback is available.
- Keep secret redaction and terminal failure payload shaping unchanged.
- Use only focused validation; AWF/GitHub owns broad validation after agent
  completion.

## Implementation Steps

1. Add a unit test for `_mark_failed_from_monitor_handoff_setup_failure` where
   `_mark_failed` fails, `_session_factory` exists, and direct persistence fails.
2. Confirm the new test fails against current behavior.
3. Re-raise after logging failures in the direct terminal fallback helper.
4. Run the targeted tests for the modified monitor-handoff setup error paths.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -q -k "monitor_handoff_setup_failure or terminal_fallback"`
  passes after the implementation.
- Initial focused test run should fail before the implementation, demonstrating
  the regression coverage.
