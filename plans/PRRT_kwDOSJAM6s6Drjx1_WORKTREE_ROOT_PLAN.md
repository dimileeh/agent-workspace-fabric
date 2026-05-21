# PRRT_kwDOSJAM6s6Drjx1 Worktree Root Plan

## Problem Statement And Scope

The PR review thread reports that an unavailable preserved active worktree root is classified as `ambiguous`, which records `ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED` immediately. That operator-required event blocks future salvage scans, so a transient `get_worktree_path(...) -> None` result cannot recover during the preservation grace period.

Scope is limited to preserved active execution salvage classification and regression tests for the unavailable worktree root path in `ControlWorker`.

## Requirements Checklist

- Classify `worktree_root_unavailable` as a retryable failed classification rather than an ambiguous operator-only classification.
- Preserve expired-grace behavior: failed classifications can still require operator recovery after grace expires.
- Add/update regression coverage showing an unavailable worktree root records `SALVAGE_BLOCKED` during grace and does not record operator-required.
- Keep changes scoped to the worker behavior, unit tests, and required plan/validation documents.

## Implementation Steps

1. Add or update unit tests first for the expected `worktree_root_unavailable` classification and during-grace salvage behavior.
2. Run the focused tests and confirm the new expectation fails against the current implementation when practical.
3. Change `_classify_preserved_active_worktree` so `worktree_root_unavailable` returns `state="failed"`.
4. Re-run the focused tests covering the changed behavior and adjacent preserved-active salvage cases.
5. Run narrow lint/type checks if the touched surface warrants it.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'worktree_root or unknown_worktree_root'`
  - Passes after implementation; the during-grace regression fails before the code change.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'git_status_failure or worktree_root or unknown_worktree_root'`
  - Passes, proving the retryable failed-classification path remains consistent with existing git failure behavior.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passes with no lint regressions.
