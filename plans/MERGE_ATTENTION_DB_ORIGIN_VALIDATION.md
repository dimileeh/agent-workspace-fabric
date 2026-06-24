# Merge Attention DB Origin Validation

Plan reference: `plans/MERGE_ATTENTION_DB_ORIGIN_PLAN.md`

## Requirement Status

- Reproduce the reported state-loss path with a focused regression test: Complete.
  - Updated `tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py::test_queue_wait_preserves_persisted_merge_rejection_origin_after_restart` to call `_persist_state()` after the queue-wait preserve helper.
  - Confirmed the regression failed before the implementation change with `KeyError: '__awf_merge_block_attention_origin__'`.
- Preserve existing behavior for explicit in-memory non-rejection origins: Complete.
  - Existing focused persistence tests still cover explicit non-rejection origin precedence.
- Copy DB-derived merge-rejection origin into in-memory state before returning from the queue-wait preserve branch: Complete.
  - `src/awf/runtime/pr_monitor_runner/merge_attention.py` now copies the structured DB origin into `MonitorState` when `_merge_block_attention_originated_from_merge_rejection()` recovers it from the workspace row.
- Avoid broad refactors or unrelated validation: Complete.
  - Changes are limited to the merge-attention helper, the focused regression test, and plan/validation documents.
- Commit the local fix with a conventional commit message tied to the thread id: Complete.

## Evidence

- Focused failing regression before implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py::test_queue_wait_preserves_persisted_merge_rejection_origin_after_restart -q`
  - Result before fix: failed with missing in-memory `__awf_merge_block_attention_origin__`.
- Focused passing regression after implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py::test_queue_wait_preserves_persisted_merge_rejection_origin_after_restart -q`
  - Result: passed.
- Focused persistence suite:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py -q`
  - Result: `15 passed`.
- Focused lint:
  - `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_attention.py tests/unit/runtime/test_pr_monitor_merge_attention_persistence.py`
  - Result: passed.

Full AWF/GitHub validation is managed by AWF after agent completion and was intentionally not run in this workspace repair phase.
