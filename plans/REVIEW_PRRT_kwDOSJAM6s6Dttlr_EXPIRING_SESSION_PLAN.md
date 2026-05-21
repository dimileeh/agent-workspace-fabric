# Review PRRT_kwDOSJAM6s6Dttlr Expiring Session Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6Dttlr` reports that preserved active validation recovery can commit a rewind from a non-running active status, then fall through after redispatch becomes unavailable while the same async session is still open. If the provided session factory expires objects on commit, subsequent `Workspace` attribute access can trigger implicit async lazy loading.

Scope is limited to `ControlWorker._recover_preserved_active_execution` and a focused regression for this fallthrough. No branch changes, pushes, broad refactors, or PR comment writes.

## Requirements Checklist

- Add a regression that exercises the post-commit redispatch fallthrough using an expiring async session factory.
- Fix the fallthrough so the loaded workspace is safe to read before computing the preservation event floor.
- Preserve existing behavior for the normal AWF `make_session_factory(..., expire_on_commit=False)` path.
- Run the narrow regression and relevant lint/type checks if practical.
- Commit only files changed for this review thread.

## Implementation Steps

1. Add a worker test that sets up a non-running active workspace with an active validation recovery operation, then makes redispatch unavailable after the rewind commit.
2. Confirm the new regression fails before the production fix when practical.
3. Refresh the workspace after the post-commit redispatch check fails and before fallthrough attribute access.
4. Re-run the new regression and a narrow existing worker slice.
5. Write validation evidence to `plans/REVIEW_PRRT_kwDOSJAM6s6Dttlr_EXPIRING_SESSION_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "expiring_session or non_running_candidate_redispatches_active_validation_recovery_rewinds_to_running"`
  - Passes with the new regression and existing nearby redispatch coverage.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passes with no lint errors in touched files.
