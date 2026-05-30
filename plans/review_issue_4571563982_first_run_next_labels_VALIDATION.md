# Review Issue 4571563982 First-Run Next Labels Validation

Plan reference: `plans/review_issue_4571563982_first_run_next_labels_PLAN.md`

## Requirement Status

- Complete: Added a focused regression test for pretty output with both remediation-level and payload-level next steps.
- Complete: Distinguished issue remediation next steps from command-level next steps in pretty output by rendering `Remediation Next:` for issue remediation steps.
- Complete: Preserved existing JSON payload shape and existing top-level `Next:` rendering.
- Complete: Kept changes scoped to renderer behavior, tests, and plan/validation docs.

## Evidence

- Files changed:
  - `src/awf/host_setup/rendering.py`
  - `tests/unit/service/test_host_setup_rendering.py`
  - `plans/review_issue_4571563982_first_run_next_labels_PLAN.md`
  - `plans/review_issue_4571563982_first_run_next_labels_VALIDATION.md`
- TDD failure observed before implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_pretty_distinguishes_remediation_and_command_next_steps -q`
  - Failed because pretty output contained two `Next:` lines.
- Passing focused checks after implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py -q`
  - `17 passed in 0.48s`
  - `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/rendering.py tests/unit/service/test_host_setup_rendering.py`
  - `All checks passed!`

Full AWF/GitHub validation was not run locally because AWF owns broad validation, provenance, logs, timeouts, and merge gating after agent completion.
