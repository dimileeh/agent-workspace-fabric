# PRRT_kwDOSJAM6s6GShQ6 Requested Local Fallback Validation

Plan reference:
`plans/ADDRESS_THREAD_PRRT_kwDOSJAM6s6GShQ6_PLAN.md`

## Requirement Status

- Add a regression proving `node_id="local"` does not list or claim a requested
  workspace reserved for a named node: Complete.
- Preserve existing behavior that `local` workers can adopt legacy local
  container-hostname reservations: Complete.
- Keep named worker scoping behavior unchanged: Complete.
- Run only focused checks for the changed files; AWF/GitHub own broad
  validation after agent completion: Complete.
- Commit the scoped fix locally with a conventional commit message for the
  review thread: Complete after local commit.

## Evidence

Files changed:

- `src/awf/db/repositories/_scheduler.py`
- `tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py`
- `tests/unit/control/test_worker_parts/test_worker_part_003.py`
- `plans/ADDRESS_THREAD_PRRT_kwDOSJAM6s6GShQ6_PLAN.md`

Red checks before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_requested_scheduler_local_scope_ignores_named_reservation_node -q`
  failed because the local scheduler listed the row reserved for `worker-node-a`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_003.py::TestRunOncePart003::test_non_capacity_local_requested_claim_ignores_named_reservation_node -q`
  failed because the local worker listed the row reserved for `worker-node-a`.

Focused checks after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_requested_scheduler_local_scope_ignores_named_reservation_node -q`
  passed: `1 passed in 1.49s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_003.py::TestRunOncePart003::test_non_capacity_local_requested_claim_ignores_named_reservation_node -q`
  passed: `1 passed in 1.61s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_requested_scheduler_local_scope_adopts_legacy_reservation_hostname tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_requested_scheduler_named_scope_ignores_legacy_reservation_hostname -q`
  passed: `2 passed in 2.31s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_003.py::TestRunOncePart003::test_non_capacity_local_requested_claim_adopts_legacy_reservation_hostname tests/unit/control/test_worker_parts/test_worker_part_003.py::TestRunOncePart003::test_non_capacity_requested_claim_honors_reservation_node -q`
  passed: `2 passed in 2.58s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories/_scheduler.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py tests/unit/control/test_worker_parts/test_worker_part_003.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/db/repositories/_scheduler.py`
  passed.
- `git diff --check`
  passed.

Full unit suites, coverage gates, frontend builds, and CI-equivalent checks were
not run in the agent phase; AWF/GitHub own broad validation after agent
completion.

## Gaps

None.
