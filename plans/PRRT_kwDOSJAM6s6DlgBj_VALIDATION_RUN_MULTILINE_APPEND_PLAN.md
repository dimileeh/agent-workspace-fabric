# PRRT_kwDOSJAM6s6DlgBj Validation Run Multiline Append Plan

## Problem Statement and Scope

The PR review thread reports that protected workflow validation-run preservation allows
safe appended commands joined with `&&`, but rejects the same safe appended commands
when they are added on later lines in a YAML block scalar. The change is scoped to the
validation run append parser and regression coverage in `tests/unit/control/test_quality_gates.py`.

## Requirements Checklist

- Reproduce the reported block-scalar append rejection with a failing regression test.
- Allow appended multiline validation/report commands when each appended command is already classified safe.
- Preserve existing blocking behavior for unsafe appended commands and blocked shell operators.
- Keep the fix narrow to quality-gate validation-run preservation.

## Implementation Steps

1. Add a unit regression for a workflow run changed from a scalar validation command to a block scalar that appends `coverage report`.
2. Add focused helper coverage for multiline safe and unsafe append suffixes.
3. Update `_validation_run_append_commands` to split append suffixes on safe append boundaries, including newline boundaries, while still rejecting blocked shell operators and empty segments.
4. Run the targeted quality-gate tests, then run lint/type checks if the narrow test passes.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  - Passes all quality-gate unit tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  - Reports no lint errors.
- `uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py`
  - Reports no type errors.
