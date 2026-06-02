# PRRT_kwDOSJAM6s6GSEGf Requested Node Scope Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6GSEGf_REQUESTED_NODE_SCOPE_PLAN.md`

## Requirement Status

- Preserve requested scheduler node scoping for named workers: Complete.
- Allow stable `local` workers to see and claim legacy requested rows whose
  workspace node is null but active reservation node contains an older non-local
  hostname: Complete.
- Keep unreserved requested rows visible to the appropriate scheduler scope:
  Complete.
- Add regression tests for the local legacy fallback and named-node isolation:
  Complete.
- Run only focused tests for the changed scheduler behavior; full AWF/GitHub
  validation remains managed by AWF after agent completion: Complete.

## Evidence

Files changed:

- `src/awf/db/repositories/_scheduler.py`
- `tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py`
- `tests/unit/control/test_worker_parts/test_worker_part_003.py`
- `plans/PRRT_kwDOSJAM6s6GSEGf_REQUESTED_NODE_SCOPE_PLAN.md`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py -q`
  passed: `50 passed in 28.10s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_003.py -q -k "non_capacity_local_requested_claim_adopts_legacy_reservation_hostname or non_capacity_requested_claim_honors_reservation_node"`
  passed: `2 passed, 10 deselected in 2.62s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories/_scheduler.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py tests/unit/control/test_worker_parts/test_worker_part_003.py`
  passed.

Full repository validation, coverage gates, and CI-equivalent checks were not
run in the agent phase; AWF/GitHub own those broad validation gates after agent
completion.

## Gaps

None.
