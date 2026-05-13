# Preserve Primary Failure Causality Validation

Plan reference: `plans/preserve_primary_failure_causality_PLAN.md`

## Requirement Status

- Complete: validation failure followed by stale-active handling keeps
  `validation_failure` and `PYTEST_TEST_FAILURE` primary, while stale runtime
  evidence is recorded under secondary diagnostics.
- Complete: validation failure followed by destroy cleanup failure keeps the
  validation root cause and stores cleanup failure details as secondary
  operation, audit, and failed-transition metadata.
- Complete: active-execution preservation after restart leaves failure fields
  and validation provenance unchanged and attaches primary context to the
  preservation event/operation.
- Complete: terminal-runtime-release cleanup failure leaves validation
  provenance unchanged and attaches primary context to the release-failure
  event.
- Complete: no-primary stale and cleanup behavior remains covered by existing
  regression suites and still classifies stale/cleanup as the primary failure.
- Complete: provider/auth primary evidence remains distinct from runtime
  stranding and is not collapsed into generic infrastructure failure.
- Complete: readiness taxonomy treats preserved validation failures with
  secondary infrastructure diagnostics as classified/actionable.
- Complete: no new public reason codes were introduced, so the reason catalog
  did not require updates.

## Evidence

Files changed:

- `src/awf/service/failure_causality.py`
- `src/awf/control/worker.py`
- `src/awf/service/controls.py`
- `tests/unit/control/test_worker.py`
- `tests/unit/service/test_controls.py`
- `tests/unit/service/test_failure_causality.py`
- `tests/unit/service/test_readiness.py`

Validation commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_stale_active_execution_preserves_validation_failure_and_records_secondary_stale tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_runtime_stranding_preserves_provider_auth_primary_failure tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_active_execution_preservation_after_restart_keeps_primary_failure_evidence tests/unit/control/test_worker.py::TestTerminalRuntimeRelease::test_terminal_runtime_release_failure_preserves_validation_provenance_details tests/unit/service/test_controls.py::test_destroy_cleanup_failure_preserves_existing_validation_failure tests/unit/service/test_readiness.py::test_readiness_treats_preserved_validation_with_secondary_infra_as_classified -q
```

Result: `6 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py tests/unit/service/test_controls.py tests/unit/service/test_controls_lifecycle.py tests/unit/service/test_readiness.py tests/unit/service/test_failure_causality.py tests/unit/api/test_validation_provenance.py -q
```

Result: original implementation evidence reported `313 passed`. Review follow-up
rerun of the updated command reached `259 passed` before Postgres returned
`DiskFullError` / `could not write init file` during isolated schema setup in
this workspace. The added service-level suite and the API provenance suite were
then run separately:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q
uv run --python 3.12 --extra dev pytest tests/unit/api/test_validation_provenance.py -q
```

Result: `6 passed`; `34 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py tests/unit/service/test_workspace_response.py -q
```

Result: `117 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
```

Result: passed.

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Result: passed with no issues.

## Gaps

No known gaps remain for the saved P0 slice. Full coverage remains delegated to
the GitHub Actions gate per the workspace execution instructions.
