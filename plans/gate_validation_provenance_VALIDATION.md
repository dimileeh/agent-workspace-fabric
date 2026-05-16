# Gate Validation Provenance Validation

Plan reference: `plans/gate_validation_provenance_PLAN.md`

## Requirement Status

- Complete: Preserve validation run and coverage provenance when the primary failure is `validation_failure`.
  - Evidence: `tests/unit/service/test_failure_causality.py::test_primary_failure_snapshot_keeps_validation_run_for_validation_failure`
- Complete: Do not attach a latest failed validation run or coverage to a non-validation primary failure solely because one exists historically.
  - Evidence: `tests/unit/service/test_failure_causality.py::test_primary_failure_snapshot_omits_historical_validation_run_for_agent_failure`
- Complete: Preserve any validation provenance already embedded in an existing primary failure payload.
  - Evidence: `tests/unit/service/test_failure_causality.py::test_primary_failure_snapshot_preserves_embedded_validation_payload_for_agent_failure` and `tests/unit/service/test_failure_causality.py::test_primary_failure_snapshot_preserves_embedded_validation_payload_for_validation_failure`
- Complete: Keep existing primary failure reason/message/reason-code preservation behavior intact.
  - Evidence: focused service assertions plus adjacent worker preservation tests.
- Complete: Commit the fix locally without pushing or changing branches.
  - Evidence: local commit will be created after this validation file is staged.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  - Result: passed, `4 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_stale_active_execution_preserves_validation_failure_and_records_secondary_stale tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_runtime_stranding_preserves_provider_auth_primary_failure -q`
  - Result: passed, `2 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py tests/unit/service/test_failure_causality.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/failure_causality.py`
  - Result: passed.

## Pre-Fix Failure Evidence

Before the production change, `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q` failed because agent failures received a `validation_run` and embedded validation payloads were overwritten by the latest unrelated failed validation run.

## Gaps

No gaps remain.
