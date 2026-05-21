# PRRT_kwDOSJAM6s6DsRgn Operator-Required Cancellation Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DsRgn_OPERATOR_REQUIRED_CANCEL_PLAN.md`

## Requirement Status

- Complete: A running preserved-active workspace with a stale pending validate
  operation now cancels that operation when operator recovery is recorded.
- Complete: Existing non-running cancellation behavior remains covered by the
  same parametrized regression test.
- Complete: Operator-required refresh operation payloads and emitted events
  include `cancelled_active_operations` for the cancelled stale operation.
- Complete: Fresh execution claims still prevent salvage writers from mutating
  the workspace.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/PRRT_kwDOSJAM6s6DsRgn_OPERATOR_REQUIRED_CANCEL_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DsRgn_OPERATOR_REQUIRED_CANCEL_VALIDATION.md`

TDD failure before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_operator_required_cancels_superseded_active_operation'`
- Result: failed for
  `test_preserved_active_operator_required_cancels_superseded_active_operation[running-validate-pending]`
  because the original validate operation remained `pending`.

Verification after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_operator_required_cancels_superseded_active_operation or preserved_active_salvage_writers_recheck_fresh_execution_claim'`
- Result: `13 passed, 258 deselected`

- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
- Result: `All checks passed!`

- `uv run --python 3.12 --extra dev ruff format --check src/awf/control/worker.py tests/unit/control/test_worker.py`
- Result: `2 files already formatted`

## Gaps

None.
