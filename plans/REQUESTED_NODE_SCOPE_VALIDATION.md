# Requested Node Scope Validation

Plan reference: `plans/REQUESTED_NODE_SCOPE_PLAN.md`

## Requirement Status

- Add a regression test proving a non-capacity worker on node B does not claim a requested workspace whose active reservation is for node A: Complete.
- Keep unreserved requested workspaces dispatchable for named non-capacity workers: Complete.
- Reuse existing scheduler node-scope semantics for requested rows: Complete.
- Guard the final id-specific requested transition so stale or incorrectly supplied ids cannot bypass node scope: Complete.
- Run only focused validation; broad AWF/GitHub validation remains managed after agent completion: Complete.

## Evidence

Files changed:

- `src/awf/control/worker/manager.py`
- `src/awf/control/worker/scheduler_methods.py`
- `src/awf/control/worker/claims.py`
- `tests/unit/control/test_worker_parts/test_worker_part_003.py`
- `plans/REQUESTED_NODE_SCOPE_PLAN.md`
- `plans/REQUESTED_NODE_SCOPE_VALIDATION.md`

Focused checks:

- Pre-fix: `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_003.py -q -k "non_capacity_requested"` failed with both new regression tests reproducing the unscoped listing and id-specific claim.
- Post-fix: `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_003.py -q -k "non_capacity_requested"` passed with `2 passed, 9 deselected`.
- Post-fix: `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_003.py -q -k "list_requested_uses_non_capacity_limit_when_called_directly or non_capacity_requested"` passed with `3 passed, 8 deselected`.
- Post-fix: `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/manager.py src/awf/control/worker/scheduler_methods.py src/awf/control/worker/claims.py tests/unit/control/test_worker_parts/test_worker_part_003.py` passed.
- Post-fix: `git diff --check` passed.

Full AWF/GitHub validation was not run in this agent phase; AWF owns broad validation, provenance, logs, timeouts, and merge gating after completion.
