# PRRT_kwDOSJAM6s6DUTJJ Step Order Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6DUTJJ_STEP_ORDER_PLAN.md`

## Requirement Status

- Add a regression test proving unchanged existing workflow steps cannot be
  reordered without a violation: Complete.
- Preserve matching by stable step identity for allowed existing-step changes
  such as pinned `uses:` ref bumps: Complete.
- Preserve the allowance for added informational steps when the relative order
  of pre-existing steps is unchanged: Complete.
- Report an operator-visible violation with file section, line, and reason when
  existing workflow step order changes: Complete.
- Run targeted quality-gate tests and static checks for the touched files:
  Complete.

## Evidence

- Changed `tests/unit/control/test_quality_gates.py` to add
  `test_workflow_existing_step_reorder_is_blocked` and
  `test_workflow_added_informational_step_preserves_existing_step_order`.
- Changed `src/awf/control/quality_gates.py` so matched existing workflow steps
  must appear in increasing order in the new workflow step list.
- Confirmed the new reorder regression failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_existing_step_reorder_is_blocked -q`
  failed with `1 failed` because no violation was emitted.
- Confirmed the focused tests passed after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_existing_step_reorder_is_blocked -q`
  passed with `1 passed`.
- Confirmed informational insertion remains allowed:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_added_informational_step_preserves_existing_step_order -q`
  passed with `1 passed`.
- Confirmed the quality-gate unit file passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passed with `47 passed`.
- Confirmed lint passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`.
- Confirmed the touched module type-checks:
  `uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py`.

## Gaps

None.
