# PRRT_kwDOSJAM6s6DVfbC Comment Run Weakening Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6DVfbC` reports that existing workflow steps with
comment/notify labels can change a validation-like `run` command without an
owned protected workflow path. The current classifier only blocks introducing a
validation command, then allows comment/notify run edits, so a comment-labeled
validation step can remove or narrow the previous validation command.

Scope is limited to protected workflow run-command classification in
`src/awf/control/quality_gates.py` and focused unit coverage in
`tests/unit/control/test_quality_gates.py`.

## Requirements Checklist

- Add regression coverage proving a comment-labeled existing step cannot remove
  a pre-existing validation command.
- Add regression coverage proving a comment-labeled existing step cannot narrow
  a pre-existing validation command while still looking validation-like.
- Preserve the existing allowance for comment/notify steps that append extra
  validation/report work while preserving the original command.
- Preserve the existing block on introducing a validation command in a
  comment/notify step.
- Run targeted tests and lint for the touched files.

## Implementation Steps

1. Add failing unit tests in `tests/unit/control/test_quality_gates.py` for
   validation removal and narrowing on a comment-labeled existing step.
2. Run the new tests to confirm the current classifier fails to report the
   weakening.
3. Update `_workflow_existing_step_violations` so old validation commands must
   be preserved before comment/notify or informational exceptions can allow a
   run edit.
4. Re-run the focused quality-gate tests and touched-file lint.
5. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_comment_validation_command_removal_is_blocked tests/unit/control/test_quality_gates.py::test_workflow_comment_validation_command_narrowing_is_blocked -q`
  fails before the production fix and passes after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.
