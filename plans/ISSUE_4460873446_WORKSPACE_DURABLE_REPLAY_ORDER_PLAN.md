# Issue 4460873446 Workspace Durable Replay Order Plan

## Problem Statement And Scope

Review-level comment `issue:4460873446` reports that `POST /v1/workspaces`
and `POST /v2/workspaces` run the non-consuming request-admission check before
the durable idempotency replay lookup for replay-key-cache misses. When the
identity has exhausted its workspace-create rate limit and the process-local
replay-key cache is cold, a genuine persisted idempotency replay can return
`429` instead of the original `202`, violating the API idempotency contract.

Scope is limited to workspace create v1/v2 ordering, focused API regressions,
and this plan/validation record.

## Requirements Checklist

- [ ] Cold-cache persisted workspace create replays must return the original
  accepted response even when the request-admission bucket is already exhausted.
- [ ] The behavior must apply to both `POST /v1/workspaces` and
  `POST /v2/workspaces`.
- [ ] Fresh unknown idempotency keys must still be rate limited when over quota
  and must not create duplicate workspace rows.
- [ ] The route must avoid broad replay-key scans or pre-lock hash probes on
  over-limit fresh keys.
- [ ] Existing warm-cache replay, conflict, replay-unavailable, rate-limit
  payload, and v2 disk-admission behavior must remain intact.

## Implementation Steps

1. Update focused workspace API tests so cold-cache persisted replays over
   quota expect `202` and the same workspace ID for both v1 and v2.
2. Update the fresh-key rate-limit regression to preserve the 429/no-create
   contract while allowing the exact durable lock+lookup that is required to
   distinguish fresh keys from cold persisted replays.
3. Run the focused updated tests before implementation and record the expected
   failure against the current ordering.
4. Reorder `create_workspace()` and `create_workspace_v2()` so replay-key-cache
   misses perform the durable replay lookup before
   `_check_workspace_create_request_admission()`.
5. Run focused tests, the workspace API module, lint/type checks for touched
   files, and a whitespace check.
6. Record requirement-by-requirement validation in
   `plans/ISSUE_4460873446_WORKSPACE_DURABLE_REPLAY_ORDER_VALIDATION.md`.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_cold_cache_persisted_idempotency_replay_bypasses_exhausted_rate_limit tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limit_rejects_fresh_idempotency_key_after_exact_durable_replay_miss tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limited_duplicate_unknown_key_uses_durable_replay_when_cache_misses -q
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspaceV2DiskPressure::test_v2_create_rate_limit_rejects_before_disk_admission tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_v1_idempotency_replay_bypasses_limit_but_fresh_keys_are_bounded tests/unit/api/test_workspaces.py::TestCreateWorkspaceV2DiskPressure::test_v2_idempotency_replay_bypasses_limit_but_fresh_keys_are_bounded -q
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py -q
uv run --python 3.12 --extra dev ruff check src/awf/api/routes/workspaces.py tests/unit/api/test_workspaces.py
uv run --python 3.12 --extra dev mypy src/awf/api/routes/workspaces.py
git diff --check
```

Pass criteria: the focused updated tests fail before implementation and pass
after; fresh over-limit keys still return structured `429`; cold persisted
replays return the original accepted response; relevant lint/type/whitespace
checks pass.
