# PRRT_kwDOSJAM6s6LfL02 Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6LfL02_PLAN.md`

## Requirement Status

- Reproduce the cross-verdict backfill bug with a focused regression test:
  Complete. The new regression failed before the parser change because a
  trailing empty `AWF-VERDICT: NEEDS_HUMAN:` returned `fix_committed`.
- Preserve final AWF-prefixed verdict precedence: Complete. The parser now
  returns the final AWF verdict when no same-verdict reason is available.
- Preserve same-verdict reason reuse: Complete. The companion regression covers
  reuse from an earlier `NEEDS_HUMAN` verdict after an intervening `FIXED`.
- Avoid broad validation in the agent phase: Complete. Only focused parser tests
  and narrow lint were run; full AWF/GitHub validation is managed after agent
  completion.

## Evidence

- Changed `src/awf/runtime/pr_monitor_runner/helpers.py`.
- Changed `tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py`.
- Added `plans/PRRT_kwDOSJAM6s6LfL02_PLAN.md`.
- Added this validation document.

## Commands

- `uv run --python 3.12 --extra dev pytest tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::TestParseVerdict -q`
  - Before fix: failed on
    `test_final_empty_awf_verdict_does_not_backfill_cross_verdict_result`.
  - After fix: `15 passed`.
- `uv run --python 3.12 --extra dev ruff format tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py`
  - Result: `1 file reformatted`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/helpers.py tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py`
  - Result: `All checks passed!`

## Gaps

No validation gaps for this scoped change. Broad repository validation and merge
gating remain owned by AWF/GitHub after the agent exits.
