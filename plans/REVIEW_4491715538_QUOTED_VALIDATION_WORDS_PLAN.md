# Review 4491715538 Quoted Validation Words Plan

## Problem Statement and Scope

Address the review-level feedback from PR comment `issue:4491715538` that
informational workflow comment steps are falsely rejected when quoted `echo` or
`printf` output mentions validation tool names such as `pytest`, `coverage`,
`ruff`, or `mypy`.

Scope is limited to `src/awf/control/quality_gates.py`, focused unit
regressions in `tests/unit/control/test_quality_gates.py`, and this
plan/validation pair.

## Requirements Checklist

- Allow safe informational `echo`/`printf` steps whose quoted output text
  mentions validation tool names or test-result wording.
- Continue blocking real validation command invocations such as `pytest`,
  `uv run coverage xml`, `python -m unittest`, package-manager `test` targets,
  and broad validation commands.
- Keep validation-command detection shell-quote-aware instead of matching
  command-name regexes against quoted string arguments.
- Remove the misleading `lexer.whitespace_split = True` assignment from
  `_shell_tokens`.
- Commit the scoped fix locally without pushing or switching branches.

## Implementation Steps

1. Add focused failing regressions for comment `continue-on-error` and added
   informational steps with quoted validation-word output.
2. Run the new tests to confirm the false-positive behavior before the fix.
3. Update validation-command detection to inspect shell command words rather
   than the raw command text.
4. Remove the dead `whitespace_split` assignment.
5. Re-run the focused tests, the quality-gate unit file, and focused lint/type
   checks for the touched Python surface.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k 'quoted_validation_words or real_validation_commands'`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes.
