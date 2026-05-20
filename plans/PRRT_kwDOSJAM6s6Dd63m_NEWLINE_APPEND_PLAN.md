# PRRT_kwDOSJAM6s6Dd63m Newline Append Plan

## Problem Statement

The protected workflow validation-run guard allows preserving an existing validation command only when appended commands are safe validation commands. The review thread reports that newline-separated appended content is tokenized as whitespace by `shlex`, allowing an unsafe second shell command to be treated as arguments to an allowed validation command.

## Scope

- Cover the newline-separated append bypass in `src/awf/control/quality_gates.py`.
- Keep the fix limited to validation-run append parsing.
- Preserve existing allowed `&&` validation-command appends.

## Requirements Checklist

- [ ] Add a regression test showing a newline after an allowed appended validation command is rejected.
- [ ] Confirm the regression fails before implementation when practical.
- [ ] Reject newline-separated appended validation-run content or validate it per line so unsafe commands cannot bypass the guard.
- [ ] Run the focused unit test file or narrower selected tests.

## Implementation Steps

1. Add a test case to `tests/unit/control/test_quality_gates.py` for `pytest && python -m unittest\ncurl ...`.
2. Run the selected regression test and confirm it fails.
3. Update `_validation_run_append_commands` to reject suffixes containing line breaks before shell tokenization.
4. Re-run the selected quality-gate tests.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`

Pass criteria: the new regression and existing quality-gate tests pass.
