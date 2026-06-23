# PRRT_kwDOSJAM6s6LfL02 Plan

## Problem And Scope

The AWF verdict parser may return an earlier AWF verdict result when the final
AWF-prefixed verdict has no reason. Scope is limited to the parser behavior
reported on `src/awf/runtime/pr_monitor_runner/helpers.py`.

## Requirements

- Reproduce the cross-verdict backfill bug with a focused regression test.
- Preserve final AWF-prefixed verdict precedence.
- Preserve same-verdict reason reuse when the final AWF line omits a reason.
- Do not run broad AWF/GitHub validation; use targeted parser tests only.

## Implementation Steps

1. Add focused parser tests for final empty AWF verdict behavior.
2. Change `_parse_verdict_result` so missing reasons only backfill from the
   same canonical verdict and never return a different verdict label.
3. Run the targeted parser test selection.
4. Record validation evidence in the matching validation document.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::TestParseVerdict -q`
