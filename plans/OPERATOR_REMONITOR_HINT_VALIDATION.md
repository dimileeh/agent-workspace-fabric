# Operator Remonitor Hint Validation

Plan reference: `plans/OPERATOR_REMONITOR_HINT_PLAN.md`

## Requirement Status

- Complete: Persist a pending operator hint for `monitoring_pr` workspaces whenever a non-empty remonitor reason is provided.
- Complete: Keep the past-settle auto-merge freeze and `REMONITOR_PAST_SETTLE` warning conditional on elapsed settle state.
- Complete: Preserve existing remonitor claim reset, operation payload, event payload, and idempotency behavior.
- Complete: Add a regression test for a pre-settle remonitor reason proving the hint is persisted without freeze/warning state.
- Complete: Run only targeted checks for the changed behavior; full AWF/GitHub validation remains managed after agent completion.

## Evidence

Files changed:

- `src/awf/service/controls.py`
- `tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py`
- `plans/OPERATOR_REMONITOR_HINT_PLAN.md`
- `plans/OPERATOR_REMONITOR_HINT_VALIDATION.md`

Focused TDD evidence:

- Before implementation, `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k remonitor_before_settle_persists_operator_hint_without_freeze` failed with `KeyError: '__awf_pending_operator_hint__'`.

Focused checks after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k remonitor`
  - Passed: `7 passed, 24 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py`
  - Passed: `All checks passed!`.

No remaining gaps.
