# PRRT_kwDOSJAM6s6HAQQp Plan

## Problem Statement And Scope

The PR monitor's completed-workspace compose teardown currently constructs a
`ComposeManager` with a compose template path derived from `Path(__file__)`
parents. That path is fragile once AWF is installed as a package, and teardown
does not render compose templates.

Scope is limited to the monitor-side completed-workspace compose teardown helper
and its focused unit tests.

## Requirements Checklist

- Verify the review comment against the local code before changing behavior.
- Remove the installed-package-fragile project-root template inference from the
  completed-workspace teardown path.
- Keep teardown behavior unchanged: use candidate compose metadata when present,
  fallback monitor compose metadata otherwise, and remove volumes.
- Add a focused regression test proving the teardown manager receives an explicit
  teardown-only sentinel template path instead of the repo template path.
- Run only targeted validation for the changed unit surface; AWF/GitHub owns
  broad validation after agent completion.

## Implementation Steps

1. Inspect `src/awf/runtime/pr_monitor_runner/lifecycle.py` and existing
   completion-GC tests.
2. Add the focused failing test in
   `tests/unit/runtime/test_monitor_completion_gc.py`.
3. Replace `_default_completed_workspace_compose_template()` with a helper that
   returns an explicit teardown-only sentinel under the monitor work directory,
   with a comment documenting that the manager is never used for render here.
4. Run the targeted unit test module.
5. Record validation evidence in
   `plans/PRRT_kwDOSJAM6s6HAQQp_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py -q`

Pass criteria: the focused test module passes, and no full AWF/GitHub validation
suite is run during the agent phase.
