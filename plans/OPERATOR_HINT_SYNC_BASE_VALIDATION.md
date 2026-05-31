# Operator Hint SyncBase Priority Validation

Plan reference: `plans/OPERATOR_HINT_SYNC_BASE_PLAN.md`

## Requirement Status

- Complete: Pending operator hints do not prevent `SyncBase` when
  `base_behind_count > 0`.
- Complete: Pending operator hints do not prevent `SyncBase` when GitHub reports
  `mergeStateStatus == BEHIND`.
- Complete: Pending operator hints do not prevent `SyncBase` when GitHub reports
  `mergeStateStatus == DIRTY`.
- Complete: Existing terminal-state behavior remains first; the terminal checks
  still precede comment pre-computation, sync, and operator-hint gates.
- Complete: Operator hints still run before merge once the PR is not behind /
  dirty, covered by the existing operator-hint merge-blocking tests.
- Complete: Decision-order documentation now places operator hints after
  SyncBase and before ordinary comment repair.

## Evidence

Changed files:

- `src/awf/runtime/pr_monitor.py`
- `tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_001.py`
- `plans/OPERATOR_HINT_SYNC_BASE_PLAN.md`
- `plans/OPERATOR_HINT_SYNC_BASE_VALIDATION.md`

Commands run:

- Failing-first regression before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_001.py::TestOperatorHints::test_pending_operator_hint_does_not_block_sync_base_for_stale_pr -q`
  failed with `AddressOperatorHint` returned instead of `SyncBase`.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_001.py::TestOperatorHints::test_pending_operator_hint_does_not_block_sync_base_for_stale_pr -q`
  passed (`3 passed`).
- Focused decision suite:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_001.py -q`
  passed (`96 passed`).
- Targeted lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor.py tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_001.py`
  passed.

Full AWF/GitHub validation is managed by AWF after agent completion and was not
run locally for this thread fix.
