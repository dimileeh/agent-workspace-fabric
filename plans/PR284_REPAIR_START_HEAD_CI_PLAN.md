# PR284 Repair Start Head CI Plan

## Context

PR #284 failed full coverage after adding protected-scope transactional rollback.
The failure cluster shows existing PR monitor tests being diverted into
`REPAIR_START_HEAD_UNAVAILABLE` before reaching the behavior under test, plus
fake GitHub GraphQL responses being consumed by the newly inserted local
`git rev-parse HEAD` call.

## Root Cause

The transactional rollback baseline must be the local worktree HEAD at the
moment a repair starts, because rollback later performs a local hard reset. PR
#284 correctly added that guard, but existing fake command runner tests did not
model the new local `git rev-parse HEAD` command. Queued GitHub GraphQL and git
responses were therefore shifted into the wrong command, causing the full
coverage failures.

## Implementation

1. Keep local `git rev-parse HEAD` as the primary repair operation baseline
   whenever the worktree exists.
2. Use the PR status/open merge-candidate head only for no-worktree helper
   paths so direct tests do not fail before exercising unrelated behavior.
3. Update fake command queues to include the local repair-start HEAD where the
   worktree exists, and remove stale queues only for no-worktree helper paths.
4. Update explicit missing-start-head tests so they still cover the fallback
   failure path by clearing candidate/status head context.

## Validation

Run the focused failing monitor tests first, then narrow lint/type checks on
touched files:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py tests/unit/runtime/test_pr_monitor_runner.py tests/unit/runtime/test_monitor_action_logging.py tests/integration/runtime/test_pr_monitor_runner.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner.py`

Full coverage remains owned by GitHub CI after the fix is pushed.
