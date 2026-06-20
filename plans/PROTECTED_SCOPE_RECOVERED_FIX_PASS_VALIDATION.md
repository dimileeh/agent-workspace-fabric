# Protected Scope Recovered Fix Pass Validation

Plan reference: `plans/PROTECTED_SCOPE_RECOVERED_FIX_PASS_PLAN.md`

## Requirement Status

- Add a regression test for a recovered missing-HEAD fix-pass commit that touches a protected file and leaves a clean worktree: Complete.
- Re-check protected scope for `fix_start_head..recovered` before treating the recovered commit as acceptable: Complete.
- Preserve existing rollback/provider-recovery behavior outside this recovery path: Complete; the recovered-delta check runs inside the existing commit-sink exception envelope and rolls back on local check failure.
- Run focused tests only; full AWF/GitHub validation remains managed by AWF after agent completion: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py::test_pre_push_validation_fix_pass_repairs_protected_scope_after_missing_head_recovery -q` passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py` passed.

Additional local observation:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py -q` was attempted and failed in existing shard setup paths with `MIRROR_HOOKS_PATH_POISONED` before the mocked commit-sink scenarios under test. The new focused regression passed independently. Full AWF/GitHub validation is managed by AWF after agent completion.

## Gaps

No planned requirements remain open.
