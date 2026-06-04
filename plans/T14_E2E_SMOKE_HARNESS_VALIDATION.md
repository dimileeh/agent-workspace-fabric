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

## CI Repair Iteration: Supported Script Surface

### Requirement Status

- Complete: Preserved the script-surface guard in
  `tests/unit/docs/test_api_surface_cleanup_docs.py`; the test still asserts
  exact membership for files in `scripts/`.
- Complete: Added `first_run_smoke.py` to `SUPPORTED_SCRIPTS` because the T14
  first-run smoke harness is an intentional supported entrypoint.
- Complete: Re-ran the AWF-provided focused pytest command covering the
  failing script-surface node and both reported source smoke lane nodes.

### Evidence

- Confirmed pre-fix focused repro:
  `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_api_surface_cleanup_docs.py::test_scripts_directory_contains_only_supported_generators tests/integration/test_first_run_smoke.py::test_source_uv_run_lane_proves_checkout_from_outside tests/integration/test_first_run_smoke.py::test_source_tool_install_lane_installs_isolated_awf -q`
  failed with `1 failed, 2 passed`; the failure was the extra
  `first_run_smoke.py` file in the script-surface allowlist assertion.
- Post-fix focused repro:
  `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_api_surface_cleanup_docs.py::test_scripts_directory_contains_only_supported_generators tests/integration/test_first_run_smoke.py::test_source_uv_run_lane_proves_checkout_from_outside tests/integration/test_first_run_smoke.py::test_source_tool_install_lane_installs_isolated_awf -q`
  passed with `3 passed`.
- File-scoped lint:
  `uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_api_surface_cleanup_docs.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after completion.
