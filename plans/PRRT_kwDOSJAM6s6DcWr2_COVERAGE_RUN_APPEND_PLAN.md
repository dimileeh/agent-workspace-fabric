# PRRT_kwDOSJAM6s6DcWr2 Coverage Run Append Plan

## Problem Statement And Scope

The protected workflow validation-run preservation check allows appended
commands whose executable basename is `coverage`. That permits
`coverage run scripts/exfiltrate.py` to pass as safe validation broadening even
though `coverage run` executes arbitrary Python code.

Scope is limited to `src/awf/control/quality_gates.py`, focused regression
tests in `tests/unit/control/test_quality_gates.py`, and the required plan and
validation documents.

## Requirements Checklist

- Block appended `coverage run ...` commands in unowned protected workflow
  validation-run edits.
- Block equivalent wrapped/module forms, including `uv run coverage run ...`
  and `python -m coverage run ...`.
- Preserve existing allowed validation broadening for non-executing coverage
  report/output subcommands such as `coverage html` and `coverage xml`.
- Keep unrelated protected workflow policies unchanged.
- Commit the local fix without pushing or changing branches.

## Implementation Steps

1. Add regression tests that demonstrate `coverage run` appends are rejected.
2. Add explicit parsing for safe coverage subcommands instead of treating the
   `coverage` executable/module as universally safe.
3. Re-run the targeted quality-gate tests.
4. Document validation results in the matching validation file.
5. Stage only touched files and commit with the PR review thread id.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`

Pass criteria: the targeted test file passes, including new regressions that
reject unsafe `coverage run` appends and existing tests that allow safe
coverage report broadening.
