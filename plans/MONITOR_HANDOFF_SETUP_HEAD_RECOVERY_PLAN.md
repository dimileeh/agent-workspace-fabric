# Monitor Handoff Setup HEAD Recovery Plan

## Problem Statement and Scope

An unresolved review thread reports that monitor handoff setup handles `ComposeExecCleanupError` by repairing mirror `core.hooksPath` and immediately marking the workspace failed, without running the missing-HEAD verification and recovery used by the main executor setup cleanup path.

Scope is limited to the monitor handoff setup cleanup failure path in `src/awf/control/executor/monitor_handoff_setup.py` and focused regression coverage.

## Requirements Checklist

- Verify whether the review comment is actionable against local code.
- On monitor handoff setup cleanup failure, check whether the worktree HEAD object exists before marking the cleanup failure.
- If HEAD is missing and the executor exposes `_recover_missing_git_head_or_mark_failed`, attempt filesystem recovery with `mark_failed_on_failure=False`.
- Preserve the existing cleanup failure classification and final `_mark_failed` behavior.
- Add focused regression coverage for the cleanup failure recovery call and arguments.
- Run only targeted validation; leave broad AWF/GitHub validation to AWF after agent completion.

## Implementation Steps

1. Read the pointed code and existing executor missing-HEAD recovery helpers.
2. Add a local helper in `monitor_handoff_setup.py` for cleanup-failure HEAD verification/recovery.
3. Call the helper in the `ComposeExecCleanupError` handler after mirror hooks repair succeeds and before marking the cleanup failure.
4. Add a unit test covering missing HEAD recovery in monitor handoff setup cleanup failure.
5. Run the targeted unit test file or test selection.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_018.py -q`

Pass criteria: targeted tests pass, and validation notes document that full AWF/GitHub validation is intentionally not run in the agent phase.
