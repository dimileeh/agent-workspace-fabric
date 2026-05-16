# PRRT_kwDOSJAM6s6Cg5oa Workspace Fresh Replay Gate Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6Cg5oa_PLAN.md`

## Requirement Status

- Complete: Preserve cached idempotency replay behavior.
  - Evidence: existing replay-key cache paths in
    `src/awf/api/routes/workspaces.py` still return before the limiter for
    known cache hits and cache-conflict durable replays.
  - Test evidence: `tests/unit/api/test_workspaces.py` passed.

- Complete: Gate unknown fresh idempotency keys through request admission before
  durable workspace idempotency lock or lookup.
  - Evidence:
    `create_workspace()` and `create_workspace_v2()` now call
    `_check_workspace_create_request_admission()` before durable replay for
    unknown replay-key-cache misses.
  - Test evidence:
    `test_rate_limit_rejects_fresh_idempotency_key_before_durable_replay_miss`
    and
    `test_unknown_cold_idempotency_key_is_rate_limited_before_durable_replay`.
  - Red check: the focused tests failed before implementation because the
    rejected requests still acquired the durable replay lock and returned cold
    replay responses.

- Complete: Keep allowed unknown idempotency keys protected by durable replay
  lock/lookup before row creation.
  - Evidence: allowed unknown keys pass a non-consuming limiter check, run the
    existing durable replay helper, then consume admission only after a durable
    miss and before create.
  - Test evidence: first accepted requests in the updated regressions still
    record the durable lock and lookup before create.

- Complete: Cover the non-consuming admission check helper directly.
  - Evidence: `RequestAdmissionLimiter.check()` and
    `check_request_async()` are covered by
    `tests/unit/api/test_deps.py::test_check_request_async_uses_worker_thread_without_consuming_quota`.
  - Test evidence: the helper-level assertion proves a check runs through
    `asyncio.to_thread()` and does not consume the only available quota slot.

- Complete: Apply the same ordering to v1 and v2 workspace create routes.
  - Evidence: both route handlers use the same cache-hit, non-consuming check,
    durable replay, then admission-consumption ordering.
  - Test evidence: updated regressions are parameterized over
    `/v1/workspaces` and `/v2/workspaces`.

- Complete: Keep v2 rate-limit rejection before disk admission and row
  creation.
  - Evidence:
    `TestCreateWorkspaceV2DiskPressure::test_v2_create_rate_limit_rejects_before_disk_admission`
    remains covered by the full workspace test module.

- Complete: Update tests without weakening unrelated response-shape assertions.
  - Evidence: rate-limit assertions still use `_assert_workspace_rate_limited`,
    and the full workspace API module passed after the route change.

## Commands Run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limit_rejects_fresh_idempotency_key_before_durable_replay_miss tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_unknown_cold_idempotency_key_is_rate_limited_before_durable_replay tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limited_duplicate_unknown_key_does_not_probe_when_cache_misses -q
```

Before implementation: failed, `6 failed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limit_rejects_fresh_idempotency_key_before_durable_replay_miss tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_unknown_cold_idempotency_key_is_rate_limited_before_durable_replay tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limited_duplicate_unknown_key_does_not_probe_when_cache_misses tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_cold_idempotency_replay_with_remaining_quota_does_not_spend_fresh_slot -q
```

After implementation: passed, `8 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py -q
```

Result: passed, `145 passed in 75.07s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py -q
```

Result: passed, `36 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/api/request_admission.py src/awf/api/routes/workspaces.py tests/unit/api/test_workspaces.py
```

Result: passed.

```bash
uv run --python 3.12 --extra dev mypy src/awf/api/request_admission.py src/awf/api/routes/workspaces.py
```

Result: passed.

```bash
git diff --check
```

Result: passed.

## Gaps

None.
