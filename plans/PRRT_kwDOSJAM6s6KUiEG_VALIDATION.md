# PRRT_kwDOSJAM6s6KUiEG CI-fix provider-error early-return validation

## Plan reference
`plans/PRRT_kwDOSJAM6s6KUiEG_PLAN.md`

## Requirement-by-requirement status
- [x] **Complete** — Added parametrized regression test
  `test_ci_fix_records_provider_agent_run_error_before_commit_sink_early_return`
  covering all three early-return commit-sink exceptions
  (`ProtectedScopeDiffError`, `_MonitorAgentRuntimeOwnershipRepairFailedError`,
  `_MonitorPolicyBlockedError`) combined with a stored `AgentRunError`. Asserts
  `_handle_provider_agent_run_error` is invoked with the workspace_id and the
  stored error before the early return.
- [x] **Complete** — Confirmed the regression test fails against the unfixed
  code (`assert 0 == 1` on `len(handle_calls)`), proving the bug.
- [x] **Complete** — Implemented the minimal fix in
  `src/awf/runtime/pr_monitor_runner/ci_ops.py`: each of the three commit-sink
  early-return `except` handlers now calls
  `await self._handle_provider_agent_run_error(workspace_id, agent_run_err, state=state)`
  when `agent_run_err is not None`, before constructing/awaiting the early-return
  result. This records provider retry/cooldown state and lets a recovery
  decision (fallback/retry/auth) propagate authoritatively.
- [x] **Complete** — Confirmed the regression test passes after the fix
  (3/3 parametrized cases green).
- [x] **Complete** — Targeted lint + typecheck clean on touched files.

## Evidence
### TDD red (before fix)
`uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py::test_ci_fix_records_provider_agent_run_error_before_commit_sink_early_return -q`
```
assert len(handle_calls) == 1
E   assert 0 == 1
3 failed
```

### TDD green (after fix) + existing CI-fix early-return tests
`uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py tests/unit/runtime/test_pr_monitor_task_tag_threading.py tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs.py tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs_validated_push.py -q`
```
53 passed
```

### Coverage-edge parts touching the early-return exception handlers
`uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_004.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_011.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_014.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_015.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_016.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_018.py -q`
```
101 passed
```

### Targeted lint + typecheck
`uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/ci_ops.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py`
```
All checks passed!
```
`uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/ci_ops.py`
```
Success: no issues found in 1 source file
```

## Files changed
- `src/awf/runtime/pr_monitor_runner/ci_ops.py` — call
  `_handle_provider_agent_run_error` in each of the three commit-sink
  early-return handlers when a stored `agent_run_err` is present.
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py`
  — added the regression test + the needed imports
  (`AgentRunError`, `_MonitorPolicyBlockedError`).

## Design note
`_handle_provider_agent_run_error` may raise `ProviderRecoveryFallbackError`,
`ProviderRecoveryRetryError`, or `ProviderRecoveryAuthError`. The fix invokes it
*before* the early-return result is constructed so that, when both an agent run
error and a commit-sink failure occur together, a recovery decision can
propagate authoritatively instead of being masked by the commit-sink early
return. This mirrors the existing post-commit behavior at the bottom of
`_run_ci_fix`, where `_handle_provider_agent_run_error` is already allowed to
raise out of the function.

## Broad validation
Full AWF/GitHub validation (full coverage gate, full suite, frontend build,
OpenAPI drift check) is managed by AWF after agent completion; it was not run
during this focused fix cycle per the AWF workspace contract.
