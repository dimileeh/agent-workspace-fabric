# PRRT_kwDOSJAM6s6KOoX7 Plan

## Problem Statement

The satisfied post-validation conformance report cleanup can fall back to
`unlink()` after `git restore` from `base_commit` or `HEAD` fails or leaves the
report path dirty. The current fallback returns success without verifying the
report path is clean, so a staged report-path residue can survive into later
pre-push dirty-worktree validation.

## Scope

- Limit changes to `src/awf/control/executor/planning_conformance.py` and a
  focused unit regression for the cleanup fallback.
- Do not broaden validation beyond focused tests for this behavior.
- Preserve existing best-effort handling for an `unlink()` OSError.

## Requirements Checklist

- Add a regression test where `git restore` leaves the report path staged dirty,
  `unlink()` removes the worktree file, and the post-unlink status still reports
  the path dirty.
- After an `unlink()` fallback, verify the report path is no longer dirty before
  returning success.
- If cleanup still leaves the report path dirty, return an explicit
  post-validation conformance failure rather than silently succeeding.
- Keep tracked-report restore success and non-fatal unlink OSError behavior
  unchanged.

## Implementation Steps

1. Add the focused regression test beside the existing conformance report
   restore/unlink tests.
2. Run that single test to confirm it fails against the current implementation.
3. Update the cleanup fallback to check `_report_path_is_dirty()` after
   `unlink()` succeeds and return a `_PlanningRunFailure` when dirty residue
   remains.
4. Run focused tests covering the new regression and nearby cleanup behavior.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_002.py -q -k "satisfied_post_validation_conformance_report"`

Full AWF/GitHub validation is managed by AWF after agent completion.
