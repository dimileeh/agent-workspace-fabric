# Review Issue 4620252998 Compose Teardown Context Plan

## Problem Statement And Scope

Address the review-level comment on PR #396 about monitor-side completed-workspace
GC compose teardown behavior.

The actionable behavior gap is that `_completed_workspace_compose_teardown`
currently returns `None` when `compose_file` is unavailable, even though
`ComposeManager.teardown_project` can use the known compose project name and a
missing compose-file path to perform its label-scoped volume reap fallback.

Issue 1 is documentation scope: the preserved-workspace fallback in
`run_workspace_filesystem_gc` is intentionally outside the production monitor
path because the monitor passes `ignore_retention=True`.

Issue 3 appears already covered by the existing tracker re-raise contract, so
the implementation should not add duplicate behavior unless verification finds a
real missing `result.compose_teardowns` entry.

## Requirements Checklist

- Add a focused regression test proving a known monitor compose project still
  builds and runs a teardown callback when `compose_file=None`.
- Keep label-fallback behavior compatible with `ComposeManager.teardown_project`
  by passing a deterministic missing compose-file path when no persisted or
  monitor compose file is available.
- Document that the preserved-workspace fallback is for non-monitor or future
  callers because production monitor GC bypasses retention with
  `ignore_retention=True`.
- Preserve existing behavior for callers with no compose project context.
- Do not change protected workflow, quality-gate, or broad validation files.
- Run only focused tests or checks for the changed files; full AWF/GitHub
  validation remains post-agent owned.

## Implementation Steps

1. Add the failing monitor regression test in
   `tests/unit/runtime/test_monitor_completion_gc.py`.
2. Confirm the new regression fails against the current guard.
3. Update `src/awf/runtime/pr_monitor_runner/lifecycle.py` so the completed
   workspace teardown callback is built when `compose_project` is known even if
   `compose_file` is missing, using the candidate compose metadata or default
   compose path as the compose-file argument.
4. Add a narrow explanatory comment near the preserved fallback branch in
   `src/awf/service/gc.py`.
5. Run the focused runtime test file or selected tests that cover the changed
   behavior.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py -q`
  should pass.
- If the focused file is too slow or blocked, run the specific new test and
  document the reason in validation.
- No broad AWF/GitHub-owned validation suite will be run in the agent phase.
