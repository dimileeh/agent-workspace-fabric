# Review Issue 4587587225 Handoff Profile Contract Plan

## Problem Statement and Scope

Greptile's review-level PR comment identified an implicit contract in
`_build_handoff_pr_monitor`: callers that pass an already prepared `profile`
must also pass `run_profile_setup=False`, otherwise monitor handoff setup can be
run again for a profile that was already prepared by
`_prepare_handoff_pr_monitor_profile`.

Scope is limited to making that helper contract explicit, adding focused
coverage for the defensive behavior, and recording focused validation evidence.

## Requirements Checklist

- Make the `profile` and `run_profile_setup` relationship explicit at the
  `_build_handoff_pr_monitor` boundary.
- Prevent future callers from silently re-running profile setup when they pass
  a pre-resolved handoff profile.
- Preserve current feature-PR and release-PR handoff behavior.
- Avoid broad AWF/GitHub-owned validation; run only focused local checks.

## Implementation Steps

1. Add a focused unit test that calls `_build_handoff_pr_monitor` with both a
   pre-supplied `profile` and `run_profile_setup=True`, asserting the helper
   fails before invoking setup.
2. Add a docstring note and defensive guard in `_build_handoff_pr_monitor` so
   pre-supplied profiles require `run_profile_setup=False`.
3. Run the focused test before and after implementation, then run targeted
   lint for the changed Python files.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_handoff_monitor_rejects_prepared_profile_with_setup_enabled -q
uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py
```

Pass criteria: the focused unit test fails before the guard, passes after the
guard, and targeted lint passes. Full AWF/GitHub validation remains owned by AWF
after agent completion.
