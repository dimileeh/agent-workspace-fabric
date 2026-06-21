# PRRT_kwDOSJAM6s6Kw8PF Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6Kw8PF_PLAN.md`

## Requirement Status

- Complete: Existing provider-recovery, policy-blocked, and generic rollback
  behavior was left intact.
- Complete: `_run_pre_push_validation_fix_pass` now re-raises
  `_MonitorHeadObjectMissingError` and
  `_MonitorMirrorHooksPathRepairFailedError` before the generic exception
  handler.
- Complete: `_run_pre_push_validation_with_fix_passes` maps the re-raised
  exceptions to `HEAD_OBJECT_MISSING_UNRECOVERABLE` and
  `MIRROR_HOOKS_PATH_POISONED` structured validation results.
- Complete: Existing fix-pass reason-coded exception tests now cover both
  additional exceptions at the low-level fix-pass boundary and the structured
  push-failure boundary.
- Complete: The fix-pass test-shard fixture now patches
  `pre_push_validation_fix_pass.verify_head_object_exists`, matching the helper
  split and keeping fake worktrees on their intended paths.

## Evidence

Changed files:

- `src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py`
- `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/conftest.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py -q -k 'preserves_reason_coded_commit_exceptions or reason_coded_commit_exception_is_structured_push_failure'` passed: 9 passed, 17 deselected.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py -q` passed: 26 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/conftest.py tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py` passed.

Full AWF/GitHub validation was not run in the agent phase per the workspace
contract; AWF owns broad validation after completion.

## Gaps

None.
