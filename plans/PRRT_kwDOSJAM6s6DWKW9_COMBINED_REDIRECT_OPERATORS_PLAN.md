# PRRT_kwDOSJAM6s6DWKW9 Combined Redirect Operators Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6DWKW9` reports that protected workflow
informational `run` classification rejects simple redirection operators but
omits Bash combined redirection operators such as `&>`, `&>>`, `>&`, and `<&`.
Because shell tokens are checked by exact operator match, an informational-looking
`echo` or `printf` command can redirect output or duplicate file descriptors and
still be classified as safe.

Scope is limited to informational workflow run-command classification in
`src/awf/control/quality_gates.py` and focused regression coverage in
`tests/unit/control/test_quality_gates.py`.

## Requirements Checklist

- Added informational workflow steps must reject Bash combined redirection
  operators `&>`, `&>>`, `>&`, and `<&`.
- Existing safe informational output commands using `echo` and `printf` without
  redirection must remain allowed.
- Existing command-separator behavior for safe informational commands must remain
  unchanged.
- The change must be covered by a failing regression test before the production
  fix and validated with targeted tests.

## Implementation Steps

1. Add parametrized unit coverage showing an added informational workflow step
   with each combined redirection operator is blocked.
2. Run the focused new test to confirm the current classifier incorrectly
   allows the examples.
3. Extend informational blocked shell operators to include the combined
   redirection tokens.
4. Re-run the focused regression, the relevant safe-case tests, the full
   quality-gate test module, and touched-file lint.
5. Record requirement-by-requirement validation evidence in the matching
   validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_added_informational_step_blocks_combined_redirection_operators -q`
  fails before the production fix and passes after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_added_informational_step_allows_echo_prose_validation_words tests/unit/control/test_quality_gates.py::test_workflow_informational_step_allows_cov_shell_variable_update -q`
  passes after the production fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.
