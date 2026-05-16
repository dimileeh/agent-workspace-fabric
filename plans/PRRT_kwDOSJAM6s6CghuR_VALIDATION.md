# PRRT_kwDOSJAM6s6CghuR Validation

Plan reference: `PRRT_kwDOSJAM6s6CghuR_PLAN.md`

## Requirement Status

- Add a failing regression test for the reported v2 replay shape: Complete.
  `tests/unit/api/test_workspaces.py` now covers `profile_ref: null` followed
  by an omitted `workspace` replay on a warm app cache. The test failed before
  the route fix with a 409 response.
- Ensure warm-cache v2 replays consult durable v2 matching before returning a
  cached raw-hash conflict: Complete. `src/awf/api/routes/workspaces.py` now
  falls through to `_workspace_create_v2_durable_replay_response` when the v2
  cache hash check raises.
- Preserve existing v2 conflict behavior for genuinely different payloads:
  Complete. The existing resource-conflict regression still returns
  `IDEMPOTENCY_CONFLICT`.
- Preserve no-create behavior when a known replay key has no durable row:
  Complete. The existing known-key durable-miss regression still returns
  `IDEMPOTENCY_REPLAY_UNAVAILABLE` without creating a workspace.
- Do not change branch, push, or weaken existing regression assertions:
  Complete. No branch or remote operations were performed.

## Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestWorkspaceCreateProviderReadinessPreflight::test_v2_warm_cache_replay_uses_durable_auto_profile_match -q`
  - Failed before implementation with `assert 409 == 202`.
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspaceV2ResourceIdempotency::test_defaulted_resource_create_conflicts_with_explicit_default_replay -q`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_known_replay_key_db_miss_returns_conflict_without_create tests/unit/api/test_workspaces.py::TestWorkspaceCreateProviderReadinessPreflight::test_direct_v2_replay_returns_existing_row_and_conflict_response tests/unit/api/test_workspaces.py::TestWorkspaceCreateProviderReadinessPreflight::test_v2_warm_cache_replay_uses_durable_auto_profile_match tests/unit/api/test_workspaces.py::TestCreateWorkspaceV2ResourceIdempotency::test_defaulted_resource_create_conflicts_with_explicit_default_replay -q`
  - Passed: 5 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/workspaces.py tests/unit/api/test_workspaces.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/api/routes/workspaces.py`
  - Passed.
