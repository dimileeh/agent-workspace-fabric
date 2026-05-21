# PRRT_kwDOSJAM6s6DjqFg Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6DjqFg_PLAN.md`

## Requirement Status

- Complete: Added a regression test for rewound validation salvage followed by another saturated `running` scan. The test covers both `validating` and `pushing` source statuses.
- Complete: Preserved the existing stale-failure behavior for validation salvage without an available executor or execution slot after grace expires.
- Complete: Updated worker recovery to recognize validation-salvage markers across `validating`/`pushing` to `running` rewinds and avoid duplicate preservation or validation-salvage side effects while the validation operation is still pending.
- Complete: Kept the code change scoped to `src/awf/control/worker.py` and focused test coverage in `tests/unit/control/test_worker.py`.

## Evidence

- Changed files:
  - `src/awf/control/worker.py`
  - `tests/unit/control/test_worker.py`
  - `plans/PRRT_kwDOSJAM6s6DjqFg_PLAN.md`
  - `plans/PRRT_kwDOSJAM6s6DjqFg_VALIDATION.md`
- Confirmed the updated regression failed before the worker fix:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k rewound_validation_salvage`
  - Failure showed two `workspace.active_execution_salvage_validation_requested` events after the second saturated scan.
- Verification after fix:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k rewound_validation_salvage`
  - Result: 2 passed, 233 deselected.
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "rewound_validation_salvage or preserved_active_validation_slot_exhaustion or validation_salvage_without_executor"`
  - Result: 4 passed, 231 deselected.
  - `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Result: all checks passed.

## Remaining Gaps

None.
