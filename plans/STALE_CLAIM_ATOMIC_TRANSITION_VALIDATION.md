# Stale Claim Atomic Transition Validation

Plan reference: `plans/STALE_CLAIM_ATOMIC_TRANSITION_PLAN.md`

## Requirement Status

- Add a regression test that proves a claim refresh between the stale check and
  failure transition prevents the stale failure: Complete.
- Recheck the stale execution claim with the status predicate in the atomic
  transition path: Complete.
- Preserve the existing stale failure event payload behavior when primary
  failure evidence exists: Complete.
- Keep changes scoped and avoid changing branch or PR workflow behavior:
  Complete.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `src/awf/db/repositories.py`
- `tests/unit/control/test_worker.py`
- `plans/STALE_CLAIM_ATOMIC_TRANSITION_PLAN.md`
- `plans/STALE_CLAIM_ATOMIC_TRANSITION_VALIDATION.md`

Regression evidence:

- Before implementation,
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k stale_active_execution_failure_transition_rechecks_refreshed_claim`
  failed because the old path called the unconditional transition after a
  refreshed claim.

Verification commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k stale_active_execution_failure`
  passed: `3 passed, 175 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py -q -k transition_if_current`
  passed: `2 passed, 68 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges.py -q -k fail_stale_active_execution`
  passed: `2 passed, 42 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/control/test_worker.py tests/unit/db/test_workspace_repository.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf` passed.

## Gaps

None.
