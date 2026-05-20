# PRRT_kwDOSJAM6s6DglLC Untrusted Env Expansion Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6DglLC` reports that protected workflow
informational `run:` steps can still print short or non-obviously named
environment variables such as `$PAT` or `$GH_PAT`. The current guard blocks
braced shell parameter expansion and obvious secret names, but non-matching
unbraced variables can leak secrets through the informational-step allowlist.

Scope is limited to informational run-command classification in
`src/awf/control/quality_gates.py` and focused regression coverage in
`tests/unit/control/test_quality_gates.py`.

## Requirements Checklist

- Add regression coverage proving added informational workflow steps reject
  `$PAT` and `$GH_PAT` shell expansion in `echo` or `printf` runs.
- Make informational runs fail closed for unbraced shell variable expansion
  unless the variable is known-safe or locally assigned earlier in the same
  informational run.
- Preserve existing allowances for literal informational prose, `$PATH`, and
  same-run literal variables such as `COV=85` followed by `echo "$COV"`.
- Run focused quality-gate tests and lint for the touched source/test files.

## Implementation Steps

1. Add failing regression cases for `$PAT` and `$GH_PAT` to the existing
   informational secret-expansion tests.
2. Confirm the new regression fails before the production change.
3. Thread a small safe-variable context through the informational shell safety
   helpers so unknown unbraced variables are blocked while explicitly safe and
   same-run assigned variables remain allowed.
4. Re-run the focused regression tests, existing allowance tests, and touched
   file lint.
5. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_added_informational_step_blocks_secret_bearing_expansions -q`
  fails before the implementation change and passes after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_informational_step_allows_cov_shell_variable_update tests/unit/control/test_quality_gates.py::test_added_informational_step_allows_echo_prose_validation_words tests/unit/control/test_quality_gates.py::test_informational_run_command_shell_safety_edges -q`
  passes after the implementation change.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.
