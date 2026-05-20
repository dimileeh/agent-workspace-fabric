# Protected Workflow Informational Run Validation

Plan reference: `PROTECTED_WORKFLOW_INFORMATIONAL_RUN_PLAN.md`

## Requirement Status

- Complete: Added informational workflow steps/jobs reject arbitrary executable
  `run` commands, including script execution and network-style commands.
  Evidence: `test_added_informational_step_blocks_arbitrary_run_commands` and
  `test_added_informational_job_blocks_arbitrary_run_commands`.
- Complete: Safe informational output commands such as `echo` and `printf`
  remain allowed, including output text that mentions validation words.
  Evidence: `test_added_informational_job_allows_command_words_in_output_prose`
  plus existing echo/printf informational tests.
- Complete: Existing validation-command protections remain intact.
  Evidence: full `tests/unit/control/test_quality_gates.py` passed.
- Complete: Existing comment/notify `uses` allowlist behavior remains intact.
  Evidence: full `tests/unit/control/test_quality_gates.py` passed.
- Complete: The fix is covered by regression tests and validated with focused
  tests. Evidence: commands below.

## Files Changed

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/PROTECTED_WORKFLOW_INFORMATIONAL_RUN_PLAN.md`
- `plans/PROTECTED_WORKFLOW_INFORMATIONAL_RUN_VALIDATION.md`

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "arbitrary_run_commands"` failed before implementation with the new regressions, as expected.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "informational or arbitrary_run_commands"` passed: 37 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q` passed: 76 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf tests` passed.
- `uv run --python 3.12 --extra dev mypy src/awf` passed.

## Non-Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit -q` was started but
  stopped after reaching 8% because it was too broad for this targeted review
  fix. It is not counted as pass evidence.
