# PRRT_kwDOSJAM6s6DUTJI Privileged Job Fields Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6DUTJI_PRIVILEGED_JOB_FIELDS_PLAN.md`

## Requirement Status

- Add a regression test proving an added informational job with privileged
  job-level fields is blocked: Complete.
- Preserve the existing allowance for minimal step-based informational jobs:
  Complete.
- Preserve existing blocks for reusable workflow jobs and step-level `uses`:
  Complete.
- Make a narrow fail-closed classifier change for newly added informational
  jobs: Complete.
- Run targeted tests covering the changed quality-gate behavior: Complete.

## Evidence

- Changed `tests/unit/control/test_quality_gates.py` to add
  `test_added_informational_job_with_privileged_fields_is_blocked`, covering
  `permissions`, `needs`, `if`, and `environment`.
- Changed `src/awf/control/quality_gates.py` so added informational jobs only
  accept the minimal job-level key set `name`, `runs-on`, and `steps`.
- Confirmed the new regression failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_added_informational_job_with_privileged_fields_is_blocked -q`
  failed with `4 failed`.
- Confirmed the new regression passed after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_added_informational_job_with_privileged_fields_is_blocked -q`
  passed with `4 passed`.
- Confirmed the targeted quality-gate unit file passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passed with `45 passed`.
- Confirmed lint passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`.
- Confirmed formatting passed:
  `uv run --python 3.12 --extra dev ruff format --check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`.
- Confirmed the touched module type-checks:
  `uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py`.

## Gaps

None.
