# PRRT_kwDOSJAM6s6Cg5oa Workspace Fresh Replay Gate Plan

## Problem Statement And Scope

The workspace create routes currently run the durable idempotency replay lookup
for every unknown `Idempotency-Key` before request admission. Fresh keys miss
that replay path, so an over-limit client can still force one workspace
idempotency advisory lock and database lookup per rejected request.

Scope is limited to `POST /v1/workspaces` and `POST /v2/workspaces` route
ordering, focused workspace API tests, and this plan/validation record.

## Requirements Checklist

- [ ] Preserve cached idempotency replay behavior: keys already known in the
  route replay-key cache may replay before the limiter and may return 202 or
  replay-unavailable conflict.
- [ ] Gate unknown fresh idempotency keys through request admission before any
  durable workspace idempotency lock or lookup.
- [ ] Keep allowed unknown idempotency keys protected by the durable replay
  lock/lookup before row creation so duplicate key creation stays serialized.
- [ ] Cover the non-consuming admission check helper directly so the route does
  not depend on untested limiter behavior.
- [ ] Apply the same ordering to v1 and v2 workspace create routes.
- [ ] Keep v2 rate-limit rejection before disk admission and row creation.
- [ ] Update tests without weakening unrelated idempotency conflict,
  replay-cache, or response-shape assertions.

## Implementation Steps

1. Update the existing fresh-key rate-limit regression in
   `tests/unit/api/test_workspaces.py` so the rejected unknown key must not call
   `WorkspaceRepository.acquire_idempotency_key_lock()` or
   `get_by_idempotency_key()`.
2. Update cold-cache over-limit expectations so unknown keys that are not in the
   replay-key cache are rate-limited before durable replay.
3. Run the focused workspace tests to confirm the current implementation fails.
4. Reorder `create_workspace()` and `create_workspace_v2()`:
   - check the in-memory replay-key cache first;
   - run durable replay before admission only for known/conflicting cache keys;
   - run a non-consuming request admission check before durable replay for
     unknown keys, returning 429 immediately when quota is already exhausted;
   - run durable replay for allowed unknown keys before consuming quota, so a
     cold persisted replay does not spend a fresh-create slot;
   - consume request admission only after a durable replay miss and before
     creating new workspace rows.
5. Run the focused workspace tests, lint/type checks for touched modules, and a
   whitespace check.
6. Record requirement-by-requirement validation in
   `plans/PRRT_kwDOSJAM6s6Cg5oa_VALIDATION.md`.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limit_rejects_fresh_idempotency_key_before_durable_replay_miss tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_unknown_cold_idempotency_key_is_rate_limited_before_durable_replay tests/unit/api/test_workspaces.py::TestCreateWorkspaceV2DiskPressure::test_v2_create_rate_limit_rejects_before_disk_admission -q
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py -q
uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py -q
uv run --python 3.12 --extra dev ruff check src/awf/api/request_admission.py src/awf/api/routes/workspaces.py tests/unit/api/test_workspaces.py tests/unit/api/test_deps.py
uv run --python 3.12 --extra dev mypy src/awf/api/request_admission.py src/awf/api/routes/workspaces.py
git diff --check
```

Pass criteria: the new focused tests fail before implementation and pass after;
the workspace API module remains green; lint, type checking, and whitespace
checks pass.
