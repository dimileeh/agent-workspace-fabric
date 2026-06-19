# Protected Scope Recovered Fix Pass Plan

## Problem Statement And Scope

Address PR review thread `PRRT_kwDOSJAM6s6KyPln`: when the pre-push validation fix-pass missing-HEAD recovery creates a commit from filesystem state, the later dirty-worktree commit sink can see a clean tree and skip protected-scope repair. The recovered delta must be checked before the pass is accepted as committed.

Scope is limited to `src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py` and focused unit coverage for that fix-pass recovery path.

## Requirements Checklist

- Add a regression test for a recovered missing-HEAD fix-pass commit that touches a protected file and leaves a clean worktree.
- Re-check protected scope for `fix_start_head..recovered` before treating the recovered commit as acceptable.
- Preserve existing rollback/provider-recovery behavior outside this recovery path.
- Run focused tests only; full AWF/GitHub validation remains managed by AWF after agent completion.

## Implementation Steps

1. Write a focused failing unit test in the fix-pass parts tests.
2. Add a minimal helper or inline check after successful missing-HEAD recovery in the fix-pass helper.
3. Ensure protected-scope repair receives the recovered paths and can leave dirty repair residue for the normal commit sink.
4. Run the focused test module or selected test.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_004.py -q`

Pass criteria: the new regression and neighboring fix-pass protected-scope tests pass. Broad validation is intentionally left to AWF/GitHub.
