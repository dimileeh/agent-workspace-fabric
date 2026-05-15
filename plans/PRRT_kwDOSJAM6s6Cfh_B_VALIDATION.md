# PRRT_kwDOSJAM6s6Cfh_B Validation

Plan reference: `PRRT_kwDOSJAM6s6Cfh_B_PLAN.md`

## Requirement Status

- Complete: Durable idempotency replays for persisted workspace keys return
  before workspace-create quota admission.
- Complete: Fresh idempotency keys still pass through workspace-create rate
  admission before a workspace row is created.
- Complete: Durable replay lookup remains lock-scoped through
  `WorkspaceRepository.acquire_idempotency_key_lock()` and does not rely on a
  pre-lock existence probe.
- Complete: The ordering fix applies to both v1 and v2 workspace create paths.
- Complete: Regression coverage proves a cold replay with remaining quota does
  not spend the next fresh-create slot.

## Evidence

- Changed `src/awf/api/routes/workspaces.py` to run the locked durable replay
  check before `admit_request_async` for idempotency-key requests.
- Added
  `tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_cold_idempotency_replay_with_remaining_quota_does_not_spend_fresh_slot`.
- Confirmed the new regression failed before the implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_cold_idempotency_replay_with_remaining_quota_does_not_spend_fresh_slot -q`
  failed for both v1 and v2 with 429 on the subsequent fresh create.
- Verified after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_cold_idempotency_replay_with_remaining_quota_does_not_spend_fresh_slot -q`
  passed.
- Verified broader workspace API coverage:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py -q`
  passed with 142 tests.
- Verified lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/workspaces.py tests/unit/api/test_workspaces.py`
  passed.

## Remaining Gaps

None.
