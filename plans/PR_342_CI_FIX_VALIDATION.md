# PR #342 CI Fix Validation

Plan reference: `plans/PR_342_CI_FIX_PLAN.md`

## Requirement Status

- Preserve the Cursor CLI installer path and do not weaken the `cursor-agent
  --version` smoke checks: Complete. The installer remains
  `curl https://cursor.com/install -fsS | HOME=/opt/cursor bash`, and both root
  and non-root `cursor-agent --version` checks remain strict.
- Make `/usr/local/bin/node` available in the agent-runtime image before
  `cursor-agent` is executed: Complete. `docker/agent-runtime.Dockerfile`
  symlinks `$(command -v node)` to `/usr/local/bin/node` during the NodeSource
  install stage and verifies it is executable before any Cursor step runs.
- Add or update focused regression coverage for the Dockerfile behavior:
  Complete. `tests/unit/test_agent_runtime_dockerfile.py` now asserts the
  `/usr/local/bin/node` symlink exists and is ordered before the Cursor
  installer.
- Run only focused local checks: Complete. No full coverage, full repository
  test suite, frontend build, or CI-equivalent Docker image build was run
  locally.
- Commit the local fix without pushing or changing branches: Complete. The
  staged fix is committed locally as part of this CI-fix cycle, and no push or
  branch change is performed.

## Evidence

Files changed:

- `docker/agent-runtime.Dockerfile`
- `tests/unit/test_agent_runtime_dockerfile.py`
- `plans/PR_342_CI_FIX_PLAN.md`
- `plans/PR_342_CI_FIX_VALIDATION.md`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py::test_agent_runtime_installs_all_supported_coding_clis -q`
  - First run failed before implementation on the missing
    `/usr/local/bin/node` symlink assertion.
  - Second run passed after the Dockerfile fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py::TestCursorAdapter tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py::test_provider_readiness_cursor_env_present -q`
  - Passed: 8 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py -q`
  - Passed: 7 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py::test_provider_readiness_all_green tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py::test_provider_readiness_cursor_env_present -q`
  - Passed: 2 tests.
- `uv run --python 3.12 --extra dev ruff check tests/unit/test_agent_runtime_dockerfile.py`
  - Passed.
- `git diff --check`
  - Passed.

## Deferred Validation

Full AWF/GitHub validation, including the actual CI Docker image build,
`python-full-coverage`, `release-artifacts`, broad coverage, and frontend
checks, is managed by AWF/GitHub after agent completion per the workspace
contract.

## Gaps

No planned implementation gaps remain.

## Iteration 2: Cursor Launcher Bundle Path

### Requirement Status

- Preserve strict `cursor-agent --version` smoke checks: Complete. The root and
  non-root smoke checks remain strict; no `|| true`, skip, or weakening was
  added.
- Expose `cursor-agent` on the system PATH without copying the bundle launcher:
  Complete. `docker/agent-runtime.Dockerfile` now creates
  `/usr/local/bin/cursor-agent` as a symlink to
  `/opt/cursor/.local/bin/cursor-agent`, preserving the Cursor installer bundle
  path used to resolve bundled `node` and `index.js`.
- Add focused regression coverage for the symlink contract: Complete.
  `tests/unit/test_agent_runtime_dockerfile.py` now asserts the symlink and
  rejects copying/installing the launcher into `/usr/local/bin`.
- Run only focused local checks: Complete. No full coverage, full repository
  test suite, frontend build, or CI-equivalent Docker image build was run
  locally.

### Evidence

Additional files changed:

- `docker/agent-runtime.Dockerfile`
- `tests/unit/test_agent_runtime_dockerfile.py`
- `plans/PR_342_CI_FIX_PLAN.md`
- `plans/PR_342_CI_FIX_VALIDATION.md`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py::test_agent_runtime_installs_all_supported_coding_clis -q`
  - Failed before implementation on the missing
    `ln -sf "$cursor_path" /usr/local/bin/cursor-agent` assertion.
  - Passed after the Dockerfile fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py -q`
  - Passed: 7 tests.
- `uv run --python 3.12 --extra dev ruff check tests/unit/test_agent_runtime_dockerfile.py`
  - Passed.
- `git diff --check`
  - Passed.
- Temporary installer probe outside the repo:
  `curl https://cursor.com/install -fsS | HOME="$tmpdir" bash`, followed by a
  symlinked launcher smoke check, returned Cursor version
  `2026.05.28-a70ca7c`. This confirmed the installer-managed symlink preserves
  the bundle-relative `index.js` lookup that failed in CI when the launcher was
  copied.
