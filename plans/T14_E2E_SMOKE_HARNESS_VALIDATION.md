# T14 E2E Smoke Harness Validation

Plan reference: `plans/T14_E2E_SMOKE_HARNESS_PLAN.md`

## Requirement Status

- Complete: Added a focused regression test in
  `tests/unit/scripts/test_first_run_smoke.py` for `run_command` timeout
  handling.
- Complete: `scripts/first_run_smoke.py::run_command` now catches
  `subprocess.TimeoutExpired` and returns a `CompletedProcess` with return code
  `124`.
- Complete: Captured timeout stdout/stderr are normalized to text, and stderr
  includes a clear timeout message.
- Complete: Changes are limited to the smoke harness, its focused unit test,
  and the required plan/validation files.

## Evidence

- Confirmed pre-fix failure:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_run_command_reports_timeout_as_failed_process -q`
  failed with an uncaught `subprocess.TimeoutExpired`.
- Post-fix targeted regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py::test_run_command_reports_timeout_as_failed_process -q`
  passed.
- Focused module test:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_first_run_smoke.py -q`
  passed with `9 passed`.
- File-scoped lint:
  `uv run --python 3.12 --extra dev ruff check scripts/first_run_smoke.py tests/unit/scripts/test_first_run_smoke.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after completion.
