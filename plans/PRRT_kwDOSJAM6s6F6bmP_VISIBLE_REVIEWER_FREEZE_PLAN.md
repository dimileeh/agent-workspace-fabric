# PRRT_kwDOSJAM6s6F6bmP Visible Reviewer Freeze Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6F6bmP` reports that past-settle remonitor freeze
state is bypassed when every configured non-check reviewer is already visible as
a check. `_non_check_reviewer_settle_decision` returns `visible_check` before it
reads the re-armed settle started marker, so auto-merge may proceed after only
initial-review grace.

Scope is limited to non-check reviewer settle decision behavior, focused
regression coverage, and this plan/validation record.

## Requirements Checklist

- Add a regression proving an active remonitor-armed settle marker still blocks
  merge when all configured reviewers have visible checks.
- Preserve the normal visible-check fast path when no re-armed settle marker is
  active.
- Reuse the existing settle wait/elapsed state shape so merge code can keep
  using the existing `reviewer_settle_wait` path.
- Run only focused local validation; AWF/GitHub own broad validation after
  agent completion.

## Implementation Steps

1. Add a focused unit regression in
   `tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py`.
2. Confirm the new regression fails before implementation.
3. Update `src/awf/runtime/pr_monitor_runner/helpers.py` so the visible-check
   branch honors an active head-scoped settle started marker before returning
   `visible_check`.
4. Re-run the targeted regression and focused nearby settle tests.
5. Record validation evidence in
   `plans/PRRT_kwDOSJAM6s6F6bmP_VISIBLE_REVIEWER_FREEZE_VALIDATION.md`.

## Verification Commands And Pass Criteria

- Red test:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py -q -k visible_check_remonitor_freeze`
  fails before the implementation because the decision returns `visible_check`.
- Green tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py -q -k "visible_check_remonitor_freeze or visible_greptile_check_skips_extra_wait or visible_check_skip_is_deduped_per_head"`
  passes after the implementation.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/helpers.py tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py`
  passes.

Full AWF/GitHub validation is intentionally not run during this agent phase.