- Local `git commit` pre-commit hooks also ran and passed: trailing whitespace,
  end-of-file, YAML/TOML checks where applicable, large-file check,
  merge-conflict check, private-key detection, `ruff check`,
  `ruff format --check`, and `mypy`.

### Deferred Validation

Full AWF/GitHub validation, including the actual CI Docker image build,
`python-full-coverage`, `release-artifacts`, broad coverage, and frontend
checks, is managed by AWF/GitHub after agent completion per the workspace
contract.

### Gaps

No planned implementation gaps remain.

## Iteration 3: Python Coverage Regressions After Cursor Wiring

### Requirement Status

- Preserve validation stale-stop behavior: Complete. `run_validation_and_fix_cycle`
  accepts both `run_model` and historical `default_model` keywords, and the
  stale transition/recheck tests pass without running downstream git work.
- Keep monitor resume profile sync retry and timeout handoff behavior intact:
  Complete. Provider-recovery default selection now tolerates lightweight test
  adapter doubles while preserving Cursor-specific behavior for production
  adapters.
- Regenerate reason catalog without weakening checks: Complete.
  `docs/REASON_CATALOG.md` was regenerated from
  `src/awf/service/doctor/reasons.py`.
- Split oversized files without weakening maintainability checks: Complete.
  Provider runtime CLI probe helpers moved into
  `src/awf/service/provider_readiness_helpers.py`, provider-readiness tests were
  redistributed into part files, and one monitor-recovery test moved into a new
  part file.
- Run only focused local checks: Complete. No full coverage, full repository
  test suite, frontend build, or CI-equivalent validation was run locally.

### Evidence

Additional files changed:

- `docs/REASON_CATALOG.md`
- `plans/PR_342_CI_FIX_PLAN.md`
- `plans/PR_342_CI_FIX_VALIDATION.md`
- `src/awf/control/executor/execution_validation.py`
- `src/awf/control/executor/helpers.py`
- `src/awf/service/provider_readiness.py`
- `src/awf/service/provider_readiness_helpers.py`
- `tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_002.py`
- `tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_005.py`
- `tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py`
- `tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py`
- `tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_003.py`

Focused checks run:

- AWF-provided repro before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py::test_execution_validation_returns_stop_when_start_transition_is_stale tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py::test_execution_validation_returns_stop_when_validate_recheck_is_stale tests/unit/control/test_executor_runtime_profile_snapshot.py::test_validation_cycle_syncs_profile_before_command_planning tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_009.py::TestExecutorCoverageEdgesPart003::test_resume_pr_monitor_retries_profile_sync_before_monitor_factory tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  failed with the reported validation keyword, monitor handoff, and line-limit
  failures.
- `uv run --python 3.12 --extra dev python scripts/generate_reason_catalog.py`
  regenerated `docs/REASON_CATALOG.md`.
- AWF-provided repro after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py::test_execution_validation_returns_stop_when_start_transition_is_stale tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py::test_execution_validation_returns_stop_when_validate_recheck_is_stale tests/unit/control/test_executor_runtime_profile_snapshot.py::test_validation_cycle_syncs_profile_before_command_planning tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_009.py::TestExecutorCoverageEdgesPart003::test_resume_pr_monitor_retries_profile_sync_before_monitor_factory tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  passed: 5 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_doctor_reasons.py::test_reason_catalog_is_synchronized_with_python_source tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_011.py::TestExecutorCoverageEdgesPart011::test_resume_pr_monitor_passes_timeouts_to_adapter -q`
  passed: 2 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_003.py tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_005.py -q`
  passed: 119 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_validation.py src/awf/control/executor/helpers.py src/awf/service/provider_readiness.py src/awf/service/provider_readiness_helpers.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_003.py tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_002.py tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_005.py`
  passed after organizing the provider-readiness helper import block.
- `uv run --python 3.12 --extra dev mypy src/awf/control/executor/execution_validation.py src/awf/control/executor/helpers.py src/awf/service/provider_readiness.py src/awf/service/provider_readiness_helpers.py`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/service/test_doctor_reasons.py::test_reason_catalog_is_synchronized_with_python_source -q`
  passed: 2 tests.
- `git diff --check`
  passed.

### Deferred Validation

Full AWF/GitHub validation, including `python-full-coverage`, broad unit tests,
full coverage gates, release artifacts, and frontend checks, is managed by
AWF/GitHub after agent completion per the workspace contract.

### Gaps

No planned implementation gaps remain.
