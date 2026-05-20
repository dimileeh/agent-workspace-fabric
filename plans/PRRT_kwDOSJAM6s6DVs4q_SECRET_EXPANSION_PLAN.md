# PRRT_kwDOSJAM6s6DVs4q Secret Expansion Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6DVs4q` reports that protected workflow
informational steps can still use safe-looking `echo` or `printf` runs to print
runtime values through shell parameter expansion, including braced expressions
such as `${VAR}` and substring forms such as `${VAR:0:4}`.

Scope is limited to informational run-command classification in
`src/awf/control/quality_gates.py` and focused regression coverage in
`tests/unit/control/test_quality_gates.py`.

## Requirements Checklist

- Add regression coverage proving added informational steps reject braced shell
  parameter expansions in `echo` or `printf` arguments.
- Add regression coverage proving added informational steps reject substring
  parameter expansions that could disclose secret fragments.
- Add regression coverage proving sensitive unbraced env references such as
  `$AWF_API_TOKEN` are rejected.
- Preserve existing allowances for non-sensitive informational prose and
  locally assigned non-sensitive shell variables.
- Run targeted tests and lint for the touched files.

## Implementation Steps

1. Add failing unit tests in `tests/unit/control/test_quality_gates.py` for
   braced expansions, substring expansions, and sensitive unbraced env
   references in added informational steps.
2. Run the new focused tests to confirm the current classifier misses the
   unsafe informational expansions.
3. Update `_informational_shell_command_is_safe` or a helper it calls so
   allowed `echo` and `printf` commands fail closed on unsafe parameter
   expansions while preserving current safe cases.
4. Re-run the focused tests, the full quality-gate test module, and touched-file
   lint.
5. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_added_informational_step_blocks_secret_bearing_expansions -q`
  fails before the production fix and passes after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_informational_step_allows_cov_shell_variable_update tests/unit/control/test_quality_gates.py::test_added_informational_step_allows_echo_prose_validation_words -q`
  passes after the production fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.
