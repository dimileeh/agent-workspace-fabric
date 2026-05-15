# AWF Parallel Final Coverage Validation

Plan: `plans/AWF_PARALLEL_FINAL_COVERAGE_PLAN.md`

## Requirement Status

- Complete: Coverage-wrapped pytest failures with passing coverage are classified
  as pytest validation failures with node IDs/evidence. Added xdist-prefixed
  failure/error regression coverage.
- Complete: Executor reason codes, failure messages, metadata, and failure
  reason preserve `PYTEST_TEST_FAILURE` when coverage met the requirement but
  pytest failed.
- Complete: Local full coverage now runs only for
  `validation.strategy.final_gate: coverage` plus a declared coverage command.
  Targeted edit validation still runs with `include_coverage=False`.
- Complete: AWF self-profile keeps `baseline_coverage: skip` and
  `edit_gate: targeted`, adds explicit local final coverage at 99%, and uses
  `parallel_workers: 3` without embedding xdist args in the command.
- Complete: GitHub Actions full coverage semantics were preserved; CI workflow
  regression tests still pass.
- Complete: No serial/shared-state grouping was added. The full xdist rerun
  produced no pytest failures, errors, worker crashes, or timeouts after stale
  local-coverage test fixtures were updated.

## Evidence

Changed files:

- `.awf/workspace.yml`
- `src/awf/runtime/validation.py`
- `src/awf/control/executor.py`
- `tests/unit/runtime/test_validation.py`
- `tests/unit/control/test_executor_coverage_edges.py`
- `tests/unit/control/test_executor_error_paths.py`
- `tests/unit/control/test_executor.py`
- `tests/unit/profiles/test_profiles.py`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::TestCoverageEnforcement::test_run_profile_coverage_classifies_xdist_errors_when_percent_passes tests/unit/control/test_executor_coverage_edges.py::test_local_coverage_runs_only_for_explicit_final_gate_with_coverage_command tests/unit/control/test_executor_coverage_edges.py::test_validation_command_records_omit_coverage_without_local_final_gate tests/unit/control/test_executor_coverage_edges.py::test_validation_command_count_ignores_coverage_without_local_final_gate tests/unit/profiles/test_profiles.py::test_awf_self_profile_uses_targeted_edit_validation_with_local_final_coverage_gate -q
```

Initial TDD result: 5 failed. Final rerun: 5 passed.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths.py::TestPullRequestUnexpectedError::test_local_coverage_is_not_marked_executed_when_phase_validation_fails tests/unit/control/test_executor_error_paths.py::TestPullRequestUnexpectedError::test_local_coverage_reuses_fresh_evidence_before_running_command tests/unit/control/test_executor_error_paths.py::TestPullRequestUnexpectedError::test_local_coverage_uses_workspace_cpu_cap_for_parallel_workers tests/unit/control/test_executor.py::TestFailurePaths::test_coverage_below_threshold_fails_validation_with_structured_reason -q
```

Result: 4 passed.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py tests/unit/control/test_executor_coverage_edges.py tests/unit/control/test_executor_validation_fix_cycle.py tests/unit/profiles/test_profiles.py tests/unit/test_ci_workflow_full_coverage.py -q
```

Result: 439 passed.

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

Result: ruff passed; mypy passed.

```bash
uv run --python 3.12 --extra dev pytest -n 3 --dist=loadscope --timeout=300 --cov=awf --cov-report=term-missing --cov-fail-under=99
```

First run found stale tests expecting coverage commands without explicit
`final_gate: coverage`: 4 failed, 6152 passed, 7 skipped, coverage 98.80%.

After updating those tests, rerun result: 6156 passed, 7 skipped, no pytest
failures/errors/timeouts/worker crashes. Coverage reported 98.82% and emitted
the provider fail-under line. The 7 skips were existing Docker-unavailable
integration skips in this container; no hidden skips were added. GitHub Actions
remains the authoritative PR full coverage gate where Docker is available.

## Remaining Caveat

The local container lacks Docker/Compose availability for the Docker-backed
integration tests, so the local full coverage percentage stayed below 99 even
though pytest itself passed under xdist. AWF's coverage parser now treats a
provider fail-under line as authoritative evidence, so a workspace-local final
gate would record this as a coverage policy failure, not an infrastructure or
stale execution failure.
