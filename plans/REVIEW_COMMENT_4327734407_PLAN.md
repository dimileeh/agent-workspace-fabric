# Review Comment 4327734407 Plan

## Problem Statement And Scope

Address the unresolved review-level feedback for PR comment `4327734407` in the worker restart preservation/salvage flow. Local inspection shows some inline items are already addressed; this plan covers the still-valid safety gaps in `src/awf/control/worker.py` and the associated regressions in `tests/unit/control/test_worker.py`.

## Requirements Checklist

- Preserve duplicate-PR protection by treating branch PR resolver failures as operator-required recovery, not as permission to validate committed work or create replacement workspaces.
- Recheck `_execution_claim_is_stale(...)` after each mutating preserved-active salvage helper acquires the workspace row lock and before it clears claims, transitions status, writes replacement/monitor/validation state, or records salvage state.
- Keep already-addressed inline fixes intact: widened restart recovery statuses, lookup failure payload assertions, and deterministic replacement idempotency checks.
- Replace the salvage serialization test's `0.2s` timeout probe with event-based synchronization around the second lock attempt/progression.
- Add or update focused regression tests for the changed behavior.

## Implementation Steps

1. Update the PR lookup failure branch in `_recover_preserved_active_execution` so any failed branch lookup records operator-required recovery with the failure payload before local worktree salvage decisions.
2. Add post-lock stale execution claim guards to preserved-active salvage mutation helpers.
3. Update the lookup-failure regression to expect operator-required recovery and no validation operation.
4. Add a regression proving a refreshed execution claim suppresses a salvage mutation after the row lock is acquired.
5. Refactor the concurrent salvage-not-possible serialization test to synchronize on second lock attempt and post-lock progress events instead of a wall-clock timeout.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
- If the narrow run is too slow or blocked, run the specific affected tests and document the broader gap in validation.
- Pass criteria: affected regressions pass and no modified behavior allows resolver failures or refreshed claims to continue into mutating salvage side effects.
