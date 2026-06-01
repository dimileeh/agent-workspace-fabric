# PR #348 CI Coverage And Line-Limit Fix Validation

Plan reference: `plans/CI_PR348_COVERAGE_LINE_LIMIT_FIX_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Keep every first-party code file at or below 1500 lines. | Complete | Split the oversized pre-push validation repair-flow tests into `tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs.py`; the line-limit guard now passes. |
| Preserve the 99% coverage gate without weakening CI or coverage config. | Complete | No workflow, quality-gate, or coverage configuration files were edited. Focused tests add coverage for uncovered PR-monitor paths from the failed CI artifact. |
| Cover the pre-push validation retry-loop branch. | Complete | `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` now uses an explicit `while True` retry loop and returns the terminal failed validation result directly, removing the unreachable `for`-loop exit branch reported by coverage. |
| Cover best-effort sync-base staleness refresh exception handling. | Complete | Added `tests/unit/runtime/test_pr_monitor_remote_ops_edges.py::test_refresh_staleness_after_sync_base_treats_session_failure_as_best_effort`. |
| Cover monitor handoff factory-returned-`None` handling. | Complete | Added `test_sync_feature_pr_monitor_factory_none_marks_unavailable_after_setup` in `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`. |
| Run only focused local verification. | Complete | Ran targeted pytest, Ruff, and one-file mypy commands listed below. Full AWF/GitHub coverage and broad CI validation remain managed after agent completion. |
| Commit the fix locally without switching branches or pushing. | Complete | This fix cycle is committed locally as a conventional commit; no branch switch, push, rebase, or force-push is performed. |

## Files Changed

- `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs.py`
- `tests/unit/runtime/test_pr_monitor_remote_ops_edges.py`
- `plans/CI_PR348_COVERAGE_LINE_LIMIT_FIX_PLAN.md`
- `plans/CI_PR348_COVERAGE_LINE_LIMIT_FIX_VALIDATION.md`

## Focused Verification

Initial repro:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
```

Result: failed before the split with
`tests/unit/runtime/test_pr_monitor_pre_push_validation.py: 1653`.

Passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
```

Result: `1 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs.py tests/unit/runtime/test_pr_monitor_remote_ops.py tests/unit/runtime/test_pr_monitor_remote_ops_edges.py -q
```

Result: `36 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_remote_ops_edges.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_feature_pr_monitor_factory_none_marks_unavailable_after_setup -q
```

Result: `2 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py src/awf/runtime/pr_monitor_runner/remote_ops.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs.py tests/unit/runtime/test_pr_monitor_remote_ops.py tests/unit/runtime/test_pr_monitor_remote_ops_edges.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py
```

Result: `All checks passed!`

```bash
uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation.py
```

Result: `Success: no issues found in 1 source file`.

## Remaining Gaps

No planned gaps remain. Full repository coverage, whole-repository tests,
frontend builds, and GitHub CI-equivalent gates were intentionally not run
locally under the AWF workspace contract; AWF/GitHub owns those broad checks
after agent completion.
