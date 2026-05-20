# PRRT_kwDOSJAM6s6DV7Na Validation Append Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6DV7Na` reports that protected workflow
validation `run` preservation accepts any `old command && ...` suffix. That can
let a comment/notify-labeled validation step keep the original validation
command while appending unrelated executable work without an explicit owned path.

Scope is limited to protected workflow `run` command classification in
`src/awf/control/quality_gates.py` and focused regression coverage in
`tests/unit/control/test_quality_gates.py`.

## Requirements Checklist

- Add regression coverage proving a comment-labeled validation step cannot
  append an arbitrary executable command after the preserved validation command.
- Preserve the existing allowance for appending additional validation/report
  commands such as `coverage html`.
- Keep non-comment validation run changes blocked by existing protected workflow
  rules.
- Run targeted quality-gate tests and lint for touched files.

## Implementation Steps

1. Add a failing unit regression for `uv run pytest && <arbitrary command>` on
   an existing comment-labeled validation step.
2. Update validation-run preservation so appended `&&` segments must be
   validation command invocations and may not contain additional shell control
   operators.
3. Re-run the new regression, the existing broadening test, the focused quality
   gate test module, and touched-file lint.
4. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_comment_validation_command_arbitrary_append_is_blocked -q`
  fails before the production fix and passes after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_comment_validation_command_broadening_is_allowed -q`
  passes after the fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.
