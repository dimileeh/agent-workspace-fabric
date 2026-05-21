# PR270 CI Failures Validation

Plan reference: `plans/PR270_CI_FAILURES_PLAN.md`

## Requirement Status

- Complete: Reproduced the reported focused failures before editing behavior.
  Evidence: focused pytest failed on the metrics allocation assertion and the
  generated plan artifact tracking assertion.
- Complete: Resource saturation metrics stay scoped to the local workspace
  routing lane for allocated resources.
  Evidence: `src/awf/service/metrics.py` now uses a metrics allocation helper
  backed by `ResourceReservationRepository.active_latest_totals_for_metrics_allocation_scope`.
- Complete: Generated plan/validation artifacts are no longer tracked public
  docs.
  Evidence: tracked generated `plans/*_PLAN.md` and `plans/*_VALIDATION.md`
  files were removed from git, leaving `plans/PLAN_EXECUTION_PROTOCOL.md`.
- Complete: Focused repro passes after the fix.
- Complete: Affected API/docs/service/repository tests and static checks pass.
- Complete: The fix is ready for a local conventional commit and no push.

## Verification Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py::test_resource_saturation_endpoint_scopes_reservations_by_workspace_routing tests/unit/docs/test_public_docs_status.py::test_generated_plan_artifacts_are_not_tracked_public_docs -q`
  passed: `2 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py tests/unit/docs/test_public_docs_status.py -q`
  passed: `28 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py tests/unit/db/test_scheduler_records.py -q`
  passed: `101 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/api/test_metrics_capacity.py tests/unit/docs/test_public_docs_status.py tests/unit/service/test_metrics.py tests/unit/db/test_scheduler_records.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

## Gaps

No known gaps remain.
