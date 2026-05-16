# Issue 4460873446 Workspace Durable Replay Order Validation

Plan reference: `plans/ISSUE_4460873446_WORKSPACE_DURABLE_REPLAY_ORDER_PLAN.md`

## Requirement Status

- Complete: Cold-cache persisted workspace create replays return the original
  accepted response even when the request-admission bucket is exhausted.
  - Evidence:
    `test_cold_cache_persisted_idempotency_replay_bypasses_exhausted_rate_limit`
    clears the replay-key cache after the first create and verifies the replay
    returns `202` with the same workspace ID for v1 and v2.

- Complete: The behavior applies to both `POST /v1/workspaces` and
  `POST /v2/workspaces`.
  - Evidence: focused regressions are parameterized over both routes, and both
    handlers now perform durable replay lookup before the non-consuming
    admission check on replay-key-cache misses.

- Complete: Fresh unknown idempotency keys remain rate limited when over quota
  and do not create duplicate workspace rows.
  - Evidence:
    `test_rate_limit_rejects_fresh_idempotency_key_after_exact_durable_replay_miss`
    still asserts structured `429` for a second fresh key after an exact durable
    miss.

- Complete: The route avoids broad replay-key scans or pre-lock hash probes on
  over-limit fresh keys.
  - Evidence: the fresh-key regression still patches
    `has_idempotency_key()` and `list_idempotency_replay_keys()` to fail if
    called, and the focused suite passes.

- Complete: Existing warm-cache replay, conflict, replay-unavailable,
  rate-limit payload, and v2 disk-admission behavior remain intact.
  - Evidence: surrounding focused replay/rate-limit/disk tests and the full
    workspace API module pass.

## Commands Run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_cold_cache_persisted_idempotency_replay_bypasses_exhausted_rate_limit tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limit_rejects_fresh_idempotency_key_after_exact_durable_replay_miss tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limited_duplicate_unknown_key_uses_durable_replay_when_cache_misses -q
```

Before implementation: failed, `6 failed`, with cold-cache replays returning
`429` and over-limit fresh keys not performing the exact durable lookup.

After implementation: passed, `6 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspaceV2DiskPressure::test_v2_create_rate_limit_rejects_before_disk_admission tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_v1_idempotency_replay_bypasses_limit_but_fresh_keys_are_bounded tests/unit/api/test_workspaces.py::TestCreateWorkspaceV2DiskPressure::test_v2_idempotency_replay_bypasses_limit_but_fresh_keys_are_bounded -q
```

Result: passed, `3 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py -q
```

Result: passed, `145 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/api/routes/workspaces.py tests/unit/api/test_workspaces.py
uv run --python 3.12 --extra dev mypy src/awf/api/routes/workspaces.py
git diff --check
```

Result: all passed.

## Gaps

No remaining gaps.
