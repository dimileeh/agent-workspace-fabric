# PRRT_kwDOSJAM6s6LcL-G Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6LcL-G_PLAN.md`

## Requirement Status

- Complete: The preserve re-stamp in `_clear_stale_merge_attention` now persists
  the re-stamped `merge_block_attention` marker to the DB row before returning,
  so a cancel/restart during the subsequent non-human gate wait cannot strand
  the old marker timestamp.
- Complete: The durable persist touches ONLY the marker key (merged onto the
  DB-persisted `monitor_threads_addressed`), never flushing the whole in-memory
  `MonitorState` — mirrors the established single-key durable persist pattern
  (`_persist_forge_transient_retry_count` / `_clear_preserved_head_marker_durably`).
- Complete: Stale (clear) branch, no-marker case, and TTL-disabled (`<= 0`) case
  are unchanged.
- Complete: Added a focused regression test that asserts the marker is durable on
  the persisted row WITHOUT any `_persist_state` flush.
- Complete: Ran only targeted validation; broad AWF/GitHub validation remains
  managed by AWF after agent completion.

## Evidence

- Changed `src/awf/runtime/pr_monitor_runner/lifecycle.py`:
  - imported `_MERGE_BLOCK_ATTENTION_STATE_KEY`,
  - added `_persist_merge_block_attention_durably` helper (single-key durable
    persist, no-op when the row is gone or the in-memory marker is absent),
  - wired it into the preserve branch of `_clear_stale_merge_attention` right
    after `state.mark_merge_block_attention(now=reference)`.
- Wired the new helper onto the runner via
  `src/awf/runtime/pr_monitor_runner/mixins.py`.
- Added `test_clear_stale_merge_attention_restamps_preserved_marker_durably` to
  `tests/unit/runtime/test_merge_queue_ordering.py`, monkeypatching
  `_persist_state` to raise so the durability cannot rely on the outer flush.

- Confirmed the new regression test fails before the production change
  (temporarily reverted the `await self._persist_merge_block_attention_durably`
  call; the test asserted `persisted is not None` and got `None`):
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py::test_clear_stale_merge_attention_restamps_preserved_marker_durably -q`
    → `1 failed`
- Confirmed targeted checks pass after the fix:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py::test_clear_stale_merge_attention_restamps_preserved_marker_durably -q`
    → `1 passed`
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py -q`
    → `19 passed`
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_state.py tests/unit/runtime/test_pr_monitor_no_caps.py -q`
    → `38 passed`
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_failures.py -q`
    → `20 passed`
  - `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/lifecycle.py src/awf/runtime/pr_monitor_runner/mixins.py tests/unit/runtime/test_merge_queue_ordering.py`
    → `All checks passed!`
  - `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/lifecycle.py src/awf/runtime/pr_monitor_runner/mixins.py`
    → `Success: no issues found in 2 source files`
