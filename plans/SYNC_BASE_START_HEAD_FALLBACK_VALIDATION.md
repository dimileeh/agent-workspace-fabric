# Sync-Base Start Head Fallback Validation

Plan reference: `plans/SYNC_BASE_START_HEAD_FALLBACK_PLAN.md`

## Requirement Status

- `sync_base` always calls `_repair_operation_start_head_result`: Complete.
- `pr_head_sha` is passed only as `fallback_head_sha`: Complete.
- A helper failure result still short-circuits the sync-base operation:
  Complete.
- The verified helper head is threaded to the eventual validated push:
  Complete.

## Evidence

Changed files:

- `src/awf/runtime/pr_monitor_runner/remote_ops.py`
- `tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py`

Focused checks:

- Confirmed the new regression failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py::test_run_sync_base_uses_pr_head_sha_only_as_start_head_fallback -q`
- Confirmed the focused sync-base regression file passes:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py -q`

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, timeouts, and merge gating after completion.
