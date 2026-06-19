# Plan: PRRT_kwDOSJAM6s6KpAD6 — CI rollback uses stale HEAD

## Problem statement and scope

Cursor Bugbot (PR #615, `src/awf/runtime/pr_monitor_runner/ci_ops.py:284`,
discussion r3438067491) reports that the CI-repair provider-recovery residue
rollback anchors against a stale HEAD.

In `_run_ci_fix` (`src/awf/runtime/pr_monitor_runner/ci_ops.py`), the
post-agent/pre-sink HEAD is captured at line 243 — BEFORE the `try` block that
calls `_commit_dirty_worktree`. The protected-scope repair agent runs INSIDE
`_commit_dirty_worktree` (via `_repair_protected_scope_changes_before_commit`)
and may self-commit, advancing HEAD past that snapshot before the
provider-recovery control-flow exception is raised. When the
`ProviderRecoveryRetryError` / `ProviderRecoveryFallbackError` /
`ProviderRecoveryAuthError` handler then calls
`_rollback_ci_fix_residue_before_provider_recovery(..., restore_ref=post_agent_head)`,
`git reset --hard <post_agent_head>` discards the protected-scope repair
self-commit, so the provider retry starts from the old tree and loses or
redoes valid repair work.

The dirty-finalize path (`pre_push_validation_dirty_finalize.py`) already fixed
this exact class of bug: it captures `post_agent_head` INSIDE each
provider-recovery `except` clause (lines 510, 538, 566) AFTER
`_commit_dirty_worktree` raised, so the anchor reflects any in-sink self-commit.
Regression test `test_pre_push_validation_finalize_provider_recovery_rolls_back_to_post_agent_head_not_finalize_start_head`
(`tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize_post_commit.py:407`)
documents that contract (PRRT_kwDOSJAM6s6KnWkn).

The CI-repair path captures the anchor too early and is therefore inconsistent
with the finalize path and the regression it already established.

### Scope

- Narrow fix to `_run_ci_fix` in `src/awf/runtime/pr_monitor_runner/ci_ops.py`
  only.
- Move the `post_agent_head` capture into the
  `ProviderRecoveryRetryError` / `ProviderRecoveryFallbackError` /
  `ProviderRecoveryAuthError` `except` block, AFTER `_commit_dirty_worktree`
  raised, mirroring the finalize path.
- Do NOT touch the fix-pass path, the finalize path, the rollback helper
  (`_rollback_ci_fix_residue_before_provider_recovery`), or any other caller.
  The reviewer pointed only at the CI-repair site.

## Explicit requirements checklist

1. `post_agent_head` is captured inside the provider-recovery `except` block
   of `_run_ci_fix`, AFTER `_commit_dirty_worktree` raised — not before the
   `try`.
2. The captured HEAD is passed as `restore_ref` to
   `_rollback_ci_fix_residue_before_provider_recovery`, preserving any
   protected-scope repair self-commit while discarding only the stranded
   residue.
3. `None` (HEAD could not be resolved after the sink raised) still skips the
   rollback, mirroring the existing `restore_ref is None` guard in the helper
   and the finalize path's missing-anchor behavior.
4. The pre-`try` capture block (current lines 226-243) is removed; its
   docstring rationale is rewritten to describe the post-raise capture and
   reference the finalize path's anchoring (PRRT_kwDOSJAM6s6KnWkn).
5. A regression test is added (test-first) that asserts the rollback anchors
   against the HEAD captured AFTER the sink raised (carrying the in-sink
   self-commit), NOT the pre-sink HEAD. The existing
   `test_ci_fix_commit_sink_provider_recovery_rolls_back_to_post_agent_head_not_operation_start_head`
   is updated if its command queue no longer matches the new capture timing.
6. No other callers or files are changed.

## Implementation steps

1. Write/update the failing regression test first in
   `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_011.py`
   to model the in-sink self-commit scenario:
   - The CI-repair agent runs and does NOT self-commit (HEAD unchanged from
     `operation_start_head`).
   - `_commit_dirty_worktree` is mocked to raise a provider-recovery exception,
     but BEFORE raising the protected-scope repair agent inside the sink
     self-committed and advanced HEAD to a distinct `in_sink_self_commit_head`.
   - Assert the rollback `git reset --hard` anchors against
     `in_sink_self_commit_head` (the post-raise HEAD), NOT
     `operation_start_head` and NOT the pre-sink `post_agent_head`.
2. Confirm the test fails against the current code (the rollback uses the
   pre-sink `post_agent_head`, which in this scenario equals
   `operation_start_head`, so the self-commit would be dropped).
3. Edit `src/awf/runtime/pr_monitor_runner/ci_ops.py`:
   - Remove the pre-`try` `post_agent_head = await self._rev_parse_head(...)`
     block (lines 226-243).
   - Inside the `ProviderRecoveryRetryError` /
     `ProviderRecoveryFallbackError` / `ProviderRecoveryAuthError` `except`
     block, capture `post_agent_head = await self._rev_parse_head(worktree_path)`
     AFTER the sink raised and BEFORE calling the rollback helper, mirroring
     `pre_push_validation_dirty_finalize.py:510`.
   - Update the docstring/comment to describe the post-raise anchoring and
     reference PRRT_kwDOSJAM6s6KnWkn (the finalize regression that established
     the in-sink self-commit contract).
4. Re-run the new regression test and the existing CI-fix residue rollback
   tests to confirm both pass.

## Verification commands and pass criteria

Focused (agent-phase safe) checks:

```
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/ci_ops.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_011.py
uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/ci_ops.py
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_011.py -q
```

Pass criteria:
- New regression test passes (anchors against post-raise HEAD).
- Existing
  `test_ci_fix_commit_sink_provider_recovery_rolls_back_to_post_agent_head_not_operation_start_head`
  and
  `test_ci_fix_commit_sink_provider_recovery_rollback_skipped_when_post_agent_head_unavailable`
  still pass.
- `ruff check` and `mypy` clean for the touched files.
- Full AWF/GitHub broad validation is managed by AWF after agent completion;
  the agent does NOT run the full coverage gate or whole-repo suite.
