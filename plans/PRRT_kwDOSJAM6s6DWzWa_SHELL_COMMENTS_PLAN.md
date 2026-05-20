# PRRT_kwDOSJAM6s6DWzWa Shell Comments Plan

## Problem Statement and Scope

The review thread reports that informational workflow `run:` scripts with shell
comment lines are classified as unsafe because the shell lexer disables comment
handling. The scope is limited to confirming the behavior, adding a regression
test, and making the smallest parser change that allows shell comments without
weakening existing unsafe command detection.

## Requirements Checklist

- Add a regression test for `continue-on-error: true` on a comment/notify step
  whose multi-line `run:` script starts with a shell comment and then runs
  `echo`.
- Preserve existing blocks for validation commands, arbitrary shell commands,
  redirection, command substitution, and sensitive parameter expansion.
- Keep the implementation localized to workflow quality-gate parsing.
- Run the narrow test proving the regression and the relevant quality-gate
  unit tests.

## Implementation Steps

1. Add a failing unit test in `tests/unit/control/test_quality_gates.py`.
2. Update `_shell_tokens` in `src/awf/control/quality_gates.py` to respect
   shell comment handling.
3. Run the targeted regression test, then the quality-gate unit test module.
4. Record validation evidence in
   `plans/PRRT_kwDOSJAM6s6DWzWa_SHELL_COMMENTS_VALIDATION.md`.
5. Stage only changed files and commit with the review-thread id.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  must pass.
