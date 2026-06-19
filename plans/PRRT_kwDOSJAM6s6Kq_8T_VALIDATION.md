# Validation: PRRT_kwDOSJAM6s6Kq_8T — fail closed when no-commit-clean HEAD cannot be compared

Plan reference: `plans/PRRT_kwDOSJAM6s6Kq_8T_PLAN.md`

## Requirement-by-requirement status

1. **Run the committed-delta ownership check whenever HEAD movement cannot be
   proven absent** — Complete.
   `pre_push_validation_dirty_finalize.py` now computes
   `head_movement_unknown = finalize_start_head is None or post_agent_head is
   None or post_agent_head != finalize_start_head` and runs the
   `_committed_delta_paths` check whenever that is true. The only case that
   skips the gate is the proven-no-movement case (both anchors present and
   equal).

2. **Missing anchor + uninspectable delta → delta-unavailable reason** —
   Complete (behaviorally preserved). The `post_no_commit_delta is None`
   branch already returns `_PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE_REASON`
   and now also covers the missing-anchor case because the gate runs whenever
   HEAD movement is unknown. No new code path required; the existing
   fail-closed branch applies.

3. **Missing anchor + unowned delta → unowned-delta reason** — Complete.
   The `unowned_no_commit` branch returns
   `_PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON` and is now reached even
   when an anchor is missing.

4. **Preserve no-self-commit (anchors equal) fast path** — Complete.
   Regression test
   `test_pre_push_validation_finalize_no_commit_clean_proceeds_when_self_commit_owned`
   and the no-op finalize tests in
   `test_pr_monitor_pre_push_validation_finalize.py` still pass; the equal-heads
   case skips the gate.

5. **Preserve present-and-differ path** — Complete. Existing regression tests
   `test_pre_push_validation_finalize_no_commit_clean_blocks_self_commit_unowned_delta`
   and
   `test_pre_push_validation_finalize_no_commit_clean_delta_unavailable_when_self_commit_delta_missing`
   still pass unchanged.

6. **Regression tests for the two new fail-closed paths** — Complete.
   Added:
   - `test_pre_push_validation_finalize_no_commit_clean_blocks_self_commit_delta_when_finalize_start_head_missing`
   - `test_pre_push_validation_finalize_no_commit_clean_blocks_self_commit_delta_when_post_agent_head_missing`
   Both failed against the pre-fix code (gate skipped on `None` anchor →
   validation proceeded and the unowned self-commit was pushed) and pass
   against the fix.

## Evidence

Files changed:
- `src/awf/runtime/pr_monitor_runner/pre_push_validation_dirty_finalize.py`
  (gate condition restructured + comment updated; +38/-6 lines)
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize_post_commit_delta.py`
  (+171 lines, two new regression tests)
- `plans/PRRT_kwDOSJAM6s6Kq_8T_PLAN.md` (plan)

Verification commands run (focused, per AWF agent-phase policy — broad
AWF/GitHub validation is owned by AWF after agent completion):
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize_post_commit_delta.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize_post_commit.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize_post_commit_edges.py -q` → 46 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation_dirty_finalize.py tests/unit/runtime/test_pr_monitor_pre_push_validation_finalize_post_commit_delta.py` → All checks passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation_dirty_finalize.py` → no issues found.

## Gaps

None. All planned requirements are complete.
