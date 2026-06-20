# Recovered Fix-Pass Diff Anchor Validation

Plan reference: `RECOVERED_FIX_PASS_DIFF_ANCHOR_PLAN.md`

## Requirement Status

- Use the actual recovery anchor for recovered-delta comparison: Complete.
  `pre_push_validation_fix_pass.py` now stores `recovery_head` as the
  protected-scope base when the recovered commit differs from that anchor.
- Only run recovered-delta validation when `recovered != recovery_head`:
  Complete. The recovered-delta block is no longer entered merely because
  `recovered != fix_start_head`.
- Preserve existing behavior for the non-fallback path: Complete. In the normal
  path `recovery_head` remains `fix_start_head`, so the existing diff range is
  unchanged.
- Add focused regression coverage for fallback recovery using the merge-candidate
  SHA as the diff base: Complete. The missing-HEAD fallback test now asserts
  `candidate_head..recovered_head` is used and `fix_start_head..recovered_head`
  is not used.
- Do not run broad AWF/GitHub-owned validation: Complete. Only focused unit
  tests were run; full AWF/GitHub validation remains managed by AWF after agent
  completion.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_006.py`
- `plans/RECOVERED_FIX_PASS_DIFF_ANCHOR_PLAN.md`
- `plans/RECOVERED_FIX_PASS_DIFF_ANCHOR_VALIDATION.md`

Focused checks:

- First confirmed the targeted regression failed before the production fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_006.py::test_pre_push_validation_fix_pass_missing_head_falls_back_from_stale_anchor -q`
- After the fix, the same targeted test passed.
- Focused recovery surface passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_005.py tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_006.py -q`
  Result: `3 passed`.
- Focused lint passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_006.py`

## Remaining Gaps

None.
