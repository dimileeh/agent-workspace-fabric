# Review PRRT_kwDOSJAM6s6HB0p Monotonic Heartbeat Validation

Plan reference: `plans/REVIEW_PRRT_kwDOSJAM6s6HB0p_MONOTONIC_HEARTBEAT_PLAN.md`

## Requirement Status

- Verify the current upsert can overwrite a newer heartbeat with an older one:
  Complete.
  - The new focused regression failed before implementation because the older
    second write changed the persisted `node_id` from `node-new` to `node-old`.
- Add focused regression coverage proving older conflicting writes do not
  regress `last_heartbeat_at`: Complete.
  - Added `test_record_heartbeat_preserves_newest_conflicting_write`.
- Preserve metadata from the heartbeat row that owns the greatest
  `last_heartbeat_at`: Complete.
  - The regression asserts the newer row keeps `node_id`,
    `poll_interval_seconds`, and `updated_at`.
- Keep `started_at` unchanged across conflict updates: Complete.
  - The regression writes an older conflicting `started_at` and asserts the
    original value remains persisted.
- Avoid broad validation: Complete.
  - Only focused heartbeat repository tests and narrow lint were run. Full
    AWF/GitHub validation is managed after agent completion.

## Evidence

Files changed:

- `src/awf/db/repositories/base.py`
- `tests/unit/db/test_worker_heartbeats.py`
- `plans/REVIEW_PRRT_kwDOSJAM6s6HB0p_MONOTONIC_HEARTBEAT_PLAN.md`
- `plans/REVIEW_PRRT_kwDOSJAM6s6HB0p_MONOTONIC_HEARTBEAT_VALIDATION.md`

Commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_worker_heartbeats.py::test_worker_heartbeat_upsert_supports_postgres_only tests/unit/db/test_worker_heartbeats.py::test_record_heartbeat_preserves_newest_conflicting_write -q`
  - Failed before implementation as expected.
  - Passed after implementation: `2 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_worker_heartbeats.py -q`
  - Passed: `4 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories/base.py tests/unit/db/test_worker_heartbeats.py`
  - Passed: `All checks passed!`
- `uv run --python 3.12 --extra dev mypy src/awf/db/repositories/base.py`
  - Passed: `Success: no issues found in 1 source file`

## Gaps

None.
