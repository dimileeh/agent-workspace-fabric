# Failed Remonitor Operator Hint Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6F5wup_FAILED_REMONITOR_HINT_PLAN.md`

## Requirement Status

- Complete: Non-empty remonitor reasons are persisted for remonitor-eligible PR
  workspaces, including `failed`.
- Complete: Failed workspaces still reset back to `monitoring_pr`.
- Complete: Past-settle failed remonitor re-arms initial review grace and
  reviewer-settle state.
- Complete: The remonitor event includes the pending hint, and the warning is
  included in the event, operation result, and control response.
- Complete: Only focused local checks were run; full AWF/GitHub validation
  remains managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/service/controls.py`
- `tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py`
- `plans/PRRT_kwDOSJAM6s6F5wup_FAILED_REMONITOR_HINT_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F5wup_FAILED_REMONITOR_HINT_VALIDATION.md`

Focused TDD evidence:

- Before implementation, `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k failed_workspace_past_settle`
  failed with `KeyError: '__awf_pending_operator_hint__'`, proving failed
  remonitor skipped operator hint persistence.

Focused checks after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k failed_workspace_past_settle`
  - Passed: `1 passed, 31 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k remonitor`
  - Passed: `8 passed, 24 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py`
  - Passed: `All checks passed!`.

No remaining gaps.
