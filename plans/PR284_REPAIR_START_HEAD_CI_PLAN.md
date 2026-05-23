# PR284 Repair Start Head CI Plan

## Context

PR #284 failed full coverage after adding protected-scope transactional rollback.
The failure cluster shows existing PR monitor tests being diverted into
`REPAIR_START_HEAD_UNAVAILABLE` before reaching the behavior under test, plus
fake GitHub GraphQL responses being consumed by the newly inserted local
`git rev-parse HEAD` call.

## Root Cause

The monitor already has the authoritative PR head SHA at decision time, but the
implementation introduced an additional local `git rev-parse HEAD` call at the
start of every CI/comment repair. In the fake command runner tests this extra
command shifts queued responses and, when the fake returns default empty stdout,
causes a terminal repair-start failure. That is a test-harness regression and a
weaker product shape than using the PR status head the monitor already fetched.

## Implementation

1. Use the PR status head SHA as the repair operation start head for comment
   repair and CI repair whenever it is available.
2. Keep the local `git rev-parse HEAD` fallback only for direct helper calls that
   lack PR status/candidate head context.
3. Add a small DB fallback from the open merge candidate head for direct unit
   helper invocations.
4. Remove now-stale queued `operation start HEAD` fake command responses from
   protected rollback tests whose status already supplies the baseline.
5. Update explicit missing-start-head tests so they still cover the fallback
   failure path by clearing candidate/status head context.

## Validation

Run the focused failing monitor tests first, then narrow lint/type checks on
touched files:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py tests/unit/runtime/test_pr_monitor_runner.py tests/unit/runtime/test_monitor_action_logging.py tests/integration/runtime/test_pr_monitor_runner.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner.py`

Full coverage remains owned by GitHub CI after the fix is pushed.
