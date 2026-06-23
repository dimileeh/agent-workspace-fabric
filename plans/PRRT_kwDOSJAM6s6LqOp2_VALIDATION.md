# PRRT_kwDOSJAM6s6LqOp2 Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6LqOp2_PLAN.md`

## Requirement Status

- Preserve the existing in-memory marker clear behavior in both callers:
  Complete. Both callers still call `state.clear_merge_block_attention()` before
  delegating to the shared row transaction.
- Preserve the existing single `get_for_update` transaction that removes the
  persisted merge-block marker and clears workspace attention:
  Complete. The common transaction now lives in
  `_clear_merge_block_attention_and_workspace_attention_row_durably`.
- Keep the missing-workspace no-op behavior:
  Complete. The shared transaction still returns immediately when
  `get_for_update` finds no workspace row.
- Avoid unrelated refactors or test rewrites:
  Complete. Only the targeted helper extraction and plan/validation docs changed.
- Run focused validation only:
  Complete. Full AWF/GitHub validation remains managed by AWF after agent
  completion.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/merge_attention.py`
- `plans/PRRT_kwDOSJAM6s6LqOp2_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6LqOp2_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_attention.py -q`
  - Result: `20 passed in 32.76s`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_attention.py`
  - Result: `All checks passed!`

No gaps remain.
