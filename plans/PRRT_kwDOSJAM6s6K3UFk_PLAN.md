# PRRT_kwDOSJAM6s6K3UFk Plan

## Problem Statement and Scope

The review reports that pre-push validation fix-pass missing-HEAD recovery can
fall back from a missing `fix_start_head` to the merge-candidate head, create a
clean recovered commit, and then still use the dangling `fix_start_head` as the
post-recovery baseline. That can turn successful recovery into
`PRE_PUSH_VALIDATION_REPARENT_FAILED` when the clean self-commit path attempts to
compare or reparent against `fix_start_head`.

Scope is limited to the fix-pass missing-HEAD fallback anchor handling in
`src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py` and focused
unit coverage for that behavior.

## Requirements Checklist

- Add a regression test where fallback recovery uses a merge-candidate anchor,
  recovery returns a clean new commit, `_commit_dirty_worktree()` returns `False`,
  and the pass accepts the recovered commit without referencing the missing
  `fix_start_head` for descendant checks, reparenting, or rollback.
- Carry the actual recovery fallback anchor into the post-recovery baseline used
  by the clean self-commit and committed-head flows.
- Preserve existing protected-scope recovered-diff validation behavior against
  the recovery anchor.
- Keep rollback and logging behavior unchanged except where the baseline must be
  the recovered fallback anchor to avoid using a missing commit.

## Implementation Steps

1. Add the focused regression test to the existing missing-HEAD fix-pass test
   shard.
2. Introduce a local effective baseline variable initialized to `fix_start_head`.
3. After successful missing-HEAD recovery, update that effective baseline to the
   `recovery_head` used for recovery.
4. Use the effective baseline for `_commit_dirty_worktree()`
   `operation_start_head`, post-clean descendant checks, reparenting, and
   no-commit/no-net-change rollbacks.
5. Run only the targeted unit test shard or narrower selected tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_006.py -q`

Pass criteria: the new regression and existing tests in the shard pass. Full
AWF/GitHub validation remains managed by AWF after agent completion.
