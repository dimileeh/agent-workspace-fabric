# PRRT_kwDOSJAM6s6DUTJJ Step Order Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6DUTJJ` reports that the diff-aware protected
workflow classifier matches existing steps by identity across any position, so
an unowned protected workflow can reorder unchanged existing steps without a
quality-gate violation. Reordering protected workflow steps can weaken gates by
moving deploy, publish, or release work before validation.

Scope is limited to workflow step matching in
`src/awf/control/quality_gates.py` and focused unit coverage in
`tests/unit/control/test_quality_gates.py`.

## Requirements Checklist

- Add a regression test proving unchanged existing workflow steps cannot be
  reordered without a violation.
- Preserve matching by stable step identity for allowed existing-step changes
  such as pinned `uses:` ref bumps.
- Preserve the allowance for added informational steps when the relative order
  of pre-existing steps is unchanged.
- Report an operator-visible violation with file section, line, and reason when
  existing workflow step order changes.
- Run targeted quality-gate tests and static checks for the touched files.

## Implementation Steps

1. Add a failing unit test for swapping existing validation and publish steps in
   an unowned protected workflow.
2. Add coverage that an informational step inserted before existing steps does
   not count as reordering existing steps.
3. Update the existing-step matcher loop to track matched new indexes and emit a
   violation when matched existing steps are no longer in increasing order.
4. Run the new regression before implementation to confirm the current bypass.
5. Re-run the targeted unit tests and touched-file lint/type checks.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_existing_step_reorder_is_blocked -q`
  fails before the implementation change and passes after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py`
  passes.
