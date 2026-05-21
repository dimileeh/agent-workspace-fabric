# Review PRRT_kwDOSJAM6s6Dttlr Expiring Session Validation

Plan reference: `REVIEW_PRRT_kwDOSJAM6s6Dttlr_EXPIRING_SESSION_PLAN.md`

## Requirement Status

- Add a regression that exercises the post-commit redispatch fallthrough using an expiring async session factory: Complete.
  - Added `test_non_running_active_validation_fallthrough_refreshes_expiring_session`.
  - Confirmed it failed before the fix with `sqlalchemy.exc.MissingGreenlet` at `workspace.execution_claim_expires_at`.
- Fix the fallthrough so the loaded workspace is safe to read before computing the preservation event floor: Complete.
  - `ControlWorker._recover_preserved_active_execution` now refreshes `ws` after a committed validation rewind when redispatch cannot continue.
- Preserve existing behavior for the normal AWF `make_session_factory(..., expire_on_commit=False)` path: Complete.
  - The refresh is gated to the committed non-running rewind fallthrough only.
- Run the narrow regression and relevant lint/type checks if practical: Complete.
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "expiring_session"`
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "expiring_session or non_running_candidate_redispatches_active_validation_recovery_rewinds_to_running"`
  - `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - `uv run --python 3.12 --extra dev mypy src/awf`
- Commit only files changed for this review thread: Complete.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/REVIEW_PRRT_kwDOSJAM6s6Dttlr_EXPIRING_SESSION_PLAN.md`
- `plans/REVIEW_PRRT_kwDOSJAM6s6Dttlr_EXPIRING_SESSION_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "expiring_session"`: passed after failing before the fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "expiring_session or non_running_candidate_redispatches_active_validation_recovery_rewinds_to_running"`: passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`: passed.
- `uv run --python 3.12 --extra dev mypy src/awf`: passed.

## Remaining Gaps

None.
