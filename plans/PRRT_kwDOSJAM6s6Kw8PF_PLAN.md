# PRRT_kwDOSJAM6s6Kw8PF Plan

## Problem Statement and Scope

The pre-push validation fix pass currently lets `_MonitorHeadObjectMissingError`
and `_MonitorMirrorHooksPathRepairFailedError` raised by `_commit_dirty_worktree`
fall into the generic commit exception handler. That can hide the structured
`HEAD_OBJECT_MISSING_UNRECOVERABLE` or `MIRROR_HOOKS_PATH_POISONED` reason code.

Scope is limited to the fix-pass commit-sink exception handling and focused
unit coverage for this review thread.

## Requirements Checklist

- Preserve existing rollback behavior for provider-recovery, policy-blocked,
  and generic commit failures.
- Re-raise deterministic head-object and mirror-hooks commit-sink failures
  before the generic `Exception` handler.
- Convert those re-raised failures into structured pre-push validation result
  reason codes in the parent fix-pass loop.
- Add focused regression coverage for both new reason-coded exceptions.
- Run only targeted tests for the touched behavior; broad AWF/GitHub validation
  remains managed by AWF after agent completion.

## Implementation Steps

1. Import the two reason-coded exception classes where needed.
2. Extend `_run_pre_push_validation_fix_pass` explicit re-raise tuple.
3. Extend `_run_pre_push_validation` with handlers that return the proper
   reason codes and diagnostic messages.
4. Update the existing reason-coded fix-pass test to cover the two additional
   exception classes.
5. Update the fix-pass test-shard fixture so the split helper module uses the
   existing fake HEAD-object success path.
6. Run the focused unit test selection for the changed behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/conftest.py tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py`

Pass criteria: the targeted test file passes and exercises both new exception
classes without running the broad repository validation suite.
